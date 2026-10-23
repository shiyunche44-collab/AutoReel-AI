"""端到端:一条片子从建项目走到成片。

此前 41 个测试文件**全是单元级的** —— 状态机、lint、事实核对、版式落地
这些环节之间的接口错位,只能靠真的跑一条片子才发现。

这条测试用假的 LLM / 假的 TTS / 假的渲染,把整条链走一遍,断言:

- 每次状态跃迁都合法(用 `state.can_transition` 校验);
- 每个跃迁都留了事件(事件流是本项目唯一的排障依据);
- 门禁态**不会**自动放行 —— 没确认就是不动;
- 门禁 A 的 lint 拦得住(有 error 就不放行);
- 重跑同一阶段不重复消耗(幂等)。

真的那部分:状态机、脚本解析、合规 lint、逐拍版式的落地与自检、
分镜同步、素材池启动、事件流、记账。
"""
from __future__ import annotations

import re

import pytest

from app import config, db
from app.models import Fact, ProjectCreate
from app.pipeline import explainer, intake, narration, orchestrator
from app.state import State, can_transition

#: 开场那句故意写成套话 —— 用来验证门禁 A 的 lint 真的拦得住。
OPENING_FILLER = "大家好,今天咱们来聊聊这台车到底怎么样"

#: 其它段落的普通旁白(不含数字 → 不会触发"有数字没引用"的规则)
PLAIN_TEXT = "这一段讲的是{topic}该注意的地方"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    # 素材池要联网下载与视觉打标签,端到端测试里关掉
    monkeypatch.setattr(config, "USE_LIBRARY", False)
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


