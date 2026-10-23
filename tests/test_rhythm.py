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


def _panel(idx, fact):
    return _spec(idx, "panel", overlay={"kind": "panel", "rows": [{"fact_id": fact}]})


def test_blockers_pass_between_advice_and_block_line():
    # 4/12 拍 = 33%:超过 30% 建议值(problems 报),但没到 35% 拦截线
    specs = [_spec(i, "broll") if i % 3 == 0 else _panel(i, f"f_{i}") for i in range(12)]
    assert rhythm.problems(specs)
    assert rhythm.blockers(specs) == []


def test_blockers_stop_broll_heavy_storyboard_before_tts():
    specs = [_spec(i, "broll") if i % 2 == 0 else _panel(i, f"f_{i}") for i in range(10)]
    assert any("拦截线" in m for m in rhythm.blockers(specs, timed=False))


def test_blockers_stop_long_quiet_gap_only_when_timed():
    specs = [_panel(0, "f_0"), _spec(1, "broll", duration=13.0, overlay=None),
             _panel(2, "f_2"), _panel(3, "f_3"), _panel(4, "f_4")]
    assert any("没有画面变化" in m for m in rhythm.blockers(specs))
    # 时长未回填(合成旁白之前):不查"几秒没变化"
    assert not any("没有画面变化" in m for m in rhythm.blockers(specs, timed=False))


def test_blockers_stop_five_identical_beats_in_a_row():
    specs = [_panel(i, "f_1") for i in range(5)] + [_panel(5, "f_9")]
    assert any("连续" in m for m in rhythm.blockers(specs, timed=False))


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
# 概念动画:全片太少就补(护城河,复盘点名要 4–6 拍)
# --------------------------------------------------------------------------
def test_concept_min_scales_with_the_length():
    assert explainer.concept_min(15) == 3
    assert explainer.concept_min(30) == 4
    assert explainer.concept_min(60) == explainer.MAX_CONCEPT


def test_concept_scene_picks_the_matching_template():
    p = load_profile("auto_long")
    rows = [{"label": "电池容量", "values": ["73kWh"], "fact_id": "f_1"}]
    assert explainer.concept_scene_for(rows, "这块电池 73 度", p.vocab) == "battery"
    rows = [{"label": "零百加速", "values": ["2.92秒"], "fact_id": "f_2"}]
    assert explainer.concept_scene_for(rows, "", p.vocab) == "accel"


def test_concept_scene_refuses_when_nothing_matches():
    p = load_profile("auto_long")
    rows = [{"label": "整备质量", "values": ["2100kg"], "fact_id": "f_3"}]
    assert explainer.concept_scene_for(rows, "今天聊点别的", p.vocab) == ""


def test_promote_concepts_raises_the_count():
    p = load_profile("auto_long")
    rows = [{"label": "电池容量", "values": ["73kWh"], "fact_id": "f_1"}]
    specs = []
    for i in range(12):
        specs.append(_spec(i, "panel", duration=4.0, section="三电性能",
                            overlay={"kind": "panel", "rows": [dict(rows[0])], "source": "官方"}))
    out = explainer.promote_concepts(specs, p)
    assert sum(s.layout == "concept" for s in out) >= explainer.concept_min(len(specs))
    # 补上去的拍要有模板、有数据
    con = next(s for s in out if s.layout == "concept")
    assert con.overlay["scene"] in p.vocab.concept_scenes
    assert con.overlay["rows"]
    assert not explainer.validate_beats(out)


def test_promote_concepts_respects_the_section_grammar():
    p = load_profile("auto_long")
    rows = [{"label": "电池容量", "values": ["73kWh"], "fact_id": "f_1"}]
    specs = [_spec(i, "panel", duration=4.0, section="结论建议",
                    overlay={"kind": "panel", "rows": [dict(rows[0])], "source": ""})
             for i in range(12)]
    out = explainer.promote_concepts(specs, p)
    assert all(s.layout != "concept" for s in out)


def test_promote_concepts_keeps_two_concepts_apart():
    p = load_profile("auto_long")
    rows = [{"label": "电池容量", "values": ["73kWh"], "fact_id": "f_1"}]
    specs = [_spec(i, "panel", duration=4.0, section="三电性能",
                    overlay={"kind": "panel", "rows": [dict(rows[0])], "source": ""})
             for i in range(12)]
    out = explainer.promote_concepts(specs, p)
    layouts = [s.layout for s in out]
    assert not any(a == b == "concept" for a, b in zip(layouts, layouts[1:]))


