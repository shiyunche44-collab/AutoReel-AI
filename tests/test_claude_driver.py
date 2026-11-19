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

