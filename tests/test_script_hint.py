"""阶段 2:账号经验回灌脚本、开场多版挑选、预测倍数。

没挂手册时脚本提示词逐字不变;挂上后多一段经验。开场挑选只留合规、有事实支撑的版本。"""
from __future__ import annotations

import time

import pytest

from app.categories import load_profile
from app.models import Fact
from app.pipeline import script_gen
from tests.test_coach import _project, fresh_db, no_playbook  # noqa: F401 - fixtures


class _Capture:
    def __init__(self, replies=()) -> None:
        self.calls: list[tuple[str, str]] = []
        self.replies = list(replies)

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        if self.replies:
            return self.replies.pop(0)
        return {k: [] if k != "title" else "t" for k in required_keys}


def _facts() -> list[Fact]:
    return [Fact(id="f_1", claim="零百加速 3.2 秒", source_tool="t", confidence="high",
                 scope="product", retrieved_at="2026-01-01")]


def _system(**kw) -> str:
    profile = load_profile("auto_long")
    plan = list(profile.plan_for(profile.supported_durations()[0]))
    llm = _Capture()
    script_gen.generate(subject="小米YU7", profile=profile, facts=_facts(), plan=plan,
                        duration=profile.supported_durations()[0], llm=llm,
                        theme=profile.theme_spec(), **kw)
    return llm.calls[-1][0]


# --------------------------------------------------------------------------
# 回灌提示词
# --------------------------------------------------------------------------
def test_prompt_unchanged_without_hint(monkeypatch):
    base = _system()
    monkeypatch.setattr(script_gen, "learned_hint", lambda: "")
    assert _system() == base
    assert "本账号的经验" not in base


def test_hint_appended_and_errors_ignored(monkeypatch):
    base = _system()
    monkeypatch.setattr(script_gen, "learned_hint", lambda: "- 开场别铺垫")
    got = _system()
    assert got.startswith(base) and got.endswith("- 开场别铺垫")

    def boom():
        raise RuntimeError("db gone")

    monkeypatch.setattr(script_gen, "learned_hint", boom)
    assert _system() == base


def test_locked_hook_rule():
    assert "评审挑出来的" in _system(hook="多花三万买的是省心", hook_locked=True)
    assert "可以直接用它" in _system(hook="多花三万买的是省心")


# --------------------------------------------------------------------------
# 开场多版挑选
# --------------------------------------------------------------------------
def test_choose_hook_filters_and_picks_best():
    profile = load_profile("auto_long")
    llm = _Capture([
        {"candidates": [
            {"text": "大家好,今天聊小米YU7", "fact_refs": []},           # 套话
            {"text": "零百 3.2 秒没用", "fact_refs": []},                # 数字没事实
            {"text": "零百 3.2 秒,对你没用", "fact_refs": ["f_1"]},
            {"text": "这车最该买的是低配", "fact_refs": []},
            {"text": "这车最该买的是低配", "fact_refs": []},              # 重复
        ]},
        {"scores": [{"i": 0, "score": 6, "why": "有反差"}, {"i": 1, "score": 8.5, "why": "立场鲜明"}]},
    ])
    got = script_gen.choose_hook(subject="小米YU7", profile=profile, facts=_facts(), llm=llm)
    assert [c["text"] for c in got["candidates"]] == ["零百 3.2 秒,对你没用", "这车最该买的是低配"]
    assert got["chosen"] == "这车最该买的是低配" and got["candidates"][1]["why"] == "立场鲜明"
    assert len(llm.calls) == 2


def test_choose_hook_gives_up_quietly():
    profile = load_profile("auto_long")
    one = _Capture([{"candidates": [{"text": "这车最该买的是低配"}]}])
    assert script_gen.choose_hook(subject="x", profile=profile, facts=_facts(), llm=one) is None
    assert len(one.calls) == 1  # 不到两版就不评审

    class Broken:
        def structured(self, *a, **k):
            raise RuntimeError("timeout")

    assert script_gen.choose_hook(subject="x", profile=profile, facts=_facts(), llm=Broken()) is None