def test_promote_concepts_does_not_land_next_to_an_existing_concept():
    """模型自己选的 concept 在后一拍时,前一拍也不能被升级成 concept。"""
    p = load_profile("auto_long")
    row = {"label": "电池容量", "values": ["73kWh"], "fact_id": "f_1"}
    specs = []
    for i in range(15):
        if i == 1:
            ov = {"kind": "concept", "scene": "battery", "rows": [dict(row)]}
            specs.append(_spec(i, "concept", duration=4.0, section="三电性能", overlay=ov))
        else:
            ov = {"kind": "panel", "rows": [dict(row)], "source": ""}
            specs.append(_spec(i, "panel", duration=4.0, section="三电性能", overlay=ov))
    out = explainer.promote_concepts(specs, p)
    layouts = [s.layout for s in out]
    assert not any(a == b == "concept" for a, b in zip(layouts, layouts[1:]))
    assert out[0].layout == "panel"


def test_promote_concepts_does_nothing_for_categories_without_templates():
    p = load_profile("baby_long")
    rows = [{"label": "承重", "values": ["25kg"], "fact_id": "f_1"}]
    specs = [_spec(i, "panel", duration=4.0, section="规格怎么选",
                    overlay={"kind": "panel", "rows": [dict(rows[0])], "source": ""})
             for i in range(12)]
    out = explainer.promote_concepts(specs, p)
    assert all(s.layout != "concept" for s in out)


def test_concept_guide_asks_for_enough_concepts():
    p = load_profile("auto_long")
    guide = explainer.concept_guide(p.vocab, 30)
    assert "4–6 拍" in guide
    assert "优先用 concept" in guide


# --------------------------------------------------------------------------
# 每个数字标注口径与来源
# --------------------------------------------------------------------------
def _card(tag: str) -> dict:
    return {"kind": "spec", "title": "核心参数", "columns": [], "source": "数据来源:官方 1",
            "rows": [{"label": "CLTC续航", "values": ["643公里"], "fact_id": "f_1", "tag": tag}]}


def _accent_pixels(path) -> int:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        colors = im.getcolors(maxcolors=1 << 24) or []
        return sum(n for n, (r, g, b) in colors
                   if abs(r - 77) < 30 and abs(g - 163) < 30 and abs(b - 255) < 30)


def test_card_draws_the_basis_tag_next_to_the_parameter(tmp_path):
    """整屏数据卡以前只画底部来源行,行上的口径没画 —— 补上。"""
    from app.pipeline import cards

    plain = cards.render_frames(_card(""), "示例", tmp_path / "plain")[-1]
    tagged = cards.render_frames(_card("CLTC · 标准版 | 官方"), "示例", tmp_path / "tagged")[-1]
    assert plain.is_file() and tagged.is_file()
    # 标签用强调色画,带标签的那张强调色像素更多
    assert _accent_pixels(tagged) > _accent_pixels(plain)


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
                          broll_cap=3, concept_guide="", note_chars=14)
    assert "最多 3 拍" in system


def test_each_beat_sees_its_own_facts():
    p = load_profile("auto_long")
    seg = ScriptSegment(idx=0, section="三电性能", text="续航 643 公里。", fact_refs=["f_1"])
    f = Fact(id="f_1", claim="小米YU7标准版 CLTC 续航 643 公里", source_tool="t",
             confidence="high", scope="product")
    line = explainer._beat_line(0, seg, p, [f])
    assert "本拍事实" in line and "f_1" in line


def test_script_prompt_has_the_three_part_opening_and_the_ending():
    from app.pipeline import variety

    text = variety.rule10(None)
    assert "开头三段式" in text
    assert "先给冲突" in text
    assert "结尾要给**行动**" in text


