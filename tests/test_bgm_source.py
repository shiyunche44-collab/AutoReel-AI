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
