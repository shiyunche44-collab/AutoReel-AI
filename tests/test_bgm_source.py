from __future__ import annotations

from types import SimpleNamespace

from app import config
from app.pipeline import narration
from app.providers import bgm_source


def test_own_bgm_prefers_category_dir_and_rotates(tmp_path):
    root = config.BGM_DIR
    (root / "auto_long").mkdir(parents=True)
    (root / "auto_long" / "a.mp3").write_bytes(b"x")
    (root / "b.mp3").write_bytes(b"x")
    profile = SimpleNamespace(name="auto_long", bgm_genres=())
    assert narration._own_bgm("p1", profile).name == "a.mp3"
    other = SimpleNamespace(name="baby_long", bgm_genres=())
    assert narration._own_bgm("p1", other).name == "b.mp3"


def test_own_bgm_auto_collects_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BGM_AUTO_COLLECT", True)
    seen = {}

    def fake_collect(genres, dest, **kw):
        seen["genres"] = tuple(genres)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "mixkit_1.mp3").write_bytes(b"x")
        return []

    monkeypatch.setattr(bgm_source, "collect", fake_collect)
    profile = SimpleNamespace(name="auto_long", bgm_genres=("tech",))
    assert narration._own_bgm("p1", profile).name == "mixkit_1.mp3"
    assert seen["genres"] == ("tech",)


def test_own_bgm_none_without_tracks_or_genres():
    assert narration._own_bgm("p1", SimpleNamespace(name="x", bgm_genres=())) is None


def test_seconds():
    assert bgm_source._seconds("2:24") == 144
    assert bgm_source._seconds("") == 0


def test_mood_of_prefers_ledger_then_curated_then_genre():
    assert bgm_source.mood_of("mixkit_871.mp3", {"genre": "cinematic", "mood": "热血"}) == "热血"
    assert bgm_source.mood_of("mixkit_1167.mp3", {"genre": "corporate-music"}) == "科技"  # 精选清单
    assert bgm_source.mood_of("mixkit_99999.mp3", {"genre": "ambient"}) == "舒缓"
    assert bgm_source.mood_of("x.mp3", {"genre": "mood/suspenseful"}) == "悬念"
    assert bgm_source.mood_of("x.mp3", {"genre": "polka"}) is None
    assert all(t["mood"] in bgm_source.MOODS for t in bgm_source.LIBRARY)
    assert 20 <= len({t["id"] for t in bgm_source.LIBRARY}) <= 30


def test_tag_moods_and_library_plan(tmp_path):
    import json

    root = tmp_path / "auto_long"
    (root / "cinema").mkdir(parents=True)
    (root / "mixkit_34.mp3").write_bytes(b"x")
    (root / "cinema" / "mixkit_871.mp3").write_bytes(b"x")
    (root / "_licenses.json").write_text(json.dumps({"mixkit_34.mp3": {"genre": "corporate-music"}}),
                                         encoding="utf-8")
    (root / "cinema" / "_licenses.json").write_text(
        json.dumps({"mixkit_871.mp3": {"genre": "cinematic", "bpm": 93}}), encoding="utf-8")
    assert bgm_source.tag_moods(root / "cinema") == 1
    meta = json.loads((root / "cinema" / "_licenses.json").read_text(encoding="utf-8"))
    assert meta["mixkit_871.mp3"] == {"genre": "cinematic", "bpm": 93, "mood": "悬念"}
    assert bgm_source.tag_moods(root / "cinema") == 0
    todo = {t["id"] for t in bgm_source.library_plan(root)}
    assert 34 not in todo and 871 not in todo and 51 in todo
