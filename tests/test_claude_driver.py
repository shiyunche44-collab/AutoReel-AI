"""Claude 驱动的项目:文字模型调用交给会话回答(`llm.handoff`),命令行推进(`app.cli`)。

断言:
- 没回答就停在原状态,请求落盘;答了以后重跑同一步,前面答过的直接复用;
- `NeedsClaude` 不会被调用点的 `except Exception` 兜底吞成降级;
- 必需字段不全的回答写不进去;
- 原始材料只采一次(重跑同一步提示词不变);
- 调度器不碰 Claude 推进的项目;
- 命令行从建项目一路答题走到门禁 A,门禁不经 `--confirm` 不放行。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from app import config, db
from app.cli import main as cli
from app.llm import handoff
from app.llm.base import LLMOutputInvalid
from app.models import Fact
from app.pipeline import intake, orchestrator
from app.providers import facts as facts_provider
from app.state import State


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(config, "USE_LIBRARY", False)
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


# --------------------------------------------------------------------------
# handoff 本身
# --------------------------------------------------------------------------
def test_unanswered_request_is_saved_and_stops(store):
    llm = handoff.ClaudeHandoff("p_x")
    with pytest.raises(handoff.NeedsClaude) as info:
        llm.structured("系统", "用户", required_keys=["facts"])
    req = info.value.request
    assert req["required_keys"] == ["facts"]
    assert handoff.request_path("p_x", req["id"]).is_file()
    assert [r["id"] for r in handoff.pending("p_x")] == [req["id"]]


def test_answer_is_replayed_by_prompt_fingerprint(store):
    llm = handoff.ClaudeHandoff("p_x")
    with pytest.raises(handoff.NeedsClaude) as info:
        llm.structured("系统", "用户", required_keys=["facts"])
    rid = info.value.request["id"]
    handoff.put_answer("p_x", rid, '{"facts": [1, 2]}')
    assert llm.structured("系统", "用户", required_keys=["facts"]) == {"facts": [1, 2]}
    assert handoff.pending("p_x") == []
    # 提示词变了就是另一个请求
    with pytest.raises(handoff.NeedsClaude):
        llm.structured("系统", "用户 改了一个字", required_keys=["facts"])


def test_answer_missing_required_keys_is_refused(store):
    llm = handoff.ClaudeHandoff("p_x")
    with pytest.raises(handoff.NeedsClaude) as info:
        llm.structured("s", "u", required_keys=["beats"])
    with pytest.raises(LLMOutputInvalid):
        handoff.put_answer("p_x", info.value.request["id"], {"other": 1})
    assert not handoff.answer_path("p_x", info.value.request["id"]).exists()


def test_needs_claude_is_not_swallowed_by_fallbacks(store):
    """调用点的 `except Exception` 兜底(选不出版式就全 broll)不能把"等人答"当成失败。"""
    llm = handoff.ClaudeHandoff("p_x")

    def call_site():
        try:
            return llm.structured("s", "u", required_keys=["beats"])
        except Exception:  # noqa: BLE001 - 模拟调用点的兜底
            return {"beats": []}

    with pytest.raises(handoff.NeedsClaude):
        call_site()


# --------------------------------------------------------------------------
# 原始材料只采一次
# --------------------------------------------------------------------------
def test_raw_material_is_collected_once_when_reused(store, tmp_path, monkeypatch):
    calls = []

    def fake_collect(profile, subject, project_dir, theme=None):
        calls.append(subject)
        return facts_provider.CollectionResult(docs=[facts_provider.RawDoc(
            tool="t", query=subject, retrieved_at="2026-11-19", text=f"材料{len(calls)}")])

    monkeypatch.setattr(intake, "collect_raw", fake_collect)
    pdir = tmp_path / "proj"
    with intake.reuse_raw():
        a = intake._gather(None, "车", pdir, None, terms="", evidence=(), evidence_query="车")
        b = intake._gather(None, "车", pdir, None, terms="", evidence=(), evidence_query="车")
    assert calls == ["车"]
    assert a.docs[0].text == b.docs[0].text == "材料1"
    n = intake.add_raw_doc(pdir, "车", None, facts_provider.RawDoc(
        tool="claude.research", query="q", retrieved_at="2026-11-19", text="补充"))
    assert n == 2
    with intake.reuse_raw():
        c = intake._gather(None, "车", pdir, None, terms="", evidence=(), evidence_query="车")
    assert [d.text for d in c.docs] == ["材料1", "补充"]
    # 不在 reuse_raw 里:照旧每次都采
    intake._gather(None, "车", pdir, None, terms="", evidence=(), evidence_query="车")
    assert len(calls) == 2


# --------------------------------------------------------------------------
# 调度器不碰
# --------------------------------------------------------------------------
def test_scheduler_skips_claude_driven_projects(store):
    from app.scheduler import Scheduler

    row = {
        "id": "p_c", "title": "t", "subject": "车", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": "DRAFT_INTENT",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0,
        "est_cost": 1.0, "created_at": 1.0, "updated_at": 1.0,
    }
    db.create_project(row)
    db.create_project({**row, "id": "p_s"})
    db.update_project("p_c", driver="claude")

    sched = Scheduler()

    async def scan():
        sched._queue = asyncio.Queue()
        sched._prep_queue = asyncio.Queue()
        sched._scan()
        sched._enqueue("p_c")
        return sched._queued

    assert asyncio.run(scan()) == {"p_s"}


# --------------------------------------------------------------------------
# 命令行:建项目 → 答题 → 门禁 A
# --------------------------------------------------------------------------
def _fake_intake(project_id, profile, subject, *, theme=None, angle="", angle_search="",
                 evidence=(), llm=None):
    """假的事实收集,但抽取那一步真的去问 llm(Claude 驱动时就是一个请求)。"""
    data = llm.structured("抽取事实", f"材料:{subject}", required_keys=["facts"])
    facts = [Fact(id=f"f_{i}", claim=c, source_tool="test", scope="product", basis="CLTC",
                  source_kind="official", topic="参数") for i, c in enumerate(data["facts"], 1)]
    db.write_json(project_id, "facts.json", [f.model_dump() for f in facts])
    report = {"sources_requested": ["t"], "sources_ok": ["t"], "sources_failed": [],
              "raw_chars": 10, "facts_extracted": len(facts), "facts_derived": 0,
              "scope_counts": {"product": len(facts)}, "product_gap": [], "extract_error": ""}
    db.write_json(project_id, "facts_report.json", report)
    return facts, report


def _plan_from_prompt(user: str) -> list[str]:
    m = re.search(r"段落序列:\n(.*?)(?:\n\n|$)", user, re.S)
    out = []
    for line in (m.group(1).splitlines() if m else []):
        mm = re.match(r"^\s*\d+[.、]\s*(.+)$", line)
        if mm:
            out.append(mm.group(1).strip())
    return out


def _answer_for(req: dict) -> dict:
    """扮演 Claude:按请求要的字段作答。"""
    keys = set(req["required_keys"])
    if "facts" in keys:
        return {"facts": ["续航 835 公里"]}
    if "segments" in keys:
        plan = _plan_from_prompt(req["user"]) or ["开场"]
        return {"segments": [{"section": s, "text": f"这一段讲的是{s}该注意的地方",
                              "fact_refs": []} for s in plan],
                "title": "测试标题", "thesis": "这车值不值,得看续航与价格"}
    if "beats" in keys:
        n = len(db.read_json(req["project_id"], "script.json", []) or []) or 1
        return {"beats": [{"idx": i + 1, "layout": "broll"} for i in range(n)]}
    return {k: [] for k in keys}


def _run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_cli_drives_project_to_gate_a(store, monkeypatch, capsys):
    monkeypatch.setattr(intake, "run", _fake_intake)
    monkeypatch.setattr(orchestrator, "_llm", lambda: pytest.fail("Claude 项目不该用 DeepSeek"))

    code, made = _run(capsys, "new", "测试车型", "--duration", "240")
    assert code == cli.OK
    pid = made["project_id"]
    assert db.get_project(pid)["driver"] == "claude"

    answered = 0
    for _ in range(20):
        code, out = _run(capsys, "step", pid, "--idle", "0")
        if code != cli.NEEDS_CLAUDE:
            break
        req = handoff.get_request(pid, out["needs_claude"]["id"])
        assert req is not None and req["project_id"] == pid
        handoff.put_answer(pid, req["id"], _answer_for(req))
        answered += 1
    assert code == cli.OK, out
    assert out["state"] == str(State.GATE_A)
    assert answered >= 2  # 至少事实抽取 + 写稿
    assert handoff.pending(pid) == []

    # 门禁不经 --confirm 不放行
    code, out = _run(capsys, "step", pid, "--idle", "0")
    assert out["state"] == str(State.GATE_A) and out["steps"] == []


def test_cli_ask_show_and_answer_via_file(store, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(intake, "run", _fake_intake)
    _, made = _run(capsys, "new", "测试车型", "--duration", "240")
    pid = made["project_id"]
    code, out = _run(capsys, "step", pid, "--idle", "0")
    assert code == cli.NEEDS_CLAUDE
    rid = out["needs_claude"]["id"]

    assert cli.main(["ask", "show", pid]) == cli.OK
    shown = capsys.readouterr().out
    assert "## SYSTEM" in shown and "抽取事实" in shown and rid in shown

    bad = tmp_path / "bad.json"
    bad.write_text('{"nope": 1}', encoding="utf-8")
    code, out = _run(capsys, "ask", "answer", pid, rid, str(bad))
    assert code == cli.REJECTED and "facts" in out["error"]

    good = tmp_path / "good.json"
    good.write_text('{"facts": ["续航 835 公里"]}', encoding="utf-8")
    code, out = _run(capsys, "ask", "answer", pid, rid, str(good))
    assert code == cli.OK and out["saved"]
    code, out = _run(capsys, "step", pid, "--max", "1", "--idle", "0")
    assert out["state"] == str(State.FACTS_RESOLVED)


def test_step_refuses_scheduler_projects(store, monkeypatch, capsys):
    monkeypatch.setattr(intake, "run", _fake_intake)
    _, made = _run(capsys, "new", "测试车型", "--duration", "240")
    pid = made["project_id"]
    _run(capsys, "adopt", pid, "--release")
    code, out = _run(capsys, "step", pid)
    assert code == cli.REJECTED and "adopt" in out["error"]



# --------------------------------------------------------------------------
# 选题:热点 → Claude 写 → 现有规则过滤
# --------------------------------------------------------------------------
def test_topics_fetch_prompt_put(store, monkeypatch, capsys):
    from app.providers import trends

    rows = [{"source": "dongchedi", "title": "小米YU7 月销破 3 万", "car": "小米YU7",
             "url": "https://e.com/1", "metric": "30000"}]
    monkeypatch.setattr(trends, "collect", lambda **kw: (rows, {"bilibili": "412"}))

    code, out = _run(capsys, "topics", "fetch", "--n", "3")
    assert code == cli.OK and out["trends"] == 1 and out["errors"] == {"bilibili": "412"}

    assert cli.main(["topics", "prompt"]) == cli.OK
    shown = capsys.readouterr().out
    assert "## SYSTEM" in shown and "小米YU7 月销破 3 万" in shown

    answer = {"topics": [
        {"subject": "小米YU7", "angle": "月销破 3 万,标准版反而最不值", "theme": "buyguide",
         "hook": "多花的钱买的是快充", "why_hot": "月销破 3 万", "sources": ["https://e.com/1",
                                                                       "https://made.up/x"],
         "heat": 5, "search_terms": "小米YU7 月销"},
        # 车型不在热点里:规则会丢掉
        {"subject": "某某车X9", "angle": "随便一个角度", "theme": "buyguide"},
    ]}
    path = config.DATA_DIR / "t.json"
    path.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    code, out = _run(capsys, "topics", "put", str(path))
    assert code == cli.OK
    assert [t["subject"] for t in out["kept"]] == ["小米YU7"]
    assert out["dropped"] == 1
    assert out["kept"][0]["sources"] == ["https://e.com/1"]  # 编的链接被摘掉


def test_topics_put_before_fetch_is_rejected(store, capsys, tmp_path):
    f = tmp_path / "a.json"
    f.write_text('{"topics": []}', encoding="utf-8")
    code, out = _run(capsys, "topics", "put", str(f))
    assert code == cli.REJECTED and "fetch" in out["error"]


def test_new_passes_evidence_and_roster(store, monkeypatch, capsys):
    seen = {}

    def fake_create(payload):
        seen["payload"] = payload
        return real_create(payload)

    real_create = orchestrator.create_project
    monkeypatch.setattr(orchestrator, "create_project", fake_create)
    code, out = _run(capsys, "new", "测试车型", "--evidence", "榜单第 1", "--evidence", "月销 3 万")
    assert code == cli.OK
    assert seen["payload"].angle_evidence == ["榜单第 1", "月销 3 万"]
    assert seen["payload"].subjects is None


def test_project_llm_follows_driver():
    assert isinstance(orchestrator._project_llm({"id": "p_1", "driver": "claude"}),
                      handoff.ClaudeHandoff)
    assert not isinstance(orchestrator._project_llm({"id": "p_1", "driver": "scheduler"}),
                          handoff.ClaudeHandoff)


# --------------------------------------------------------------------------
# 包装
# --------------------------------------------------------------------------
def _gate_a_project(capsys) -> str:
    _, made = _run(capsys, "new", "测试车型", "--duration", "240")
    pid = made["project_id"]
    for _ in range(20):
        code, out = _run(capsys, "step", pid, "--idle", "0")
        if code != cli.NEEDS_CLAUDE:
            break
        req = handoff.get_request(pid, out["needs_claude"]["id"])
        handoff.put_answer(pid, req["id"], _answer_for(req))
    assert out["state"] == str(State.GATE_A)
    return pid


def test_package_prompt_and_put(store, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(intake, "run", _fake_intake)
    pid = _gate_a_project(capsys)

    code, out = _run(capsys, "package", pid)
    assert code == cli.REJECTED

    assert cli.main(["package-prompt", pid]) == cli.OK
    shown = capsys.readouterr().out
    assert "## SYSTEM" in shown and "titles" in shown

    answer = {"titles": [
        {"kind": "stake", "text": "测试车型到底该不该买,看续航", "note": "立场"},
        {"kind": "number", "text": "续航 999 公里的车", "note": "编的数字,不以车名开头"},
        {"kind": "person", "text": "测试车型适合天天跑高速的人", "note": "人群"}],
        "comment": {"text": "你选A还是B?", "options": ["A", "B"]},
        "replies": ["a", "b", "c"]}
    f = tmp_path / "p.json"
    f.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    code, out = _run(capsys, "package-put", pid, str(f))
    assert code == cli.OK and out["saved"]
    assert "续航 999 公里的车" in out["replaced_by_template"]
    assert "续航 999 公里的车" not in out["titles"]
    code, out = _run(capsys, "package", pid)
    assert code == cli.OK and out["titles"]

    bad = tmp_path / "bad.json"
    bad.write_text('{"comment": {}}', encoding="utf-8")
    code, out = _run(capsys, "package-put", pid, str(bad))
    assert code == cli.REJECTED
