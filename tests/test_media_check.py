"""素材技术体检:分辨率 / 码率门槛、以及它们在铺背景时怎么生效。全部离线。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import config
from app.pipeline import library as lib_mod
from app.pipeline import media_check
from app.pipeline.library import Item, Library
from app.providers import clips as clips_mod


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _jpg(path: Path, size=(1920, 1080), color=(90, 120, 150)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def test_bitrate_floor_is_codec_aware():
    # H.264 1080p 1200k 算低;同码率的 AV1 不算(下限打七折 = 1050k)
    assert media_check.is_low_bitrate(1920, 1080, 1200, "h264")
    assert not media_check.is_low_bitrate(1920, 1080, 1200, "av1")
    # 720p 的下限按像素折算
    assert media_check.min_bitrate(1280, 720, "h264") == pytest.approx(1500 * 1280 * 720 / 1920 / 1080)
    # 参数不全不判
    assert not media_check.is_low_bitrate(0, 0, 100, "h264")
    assert not media_check.is_low_bitrate(1920, 1080, 0, "h264")


def test_low_res_video_is_not_usable_but_unknown_size_is():
    base = dict(kind="video", path="x.mp4", tagged=True, view="exterior_45", target="yes",
                start=0, end=3)
    assert not Item(id="a", width=480, height=852, **base).usable
    assert Item(id="b", width=1080, height=1920, **base).usable   # 竖屏 1080p 短边够
    assert Item(id="c", **base).usable                             # 老池子没回填:不拦


def test_old_library_json_loads_without_media_fields(data_dir):
    lib = Library(subject="小米YU7")
    lib.items = [Item(id="a", kind="video", path="x.mp4")]
    lib.save()
    raw = (lib.dir / "library.json").read_text(encoding="utf-8")
    for key in ('"width": 0,', '"height": 0,', '"bitrate": 0,'):
        raw = raw.replace(key, "")
    (lib.dir / "library.json").write_text(raw, encoding="utf-8")
    got = Library.load("小米YU7").items[0]
    assert got.width == 0 and not got.low_res


def test_fill_skips_small_stills(data_dir, tmp_path):
    big = _jpg(tmp_path / "big.jpg")
    small = _jpg(tmp_path / "small.jpg", (860, 484))
    lib = Library(subject="小米YU7")
    lib.items = [
        Item(id="big", kind="still", path=str(big), frame=str(big), view="exterior_45",
             target="yes", tagged=True, official=True, width=1920, height=1080),
        Item(id="small", kind="still", path=str(small), frame=str(small), view="exterior_45",
             target="yes", tagged=True, official=True, width=860, height=484),
    ]
    lib.save()
    pieces = lib_mod.fill("小米YU7", 12.0)
    assert pieces and {p.item_id for p in pieces} == {"big"}


def test_fill_prefers_higher_bitrate_within_tier(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(lib_mod, "near_dup_groups", lambda items, bits=0: {i.id: i.id for i in items})
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    common = dict(kind="video", path=str(video), frame="", tagged=True, view="exterior_45",
                  target="yes", official=True, start=1, end=4, width=1920, height=1080,
                  vcodec="h264")
    lib = Library(subject="小米YU7")
    # 低码率的那条更长(原排序里长的优先),码率够的仍应先选
    lib.items = [Item(id="low", bitrate=600, **{**common, "end": 5}),
                 Item(id="ok", bitrate=4000, **common)]
    lib.save()
    pieces = lib_mod.fill("小米YU7", 2.0)
    assert pieces[0].item_id == "ok"


@pytest.mark.skipif(not (config.FFPROBE_PATH and Path(config.FFPROBE_PATH).is_file()),
                    reason="没有 ffprobe")
def test_probe_video_reads_stream_params(tmp_path):
    out = tmp_path / "t.mp4"
    subprocess.run([clips_mod._ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=size=640x360:rate=25:duration=1", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(out)], check=True, timeout=60)
    info = media_check.probe_video(out)
    assert (info["width"], info["height"], info["vcodec"]) == (640, 360, "h264")
    assert info["fps"] == 25 and info["bitrate"] > 0
    assert media_check.is_low_res_video(info["width"], info["height"])
    assert media_check.probe_video(tmp_path / "nope.mp4") == {}


# --------------------------------------------------------------------------
# 本地三帧体检
# --------------------------------------------------------------------------
def test_frame_verdict():
    v = media_check.frame_verdict
    assert v([]) == ""
    assert v([(5, 900), (10, 900), (20, 900)]) == "frame_dark"
    assert v([(5, 900), (120, 900), (20, 900)]) == ""          # 只是一帧黑:淡入,不判
    assert v([(200, 10), (200, 50), (200, 900)]) == "frame_flat"
    assert v([(200, 600), (200, 50), (200, 900)]) == ""


def test_skipped_item_is_not_usable():
    it = Item(id="a", kind="video", path="x.mp4", tagged=True, view="exterior_45",
              target="yes", start=0, end=3, skipped="frame_dark")
    assert not it.usable


@pytest.mark.skipif(not (config.FFPROBE_PATH and Path(config.FFPROBE_PATH).is_file()),
                    reason="没有 ffmpeg")
def test_frame_stats_tells_black_from_content(tmp_path):
    black = tmp_path / "black.mp4"
    busy = tmp_path / "busy.mp4"
    for src, out in (("color=c=black:size=640x360:rate=25:duration=2", black),
                     ("testsrc=size=640x360:rate=25:duration=2", busy)):
        subprocess.run([clips_mod._ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", src,
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True, timeout=60)
    sb = media_check.frame_stats(black, 0.1, 1.9)
    assert len(sb) == media_check.HEALTH_FRAMES
    assert media_check.frame_verdict(sb) == "frame_dark"
    assert media_check.frame_verdict(media_check.frame_stats(busy, 0.1, 1.9)) == ""
