"""概念动画的数据层:数值解析、量程(数量图形只按真实数值画)、参数跟着旁白出现的时刻。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.categories import load_profile
from app.pipeline import motion

RANGES = load_profile("auto_long").vocab.concept_ranges


def _cue(text, start, end, shot_idx=0):
    return SimpleNamespace(text=text, start=start, end=end, shot_idx=shot_idx)


@pytest.mark.parametrize("text,want", [
    ("96.3kWh", (96.3, "kWh")),
    ("3.23 秒", (3.23, "秒")),
    ("835公里", (835.0, "km")),
    ("1,200 km", (1200.0, "km")),
    ("200米", (200.0, "米")),
    ("12min", (12.0, "分钟")),
    ("700TOPS", (700.0, "TOPS")),
    ("4D 毫米波雷达 1 颗", (1.0, "")),   # "4D" 是名字,不算数
    ("Thor-U", None),
    ("", None),
])
def test_parse_value(text, want):
    assert motion.parse_value(text) == want


def test_ranges_loaded_from_profile():
    assert "kWh" in RANGES and "零百|秒" in RANGES


def test_scale_row_ratio_in_range():
    got = motion.scale_row("电池容量", "96.3kWh", RANGES)
    assert got["num"] == 96.3 and got["unit"] == "kWh"
    assert 0.6 < got["ratio"] < 0.8          # 40–120


def test_scale_row_inverse_metric():
    """零百越小越好:3 秒比 8 秒的 ratio 大。"""
    fast = motion.scale_row("零百加速", "3.0秒", RANGES)["ratio"]
    slow = motion.scale_row("零百加速", "8.0秒", RANGES)["ratio"]
    assert fast > slow and fast == pytest.approx(0.875)


def test_scale_row_clamps():
    assert motion.scale_row("CLTC续航", "1500km", RANGES)["ratio"] == 1.0
    assert motion.scale_row("电池容量", "10kWh", RANGES)["ratio"] == 0.0


def test_keyword_range_beats_unit_only():
    """"轴距|mm" 比只按单位的量程优先;mm 没有通用量程,参数名对不上就不给 ratio。"""
    wb = motion.scale_row("轴距", "3000mm", RANGES)
    assert wb["ratio"] == pytest.approx(0.6)
    assert "ratio" not in motion.scale_row("离地间隙", "180mm", RANGES)


def test_unknown_unit_has_no_ratio():
    got = motion.scale_row("摄像头", "11个", RANGES)
    assert got == {"num": 11.0, "unit": ""}
    assert motion.scale_row("芯片", "Thor-U", RANGES) == {}


def test_bad_range_spec_ignored():
    assert "ratio" not in motion.scale_row("x", "5kWh", {"kWh": "abc"})
    assert "ratio" not in motion.scale_row("x", "5kWh", {"kWh": "5-5"})


def test_scene_data_scale_share_and_at():
    ov = {"title": "对比", "columns": ["A", "B"], "rows": [
        {"label": "CLTC续航", "values": ["835km", "719km"], "at": 1.5},
        {"label": "零百加速", "values": ["3.23秒", "4.3km"]},      # 单位不同,不给 share
        {"label": "空行", "values": []},
    ]}
    d = motion.scene_data(ov, "YU7", RANGES)
    assert len(d["rows"]) == 2
    r0, r1 = d["rows"]
    assert r0["at"] == 1.5 and r0["share"] == [1.0, round(719 / 835, 4)] and "ratio" in r0
    assert "share" not in r1 and "at" not in r1


def test_scene_data_without_ranges_has_no_ratio():
    d = motion.scene_data({"rows": [{"label": "电池容量", "values": ["96.3kWh"]}]}, "YU7")
    assert "ratio" not in d["rows"][0] and d["rows"][0]["num"] == 96.3


def test_row_reveal_times_follow_narration():
    cues = [_cue("这台车的电池容量是96.3度", 10.0, 12.0), _cue("CLTC续航835公里", 12.0, 14.0)]
    rows = [{"label": "电池容量", "values": ["96.3kWh"]}, {"label": "CLTC续航", "values": ["835km"]},
            {"label": "电压平台", "values": ["871V"]}]
    t = motion.row_reveal_times(rows, cues, 10.0, 8.0)
    assert t[0] == pytest.approx(9 / 14 * 2, abs=0.01)
    assert 2.0 < t[1] < 3.5
    assert t[2] is None                       # 旁白没说到


def test_row_reveal_times_label_fallback_and_clamp():
    cues = [_cue("零百加速只要三秒出头", 0.0, 2.0), _cue("最后说说最高车速", 6.5, 8.0)]
    rows = [{"label": "零百加速", "values": ["3.23秒"]}, {"label": "最高车速", "values": ["253km/h"]}]
    t = motion.row_reveal_times(rows, cues, 0.0, 8.0)
    assert t[0] == 0.3                         # 夹到入场之后
    assert t[1] == 6.8                         # 夹到退场之前(8 - 1.2)


def test_row_reveal_times_short_number_not_inside_other_numbers():
    """"1颗"不能撞上"11个"或"2025";一位数先找参数名。"""
    cues = [_cue("2025款配了11个摄像头", 0.0, 2.0), _cue("车顶有1颗激光雷达", 2.0, 4.0)]
    rows = [{"label": "激光雷达", "values": ["1颗"]}]
    t = motion.row_reveal_times(rows, cues, 0.0, 8.0)
    assert t[0] > 2.0
    cues2 = [_cue("2025款配了11个摄像头", 0.0, 2.0), _cue("只有1颗", 2.0, 4.0)]
    t2 = motion.row_reveal_times([{"label": "主雷达", "values": ["1颗"]}], cues2, 0.0, 8.0)
    assert t2[0] == pytest.approx(2.0 + 2 / 4 * 2, abs=0.01)
