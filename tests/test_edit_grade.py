"""剪辑 v2 商业包装的调色:LUT 公式、.cube 文件、预裁套色和失败退原色。"""
from __future__ import annotations

import json
import subprocess
import types

import numpy as np
import pytest

from app import config
from app.pipeline.edit import edl, grade
from app.pipeline.edit import render as er

HAS_FFMPEG = bool(config.FFMPEG_PATH and config.FFMPEG_PATH.is_file())


def _gray(rows: np.ndarray, size: int, v: int) -> np.ndarray:
    """灰轴上第 v 格的输出(R、G、B 下标都是 v)。"""
    return rows[v + v * size + v * size * size]


def test_identity_grade_is_identity_and_red_runs_fastest():
    rows = grade.table(grade.Grade(), 5)
    assert rows.shape == (125, 3)
    assert rows[1].tolist() == [0.25, 0.0, 0.0]  # .cube:R 变得最快
    assert rows[5].tolist() == [0.0, 0.25, 0.0]
    assert rows[25].tolist() == [0.0, 0.0, 0.25]
    v = np.linspace(0, 1, 5)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    assert np.allclose(rows, np.stack([r, g, b], -1).reshape(-1, 3))


def test_commercial_grade_is_gentle_teal_orange():
    size = 17
    rows = grade.table(grade.GRADES["commercial"], size)
    gray = np.array([_gray(rows, size, i) for i in range(size)])
    luma = gray @ [0.2126, 0.7152, 0.0722]
    assert np.all(np.diff(luma) > 0)  # 灰阶单调,不翻转
    # 纯黑纯白不带色,黑位微抬、白位微压
    assert np.allclose(gray[0], gray[0][0]) and 0 < gray[0][0] < 0.03
    assert np.allclose(gray[-1], gray[-1][0]) and 0.97 < gray[-1][0] < 1
    q1, q3 = gray[size // 4], gray[3 * size // 4]
    assert q1[2] > q1[0]  # 暗部偏青
    assert q3[0] > q3[2]  # 亮部偏橙
    assert np.abs(gray - np.linspace(0, 1, size)[:, None]).max() < 0.06  # 幅度小
    assert q1.mean() < 0.25 < 0.75 < q3.mean()  # 轻 S 曲线:暗的更暗、亮的更亮


def test_lut_file_is_cached_under_data_dir():
    assert grade.lut_file("nope") is None
    path = grade.lut_file("commercial")
    assert path is not None and path.parent == config.DATA_DIR / "_cache" / "luts"
    lines = path.read_text(encoding="ascii").splitlines()
    assert f"LUT_3D_SIZE {grade.LUT_SIZE}" in lines[:4]
    assert len(lines) == 4 + grade.LUT_SIZE ** 3
    mtime = path.stat().st_mtime_ns
    assert grade.lut_file("commercial") == path and path.stat().st_mtime_ns == mtime


def test_filter_chain():
    video = grade.filter_chain("x.cube", 1920, 1080, 30)
    assert video.startswith("scale=") and "fps=30" in video
    assert "lut3d=file=x.cube" in video and video.endswith("format=yuv420p")
    assert "out_color_matrix=bt709" in video
    assert grade.filter_chain("x.cube", 1920, 1080, 30, tagged=False).startswith("setparams=")
    still = grade.filter_chain("x.cube", 1920, 1080, None)
    assert "fps=" not in still and still.endswith("format=rgb24")


def _timeline(tmp_path, grade_name="commercial"):
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"v")
    img = tmp_path / "b.jpg"
    img.write_bytes(b"i")
    card = tmp_path / "c.mp4"
    card.write_bytes(b"c")
    tl = edl.Timeline(width=1920, height=1080, fps=30, duration=9.0,
                      theme={"style": "commercial", "grade": grade_name} if grade_name else {})
    tl.clips = [edl.Clip(0.0, 3.0, "video", str(vid), src_in=12.5),
                edl.Clip(3.0, 3.0, "still", str(img)),
                edl.Clip(6.0, 3.0, "beat", str(card))]
    return tl


