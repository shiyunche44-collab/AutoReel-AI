"""音效:三处出声、至少隔 4 秒、按包络峰对齐、采样包台账、缺包降级。"""
from __future__ import annotations

import json
import wave

import numpy as np
import pytest

from app import config
from app.pipeline import sfx

SR = sfx.SR


def _wav(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


def _read(path):
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float) / 32768


def _burst(seconds, peak_at, width=0.03):
    """一段只有 `peak_at` 附近有声的采样(包络峰位置已知)。"""
    t = np.arange(int(seconds * SR)) / SR
    return np.sin(2 * np.pi * 800 * t) * np.exp(-((t - peak_at) / width) ** 2) * 0.9


def _pack(root, files):
    ledger = {}
    for name, (kind, data, lic) in files.items():
        _wav(root / name, data)
        ledger[name] = {"kind": kind, "license": lic, "source": "https://freesound.org/x"}
    (root / "_licenses.json").write_text(json.dumps(ledger), encoding="utf-8")


def test_only_three_places_and_min_gap():
    ev = sfx.cue_events(open_at=2.5, whips=[0.0, 5.0, 30.0, 33.0],
                        headlines=[9.0, 12.0, 31.0, 50.0])
    assert ev == [(2.5, "open"), (9.0, "headline"), (30.0, "whip"), (50.0, "headline")]
    times = [t for t, _ in ev]
    assert all(b - a >= sfx.MIN_GAP for a, b in zip(times, times[1:], strict=False))


def test_priority_open_over_whip_over_headline():
    assert sfx.cue_events(open_at=3.0, whips=[4.0], headlines=[5.0]) == [(3.0, "open")]
    assert sfx.cue_events(whips=[10.0], headlines=[8.0]) == [(10.0, "whip")]
    assert sfx.cue_events(open_at=0.0) == []  # 没有首刀


def test_pack_only_lists_cc0_files_on_ledger(tmp_path):
    _pack(tmp_path, {"whoosh_1.wav": ("whoosh", _burst(1.0, 0.5), "CC0 1.0"),
                     "hit_2.wav": ("hit", _burst(1.0, 0.1), "CC BY 4.0")})
    (tmp_path / "stray.wav").write_bytes(b"x")
    assert sfx.pack(tmp_path) == {"whoosh": [tmp_path / "whoosh_1.wav"]}
    assert sfx.pack(tmp_path / "missing") == {}


def test_samples_anchor_at_envelope_peak(tmp_path):
    root = tmp_path / "pack"
    _pack(root, {"whoosh_1.wav": ("whoosh", _burst(1.5, 0.9), "CC0 1.0"),
                 "hit_1.wav": ("hit", _burst(1.0, 0.05), "CC0 1.0"),
                 "riser_1.wav": ("riser", _burst(3.0, 2.8), "CC0 1.0")})
    out = sfx.build_track([(1.5, "open"), (6.0, "whip"), (12.0, "headline")], 15.0,
                          tmp_path / "sfx.wav", root=root)
    y = _read(out)
    assert len(y) == 15 * SR
    env = np.abs(y)
    for t in (1.5, 6.0, 12.0):  # 每个事件的最响处就在画面那一下
        lo, hi = int((t - 1.0) * SR), int((t + 1.0) * SR)
        assert abs(lo + int(np.argmax(env[lo:hi])) - t * SR) < 0.01 * SR
    # 电平:锚点前后 BODY 秒的 RMS 按 LEVEL_DB(不按峰值)
    body = y[int((6.0 - sfx.BODY) * SR):int((6.0 + sfx.BODY) * SR)]
    assert 20 * np.log10(np.sqrt(np.mean(body ** 2))) == pytest.approx(
        sfx.LEVEL_DB["whoosh"], abs=0.3)


def test_level_by_body_rms_with_peak_cap():
    short = _burst(1.0, 0.5, width=0.01)  # 一个尖峰:按 RMS 要拉很高,被峰值上限拦住
    long_ = _burst(1.0, 0.5, width=0.2)
    a = sfx.anchor(short)
    s, lg = sfx._level(short, a, "hit"), sfx._level(long_, sfx.anchor(long_), "hit")
    assert np.abs(s).max() <= sfx.PEAK_CAP + 1e-9
    half = int(sfx.BODY * SR)
    rms = 20 * np.log10(np.sqrt(np.mean(lg[a - half:a + half] ** 2)))
    assert rms == pytest.approx(sfx.LEVEL_DB["hit"], abs=0.3)


