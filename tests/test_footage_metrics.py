"""素材指标:必备视角覆盖、门禁 A 的「先补素材」提示、已渲染背景构成。全部离线。"""
from __future__ import annotations

from app import config
from app.pipeline import explainer, library
from app.pipeline.library import Item


def _lib(tmp_path, monkeypatch, items):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    lib = library.Library(subject="测试车", category="auto_long")
    lib.items = [Item(kind="video", path=str(vid), tagged=True, **{"target": "yes", **kw})
                 for kw in items]
    return lib


def test_coverage_counts_confirmed_video_per_required_view(tmp_path, monkeypatch):
    lib = _lib(tmp_path, monkeypatch, [
        dict(id="f", view="exterior_front", start=0, end=10, official=True),
        dict(id="s", view="seat", start=10, end=15, official=True),
        dict(id="d", view="aerial", start=20, end=26, official=True),
        dict(id="u", view="rear", start=30, end=40, target="unknown"),   # 没认出、非官方:不算
        dict(id="h", view="exterior_side", start=40, end=50, official=True, desc="博主出镜讲解"),
    ])
    cov = library.coverage_seconds(lib)
    assert cov == {"正面": 10.0, "侧面": 0.0, "尾部": 0.0, "内饰": 5.0, "行驶": 6.0}
    st = lib.stats()
    assert st["missing_coverage"] == ["侧面", "尾部", "内饰", "行驶"]   # 不到 8 秒算缺


def test_footage_gaps_hint_on_seconds_or_missing_views():
    ok = {"confirmed_video_seconds": 420.0, "missing_coverage": ["侧面"]}
    assert library.footage_gaps(ok) == []
    thin = library.footage_gaps({"confirmed_video_seconds": 120.0, "missing_coverage": []})
    assert len(thin) == 1 and "120" in thin[0]
    views = library.footage_gaps({"confirmed_video_seconds": 500.0,
                                  "missing_coverage": ["正面", "侧面"]})
    assert views == ["缺正面、侧面的视频"]


def test_stats_report_resolution_and_logo_results(tmp_path, monkeypatch):
    lib = _lib(tmp_path, monkeypatch, [
        dict(id="hd", view="driving", start=0, end=5, width=1920, height=1080),
        dict(id="mid", view="driving", start=5, end=10, width=1280, height=720),
        dict(id="lo", view="driving", start=10, end=15, width=480, height=852),
        dict(id="drop", view="driving", start=15, end=20, skipped="logo_uncroppable"),
        dict(id="dup", view="driving", start=20, end=25, skipped="dup_official"),
    ])
    lib.sources = [library.Source(url="a", id="a", logo_checked=True, logo_boxes=[[0.8, 0, 1, 0.1]]),
                   library.Source(url="b", id="b", logo_checked=True)]
    st = lib.stats()
    assert st["video_res"] == {"1080p+": 1, "720p": 1, "低清": 1}
    assert (st["logo_checked"], st["logo_sources"]) == (2, 1)
    assert (st["logo_dropped"], st["dup_official"]) == (1, 1)


def test_footage_mix_counts_stills_third_party_and_edge_sections(monkeypatch):
    def spec(idx, chapter):
        return explainer.ShotSpec(idx=idx, duration=4.0, section=chapter, visual_zh="", narration_zh="",
                                  angle="exterior_45", layout="broll", chapter=chapter)

    specs = [spec(0, "开头"), spec(1, "外观"), spec(2, "结论")]
    beats = {
        "beats/000.json": [{"kind": "video", "length": 4.0}],
        "beats/001.json": [{"kind": "still", "length": 2.0},
                           {"kind": "video", "length": 2.0, "third_party": True}],
        "beats/002.json": [{"kind": "video", "length": 4.0, "third_party": True}],
    }
    monkeypatch.setattr(explainer.db, "read_json", lambda pid, name, default=None: beats.get(name, default))
    mix = explainer.footage_mix("p", specs)
    assert mix == {"seconds": 12.0, "still_ratio": round(2 / 12, 3),
                   "third_party_ratio": 0.5, "third_party_edge_seconds": 4.0}
    monkeypatch.setattr(explainer.db, "read_json", lambda pid, name, default=None: default)
    assert explainer.footage_mix("p", specs) == {}
