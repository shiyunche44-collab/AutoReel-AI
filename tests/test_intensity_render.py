"""情绪曲线 B:换刀节奏、转场池、BGM 起伏滤镜,以及开关。"""
from __future__ import annotations

import subprocess

import pytest

from app import config
from app.models import ShotSpec
from app.pipeline import explainer, intensity, render
from app.pipeline.explainer import ffmpeg as ff


def test_pace_orders_calm_longer_than_strong():
    p = intensity.PIECE_PACE
    assert p[intensity.CALM] > p[intensity.MID] == 1.0 > p[intensity.STRONG]


def test_xfade_pool_calm_is_soft_strong_is_punchy():
    assert set(intensity.XFADE_POOL[intensity.CALM]) <= {"fade", "dissolve"}
    assert not set(intensity.XFADE_POOL[intensity.STRONG]) & {"fade", "dissolve"}
    assert set(intensity.XFADE_POOL[intensity.STRONG]) <= set(ff.XFADE_KINDS) | {"zoomin", "circleopen"}


def test_curve_string_merges_equal_neighbours():
    out = render.bgm_curve([2, 2, 3, 1], [1.25, 1.25, 1.0, 0.8])
    assert out.startswith(",volume='1") and out.endswith("':eval=frame")
    assert out.count("gte(") == 2  # 两段强 → 一段;中不出项;缓 → 一段
    assert "+0.250*gte(t,0.00)*lt(t,4.00)" in out
    assert "-0.200*gte(t,7.00)*lt(t,8.00)" in out


def test_curve_empty_when_flat_or_missing():
    assert render.bgm_curve([2, 2], None) == ""
    assert render.bgm_curve([2, 2], [1.0, 1.0]) == ""
    # levels 比 durations 短(片尾卡)按 1 算
    assert render.bgm_curve([2, 2, 3], [1.25, 1.25]).count("gte(") == 1


def test_graph_puts_curve_before_sidechain():
    graph, _ = render._build_audio_graph(True, True, 10.0, bgm_levels=",volume='1+0.250*gte(t,0.00)*lt(t,4.00)':eval=frame")
    assert graph.index("eval=frame") < graph.index("sidechaincompress")
    plain, _ = render._build_audio_graph(True, True, 10.0)
    assert "eval=frame" not in plain


def test_bgm_levels_switch_and_empty(monkeypatch):
    monkeypatch.setattr(config, "INTENSITY_CURVE", False)
    beats = [ShotSpec(idx=i, duration=4.0, visual_zh="x", layout="broll") for i in range(3)]
    assert explainer._bgm_levels(beats) is None
    monkeypatch.setattr(config, "INTENSITY_CURVE", True)
    assert explainer._bgm_levels([]) is None
    levels = explainer._bgm_levels(beats)  # 首拍 hook=强,其余按角色兜底
    assert levels is not None and levels[0] == intensity.BGM_GAIN[intensity.STRONG]


def _mean_db(ff_bin, path, a, b):
    r = subprocess.run([ff_bin, "-hide_banner", "-ss", str(a), "-t", str(b - a), "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    raise AssertionError(r.stderr[-300:])


def test_real_ffmpeg_curve_lifts_and_dips_bgm_only(tmp_path):
    ff_bin = str(config.FFMPEG_PATH)
    try:
        subprocess.run([ff_bin, "-version"], capture_output=True, check=True)
    except Exception:
        pytest.skip("没有 ffmpeg")
    bgm = tmp_path / "bgm.wav"
    subprocess.run([ff_bin, "-y", "-f", "lavfi", "-i", "sine=f=220:d=8", str(bgm)],
                   capture_output=True, check=True)
    nar = tmp_path / "nar.wav"  # 只在开头 1 秒出声,后面是纯 BGM 段,便于量两半的电平
    subprocess.run([ff_bin, "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", "-af", "apad=whole_dur=8",
                    str(nar)], capture_output=True, check=True)
    flat, e1 = render._mix_audio(nar, bgm, 8.0, tmp_path / "flat.wav")
    curved, e2 = render._mix_audio(nar, bgm, 8.0, tmp_path / "curve.wav",
                                   bgm_levels=render.bgm_curve([4, 4], [0.5, 1.25]))
    assert flat and curved, (e1, e2)
    gap_flat = _mean_db(ff_bin, flat, 2, 3.8) - _mean_db(ff_bin, flat, 5, 7)
    gap_curve = _mean_db(ff_bin, curved, 2, 3.8) - _mean_db(ff_bin, curved, 5, 7)
    # 平铺本身有淡入淡出的差;曲线让前半(缓 0.5)比后半(强 1.25)多低约 8 dB
    assert gap_curve - gap_flat < -6
