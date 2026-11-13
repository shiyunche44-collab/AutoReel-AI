"""虚拟主持人层:嘴型序列、隐藏区间、片尾不覆盖、位置避开按钮区、开场 / 收尾放大、表情。"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.pipeline import aspect, host

FPS = 24


def test_mouth_closed_outside_cues():
    rms = [1000.0] * 48
    states = host.mouth_track(rms, [{"start": 0.5, "end": 1.0}], FPS)
    assert all(s in ("closed", "blink") for s in states[:12])
    assert all(s in ("closed", "blink") for s in states[24:])
    assert "open" in states[12:24]


def test_mouth_follows_volume():
    rms = [0.0] * 10 + [300.0] * 10 + [1000.0] * 10
    states = host.mouth_track(rms, [{"start": 0, "end": 30 / FPS}], FPS)
    assert states[25] == "open"
    assert states[15] == "half"
    assert states[5] in ("closed", "blink")


def test_min_hold_removes_single_frame_flicker():
    rms = [1000.0] * 10 + [0.0] + [1000.0] * 10
    states = host.mouth_track(rms, [{"start": 0, "end": 21 / FPS}], FPS)
    assert states[10] == "open"


def test_blink_only_while_closed():
    n = FPS * 20
    states = host.mouth_track([0.0] * n, [], FPS, seed=3)
    assert "blink" in states
    assert set(states) <= {"closed", "blink"}


def test_hide_spans_cover_fullframe_beats():
    g = host.GUARD_FRAMES
    mask = host.hide_spans(["broll", "concept", "broll"], [1.0, 1.0, 1.0], FPS, 3 * FPS)
    assert not mask[:FPS - g].any()
    assert mask[FPS - g:2 * FPS + g].all()
    assert not mask[2 * FPS + g:].any()


def test_hide_spans_cover_dissolve_out_of_card():
    """数据卡下一拍从卡片最后一帧叠化进来:残影期间主持人也不能回来。"""
    g = host.GUARD_FRAMES
    mask = host.hide_spans(["broll", "stat", "broll"], [1.0, 1.0, 1.0], FPS, 3 * FPS,
                           dissolve_into=[2], dissolve_seconds=0.25)
    end = round(2.25 * FPS) + g
    assert mask[FPS:end].all()
    assert not mask[end:].any()


# 2026-11-13 审片(p_ce1d5a3b07d6):每拍镜头编码成整帧,画面比拍时长越走越靠后,
# 按拍时长算的隐藏段比数据卡早收约 0.2–0.4 秒,主持人压住卡片左侧的数字
DRIFT_BEATS = [1.422, 1.608, 1.596, 1.904, 3.295, 1.845, 2.252, 2.482, 2.211, 1.892, 3.501, 2.921]
DRIFT_CLIPS = [43, 49, 48, 58, 99, 56, 68, 75, 67, 57, 105, 88]  # 30fps 实测帧数
DRIFT_LAYOUTS = ["broll"] * 10 + ["stat", "broll"]


def _card_window(fps: int) -> tuple[int, int]:
    """数据卡在成片画面里实际占的帧区间(按镜头帧数累计)。"""
    a = sum(DRIFT_CLIPS[:10])
    return a, a + DRIFT_CLIPS[10]


def test_hide_spans_follow_picture_not_beat_durations():
    fps = 30
    n = int(round(sum(DRIFT_BEATS) * fps))
    a, b = _card_window(fps)
    picture = [f / fps for f in DRIFT_CLIPS]
    assert host.hide_spans(DRIFT_LAYOUTS, picture, fps, n)[a:b].all()
    # 旧算法(按拍时长)确实漏了卡片尾部:这条断言守住复现条件本身
    assert not host.hide_spans(DRIFT_LAYOUTS, DRIFT_BEATS, fps, n)[a:b].all()


def _hidden_frames(listing: Path, fps: int) -> np.ndarray:
    """把主持人 concat 清单展开成逐帧「是否隐藏」。"""
    flags: list[bool] = []
    cur = ""
    for line in listing.read_text(encoding="utf-8").splitlines():
        if line.startswith("file"):
            cur = line
        elif line.startswith("duration"):
            flags += ["hidden.png" in cur] * int(round(float(line.split()[1]) * fps))
    return np.array(flags)


@pytest.mark.skipif(not host.available(), reason="主持人素材未入库")
def test_build_hides_host_for_whole_card_on_screen(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", True)
    fps = 30
    wav = _wav(tmp_path / "n.wav", sum(DRIFT_BEATS))
    nar = {"narration_path": str(wav), "cues": [{"start": 0.0, "end": sum(DRIFT_BEATS) - 0.5}]}
    out = host.build(tmp_path, nar, config.PORTRAIT_SIZE, DRIFT_BEATS, DRIFT_LAYOUTS, fps,
                     clip_seconds=[f / fps for f in DRIFT_CLIPS])
    assert out is not None
    hidden = _hidden_frames(out[0], fps)
    a, b = _card_window(fps)
    assert hidden[a:b].all()
    assert not hidden[:a - host.GUARD_FRAMES - 1].any()


def test_build_falls_back_to_beat_durations_when_clips_short(tmp_path, monkeypatch):
    """量出来的镜头数不够(某拍探测失败)就按拍时长,不报错。"""
    seen = {}

    def fake_hide(layouts, durations, fps, n, *a):
        seen["d"] = list(durations)
        return np.zeros(n, bool)

    monkeypatch.setattr(config, "HOST_ENABLED", True)
    monkeypatch.setattr(host, "available", lambda *_: True)
    monkeypatch.setattr(host, "hide_spans", fake_hide)
    monkeypatch.setattr(host, "_Painter", lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
    host.build(tmp_path, {}, config.PORTRAIT_SIZE, [1.0, 2.0], ["broll", "stat"], FPS,
               clip_seconds=[1.1])
    assert seen["d"] == [1.0, 2.0]


def test_video_seconds_reads_frame_counts(monkeypatch, tmp_path):
    import json
    import subprocess

    frames = iter(["43", "", "105"])

    def fake_run(cmd, **_):
        nb = next(frames)
        body = {"streams": [{"nb_frames": nb, "duration": "1.9"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")

    monkeypatch.setattr(config, "FFPROBE_PATH", tmp_path / "ffprobe.exe")
    monkeypatch.setattr(subprocess, "run", fake_run)
    got = host.video_seconds([tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "c.mp4"], 30)
    assert got == pytest.approx([43 / 30, 1.9, 3.5])


def test_video_seconds_none_without_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "FFPROBE_PATH", None)
    assert host.video_seconds([tmp_path / "a.mp4"], 30) is None


def test_for_compose_passes_measured_clips_and_dissolves(monkeypatch, tmp_path):
    from app.pipeline import looks

    class _Beat:
        def __init__(self, d, lay):
            self.duration, self.layout = d, lay

    got = {}
    monkeypatch.setattr(looks, "current",
                        lambda: type("L", (), {"host": True, "accent": (1, 2, 3)})())
    monkeypatch.setattr(host, "video_seconds", lambda paths, fps: [0.5] * len(paths))
    monkeypatch.setattr(host, "build", lambda *a, **k: got.update(k) or None)
    beats = [_Beat(1.0, "broll"), _Beat(1.0, "stat")]
    clips = [tmp_path / "n0.mp4", tmp_path / "n1.mp4", tmp_path / "outro.mp4"]
    host.for_compose(tmp_path, {}, config.PORTRAIT_SIZE, beats, 30, clips=clips,
                     dissolve_into=[1])
    assert got["clip_seconds"] == [0.5, 0.5]  # 片尾卡不算
    assert got["dissolve_into"] == [1]


def test_portrait_placement_left_and_below_band():
    size = config.PORTRAIT_SIZE
    sh = host.sprite_height(size)
    x, y = host.placement(size, sh, int(sh * 0.75))
    g = aspect.portrait_grid(size)
    assert x + int(sh * 0.75) < size[0] * (1 - aspect.PORTRAIT_SAFE_RIGHT)
    assert y >= g.band.bottom
    assert y + sh <= g.data.bottom


def _wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


@pytest.mark.skipif(not host.available(), reason="主持人素材未入库")
def test_build_covers_only_main_beats(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", True)
    wav = _wav(tmp_path / "n.wav", 3.0)
    nar = {"narration_path": str(wav), "cues": [{"start": 0.0, "end": 2.0}]}
    out = host.build(tmp_path, nar, config.PORTRAIT_SIZE, [1.0, 1.0, 1.0],
                     ["broll", "concept", "broll"], FPS)
    assert out is not None
    listing, x, y = out
    text = listing.read_text(encoding="utf-8")
    total = sum(float(line.split()[1]) for line in text.splitlines()
                if line.startswith("duration"))
    assert total == pytest.approx(3.0, abs=0.05)
    assert "hidden.png" in text


def test_build_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", False)
    assert host.build(tmp_path, {}, config.PORTRAIT_SIZE, [1.0], ["broll"], FPS) is None


# --------------------------------------------------------------------------
# 开场拍 / 收尾拍 / 表情
# --------------------------------------------------------------------------
CUES = [{"start": 0.0, "end": 2.4, "text": "常跑高速烂路,别冲销量。"},
        {"start": 2.4, "end": 8.0, "text": "它居然卖了 2.3 万辆。"},
        {"start": 8.0, "end": 20.0, "text": "没想到排第 3。"},
        {"start": 20.0, "end": 24.0, "text": "市区通勤值得选。"},
        {"start": 24.0, "end": 27.0, "text": "下期把它和海豚放一起比。"},
        {"start": 27.0, "end": 30.0, "text": "关注柒哥,下期接着聊。"}]


def test_opening_span_follows_first_sentence():
    assert host.opening_span(CUES, ["broll"], 30.0) == pytest.approx(2.4)
    assert host.opening_span([{"start": 0, "end": 0.8}], ["broll"], 30.0) == host.OPEN_MIN
    assert host.opening_span([{"start": 0, "end": 9.0}], ["broll"], 30.0) == host.OPEN_MAX
    assert host.opening_span(CUES, ["card"], 30.0) == 0.0      # 第一拍是全屏卡:不做
    assert host.opening_span(CUES, ["broll"], 8.0) == 0.0      # 片子太短
    assert host.opening_span([], ["broll"], 30.0) == 0.0


def test_ending_span_covers_trailing_follow_lines():
    assert host.ending_span(CUES, 30.0) == pytest.approx(24.0)
    # 没有「下期 / 关注」:从最后一句起,至少露 2.5 秒
    plain = [{"start": 0, "end": 29.0, "text": "a"}, {"start": 29.0, "end": 30.0, "text": "b"}]
    assert host.ending_span(plain, 30.0) == pytest.approx(27.5)
    # 往回最多 END_MAX 秒
    long = [{"start": 10.0, "end": 30.0, "text": "下期见"}]
    assert host.ending_span(long, 30.0) == pytest.approx(30.0 - host.END_MAX)
    assert host.ending_span(CUES, 8.0) is None


def test_scale_track_big_at_both_ends():
    n = 30 * FPS
    sc = host.scale_track(n, FPS, 2.4, 24.0)
    assert sc[0] == host.BIG and sc[int(2.3 * FPS)] == host.BIG
    assert sc[int(2.4 * FPS) + host.TWEEN] == 1.0 and sc[n // 2] == 1.0
    assert sc[-1] == host.BIG
    assert all(1.0 <= v <= host.BIG for v in sc)
    assert set(host.scale_track(n, FPS, 0.0, None)) == {1.0}


def test_reaction_for_stance():
    assert host.reaction_for("这个价不值得买") == "shake"   # 否定优先于「值得」
    assert host.reaction_for("市区通勤值得选") == "thumbs"
    assert host.reaction_for("它居然卖了这么多") == "surprised"
    assert host.reaction_for("排第 3") == ""


def test_reaction_track_needs_assets_and_spacing():
    n = 30 * FPS
    assert host.reaction_track(CUES, FPS, n, [], 24.0) == {}
    got = host.reaction_track(CUES, FPS, n, ["surprised", "thumbs", "point"], 24.0)
    assert int(2.4 * FPS) not in got                  # 紧跟开场拍的那句不抢戏
    assert got[int(8.0 * FPS)] == "surprised"
    assert got[int(20.0 * FPS)] == "thumbs"
    assert got[int(24.0 * FPS)] == "point"            # 收尾拍开头指向关注
    assert int(27.5 * FPS) not in got
    # 两次表情太近:后一个不出
    near = [{"start": 4.0, "end": 5.0, "text": "居然"}, {"start": 6.0, "end": 7.0, "text": "值得"}]
    assert set(host.reaction_track(near, FPS, n, ["surprised", "thumbs"]).values()) == {"surprised"}


def _alpha_box(path):
    from PIL import Image

    return Image.open(path).split()[3].getbbox()


def _frames(listing: Path) -> list[tuple[Path, float]]:
    lines = listing.read_text(encoding="utf-8").splitlines()
    files = [Path(x[6:-1]) for x in lines if x.startswith("file")]
    return list(zip(files, [float(x.split()[1]) for x in lines if x.startswith("duration")],
                    strict=False))


def _at(listing: Path, t: float) -> Path:
    acc = 0.0
    for f, d in _frames(listing):
        if acc <= t < acc + d:
            return f
        acc += d
    raise AssertionError(t)


@pytest.mark.skipif(not host.available(), reason="主持人素材未入库")
def test_build_opening_and_ending_are_big(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOST_ENABLED", True)
    size = config.PORTRAIT_SIZE
    wav = _wav(tmp_path / "n.wav", 30.0)
    nar = {"narration_path": str(wav), "cues": CUES}
    listing, x, y = host.build(tmp_path, nar, size, [10.0, 10.0, 10.0], ["broll"] * 3, FPS,
                               name="柒哥", series="柒哥帮你挑")
    assert (x, y) == (0, 0)
    g = aspect.portrait_grid(size)
    small = _alpha_box(_at(listing, 12.0))
    big = _alpha_box(_at(listing, 1.0))
    end = _alpha_box(_at(listing, 29.0))
    assert small[1] >= g.band.bottom                       # 平时不挡画面带
    assert big[1] < g.band.bottom and big[3] == small[3]   # 开场往上长,脚不动
    assert (big[3] - big[1]) > 1.5 * (small[3] - small[1])
    assert end[2] > big[2] and end[2] <= g.right           # 收尾右边多了贴纸,不进按钮区


@pytest.mark.skipif(not host.available(), reason="主持人素材未入库")
def test_build_hook_opening_peeks_from_band(tmp_path, monkeypatch):
    """大字钩子开场:数据区被大字占着,半身落在画面带里。"""
    monkeypatch.setattr(config, "HOST_ENABLED", True)
    size = config.PORTRAIT_SIZE
    nar = {"narration_path": str(_wav(tmp_path / "n.wav", 30.0)), "cues": CUES}
    listing, _, _ = host.build(tmp_path, nar, size, [10.0, 10.0, 10.0],
                               ["hook", "broll", "broll"], FPS)
    g = aspect.portrait_grid(size)
    box = _alpha_box(_at(listing, 1.0))
    assert g.band.top < box[1] and box[3] <= g.band.bottom
    assert _alpha_box(_at(listing, 5.0))[1] >= g.band.bottom
