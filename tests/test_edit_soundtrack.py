"""剪辑 v2 的声音接线:配乐 / 音效开关跟着视觉包、换章 whip 吸拍、音效事件取自时间线、底床退路。"""
from __future__ import annotations

import pytest

from app import config
from app.pipeline import looks
from app.pipeline.edit import edl
from app.pipeline.edit import project as edit_project
from app.pipeline.edit.edl import Clip, Overlay, Timeline


def test_switches_follow_look_unless_env_forces(monkeypatch):
    monkeypatch.setattr(config, "BGM_SWITCH", None)
    monkeypatch.setattr(config, "SFX_SWITCH", None)
    assert config.bgm_on(True) and not config.bgm_on(False)
    assert config.sfx_on(True) and not config.sfx_on(False)
    monkeypatch.setattr(config, "BGM_SWITCH", False)
    monkeypatch.setattr(config, "SFX_SWITCH", True)
    assert not config.bgm_on(True) and config.sfx_on(False)


@pytest.mark.parametrize("raw, want", [("1", True), ("on", True), ("0", False), ("off", False),
                                       ("", None), ("  ", None)])
def test_switch_parsing(monkeypatch, raw, want):
    monkeypatch.setenv("X_SWITCH_TEST", raw)
    assert config._switch("X_SWITCH_TEST") is want


def test_commercial_look_wants_music_and_sfx():
    lk = looks.get("commercial")
    assert lk.bgm and lk.sfx and lk.bgm_moods
    assert not looks.get("lite").bgm  # 旧包不默认配乐


def _tl(*clips, overlays=()):
    tl = Timeline(width=1920, height=1080, fps=30, duration=sum(c.dur for c in clips))
    tl.clips = list(clips)
    tl.overlays = list(overlays)
    return tl


def test_hook_cut_is_the_cut_nearest_three_seconds():
    tl = _tl(Clip(0, 1.1, "video", "a"), Clip(1.1, 1.0, "video", "b"), Clip(2.1, 1.3, "video", "c"),
             Clip(3.4, 5.0, "video", "d"))
    assert edl.hook_cut(tl) == 3.4
    assert edl.hook_cut(_tl(Clip(0, 9.0, "video", "a"))) == edl.HOOK_SECONDS
    assert edl.hook_cut(_tl(Clip(0, 2.0, "video", "a"))) == 2.0  # 片子比钩子还短


def test_snap_whip_earlier_pulls_in_point_back():
    card = Overlay(5.0, 1.0, "chapter_card", {"n": 1})
    tl = _tl(Clip(0, 5.0, "video", "a"), Clip(5.0, 3.0, "video", "b", src_in=2.0, transition="whip"),
             overlays=[card, Overlay(6.0, 1.5, "headline")])
    assert edl.snap_whips(tl, [4.0, 4.9, 5.6], 0.15) == [(5.0, 4.9)]
    a, b = tl.clips
    assert a.dur == pytest.approx(4.9) and (b.start, b.dur, b.src_in) == (4.9, 3.1, 1.9)
    assert (card.start, card.dur) == (4.9, 1.1)
    assert tl.overlays[1].start == 6.0  # 不是和 whip 同时起的不挪


def test_snap_whip_skips_unsafe_moves():
    # 入点前没有素材可补
    tl = _tl(Clip(0, 5.0, "video", "a"), Clip(5.0, 3.0, "video", "b", src_in=0.05, transition="whip"))
    assert edl.snap_whips(tl, [4.9], 0.15) == []
    # 前一刀是整屏成品,不往后拉长
    tl = _tl(Clip(0, 5.0, "beat", "a"), Clip(5.0, 3.0, "video", "b", transition="whip"))
    assert edl.snap_whips(tl, [5.1], 0.15) == []
    # 静图往前补不需要入点
    tl = _tl(Clip(0, 5.0, "video", "a"), Clip(5.0, 3.0, "still", "b", transition="whip"))
    assert edl.snap_whips(tl, [4.9], 0.15) == [(5.0, 4.9)]
    # 拍点太远、普通硬切都不动
    tl = _tl(Clip(0, 5.0, "video", "a"), Clip(5.0, 3.0, "video", "b", src_in=1, transition="whip"),
             Clip(8.0, 2.0, "video", "c", src_in=1))
    assert edl.snap_whips(tl, [5.3, 8.1], 0.15) == []


def test_sfx_events_come_from_hook_whips_and_reveals(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SFX_SWITCH", None)
    got = {}

    def build(events, seconds, dest, *, seed="", root=None):
        got.update(events=events, seconds=seconds, seed=seed)
        return dest

    monkeypatch.setattr(edit_project.sfx, "build_track", build)
    tl = _tl(Clip(0, 3.0, "video", "a", transition="flash"), Clip(3.0, 17.0, "video", "b"),
             Clip(20.0, 30.0, "video", "c", transition="whip"),
             overlays=[Overlay(0, 3.5, "hook"), Overlay(5.0, 2.0, "headline"),
                       Overlay(30.0, 2.0, "headline"), Overlay(40.0, 3.0, "stat", {"at": 0.5})])
    project_dir = tmp_path / "p_sfx"
    with looks.use("commercial"):
        out = edit_project._sfx(project_dir, tl)
    assert out == project_dir / "_render" / "sfx.wav"
    # 5 秒的大字离开场太近(<4 秒),让给开场
    assert got["events"] == [(3.0, "open"), (20.0, "whip"), (30.1, "headline"), (41.3, "headline")]
    assert got["seconds"] == 50.0 and got["seed"] == "p_sfx"
    with looks.use("hud"):
        assert edit_project._sfx(project_dir, tl) is None  # 旧包没要音效


def test_bgm_bed_falls_back_to_narration_track(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BGM_SWITCH", None)
    old = tmp_path / "bgm.mp3"
    old.write_bytes(b"x")
    nar = {"bgm_path": str(old)}
    tl = _tl(Clip(0, 10.0, "video", "a"))
    with looks.use("commercial"):
        assert edit_project._bgm(tl, nar, tmp_path / "bed.wav") == old  # 没排上配乐
        tl.music = {"segments": [{"path": "x"}], "duration": 10.0}
        monkeypatch.setattr(edit_project.music, "render_bed", lambda plan, dest: dest)
        assert edit_project._bgm(tl, nar, tmp_path / "bed.wav") == tmp_path / "bed.wav"
        monkeypatch.setattr(edit_project.music, "render_bed", lambda plan, dest: None)
        assert edit_project._bgm(tl, nar, tmp_path / "bed.wav") == old  # 渲失败
    with looks.use("lite"):
        assert edit_project._bgm(tl, nar, tmp_path / "bed.wav") is None


def test_film_keeps_one_mood_with_enough_tracks():
    def t(mood, beats=True, pulse=None):
        return {"mood": mood, "beats": [0.5, 1.0] if beats else [], "pulse": pulse}

    lib = [t("科技"), t("科技"), t("科技", pulse=0.1), t("热血"), t("热血"), t("热血"), t("热血")]
    # 科技只有 2 首律动清楚的,不够撑一整片 → 退到热血
    assert edit_project.film_mood(lib, ("科技", "热血")) == "热血"
    lib.append(t("科技"))
    assert edit_project.film_mood(lib, ("科技", "热血")) == "科技"
    assert edit_project.film_mood([t("科技", beats=False)] * 5, ("科技",)) is None
    assert edit_project.film_mood(lib, ()) is None