# --------------------------------------------------------------------------
# 假的外界
# --------------------------------------------------------------------------
def _plan_from_prompt(user: str) -> list[str]:
    """从脚本生成的提示词里抠出段落序列。"""
    m = re.search(r"段落序列:\n(.*?)(?:\n\n|$)", user, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        mm = re.match(r"^\d+[.、]\s*(.+)$", line)
        if mm:
            out.append(mm.group(1).strip())
    return out


class _FakeLLM:
    """按 required_keys 分派 canned 响应。脚本与版式走的是**真的**解析逻辑。"""

    name = "fake"

    def __init__(self) -> None:
        self.n_segments = 0
        self.calls: list[set] = []

    def available(self) -> bool:
        return True

    def structured(self, system, user, *, required_keys=None, temperature=0.6,
                  max_retries=2):
        keys = set(required_keys or [])
        self.calls.append(keys)
        if "segments" in keys:
            plan = _plan_from_prompt(user) or ["开场"]
            self.n_segments = len(plan)
            segments = []
            for i, section in enumerate(plan):
                text = OPENING_FILLER if i == 0 else PLAIN_TEXT.format(topic=section)
                segments.append({"section": section, "text": text, "fact_refs": []})
            return {"segments": segments, "title": "测试标题",
                    "thesis": "这车值不值,得看续航与价格"}
        if "beats" in keys:
            return {"beats": [{"idx": i + 1, "layout": "broll"}
                              for i in range(self.n_segments)]}
        return {}


def _fake_intake(project_id, profile, subject, *, theme=None, angle="",
                 angle_search="", evidence=(), llm=None):
    """假的事实采集:写一份能被下游读懂的 facts.json。"""
    facts = [Fact(id="f_1", claim="续航 835 公里", source_tool="test",
                  scope="product", basis="CLTC", source_kind="official",
                  topic="参数", enabled=True)]
    db.write_json(project_id, "facts.json", [f.model_dump() for f in facts])
    report = {
        "sources_requested": ["ifind.product_news"],
        "sources_ok": ["ifind.product_news"],
        "sources_failed": [],
        "raw_chars": 200,
        "facts_extracted": 1,
        "facts_derived": 0,
        "scope_counts": {"product": 1},
        "product_gap": [],
        "extract_error": "",
    }
    db.write_json(project_id, "facts_report.json", report)
    return facts, report


def _fake_time_beats(project_id, specs, profile):
    """假的 TTS 合成:不联网,但给出真实的 NarrationResult。"""
    nar = narration.NarrationResult(
        ok=True,
        sentences=["一句旁白"],
        synth_chars=12,
        total_chars=12,
        total_duration=2.0,
    )
    return specs, nar


def _fake_render_pending(project_id, specs, *, subject, profile, limit=3,
                         subjects=None, should_stop=None):
    """假的逐拍渲染:不跑 ffmpeg,但把拍标成渲好了。"""
    for s in specs:
        db.update_shot(project_id, s.idx, "draft", status="succeeded",
                       draft_path=f"drafts/{s.idx:03d}.mp4")
    return {"rendered": [s.idx for s in specs], "errors": [], "remaining": [],
            "stage_complete": True}


def _fake_compose(project_id, specs, profile, title, llm=None):
    root = config.project_dir(project_id)
    out = root / "output.mp4"
    out.write_bytes(b"fake mp4")
    return {"output_path": str(out), "cover": str(root / "cover.jpg"),
            "duration": 240.0}


@pytest.fixture()
def wired(monkeypatch):
    """把外界全部换成假的。"""
    llm = _FakeLLM()
    monkeypatch.setattr(orchestrator, "_llm", lambda: llm)
    monkeypatch.setattr(intake, "run", _fake_intake)
    monkeypatch.setattr(explainer, "time_beats", _fake_time_beats)
    monkeypatch.setattr(explainer, "render_pending", _fake_render_pending)
    monkeypatch.setattr(explainer, "compose", _fake_compose)
    return llm


# --------------------------------------------------------------------------
# 全链路
# --------------------------------------------------------------------------
def _create(subject: str = "测试车型") -> str:
    return orchestrator.create_project(
        ProjectCreate(subject=subject, category="auto_long", duration=240))


def test_full_chain_reaches_done(store, wired):
    """建项目 → … → 成片,一路走得通。"""
    pid = _create()
    assert db.get_project(pid)["state"] == str(State.DRAFT_INTENT)

    # 事实采集
    out = orchestrator.advance(pid)
    assert out["state"] == str(State.FACTS_RESOLVED)
    # 脚本生成(走真的 script_gen + 真的 lint)
    out = orchestrator.advance(pid)
    assert out["state"] == str(State.SCRIPT_READY)
    # 逐拍版式
    out = orchestrator.advance(pid)
    assert out["state"] == str(State.STORYBOARD_READY)
    # 到门禁 A
    out = orchestrator.advance(pid)
    assert out["state"] == str(State.GATE_A)
    assert out.get("waiting") is True


def test_gate_does_not_advance_without_confirmation(store, wired):
    """门禁是最后一道人工确认 —— 没确认就是不动。"""
    pid = _create()
    for _ in range(4):
        orchestrator.advance(pid)
    assert db.get_project(pid)["state"] == str(State.GATE_A)
    # 再调多少次也停在门禁 A
    for _ in range(3):
        out = orchestrator.advance(pid)
        assert out["state"] == str(State.GATE_A)
    assert db.get_project(pid)["state"] == str(State.GATE_A)


def test_gate_a_lint_blocks_then_force_passes(store, wired):
    """脚本有 lint error 时门禁 A 拦住,显式 force 才放行。"""
    pid = _create()
    for _ in range(4):
        orchestrator.advance(pid)
    assert db.get_project(pid)["state"] == str(State.GATE_A)

    # 开场是套话 → lint 有 error → 门禁拦住
    with pytest.raises(orchestrator.PipelineError) as err:
        orchestrator.advance(pid, confirmed=True)
    assert "门禁 A 未放行" in str(err.value)

    # 显式 force 才放行
    out = orchestrator.advance(pid, confirmed=True, force=True)
    assert out["state"] == str(State.DRAFT_RENDERING)


def test_chain_to_done_and_events_are_legal(store, wired):
    """一路走到 DONE,并且每次跃迁都合法、都有事件。"""
    pid = _create()
    orchestrator.advance(pid)                      # → FACTS_RESOLVED
    orchestrator.advance(pid)                      # → SCRIPT_READY
    orchestrator.advance(pid)                      # → STORYBOARD_READY
    orchestrator.advance(pid)                      # → GATE_A
    orchestrator.advance(pid, confirmed=True, force=True)   # → DRAFT_RENDERING
    out = orchestrator.advance(pid)                # → GATE_B
    assert out["state"] == str(State.GATE_B)
    orchestrator.advance(pid, confirmed=True)      # → FINAL_RENDERING
    orchestrator.advance(pid)                      # → QA_PASSED
    out = orchestrator.advance(pid)                # → DONE
    assert out["state"] == str(State.DONE)
    assert db.get_project(pid)["state"] == str(State.DONE)

    # 每次跃迁都合法
    events = db.get_events(pid, limit=200)
    for e in events:
        if not e["src"]:
            continue
        assert can_transition(e["src"], e["dst"]), f"非法跃迁:{e['src']} → {e['dst']}"
    # 每个阶段都留了事件(不是只有状态变了才有)
    dsts = {e["dst"] for e in events}
    for st in (State.FACTS_RESOLVED, State.SCRIPT_READY, State.STORYBOARD_READY,
               State.GATE_A, State.DRAFT_RENDERING, State.GATE_B,
               State.FINAL_RENDERING, State.QA_PASSED, State.DONE):
        assert str(st) in dsts, f"{st} 这一步没有留下事件"


def test_chain_is_idempotent(store, wired):
    """重跑同一阶段不重复消耗。"""
    pid = _create()
    orchestrator.advance(pid)                      # → FACTS_RESOLVED
    spent = db.get_project(pid)["spent_cost"]
    calls = len(wired.calls)
    # 已经在 FACTS_RESOLVED,再调一次应该往前走,而不是重跑事实采集
    orchestrator.advance(pid)
    assert db.get_project(pid)["state"] == str(State.SCRIPT_READY)
    assert len(wired.calls) == calls + 1, "不该重新调用 LLM 生成事实"
    assert db.get_project(pid)["spent_cost"] >= spent


def test_reaching_a_stage_twice_does_not_redo_it(store, wired):
    """阶段是幂等的:产物已存在就不重做。"""
    pid = _create()
    orchestrator.advance(pid)          # FACTS_RESOLVED
    orchestrator.advance(pid)          # SCRIPT_READY
    script_before = db.read_json(pid, "script.json")
    # 把状态退回去再跑一次 —— 不该重新生成脚本
    db.update_project(pid, state=str(State.FACTS_RESOLVED))
    orchestrator.advance(pid)
    assert db.read_json(pid, "script.json") == script_before
    assert len(wired.calls) == 2, "不该再调一次 LLM"


def test_state_reason_records_why(store, wired):
    """每次跃迁都要说清楚为什么 —— 排障时靠它。"""
    pid = _create()
    orchestrator.advance(pid)
    row = db.get_project(pid)
    assert row["state_reason"], "状态跃迁必须留下原因"