def _fake_run(calls, fail=()):
    def run(args, *, timeout=0, cwd=None):
        calls.append((list(args), cwd))
        out = args[-1]
        if not any(f in " ".join(args) for f in fail):
            with open(out, "wb") as fh:
                fh.write(b"graded")
        return types.SimpleNamespace(returncode=0, stderr="")
    return run


def test_stage_media_swaps_in_graded_cuts(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(grade.render_mod, "_run", _fake_run(calls))
    monkeypatch.setattr(grade, "_tagged", lambda src: True)
    monkeypatch.delenv("EDIT_V2_FONT_FILE", raising=False)
    tl = _timeline(tmp_path)
    props = er.stage_media(tl, tmp_path / "pub")
    v, s, b = props["clips"]
    assert v["src"].startswith("g_") and v["src"].endswith(".mp4") and v["src_in"] == 0.0
    assert s["src"].startswith("g_") and s["src"].endswith(".jpg")
    assert not b["src"].startswith("g_")  # 整屏成品不调色
    assert len(calls) == 2
    args, cwd = calls[0]
    assert cwd == grade.lut_file("commercial").parent
    assert args[args.index("-ss") + 1] == "12.500"  # 按入点预裁
    assert float(args[args.index("-t") + 1]) == pytest.approx(3.0 + grade.TAIL)
    assert str(tmp_path / "a.mp4") in args  # 原始素材的绝对路径(ffmpeg 不在项目目录里跑)
    # 第二次走缓存,不再调 ffmpeg(素材的 mtime / 大小进缓存键,换了素材会重做)
    er.stage_media(tl, tmp_path / "pub")
    assert len(calls) == 2
    # 合成完清掉中间文件
    assert grade.prune(tmp_path / "pub") == 2
    assert not list((tmp_path / "pub").glob("g_*"))


def test_no_grade_leaves_media_alone(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(grade.render_mod, "_run", _fake_run(calls))
    props = er.stage_media(_timeline(tmp_path, grade_name=""), tmp_path / "pub")
    assert calls == [] and props["clips"][0]["src_in"] == 12.5


def test_failed_cut_keeps_original_colours_and_notes(tmp_path, monkeypatch):
    calls: list = []
    notes: list = []
    monkeypatch.setattr(grade.render_mod, "_run", _fake_run(calls, fail=("-ss",)))
    monkeypatch.setattr(grade, "_tagged", lambda src: True)
    monkeypatch.setattr(grade.degrade, "note", lambda kind, detail="", **kw: notes.append((kind, detail)))
    props = er.stage_media(_timeline(tmp_path), tmp_path / "pub")
    v, s, _ = props["clips"]
    assert not v["src"].startswith("g_") and v["src_in"] == 12.5  # 失败的刀用原色、原入点
    assert s["src"].startswith("g_")
    assert notes and notes[0][0] == "edit_v2.grade" and "1/2" in notes[0][1]
    assert not list((tmp_path / "pub").glob("*.part*"))


def _probe(path):
    out = subprocess.run([str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "v:0",
                          "-show_entries", "stream=width,height,r_frame_rate,color_space,pix_fmt"
                          ":format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True).stdout
    data = json.loads(out)
    return data["streams"][0], float(data["format"]["duration"])


@pytest.mark.skipif(not HAS_FFMPEG, reason="没有 ffmpeg")
def test_real_ffmpeg_cut_and_grade(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run([str(config.FFMPEG_PATH), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=25:duration=3", "-pix_fmt", "yuv420p", str(src)],
                   check=True)
    clip = {"kind": "video", "src_in": 0.5, "dur": 1.0}
    lut = grade.lut_file("commercial")
    (tmp_path / "pub").mkdir()
    name = grade._one(src, clip, lut, tmp_path / "pub", 1920, 1080, 30)
    assert name and name.startswith("g_")
    stream, dur = _probe(tmp_path / "pub" / name)
    assert (stream["width"], stream["height"]) == (1280, 720)  # 只缩不放
    assert stream["r_frame_rate"] == "30/1" and stream["pix_fmt"] == "yuv420p"
    assert stream.get("color_space") == "bt709"
    assert dur == pytest.approx(1.0 + grade.TAIL, abs=0.07)