def test_choose_hook_carries_hint(monkeypatch):
    monkeypatch.setattr(script_gen, "learned_hint", lambda: "- 「反面例子」")
    llm = _Capture([{"candidates": []}])
    script_gen.choose_hook(subject="x", profile=load_profile("auto_long"), facts=_facts(), llm=llm)
    assert "反面例子" in llm.calls[0][0]


# --------------------------------------------------------------------------
# 账号经验(playbook.script_hint)
# --------------------------------------------------------------------------
def _work(db, pid, *, t0, bounce, opening):
    _project(db, f"p_{pid}")
    db.write_json(f"p_{pid}", "script.json", [{"idx": 0, "section": "开场", "text": f"{opening}。后面"}])
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": "douyin", "title": "t", "state": "published",
                           "remote_id": f"w_{pid}", "published_at": t0, "variant": "{}"})
    db.add_snapshot(pid, "douyin", t0 + 3600, {"views": 200, "bounce_2s": bounce,
                                               "avg_watch_sec": 20, "hours_since_publish": 1.0})


def test_script_hint_examples_and_rules(fresh_db):
    from app.team import playbook

    now = time.time()
    assert playbook.script_hint(now) == ""
    for i, (b, line) in enumerate([(0.2, "最好的开场"), (0.25, "次好的开场"),
                                   (0.3, "一般的开场"), (0.6, "最差的开场")]):
        _work(fresh_db, f"w{i}", t0=now - 20 * 86400, bounce=b, opening=line)
    hint = playbook.script_hint(now)
    assert "「最好的开场」" in hint and "「次好的开场」" in hint
    assert hint.index("最好的开场") < hint.index("留人最差") < hint.index("最差的开场")
    assert "诊断" not in hint  # 都是 20 天前发的,不算最近
    assert not any(ch.isdigit() for ch in hint)

    _work(fresh_db, "fresh", t0=now - 86400, bounce=0.7, opening="新的开场")  # 留存 30%:hook 问题
    assert playbook.SCRIPT_RULE["hook"] in playbook.script_hint(now)
    assert len(playbook.script_hint(now)) <= playbook.HINT_MAX_CHARS


def test_install_wires_script_hooks(fresh_db, monkeypatch):
    from app import config
    from app.team import playbook

    monkeypatch.setattr(config, "TEAM_HOOK_CANDIDATES", 3)
    playbook.install()
    assert script_gen.learned_hint is playbook.script_hint and script_gen.hook_candidates == 3
    playbook.uninstall()
    assert script_gen.learned_hint is None and script_gen.hook_candidates == 0


# --------------------------------------------------------------------------
# 预测倍数
# --------------------------------------------------------------------------
def test_predict_geo_mean():
    from app.team import playbook

    rows = [{"dimension": "subject", "value": "A", "metric": "views", "effect": 2.0},
            {"dimension": "theme", "value": "single", "metric": "views", "effect": 0.5},
            {"dimension": "duration", "value": "x", "metric": "views", "effect": 1.6,
             "status": "off"}]
    assert playbook.predict({"subject": "A", "theme": "single"}, rows) == pytest.approx(1.0)
    assert playbook.predict({"subject": "A", "theme": "other"}, rows) == 2.0  # 没结论的维度不拉平
    assert playbook.predict({"duration": "x"}, rows) == 1.0  # 停用的不算
    assert playbook.predict({}, []) == 1.0


def test_pending_items_carry_predict(fresh_db):
    from app.team import views
    from tests.test_coach import _learn

    _learn(fresh_db, "subject", "理想i8", 1.5)
    for pid, subject, status in (("a", "理想i8", "proposed"), ("b", "小米YU7", "proposed"),
                                 ("c", "理想i8", "approved")):
        fresh_db.create_plan_item({"id": pid, "category": "auto_long", "slot_at": time.time() + 86400,
                                   "subject": subject, "status": status})
    items = {i["id"]: i for i in views.overview("auto_long")["plan"]}
    assert items["a"]["predict"] == 1.5 and items["b"]["predict"] == 1.0
    assert "predict" not in items["c"]