def test_portrait_beats_are_shorter():
    seg = ScriptSegment(idx=0, section="三电性能",
                        text="电池容量是一百零一点七度,CLTC续航八百三十五公里,快充十五分钟能补六百二十公里。")
    wide = script_gen.split_segments([seg])
    tall = script_gen.split_segments([seg], script_gen.PORTRAIT_SHOOT_CHARS_MAX)
    assert all(len(s.text) <= script_gen.PORTRAIT_SHOOT_CHARS_MAX for s in tall)
    assert len(tall) >= len(wide)


def test_split_never_breaks_a_word_or_leaves_a_midsentence_period():
    """竖屏实跑的两处毛病:「激光雷 / 达」硬切劈词;「我的判断是。YU7……」并碎块后句号留在句中。"""
    from app.pipeline import script_gen

    parts = script_gen._split_text("全系标配700TOPS Thor芯片加激光雷达，HAD终身免费。", 22)
    assert parts == ["全系标配700TOPS Thor芯片加激光雷达。", "HAD终身免费。"]
    parts = script_gen._split_text("我的判断是，YU7不是一台车，是两个价位段的两种车。", 22)
    assert parts[0] == "我的判断是，YU7不是一台车。"
    # 真的太长才硬切,且优先切在空格上
    assert script_gen._hard_cut("a" * 15 + " " + "b" * 20, 22) == ["a" * 15, "b" * 20]


# --------------------------------------------------------------------------
# 观点拍配关键词大字:压低静默串
# --------------------------------------------------------------------------
class _KwLLM:
    """按提示词里每一拍的旁白关键词回话:{旁白里的词: 大字}。"""

    def __init__(self, by_marker):
        self.by_marker = by_marker
        self.user = ""

    def structured(self, system, user, **_):
        import re as _re
        self.user = user
        items = []
        for line in user.splitlines():
            m = _re.match(r"\[拍号 (\d+)\]", line)
            for marker, text in self.by_marker.items():
                if m and marker in line:
                    items.append({"idx": int(m.group(1)), "lines": [text]})
        return {"items": items}


def _opinion_run(profile, n=6):
    sec = next(s.name for s in profile.sections.values() if "highlight" in (s.layouts or ()))
    texts = ["代价是能量密度低,冬天掉电更明显一些。", "这套方案靠摄像头感知,不堆激光雷达。",
             "它的能力上限取决于算法迭代速度。", "买它等于买一个会持续更新的预期。",
             "系数低不代表总风阻就小一些。", "它是台SUV,车身不小,迎风面积大。"][:n]
    segs = [ScriptSegment(idx=i, section=sec, text=t) for i, t in enumerate(texts)]
    specs = [_spec(i, "broll", duration=0.0, section=sec) for i in range(n)]
    return specs, segs


def test_keyword_beats_break_up_a_quiet_run_without_echoing_narration():
    p = load_profile("auto_long")
    specs, segs = _opinion_run(p)
    llm = _KwLLM({"能量密度": "代价:能量密度",
                  "摄像头": "这套方案靠摄像头感知",          # 照抄旁白 → 不上屏
                  "预期": "买的是预期",
                  "迎风": "迎风 0.22"})                      # 旁白里没有的数字 → 不上屏
    out = explainer.add_keyword_beats(specs, segs, [], p, llm)
    kept = {s.idx: s.overlay["lines"] for s in out if s.layout == "highlight"}
    assert kept and all(i > 0 for i in kept)                     # 第一拍不动
    assert not any("摄像头感知" in "".join(v) for v in kept.values())
    assert not any("0.22" in "".join(v) for v in kept.values())
    # 连着的 broll 串被截短:没有任何一段连续 broll 超过 3 拍
    run = longest = 0
    for s in out:
        run = run + 1 if s.layout == "broll" else 0
        longest = max(longest, run)
    assert longest <= 3


def test_keyword_beats_skip_when_no_llm_or_nothing_quiet():
    p = load_profile("auto_long")
    specs, segs = _opinion_run(p)
    assert [s.layout for s in explainer.add_keyword_beats(specs, segs, [], p, None)] == ["broll"] * 6
    short, short_segs = _opinion_run(p, n=2)
    for seg in short_segs:
        seg.text = "好的。"                                # 两拍加起来也就几秒:不算静默
    llm = _KwLLM({"好的": "不该用到"})
    out = explainer.add_keyword_beats(short, short_segs, [], p, llm)
    assert all(s.layout == "broll" for s in out) and llm.user == ""
