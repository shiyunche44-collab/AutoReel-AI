"""情绪曲线 A:强度的解析、整理、拆分 / 合并保留、兜底,以及从脚本带到分镜。"""
from __future__ import annotations

from app.categories import load_profile
from app.models import ScriptSegment, ShotSpec
from app.pipeline import intensity
from app.pipeline.explainer import planning_b
from app.pipeline.script_gen import segments as seg_mod


def _seg(i: int, v: int, text: str = "测试旁白。", section: str = "开场") -> ScriptSegment:
    return ScriptSegment(idx=i, section=section, text=text, intensity=v)


def test_clamp_accepts_only_1_to_3():
    assert [intensity.clamp(x) for x in (1, "2", 3.0, 0, 4, -1, "强", None)] == [1, 2, 3, 0, 0, 0, 0, 0]


def test_role_default_curve():
    assert intensity.role_default("hook") == intensity.role_default("ending") == 3
    assert intensity.role_default("body", "card") == 2
    assert intensity.role_default("body", "broll") == 1


def test_of_prefers_declared_then_falls_back():
    spec = ShotSpec(idx=1, visual_zh="x", layout="broll", intensity=3)
    assert intensity.of(spec, "body") == 3
    spec.intensity = 0
    assert intensity.of(spec, "body") == 1
    assert intensity.of(spec, "hook") == 3


def test_normalize_flat_curve_is_cleared_for_fallback():
    segs = [_seg(i, 3) for i in range(6)]
    intensity.normalize(segs)
    assert [s.intensity for s in segs] == [0] * 6


def test_normalize_caps_strong_beats_but_keeps_ends():
    segs = [_seg(i, 3) for i in range(8)]
    segs[3].intensity = 1  # 不是平的曲线
    intensity.normalize(segs)
    got = [s.intensity for s in segs]
    assert got[0] == 3 and got[-1] == 3
    assert got.count(3) <= 8 // 3 + 1
    assert set(got) <= {1, 2, 3}


def test_normalize_keeps_a_real_curve():
    segs = [_seg(0, 3), _seg(1, 1), _seg(2, 2), _seg(3, 1), _seg(4, 3)]
    intensity.normalize(segs)
    assert [s.intensity for s in segs] == [3, 1, 2, 1, 3]


def test_split_keeps_intensity():
    long = "这是一句比较长的话,需要被拆开。" * 8
    out = seg_mod.split_segments([_seg(0, 3, long)], max_chars=30)
    assert len(out) > 1 and {s.intensity for s in out} == {3}


def test_pad_copies_intensity_and_merge_takes_max():
    padded = seg_mod.normalize_segments([_seg(0, 2)], ["开场", "结论"])
    assert [s.intensity for s in padded] == [2, 2]
    merged = seg_mod.normalize_segments([_seg(0, 1), _seg(1, 3), _seg(2, 2)], ["开场", "结论"])
    assert merged[1].intensity == 3


def test_fallback_beats_carry_intensity_to_specs():
    profile = load_profile("auto_long")
    specs = planning_b.fallback_beats(subject="测试车", profile=profile,
                                      segments=[_seg(0, 3), _seg(1, 1)])
    assert [s.intensity for s in specs] == [3, 1]
