"""阶段 3:你的审核偏好 —— 两道闸的表态记录、通过率乘数、归纳出的「你的偏好」。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests.test_coach import _project, fresh_db, no_playbook  # noqa: F401 - fixtures
from tests.test_team import _fake_enqueue, _item


class _LLM:
    def __init__(self, reply=None, error=None) -> None:
        self.reply, self.error, self.calls = reply, error, []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


def _fb(db, subject, decision, *, theme="single", note="", ts=None, stage="topic"):
    db.add_review_feedback({"item_id": f"pl_{subject}", "stage": stage, "decision": decision,
                            "note": note, "features": {"subject": subject, "theme": theme},
                            **({"ts": ts} if ts else {})})


# --------------------------------------------------------------------------
# 记录
# --------------------------------------------------------------------------
def test_topic_gate_records_every_decision(fresh_db):
    from app.team import plan

    _item(fresh_db)
    plan.patch("pl_1", {"slot_at": time.time() + 7200})  # 只挪排期:不算口味
    assert fresh_db.list_review_feedback() == []
    plan.patch("pl_1", {"angle": "新角度", "subject": "小米YU7"})
    plan.approve("pl_1")
    _item(fresh_db, id="pl_2", subject="理想i8")
    plan.reject("pl_2", "最近做过类似的")
    _item(fresh_db, id="pl_3", subject="问界M8")
    _item(fresh_db, id="pl_4", subject="蔚来ES8")
    assert plan.approve_all("auto_long") == 2

    rows = fresh_db.list_review_feedback(stage="topic")
    got = sorted((r["item_id"], r["decision"]) for r in rows)
    assert got == [("pl_1", "approve"), ("pl_1", "edit"), ("pl_2", "reject"),
                   ("pl_3", "approve"), ("pl_4", "approve")]
    edit = next(r for r in rows if r["decision"] == "edit")
    assert edit["note"] == "角度「月销 3 万背后」→「新角度」"  # 车型没变,不写
    reject = next(r for r in rows if r["decision"] == "reject")
    assert reject["note"] == "最近做过类似的"
    assert reject["features"]["subject"] == "理想i8" and reject["features"]["aspect"] == "16:9"


def test_cut_gate_records_title_change_and_knobs(fresh_db, monkeypatch):
    from app.team import publisher

    _fake_enqueue(fresh_db, monkeypatch)
    _project(fresh_db, "p_x")
    _item(fresh_db, status="review", project_id="p_x")
    publisher.approve_cut("pl_1", ["acc1"], title="  零百 3 秒对你没用  ")
    _item(fresh_db, id="pl_2", status="review", project_id="p_x")
    publisher.approve_cut("pl_2", ["acc2"])  # 用默认标题:不算改
    _item(fresh_db, id="pl_3", status="review", project_id="p_x")
    publisher.reject_cut("pl_3", "开场没抓人")

    rows = {r["item_id"]: r for r in fresh_db.list_review_feedback(stage="cut")}
    assert rows["pl_1"]["title_from"] == "t" and rows["pl_1"]["title_to"] == "零百 3 秒对你没用"
    assert rows["pl_2"]["title_from"] == rows["pl_2"]["title_to"] == ""
    assert rows["pl_3"]["decision"] == "reject" and rows["pl_3"]["note"] == "开场没抓人"
    assert rows["pl_1"]["features"]["opening"] == "scene" and rows["pl_1"]["features"]["look"] == "ink"


def test_record_never_breaks_gate(fresh_db, monkeypatch):
    from app import db
    from app.team import plan

    def boom(row):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_review_feedback", boom)
    _item(fresh_db)
    assert plan.approve("pl_1")["status"] == "approved"


# --------------------------------------------------------------------------
# 通过率乘数
# --------------------------------------------------------------------------
def test_rates_shrunk_and_clamped(fresh_db):
    from app.team import preference

    for _ in range(4):
        _fb(fresh_db, "A", "approve")
        _fb(fresh_db, "B", "reject")
    _fb(fresh_db, "C", "approve")
    _fb(fresh_db, "C", "approve")  # n=2,不够
    _fb(fresh_db, "A", "edit")  # 改动不算票
    _fb(fresh_db, "B", "reject", ts=time.time() - 90 * 86400)  # 太久以前

    table = preference.rates()
    assert table["base"] == {"n": 10, "rate": 0.6}
    dims = table["dims"]["subject"]
    # A:(4 + 4×0.6) / 8 = 0.8 → /0.6 = 1.33 → 夹到 1.3
    assert dims["A"]["mult"] == 1.3
    # B:(0 + 2.4) / 8 = 0.3 → 0.5 → 夹到 0.7
    assert dims["B"]["mult"] == 0.7
    assert dims["C"]["mult"] == 1.0
    assert preference.multiplier({"subject": "A", "theme": "single"}, table) == 1.3
    assert preference.multiplier({"subject": "nope"}, table) == 1.0

    fresh_db.set_team_setting(preference.RATE_KEY, False)
    assert preference.multiplier({"subject": "A"}) == 1.0


def test_rates_empty_and_all_rejected(fresh_db):
    from app.team import preference

    assert preference.rates() == {"base": {"n": 0, "rate": None}, "dims": {}}
    for _ in range(3):
        _fb(fresh_db, "A", "reject")
    assert preference.rates()["dims"]["subject"]["A"]["mult"] == 1.0  # 全驳:没有基准可比


def test_rank_ideas_uses_taste(fresh_db):
    from app.team import editor, preference

    for _ in range(4):
        _fb(fresh_db, "A", "approve")
        _fb(fresh_db, "B", "reject")
    idea = lambda s: SimpleNamespace(subject=s, subjects=[], heat=3, theme="single", why_hot="")  # noqa: E731
    ideas = [idea("B"), idea("A")]
    assert [i.subject for i in editor.rank_ideas(ideas, set(), set())] == ["B", "A"]
    rates = preference.rates()
    assert [i.subject for i in editor.rank_ideas(ideas, set(), set(), rates=rates)] == ["A", "B"]
    assert "你常批这类 ×1.30" in editor._rationale(ideas[1], False, taste=1.3)
    assert "你常驳这类" in editor._rationale(ideas[0], False, taste=0.7)


# --------------------------------------------------------------------------
# 你的偏好
# --------------------------------------------------------------------------
def test_preference_crud_and_prompt(fresh_db):
    from app.team import preference

    assert preference.prompt_text() == ""
    a = preference.add("  标题别用问号结尾 ")
    b = preference.add("少排纯参数对比")
    with pytest.raises(ValueError):
        preference.add("  ")
    preference.update(b["id"], status="off")
    assert preference.prompt_text() == "审核人的偏好(照这个来,别违背):\n- 标题别用问号结尾"
    with pytest.raises(ValueError):
        preference.update(a["id"], status="maybe")
    preference.remove(a["id"])
    with pytest.raises(ValueError):
        preference.remove(a["id"])
    assert [p["text"] for p in preference.items()] == ["少排纯参数对比"]


def test_summarize_keeps_manual_and_off(fresh_db):
    from app.team import preference

    now = time.time()
    assert preference.summarize(_LLM({"preferences": ["x"]}), now) is None  # 没料
    _fb(fresh_db, "A", "reject", note="角度没意思")
    _fb(fresh_db, "B", "reject", note="节奏拖", stage="cut")
    _fb(fresh_db, "C", "edit", note="角度「a」→「b」")
    _fb(fresh_db, "D", "reject")  # 没写理由:不算料
    manual = preference.add("标题别用问号结尾")
    off = preference.add("别做 MPV")
    preference.update(off["id"], status="off")
    old = preference.summarize(_LLM({"preferences": ["旧的一条"]}), now)
    assert [p["text"] for p in old] == ["旧的一条"]

    llm = _LLM({"preferences": ["多讲买车决策", "标题别用问号结尾", "", "多讲买车决策",
                                "a", "b", "c", "d", "e"]})
    fresh = preference.summarize(llm, now)
    assert [p["text"] for p in fresh] == ["多讲买车决策", "a", "b", "c", "d"]  # 去重、去手写、最多 5 条
    texts = {p["text"]: p for p in preference.items()}
    assert "旧的一条" not in texts and texts["标题别用问号结尾"]["id"] == manual["id"]
    assert texts["别做 MPV"]["status"] == "off"
    user = llm.calls[0][1]
    assert "[成片驳回 B] 节奏拖" in user and "别做 MPV(已停用)" in user and "[选题驳回 D]" not in user

    log = fresh_db.list_iteration_log()
    assert log[0]["kind"] == "preference" and "5 条" in log[0]["title"]
    n = len(log)
    preference.summarize(llm, now)  # 同样的结果:不再记日志
    assert len(fresh_db.list_iteration_log()) == n

    assert preference.summarize(_LLM(error=RuntimeError("timeout")), now) is None
    assert preference.summarize(None, now) is None
    assert len(preference.items()) == 7


def test_review_summarizes_even_without_works(fresh_db):
    from app.team import Skip, coach, preference

    for s, note in (("A", "角度没意思"), ("B", "节奏拖"), ("C", "热度不够")):
        _fb(fresh_db, s, "reject", note=note)
    with pytest.raises(Skip):
        coach.review(llm=_LLM({"preferences": ["多讲买车决策"]}))
    assert [p["text"] for p in preference.items()] == ["多讲买车决策"]


def test_preferences_lead_script_hint(fresh_db):
    from app.team import playbook, preference

    assert playbook.script_hint() == ""
    preference.add("开场直接给结论")
    assert playbook.script_hint().startswith("审核人的偏好")


def test_overview_lists_rates(fresh_db):
    from app.team import preference

    for _ in range(4):
        _fb(fresh_db, "A", "approve")
        _fb(fresh_db, "B", "reject")
    ov = preference.overview()
    assert ov["rate_on"] is True and ov["base"]["n"] == 8
    assert {(r["label"], r["mult"]) for r in ov["rates"]} == {("车型:A", 1.3), ("车型:B", 0.7)}
