"""剪辑 v2 取材只做一次:进门禁 B 前定好的时间线,之后的预览 / 成片按记录回放。

覆盖:第二次 plan / compose 不碰素材池;排除清单、重拍、换一刀只重取相关的刀;
试片另存一份;素材记录(`footage.beat_pieces`)跟着算数的时间线走。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config, db
from app.models import ShotSpec
from app.pipeline import explainer, library, outro
from app.pipeline.edit import edl
from app.pipeline.edit import project as edit_project
from app.pipeline.edit import render as edit_render
from app.pipeline.explainer import footage
from app.pipeline.library import select

PID = "p_edit_plan"
POOL = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9", "m10", "m11", "m12"]


def _spec(idx, dur, layout="broll", chapter="开场", text=""):
    return ShotSpec(idx=idx, duration=dur, visual_zh="x", layout=layout, chapter=chapter,
                    narration_zh=text)


def _cue(i, start, end, text, shot):
    return {"index": i, "start": start, "end": end, "text": text, "shot_idx": shot}


SPECS = [_spec(0, 3.0, layout="hook", text="它两秒破百"),
         _spec(1, 6.0, chapter="外观", text="前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式")]
CUES = [_cue(0, 0.0, 3.0, "它两秒破百", 0),
        _cue(1, 3.0, 9.0, "前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式", 1)]


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """假素材池:按「没被排除、用得少、id 小」挑一段;记下每次调用看到的排除集合和已用计数。"""
    media = tmp_path / "media"
    media.mkdir()
    for iid in POOL:
        (media / f"{iid}.mp4").write_bytes(b"x")
    calls: list[dict] = []

    def fill(car, seconds, *, hint=None, used=None, recent=None, detail="", role="",
             views=None, choose=None):
        banned = select._BANNED.get()
        calls.append({"car": car, "seconds": seconds, "detail": detail, "banned": set(banned),
                      "used": dict(used or {})})
        free = [i for i in POOL if i not in banned and f"https://v/{i}" not in banned]
        free.sort(key=lambda i: ((used or {}).get(i, 0), POOL.index(i)))
        iid = free[0]
        if used is not None:
            used[iid] = used.get(iid, 0) + 1
        return [SimpleNamespace(item_id=iid, kind="video", path=str(media / f"{iid}.mp4"),
                                start=1.0, length=30.0, source_url=f"https://v/{iid}",
                                platform="bilibili", license_status="unknown")]

    monkeypatch.setattr(library, "fill", fill)
    monkeypatch.setattr(outro, "build", lambda *a, **k: None)
    monkeypatch.setattr(explainer, "cross_project_seed", lambda pid, car: {})
    monkeypatch.setattr(config, "SFX_SWITCH", False)
    monkeypatch.setattr(config, "BGM_SWITCH", False)
    beat = config.project_dir(PID) / "beats" / "draft_001.mp4"
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_bytes(b"x")
    pool_ns = SimpleNamespace(calls=calls, beat=beat)
    return pool_ns


def _plan(pool, **kw):
    rows = {s.idx: {"draft_path": str(pool.beat)} for s in SPECS}
    return edit_project.plan(PID, SPECS, rows, {"cues": CUES}, size=(1920, 1080), title="t",
                             subject="车A", **kw)


def _ids(tl):
    return {c.key: c.item_id for c in tl.clips if c.key}


def test_second_plan_replays_without_touching_pool(pool):
    first = _plan(pool)
    n = len(pool.calls)
    assert n >= 2 and len(_ids(first)) == n
    meta = edit_project.load(PID)["plan"]
    assert meta["fresh"] == n and meta["replayed"] == 0

    pool.calls.clear()
    again = _plan(pool)
    assert pool.calls == []
    assert [(c.key, c.src, c.start, c.dur) for c in again.clips] == \
        [(c.key, c.src, c.start, c.dur) for c in first.clips]
    assert edit_project.load(PID)["plan"]["replayed"] == n


def test_fresh_plan_repicks_everything(pool):
    _plan(pool)
    n = len(pool.calls)
    pool.calls.clear()
    _plan(pool, fresh=True)
    assert len(pool.calls) == n


def test_compose_renders_the_planned_timeline_without_picking(pool, monkeypatch):
    planned = _plan(pool)
    pool.calls.clear()
    seen = {}

    def fake_compose(project_id, tl, **kw):
        seen["tl"] = tl
        return SimpleNamespace(warnings=[])

    monkeypatch.setattr(edit_render, "compose", fake_compose)
    edit_project.compose(PID, SPECS, {}, {"cues": CUES}, size=(1920, 1080), title="t",
                         subject="车A", output_name="final.mp4")
    assert pool.calls == []
    assert _ids(seen["tl"]) == _ids(planned)


def test_exclude_list_forces_repick_of_cuts_using_it(pool):
    first = _ids(_plan(pool))
    gone = first["1:0"]
    db.write_json(PID, edit_project.EXCLUDE_FILE, [f"https://v/{gone}"])
    pool.calls.clear()
    second = _ids(_plan(pool))
    hit = [k for k, v in first.items() if v == gone]
    assert len(pool.calls) == len(hit)
    assert all(f"https://v/{gone}" in c["banned"] for c in pool.calls)
    assert gone not in second.values()
    assert {k: v for k, v in second.items() if k not in hit} == \
        {k: v for k, v in first.items() if k not in hit}


def test_mark_dirty_repicks_only_that_beat_and_pushes_old_back(pool):
    first = _ids(_plan(pool))
    old = {v for k, v in first.items() if k.startswith("1:")}
    gone = edit_project.mark_dirty(PID, [1])
    assert gone and all(k.startswith("1:") for k in gone)
    pool.calls.clear()
    second = _ids(_plan(pool))
    assert len(pool.calls) == len(gone)
    assert all(pool.calls[0]["used"].get(i, 0) >= edit_project.AVOID_WEIGHT for i in old)
    assert {k: v for k, v in second.items() if k.startswith("0:")} == \
        {k: v for k, v in first.items() if k.startswith("0:")}


def test_swap_repicks_one_cut_and_bans_its_old_footage(pool):
    first = _ids(_plan(pool))
    pool.calls.clear()
    second = _ids(_plan(pool, source="swap", swap="1:0"))
    assert len(pool.calls) == 1
    assert first["1:0"] in pool.calls[0]["banned"]
    assert second["1:0"] != first["1:0"]
    assert {k: v for k, v in second.items() if k != "1:0"} == \
        {k: v for k, v in first.items() if k != "1:0"}
    # 换下来的素材记进 avoid,之后的取材也往后排
    assert first["1:0"] in edit_project.load(PID)["plan"]["avoid"]
    with pytest.raises(KeyError):
        _plan(pool, swap="9:9")


def test_changed_narration_repicks_only_affected_beat(pool):
    first = _ids(_plan(pool))
    pool.calls.clear()
    new_cues = [CUES[0], _cue(1, 3.0, 9.0, "前脸换了样子,侧面线条很低趴,尾灯是贯穿式", 1)]
    rows = {s.idx: {"draft_path": str(pool.beat)} for s in SPECS}
    tl = edit_project.plan(PID, SPECS, rows, {"cues": new_cues}, size=(1920, 1080), title="t",
                           subject="车A")
    beat1 = [k for k in _ids(tl) if k.startswith("1:")]
    assert 1 <= len(pool.calls) <= len(beat1)
    assert any("前脸换了样子" in c["detail"] for c in pool.calls)
    # 开场那一拍的旁白没变:原样回放
    assert {k: v for k, v in _ids(tl).items() if k.startswith("0:")} == \
        {k: v for k, v in first.items() if k.startswith("0:")}


def test_trial_plan_writes_its_own_file(pool):
    _plan(pool, source="trial")
    d = config.project_dir(PID)
    assert (d / edit_project.TRIAL_FILE).is_file()
    assert not (d / edit_project.TIMELINE_FILE).exists()
    assert edit_project.active(PID) is None
    assert edit_project.load(PID, trial=True)["plan"]["source"] == "trial"


def test_beat_pieces_follow_active_timeline_then_beats_after_supersede(pool):
    db.write_json(PID, "beats/001.json", [{"item_id": "old", "kind": "still", "length": 6.0,
                                           "path": "x.jpg"}])
    db.write_json(PID, "beats/000.json", [{"item_id": "old0", "kind": "still", "length": 3.0,
                                           "path": "y.jpg"}])
    tl = _plan(pool)
    by_beat = footage.beat_pieces(PID, SPECS)
    assert {p["item_id"] for p in by_beat[1]} == {c.item_id for c in tl.clips if c.beat == 1}
    assert footage.cut_count(PID, SPECS) == len([c for c in tl.clips if c.beat >= 0])
    used = footage.materials_used(PID, SPECS)
    assert {r["item_id"] for r in used["pieces"]} == set(_ids(tl).values())

    edit_project.supersede(PID, "成片走了旧合成")
    assert edit_project.active(PID) is None
    by_beat = footage.beat_pieces(PID, SPECS)
    assert [p["item_id"] for p in by_beat[1]] == ["old"]


def test_reset_drops_timeline_and_picks(pool):
    _plan(pool)
    edit_project.reset(PID)
    assert edit_project.load(PID) == {}
    pool.calls.clear()
    _plan(pool)
    assert pool.calls


def test_cuts_that_fell_back_are_repicked_next_time(pool, monkeypatch):
    real = library.fill
    monkeypatch.setattr(library, "fill", lambda *a, **k: [])  # 池子空:全部退回逐拍成品
    first = _plan(pool)
    assert first.clips and all(c.via == "fallback" for c in first.clips)
    monkeypatch.setattr(library, "fill", real)
    pool.calls.clear()
    second = _plan(pool)
    assert pool.calls  # 空记录不回放
    assert second.clips and not any(c.via == "fallback" for c in second.clips)
    pool.calls.clear()
    _plan(pool)
    assert pool.calls == []  # 取到了的照常回放


def _bgm_library(root, *, spacing):
    """曲库里放一首假曲子:drop 在 10 秒,拍点从 drop 起每 `spacing` 秒一个(往前也排)。"""
    import json

    root.mkdir(parents=True, exist_ok=True)
    (root / "a.mp3").write_bytes(b"x")
    beats = sorted({round(10.0 + k * spacing, 3) for k in range(-18, 180) if 10.0 + k * spacing >= 0})
    meta = {"title": "A", "mood": "科技", "duration": 120.0, "bpm": round(60 / spacing, 2),
            "pulse": 0.8, "beats": beats, "downbeats": beats[::4], "drops": [10.0],
            "drop_db": [6.0], "lufs": -14.0, "source": "test:a"}
    (root / "_licenses.json").write_text(json.dumps({"a.mp3": meta}), encoding="utf-8")


def test_plan_scores_music_and_snaps_cuts_to_the_beat(pool, tmp_path, monkeypatch):
    """开了配乐:drop 对钩子收尾那一刀(3 秒);换章 whip(4.1 秒)和普通硬切(8.3 秒)都吸到
    0.1 秒后的拍上,前一刀跟着拉长、章节卡跟着挪;离拍点太远的刀不动。配乐编排存进时间线文件。"""
    _bgm_library(tmp_path / "bgm", spacing=0.6)  # 拍点在 …、1.8、2.4、3.0、3.6、4.2 …
    monkeypatch.setattr(config, "BGM_DIR", tmp_path / "bgm")
    monkeypatch.setattr(config, "BGM_SWITCH", True)
    monkeypatch.setattr(edit_project, "pacing", lambda: edl.Pacing(rhythm=(2.0,)))
    specs = [_spec(0, 3.0, layout="hook", text="它两秒破百"),
             _spec(1, 1.1, text="先看外观"),
             _spec(2, 7.0, chapter="外观", text="前脸是封闭式格栅侧面线条很低趴")]
    cues = [_cue(0, 0.0, 3.0, "它两秒破百", 0), _cue(1, 3.0, 4.1, "先看外观", 1),
            _cue(2, 4.1, 11.1, "前脸是封闭式格栅侧面线条很低趴", 2)]
    rows = {s.idx: {"draft_path": str(pool.beat)} for s in specs}
    with edit_project.looks.use("commercial"):
        tl = edit_project.plan(PID, specs, rows, {"cues": cues}, size=(1920, 1080), title="t",
                               subject="车A")
    bgm = tl.music
    assert bgm["mode"] == "beats" and bgm["drop_at"] == 3.0
    # 钩子 1.5、换章后 6.3 离拍点 0.3 秒,不吸
    assert bgm["snapped"] == [[4.1, 4.2], [8.3, 8.4]]
    assert [round(c.start, 3) for c in tl.clips] == [0.0, 1.5, 3.0, 4.2, 6.3, 8.4]
    whip = next(c for c in tl.clips if c.transition == "whip")
    prev = tl.clips[tl.clips.index(whip) - 1]
    assert whip.start == 4.2 and prev.start + prev.dur == pytest.approx(4.2)
    assert whip.src_in == pytest.approx(1.1)  # 头上剪掉 0.1 秒,素材出点不变
    card = next(o for o in tl.overlays if o.type == "chapter_card")
    assert card.start == 4.2
    ends = [round(c.start + c.dur, 3) for c in tl.clips]
    assert [round(c.start, 3) for c in tl.clips[1:]] == ends[:-1]  # 刀与刀首尾相接
    assert ends[-1] == pytest.approx(tl.duration)
    assert edit_project.load(PID)["music"]["segments"][0]["path"].endswith("a.mp3")


def test_plan_without_music_leaves_cuts_alone(pool):
    tl = _plan(pool)
    assert tl.music == {}
    assert "music" not in edit_render.stage_media(tl, config.project_dir(PID) / "_pub")
