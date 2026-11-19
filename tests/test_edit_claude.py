"""Claude 剪辑(门禁 B):钉选画面、候选、体检。

断言:
- 钉选按指定素材铺(`library.piece_for`),重编时间线一直沿用,别的刀照常回放;
- 半句变了 / 重拍这一拍 / 换一刀,钉选作废;
- `piece_for` 守住和 `fill` 一样的硬门槛(开头结尾只用一方素材、铺得满);
- 候选和正式取材同一套排序,只读;体检能报出重复用素材、开头挂第三方。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config
from app.models import ShotSpec
from app.pipeline import explainer, library, outro
from app.pipeline.edit import project as edit_project
from app.pipeline.library import select
from app.pipeline.library.model import Item, Library

PID = "p_edit_claude"
POOL = ["m1", "m2", "m3", "m4", "m5", "m6"]


def _spec(idx, dur, layout="broll", chapter="开场", text=""):
    return ShotSpec(idx=idx, duration=dur, visual_zh="x", layout=layout, chapter=chapter,
                    narration_zh=text)


def _cue(i, start, end, text, shot):
    return {"index": i, "start": start, "end": end, "text": text, "shot_idx": shot}


SPECS = [_spec(0, 3.0, layout="hook", text="它两秒破百"),
         _spec(1, 6.0, chapter="外观", text="前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式")]
CUES = [_cue(0, 0.0, 3.0, "它两秒破百", 0),
        _cue(1, 3.0, 9.0, "前脸是封闭式格栅,侧面线条很低趴,尾灯是贯穿式", 1)]


def _piece(media, iid, length=30.0, third_party=False):
    return SimpleNamespace(item_id=iid, kind="video", path=str(media / f"{iid}.mp4"), start=1.0,
                           length=length, source_url=f"https://v/{iid}", platform="bilibili",
                           license_status="unknown", third_party=third_party)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """假素材池:fill 按「没被排除、用得少、id 小」挑;piece_for 照钉的素材原样铺。"""
    media = tmp_path / "media"
    media.mkdir()
    for iid in POOL:
        (media / f"{iid}.mp4").write_bytes(b"x")
    calls: list[str] = []
    items = [SimpleNamespace(id=i, kind="video", view="exterior_front", desc=f"素材{i}",
                             start=0.0, end=10.0, official=True, platform="bilibili",
                             frame="", path=str(media / f"{i}.mp4")) for i in POOL]

    def fill(car, seconds, *, hint=None, used=None, recent=None, detail="", role="",
             views=None, choose=None):
        banned = select._BANNED.get()
        free = [i for i in POOL if i not in banned]
        free.sort(key=lambda i: ((used or {}).get(i, 0), POOL.index(i)))
        calls.append(detail)
        if choose is not None:
            choose(detail, [x for x in items if x.id in free][:select.CHOOSE_TOP])
        if not free:
            return []
        iid = free[0]
        if used is not None:
            used[iid] = used.get(iid, 0) + 1
        return [_piece(media, iid)]

    def piece_for(car, picks, seconds, *, role="body", used=None, recent=None):
        bad = [p["item_id"] for p in picks if p["item_id"] not in POOL]
        if bad:
            raise ValueError(f"素材池里没有 {bad[0]}")
        return [_piece(media, p["item_id"], length=seconds) for p in picks]

    monkeypatch.setattr(library, "fill", fill)
    monkeypatch.setattr(library, "piece_for", piece_for)
    monkeypatch.setattr(library.Library, "load",
                        classmethod(lambda cls, car: SimpleNamespace(items=items)))
    monkeypatch.setattr(outro, "build", lambda *a, **k: None)
    monkeypatch.setattr(explainer, "cross_project_seed", lambda pid, car: {})
    monkeypatch.setattr(config, "SFX_SWITCH", False)
    monkeypatch.setattr(config, "BGM_SWITCH", False)
    beat = config.project_dir(PID) / "beats" / "draft_001.mp4"
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_bytes(b"x")
    return SimpleNamespace(calls=calls, beat=beat, media=media)


def _plan(pool, cues=CUES, **kw):
    rows = {s.idx: {"draft_path": str(pool.beat)} for s in SPECS}
    return edit_project.plan(PID, SPECS, rows, {"cues": cues}, size=(1920, 1080), title="t",
                             subject="车A", **kw)


def _ids(tl):
    out: dict[str, str] = {}
    for c in tl.clips:
        if c.key:
            out.setdefault(c.key, c.item_id)
    return out


def test_pin_sticks_across_replans(pool):
    first = _ids(_plan(pool))
    key = next(k for k in first if k.startswith("1:"))
    edit_project.set_pins(PID, {key: [{"item_id": "m6"}]})
    tl = _plan(pool, source="claude")
    assert _ids(tl)[key] == "m6"
    assert {c.via for c in tl.clips if c.key == key} == {"claude"}
    assert {k: v for k, v in _ids(tl).items() if k != key} == \
        {k: v for k, v in first.items() if k != key}
    row = next(r for r in edit_project.cuts(PID) if r["key"] == key)
    assert row["pinned"] and edit_project.pins_of(PID)[key]["picks"] == [{"item_id": "m6"}]

    pool.calls.clear()
    again = _plan(pool)
    assert _ids(again)[key] == "m6" and pool.calls == []  # 其余刀回放,钉选重铺不碰排序


def test_pin_dropped_when_text_changes(pool):
    _plan(pool)
    key = "0:0"
    edit_project.set_pins(PID, {key: [{"item_id": "m6"}]})
    changed = [_cue(0, 0.0, 3.0, "它三秒破百", 0), CUES[1]]
    tl = _plan(pool, cues=changed)
    assert _ids(tl)[key] != "m6"
    meta = edit_project.load(PID)["plan"]
    assert key not in meta["pins"] and "半句变了" in meta["pins_dropped"][key]
    assert any("钉选作废" in n for n in tl.notes)


def test_bad_pin_falls_back_and_is_reported(pool):
    _plan(pool)
    edit_project.set_pins(PID, {"0:0": [{"item_id": "nope"}]})
    tl = _plan(pool)
    assert _ids(tl)["0:0"] in POOL
    check = edit_project.check(PID)
    assert any("钉选作废" in i["message"] for i in check["issues"])


def test_rerender_and_swap_release_pins(pool):
    first = _ids(_plan(pool))
    k1 = next(k for k in first if k.startswith("1:"))
    edit_project.set_pins(PID, {"0:0": [{"item_id": "m6"}], k1: [{"item_id": "m5"}]})
    _plan(pool)
    edit_project.mark_dirty(PID, [1])
    assert set(edit_project.pins_of(PID)) == {"0:0"}
    tl = _plan(pool, source="swap", swap="0:0")
    assert _ids(tl)["0:0"] != "m6" and edit_project.pins_of(PID) == {}


def test_set_pins_rejects_unknown_cut(pool):
    _plan(pool)
    with pytest.raises(KeyError):
        edit_project.set_pins(PID, {"9:9": [{"item_id": "m1"}]})


def test_candidates_use_same_ranking_and_are_read_only(pool):
    _plan(pool)
    before = edit_project.load(PID)
    got = edit_project.candidates(PID, "0:0", SPECS, subject="车A", pages=2)
    ids = [c["item_id"] for c in got["candidates"]]
    assert ids and len(ids) == len(set(ids)) == len(POOL)  # 两页不重复
    assert got["text"] and got["text"] in "它两秒破百" and got["role"] == "hook"
    assert any(c["current"] for c in got["candidates"])
    assert edit_project.load(PID) == before
    with pytest.raises(KeyError):
        edit_project.candidates(PID, "7:0", SPECS, subject="车A")


def test_check_flags_repeats_and_third_party_in_hook(pool):
    _plan(pool)
    saved = edit_project.load(PID)
    for c in saved["clips"]:
        if c.get("key"):
            c["item_id"] = "m1"
            if c["key"] == "0:0":
                c["third_party"] = True
    from app import db

    db.write_json(PID, edit_project.TIMELINE_FILE, saved)
    got = edit_project.check(PID)
    msgs = [i["message"] for i in got["issues"]]
    assert not got["pass"] and any("别家账号" in m for m in msgs)
    assert any("m1" in m and "刀" in m for m in msgs)
    assert got["stats"]["cuts"] >= 2


# --------------------------------------------------------------------------
# piece_for:真素材池,守和 fill 一样的门槛
# --------------------------------------------------------------------------
@pytest.fixture()
def real_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")
    lib = Library(subject="测试车")
    lib.items = [
        Item(id="v1", kind="video", path=str(vid), tagged=True, start=2.0, end=6.0,
             view="exterior_45", target="yes", official=True),
        Item(id="v2", kind="video", path=str(vid), tagged=True, start=0.0, end=20.0,
             view="driving", target="yes", official=True),
        Item(id="t1", kind="video", path=str(vid), tagged=True, start=0.0, end=20.0,
             view="driving", target="yes", official=False, source="bili:other"),
    ]
    lib.save()
    return lib


def test_piece_for_concatenates_and_needs_full_coverage(real_pool):
    out = library.piece_for("测试车", [{"item_id": "v1"}, {"item_id": "v2"}], 6.0)
    assert [p.item_id for p in out] == ["v1", "v2"]
    assert sum(p.length for p in out) == pytest.approx(6.0, abs=0.06)
    with pytest.raises(ValueError, match="再接一段"):
        library.piece_for("测试车", [{"item_id": "v1"}], 6.0)
    with pytest.raises(ValueError, match="没有"):
        library.piece_for("测试车", [{"item_id": "zz"}], 2.0)


def test_piece_for_chunk_uses_given_span(real_pool):
    out = library.piece_for("测试车", [{"item_id": "v2~1", "start": 10.0, "end": 15.0}], 4.0)
    assert out[0].item_id == "v2~1" and out[0].start == pytest.approx(10.0)