def test_riser_trimmed_when_first_cut_is_early(tmp_path):
    root = tmp_path / "pack"
    _pack(root, {"riser_1.wav": ("riser", _burst(3.0, 2.8), "CC0 1.0"),
                 "hit_1.wav": ("hit", _burst(1.0, 0.05), "CC0 1.0")})
    y = _read(sfx.build_track([(1.0, "open")], 5.0, tmp_path / "a.wav", root=root))
    assert abs(int(np.argmax(np.abs(y))) - SR) < 0.01 * SR  # riser 砍头,顶点照样落在 1.0 秒
    y = _read(sfx.build_track([(0.5, "open")], 5.0, tmp_path / "b.wav", root=root))
    assert abs(int(np.argmax(np.abs(y))) - SR // 2) < 0.01 * SR  # 太早放不下 riser → hit


def test_variants_rotate(tmp_path):
    root = tmp_path / "pack"
    _pack(root, {"whoosh_1.wav": ("whoosh", _burst(1.0, 0.5) * 0.5, "CC0 1.0"),
                 "whoosh_2.wav": ("whoosh", _burst(1.0, 0.5, width=0.1), "CC0 1.0")})
    y = _read(sfx.build_track([(2.0, "whip"), (8.0, "whip")], 10.0, tmp_path / "a.wav", root=root))
    a, b = y[int(1.5 * SR):int(2.5 * SR)], y[int(7.5 * SR):int(8.5 * SR)]
    assert np.abs(a).sum() != pytest.approx(np.abs(b).sum(), rel=0.05)  # 两次用的不是同一个


def test_no_pack_degrades_to_synth_whip_only(tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(sfx.degrade, "note", lambda kind, detail="", **kw: notes.append(kind))
    empty = tmp_path / "none"
    out = sfx.build_track([(2.0, "whip"), (8.0, "headline")], 10.0, tmp_path / "a.wav", root=empty)
    y = _read(out)
    assert np.abs(y[int(1.0 * SR):int(3.0 * SR)]).max() > 0.05
    assert np.abs(y[int(7.0 * SR):]).max() == 0  # 大字没采样就不出声
    assert notes == ["sfx.no_pack"]
    assert sfx.build_track([(8.0, "headline")], 10.0, tmp_path / "b.wav", root=empty) is None


def test_default_pack_dir_follows_data_dir():
    assert sfx.pack_dir() == config.DATA_DIR / "sfx" / "pack"


def test_manifest_is_cc0_freesound_and_covers_kinds():
    kinds = {m["kind"] for m in sfx.PACK_MANIFEST}
    assert kinds == set(sfx.LEVEL_DB)
    assert all(m["url"].startswith("https://cdn.freesound.org/previews/") for m in sfx.PACK_MANIFEST)


@pytest.mark.skipif(not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()), reason="没有 ffmpeg")
def test_fetch_pack_converts_and_writes_ledger(tmp_path):
    src = tmp_path / "src.wav"
    with wave.open(str(src), "wb") as w:  # 44.1k 立体声,看转码
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        tone = (np.sin(np.arange(44100) / 44100 * 2 * np.pi * 440) * 0.2 * 32767).astype("<i2")
        w.writeframes(np.repeat(tone, 2).tobytes())

    class Resp:
        content = src.read_bytes()

        def raise_for_status(self):
            return None

    class Client:
        def get(self, url):
            return Resp()

    item = {"kind": "hit", "id": 1, "title": "t", "author": "a", "url": "https://x/1.mp3"}
    dest = tmp_path / "pack"
    res = sfx.fetch_pack(dest, manifest=[item], client=Client())
    assert res["done"] == ["hit_1.wav"]
    with wave.open(str(dest / "hit_1.wav"), "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
    assert np.abs(_read(dest / "hit_1.wav")).max() == pytest.approx(sfx.PEAK, abs=0.01)
    ledger = json.loads((dest / "_licenses.json").read_text(encoding="utf-8"))
    assert ledger["hit_1.wav"]["license"] == "CC0 1.0"
    assert sfx.pack(dest) == {"hit": [dest / "hit_1.wav"]}
    assert sfx.fetch_pack(dest, manifest=[item], client=Client())["skipped"] == ["hit_1.wav"]
