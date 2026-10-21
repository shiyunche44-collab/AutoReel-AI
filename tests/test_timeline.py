"""时间轴、字幕与 TTS 文本预处理测试(计划 §6.1)。"""
from __future__ import annotations

import pytest

from app.pipeline.timeline import (
    build_subtitle_cues,
    format_ass,
    narration_budget_chars,
    normalize_for_tts,
    number_to_chinese,
    shot_timeline,
    split_sentences,
    total_duration,
    wrap_subtitle,
)


# --------------------------------------------------------------------------
# 镜头时间轴:各时长的镜头数必须与 6 秒栅格吻合
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "duration,expected_shots",
    [(60, 10), (90, 15), (120, 20), (180, 30)],
)
def test_shot_count_matches_grid(duration, expected_shots):
    segments = shot_timeline([6.0] * expected_shots)
    assert len(segments) == expected_shots
    assert total_duration([6.0] * expected_shots) == float(duration)


def test_timeline_segments_are_contiguous_and_ordered():
    segments = shot_timeline([6.0] * 10)
    for prev, curr in zip(segments, segments[1:]):
        assert prev.end == curr.start
        assert curr.start > prev.start
    assert segments[0].start == 0.0
    assert segments[-1].end == 60.0


def test_timeline_final_end_equals_target_duration():
    for duration in (60, 90, 120, 180):
        n = duration // 6
        segments = shot_timeline([6.0] * n)
        assert segments[-1].end == float(duration)


# --------------------------------------------------------------------------
# 句子切分
# --------------------------------------------------------------------------
def test_split_sentences_breaks_on_chinese_punctuation():
    text = "这台车的续航是七百零二公里。百公里加速三点八秒。内饰做工不错。"
    result = split_sentences(text)
    assert len(result) == 3
    assert result[0].endswith("。")


def test_split_sentences_enforces_max_chars():
    long_sentence = "这台车的内饰用料非常扎实而且大面积使用了软性皮革包裹触感相当舒服" + "。" * 1
    result = split_sentences(long_sentence, max_chars=15)
    assert len(result) > 1
    assert all(len(s) <= 21 for s in result)


def test_split_sentences_handles_empty_input():
    assert split_sentences("") == []
    assert split_sentences("   ") == []


# --------------------------------------------------------------------------
# 中文数字:解说视频准确率的隐形分水岭
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "number,expected",
    [
        (0, "零"),
        (7, "七"),
        (10, "十"),
        (15, "十五"),
        (42, "四十二"),
        (100, "一百"),
        (702, "七百零二"),
        (1000, "一千"),
        (10000, "一万"),
    ],
)
def test_number_to_chinese(number, expected):
    assert number_to_chinese(number) == expected


def test_normalize_for_tts_converts_range_and_decimal():
    assert normalize_for_tts("续航702公里") == "续航七百零二公里"
    assert normalize_for_tts("零百加速3.8秒") == "零百加速三点八秒"


def test_normalize_for_tts_handles_units():
    assert "千瓦时" in normalize_for_tts("电池容量 75 kWh")
    assert "公里每小时" in normalize_for_tts("最高时速 180 km/h")


def test_normalize_for_tts_leaves_plain_text_untouched():
    assert normalize_for_tts("这是一段没有数字的文案") == "这是一段没有数字的文案"


# --------------------------------------------------------------------------
# 字幕
# --------------------------------------------------------------------------
def test_build_subtitle_cues_accumulates_actual_audio_durations():
    cues = build_subtitle_cues(
        ["第一句", "第二句", "第三句"], [2.0, 2.5, 1.8], gap=0.0
    )
    assert len(cues) == 3
    assert cues[0].start == 0.0
    assert cues[0].end == 2.0
    assert cues[1].start == 2.0
    assert cues[1].end == 4.5
    assert cues[2].start == 4.5
    assert cues[2].end == pytest.approx(6.3)


def test_subtitle_cues_are_monotonic_and_non_overlapping():
    cues = build_subtitle_cues(
        [f"第{i}句" for i in range(1, 8)],
        [1.5, 2.0, 1.2, 2.4, 1.8, 2.2, 1.6],
        gap=0.3,
    )
    for prev, curr in zip(cues, cues[1:]):
        assert curr.start >= prev.end
        assert curr.start > prev.start


def test_subtitle_cues_carry_shot_index():
    cues = build_subtitle_cues(
        ["a", "b", "c"], [1.0, 1.0, 1.0], shot_indices=[1, 1, 2], gap=0.0
    )
    assert [c.shot_idx for c in cues] == [1, 1, 2]


def test_narration_budget_scales_with_duration():
    assert narration_budget_chars(60) == 348
    assert narration_budget_chars(120) == 696
    assert narration_budget_chars(60) < narration_budget_chars(180)


# --------------------------------------------------------------------------
# ASS 字幕
# --------------------------------------------------------------------------
def test_wrap_subtitle_breaks_long_text():
    text = "这是一段非常长的字幕内容需要在合适的位置换行避免顶到画面边缘"
    wrapped = wrap_subtitle(text, max_chars=12)
    assert "\\N" in wrapped
    assert len(wrapped.replace("\\N", "")) == len(text)


def test_wrap_subtitle_keeps_short_text_on_one_line():
    assert wrap_subtitle("短字幕", max_chars=12) == "短字幕"


def test_format_ass_produces_playable_structure():
    cues = build_subtitle_cues(["第一句", "第二句"], [2.0, 2.0], gap=0.0)
    ass = format_ass(cues, width=1080, height=1920)
    assert "[Script Info]" in ass
    assert "[V4+ Styles]" in ass
    assert "[Events]" in ass
    assert "Microsoft YaHei" in ass
    assert ass.count("Dialogue: 0,") == 2
    assert "0:00:00.00" in ass
    assert "0:00:02.00" in ass


def test_format_ass_includes_playres_for_vertical_video():
    cues = build_subtitle_cues(["测试"], [1.0], gap=0.0)
    ass = format_ass(cues, width=1080, height=1920)
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass


def test_wrap_subtitle_breaks_at_fullwidth_comma_and_never_inside_numbers():
    from app.pipeline.timeline import wrap_subtitle
    out = wrap_subtitle("标准版CLTC续航643公里，15分钟补能405公里，折算每分钟约27公里", 22)
    assert r"\N405" in out  # 窗口里没有逗号:硬切退到数字开头
    out = wrap_subtitle("标准版续航很长，充电十五分钟能跑很远很远", 9)
    assert out.split(r"\N")[0].endswith("，")
    out = wrap_subtitle("续航六百公里十五分钟补能大约405公里折算", 15)
    assert r"4\N05" not in out and r"40\N5" not in out
