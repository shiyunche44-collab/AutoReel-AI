"""画面时间轴 = 拍时长之和:每拍按累计帧边界分帧,拼接后不再越走越晚。"""
from __future__ import annotations

import math

from app.pipeline import render, timeline
from tests.test_motion_visuals import needs_ffmpeg

FPS = render.FPS


def test_frame_counts_follow_cumulative_edges():
    durations = [1.422, 1.633, 1.6, 1.92, 3.285, 1.856] * 6
    got = render.frame_counts(durations)
    assert sum(got) == round(sum(durations) * FPS)
    cum, edge = 0.0, 0
    for d, n in zip(durations, got, strict=True):
        cum += d
        edge += n
        # 任一拍结束时,画面和拍时长累计差不过半帧
        assert abs(edge / FPS - cum) <= 0.5 / FPS + 1e-9


def test_per_beat_rounding_would_drift():
    """反例:p_ce1d5a3b07d6 实测每拍编码成向上取整的帧数(1.422 秒 → 43 帧),
    36 拍(约 70 秒)画面就晚 9 帧 = 0.3 秒。"""
    durations = [1.422, 1.633, 1.6, 1.92, 3.285, 1.856] * 6
    naive = sum(math.ceil(round(d * FPS, 6)) for d in durations)
    assert (naive, sum(render.frame_counts(durations))) == (2118, 2109)


def test_frame_counts_keep_every_beat_on_screen():
    got = render.frame_counts([0.01, 0.01, 2.0])
    assert all(n >= 1 for n in got)
    assert sum(got) == round(2.02 * FPS)
    assert render.frame_counts([]) == []


def _clip(path, seconds: float, color: str):
    proc = render._run(["-y", "-v", "error", "-f", "lavfi",
                        "-i", f"color={color}:s=320x180:r={FPS}:d={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", f"{seconds:.3f}", "-an",
                        str(path)])
    assert proc.returncode == 0, proc.stderr
    return path


@needs_ffmpeg
def test_concat_total_frames_match_durations(tmp_path):
    """单独拼几拍:每拍整帧编码后多出来的零头不再累加,拼接总帧数 = round(拍时长之和 × fps)。"""
    durations = [1.422, 1.633, 0.95, 2.017, 1.111]
    colors = ["red", "blue", "green", "white", "black"]
    shots = [_clip(tmp_path / f"s{i}.mp4", d, c)
             for i, (d, c) in enumerate(zip(durations, colors, strict=True))]
    work = tmp_path / "_render"
    size = (320, 180)
    clips, warnings = render.normalise_all(shots, work, size=size, trim=None, durations=durations)
    assert not warnings and len(clips) == len(shots)
    assert [render.video_frames(c) for c in clips] == render.frame_counts(durations)
    clips = render._dissolve_heads(clips, [2], work, size=size)
    listing = timeline.concat_list_file(clips, work / "concat.txt")
    out = tmp_path / "joined.mp4"
    proc = render._run(["-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                        "-c", "copy", str(out)])
    assert proc.returncode == 0, proc.stderr
    assert render.video_frames(out) == round(sum(durations) * FPS)


@needs_ffmpeg
def test_stale_cache_with_wrong_frame_count_is_redone(tmp_path):
    shot = _clip(tmp_path / "s.mp4", 1.422, "red")
    work = tmp_path / "_render"
    work.mkdir()
    # 第 2 拍的旧缓存:按旧逻辑整帧编码出来的 43 帧,按累计边界它该是 42 帧
    stale = work / "norm_001.mp4"
    stale.write_bytes(shot.read_bytes())
    assert render.video_frames(stale) == 43
    clips, _ = render.normalise_all([shot, shot], work, size=(320, 180), trim=None,
                                    durations=[1.422, 1.422])
    assert render.frame_counts([1.422, 1.422]) == [43, 42]
    assert [render.video_frames(c) for c in clips] == [43, 42]
