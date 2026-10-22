"""节奏硬约束:画面变化间隔 + broll 占比 + 句子级拆拍。

复盘原话(analysis/teardown.html):「每 5–6 秒必须有一次画面变化,
且全片 broll 占比从四成压到三成以内」。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.categories import load_profile
from app.models import Fact, ScriptSegment, ShotSpec
from app.pipeline import explainer, rhythm, script_gen


def _spec(idx, layout, duration=5.0, section="三电性能", overlay=None, card=None):
    return ShotSpec(idx=idx, duration=duration, section=section, visual_zh=f"[{layout}] x",
                    narration_zh="旁白", layout=layout, overlay=overlay, card=card)


def _words(text: str) -> str:
    """去掉标点只留字与数字:比"词有没有丢"用,不比标点。"""
    return re.sub(r"[，,。、；;：:!?！？\s]", "", text or "")


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def test_broll_ratio_counts_beats():
    specs = [_spec(0, "broll"), _spec(1, "panel"), _spec(2, "broll"), _spec(3, "stat")]
    assert rhythm.broll_ratio(specs) == 0.5
    ratio, total = rhythm.broll_seconds_ratio(specs)
    assert total == 20.0 and ratio == 0.5


def test_broll_seconds_ratio_uses_real_durations():
    specs = [_spec(0, "broll", duration=20.0), _spec(1, "panel", duration=5.0)]
    ratio, total = rhythm.broll_seconds_ratio(specs)
    assert total == 25.0
    assert ratio == pytest.approx(0.8)


def test_long_beat_shows_up_as_a_visual_gap():
    """一个 12 秒的拍,后面接一个变化 —— 中间 12 秒没有画面变化。"""
    specs = [_spec(0, "panel", duration=12.0), _spec(1, "stat", duration=4.0)]
    assert rhythm.max_change_gap(specs) == pytest.approx(12.0)


def test_short_beats_keep_the_gap_small():
    specs = [_spec(i, "panel" if i % 2 else "stat", duration=5.0) for i in range(6)]
    assert rhythm.max_change_gap(specs) <= rhythm.MAX_CHANGE_GAP


def test_same_layout_with_same_rows_is_not_a_change():
    """两拍画面一模一样 = 观众看不出换了一屏,间隔要算进去。"""
    ov = {"kind": "panel", "rows": [{"label": "续航", "values": ["643公里"], "fact_id": "f_1"}]}
    specs = [_spec(0, "panel", duration=5.0, overlay=dict(ov)),
             _spec(1, "panel", duration=5.0, overlay=dict(ov))]
    assert rhythm.max_change_gap(specs) == pytest.approx(10.0)


def test_concept_animation_counts_as_always_changing():
    specs = [_spec(0, "concept", duration=20.0, overlay={"kind": "concept", "rows": []}),
             _spec(1, "stat", duration=4.0)]
    assert rhythm.max_change_gap(specs) <= rhythm.MAX_CHANGE_GAP


def test_problems_reports_broll_over_cap():
    specs = [_spec(i, "broll" if i < 5 else "panel", duration=5.0) for i in range(10)]
    problems = rhythm.problems(specs)
    assert any("broll" in p for p in problems)


def test_problems_quiet_when_within_limits():
    specs = [_spec(i, "broll" if i < 3 else "panel", duration=5.0) for i in range(10)]
    specs[0].overlay = {"kind": "panel", "rows": [{"label": "a", "values": ["1"], "fact_id": "f"}]}
    assert not [p for p in rhythm.problems(specs) if "broll" in p]


def test_report_has_the_keys_the_ui_needs():
    rep = rhythm.report([_spec(0, "broll", duration=5.0), _spec(1, "panel", duration=5.0)])
    for key in ("beats", "broll_ratio", "max_change_gap", "problems", "total_seconds"):
        assert key in rep


# --------------------------------------------------------------------------
# 句子级拆拍
# --------------------------------------------------------------------------
def test_long_paragraph_is_split_into_short_beats():
    segs = [ScriptSegment(idx=0, section="三电性能", text=(
        "它用 752V 碳化硅高压平台,15 分钟补能 405 公里,"
        "标准版 CLTC 续航 643 公里,长续航版能到 835 公里。"),
        fact_refs=["f_1"])]
    out = script_gen.split_segments(segs)
    assert len(out) > 1
    assert all(len(s.text) <= script_gen.SHOOT_CHARS_MAX for s in out)
    # 拆开不能丢事实引用:数字还得有出处
    assert all(s.fact_refs == ["f_1"] for s in out)
    # 词一个都不能少(标点会从逗号变成句号,不比标点)
    assert _words("".join(s.text for s in out)) == _words(segs[0].text)
    # 拆完的块不该挂着半个逗号
    assert all(not s.text.rstrip().endswith(("，", ",")) for s in out)


def test_short_paragraph_is_left_alone():
    segs = [ScriptSegment(idx=0, section="开场", text="别急着订车,先搞清楚买哪版。")]
    assert len(script_gen.split_segments(segs)) == 1


def test_split_keeps_section_and_car():
    segs = [ScriptSegment(idx=0, section="车型亮相", subject="小米YU7", text="很长的一段话。" * 6,
                           fact_refs=["f_1"])]
    out = script_gen.split_segments(segs)
    assert all(s.section == "车型亮相" and s.subject == "小米YU7" for s in out)
    assert [s.idx for s in out] == list(range(len(out)))


def test_split_does_not_create_stub_beats():
    """2 个字的碎块不单独成拍 —— 撑不住画面,也不像人话。"""
    segs = [ScriptSegment(idx=0, section="三电性能",
                           text="第一句讲完了,第二句也讲完了,第三句补一个尾巴。")]
    out = script_gen.split_segments(segs)
    assert all(len(s.text) >= script_gen.MIN_PART_CHARS or len(out) == 1 for s in out)


# --------------------------------------------------------------------------
# broll 兜底:有数字就上屏
# --------------------------------------------------------------------------
def _fact():
    return Fact(id="f_1", claim="小米YU7标准版售价23.35万元。", source_tool="t",
                confidence="high", scope="product", topic="售价")


def test_auto_panel_only_when_the_number_is_spoken():
    seg = ScriptSegment(idx=0, section="定位与价格", text="入门价 23.35 万。", fact_refs=["f_1"])
    ov = explainer.auto_panel(seg, [_fact()])
    assert ov is not None
    assert ov["rows"][0]["values"] == ["23.35万元"]
    assert ov["rows"][0]["fact_id"] == "f_1"


def test_auto_panel_refuses_unrelated_facts():
    """旁白没提这个数字 → 不许把这条参数安到这一拍上。"""
    seg = ScriptSegment(idx=0, section="定位与价格", text="外观挺有辨识度。", fact_refs=["f_1"])
    assert explainer.auto_panel(seg, [_fact()]) is None


def test_auto_panel_needs_a_number_in_the_narration():
    seg = ScriptSegment(idx=0, section="定位与价格", text="这车挺好看。", fact_refs=["f_1"])
    assert explainer.auto_panel(seg, [_fact()]) is None


def test_rescue_broll_turns_a_data_beat_into_a_panel():
    p = load_profile("auto_long")
    segs = [ScriptSegment(idx=0, section="定位与价格", text="入门价 23.35 万。", fact_refs=["f_1"])]
    specs = [_spec(0, "broll", duration=5.0, section="定位与价格")]
    out = explainer.rescue_broll(specs, segs, [_fact()], p)
    assert out[0].layout == "panel"
    assert out[0].overlay["rows"][0]["values"] == ["23.35万元"]
    assert out[0].visual_zh.startswith("[panel]")


def test_rescue_broll_leaves_qualitative_beats_alone():
    p = load_profile("auto_long")
    segs = [ScriptSegment(idx=0, section="定位与价格", text="外观挺有辨识度。", fact_refs=[])]
    specs = [_spec(0, "broll", duration=5.0, section="定位与价格")]
    assert explainer.rescue_broll(specs, segs, [_fact()], p)[0].layout == "broll"


def test_rescue_broll_respects_the_section_grammar():
    """这一段的视觉语法里没有面板 → 不硬塞。"""
    p = load_profile("auto_long")
    segs = [ScriptSegment(idx=0, section="结论建议", text="入门价 23.35 万。", fact_refs=["f_1"])]
    specs = [_spec(0, "broll", duration=5.0, section="结论建议")]
    assert explainer.rescue_broll(specs, segs, [_fact()], p)[0].layout == "broll"


# --------------------------------------------------------------------------
# 降级优先改成面板,而不是扔回 broll
# --------------------------------------------------------------------------
def test_card_run_demotes_to_panel_when_the_section_allows_it():
    p = load_profile("auto_long")
    card = {"kind": "spec", "title": "t", "columns": [], "source": "官方",
            "rows": [{"label": "续航", "values": ["643公里"], "fact_id": "f_1"}]}
    specs = [_spec(i, "card", section="三电性能", card=dict(card)) for i in range(3)]
    out = explainer.enforce_rhythm(specs, p)
    assert out[2].layout == "panel"
    assert out[2].overlay["rows"][0]["values"] == ["643公里"]
    assert out[2].card is None


def test_demote_without_profile_still_falls_back_to_broll():
    card = {"kind": "spec", "title": "t", "columns": [], "source": "",
            "rows": [{"label": "续航", "values": ["643公里"], "fact_id": "f_1"}]}
    specs = [_spec(i, "card", section="三电性能", card=dict(card)) for i in range(3)]
    out = explainer.enforce_rhythm(specs)
    assert out[2].layout == "broll"


# --------------------------------------------------------------------------
# 结尾要给行动
# --------------------------------------------------------------------------
def test_ending_without_action_is_flagged():
    segs = [ScriptSegment(idx=0, section="结论建议", text="标准版闭眼入。")]
    issues = script_gen.ending_lint(segs)
    assert issues and not issues[0].is_error


def test_ending_with_a_question_is_quiet():
    segs = [ScriptSegment(idx=0, section="结论建议", text="你在纠结哪款?评论区说。")]
    assert script_gen.ending_lint(segs) == []


def test_ending_with_a_preview_is_quiet():
    segs = [ScriptSegment(idx=0, section="结论建议", text="下期做同价位横评。")]
    assert script_gen.ending_lint(segs) == []


# --------------------------------------------------------------------------
# 提示词口径
# --------------------------------------------------------------------------
def test_beat_prompt_tells_the_model_broll_is_capped():
    p = load_profile("auto_long")
    text = explainer.SYSTEM
    assert "不超过三成" in text
    assert "全片大约四成是 broll" not in text
    # 口径里的拍数上限要跟着拍数走
    system = text.format(kw=12, domain="汽车", subject_noun="车", keyword_examples="x",
                          stat_examples="x", view_enum="x", view_guide="x", count=10,
                          broll_cap=3, concept_guide="")
    assert "最多 3 拍" in system


def test_each_beat_sees_its_own_facts():
    p = load_profile("auto_long")
    seg = ScriptSegment(idx=0, section="三电性能", text="续航 643 公里。", fact_refs=["f_1"])
    f = Fact(id="f_1", claim="小米YU7标准版 CLTC 续航 643 公里", source_tool="t",
             confidence="high", scope="product")
    line = explainer._beat_line(0, seg, p, [f])
    assert "本拍事实" in line and "f_1" in line


def test_script_prompt_has_the_three_part_opening_and_the_ending():
    text = script_gen.SYSTEM
    assert "开头三段式" in text
    assert "先给冲突" in text
    assert "结尾要给**行动**" in text
