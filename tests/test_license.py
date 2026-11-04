"""素材授权闸门与使用记录。全部离线。"""
from __future__ import annotations

import json
import time

import pytest

from app import config, db
from app.models import ShotSpec
from app.pipeline import library
from app.pipeline.explainer import footage
from app.pipeline.library import Item, Library, Source, licensing

DAY = 24 * 3600


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """三个源:官网回放(推断 restricted)、官方号(unverified)、人工标了 allowed 的第三方。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")
    lib = Library(subject="测试车")
    lib.sources = [
        Source(url="https://cdn.example.com/launch.m3u8", id="site", platform="site", uploader="官网",
               official=True),
        Source(url="https://www.bilibili.com/video/BV1xx411c7mD", id="off", platform="bilibili",
               uploader="官方号", official=True),
        Source(url="https://www.bilibili.com/video/BV1yy411c7mD", id="ok", platform="bilibili",
               license_status="allowed", license_proof="合同#1"),
    ]
    lib.items = [
        Item(id=f"v_{sid}", kind="video", path=str(vid), tagged=True, view="exterior_45",
             target="yes", start=0.0, end=4.0, source=sid, url=src.url, platform=src.platform,
             official=src.official, logo_crop=0.0)
        for sid, src in ((s.id, s) for s in lib.sources)
    ]
    lib.save()
    return lib


def _ids(pieces):
    return {p.item_id for p in pieces}


def test_status_inferred_from_source(pool):
    by = {s.id: licensing.license_of_source(s).status for s in pool.sources}
    assert by == {"site": "restricted", "off": "unverified", "ok": "allowed"}


def test_allows_rules():
    now = time.time()
    L = licensing.License
    assert not L("unverified").allows()
    assert not L("pending").allows()
    assert not L("denied").allows()
    assert not L("expired").allows()
    assert L("allowed").allows()
    assert L("allowed", scope=("background",)).allows("background")
    assert not L("allowed", scope=("background",)).allows("republish")
    # restricted 没写范围 = 只许背景
    assert L("restricted").allows("background") and not L("restricted").allows("republish")
    assert not L("allowed", expires=now - 1).allows(now=now)
    assert L("allowed", expires=now + DAY).allows(now=now)
    assert not L("allowed", rejected=True).allows()


def test_expired_status_reported(pool):
    src = pool.source("ok")
    src.license_expires = time.time() - DAY
    assert licensing.license_of_source(src).status == "expired"


def test_warn_keeps_everything_but_records_status(pool, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_GATE", "warn")
    pieces = library.fill("测试车", 12.0)
    assert _ids(pieces) == {"v_site", "v_off", "v_ok"}
    status = {p.item_id: p.license_status for p in pieces}
    assert status == {"v_site": "restricted", "v_off": "unverified", "v_ok": "allowed"}
    assert {p.item_id: p.source_url for p in pieces}["v_off"] == "https://www.bilibili.com/video/BV1xx411c7mD"


def test_enforce_blocks_unverified(pool, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_GATE", "enforce")
    pieces = library.fill("测试车", 12.0)
    assert _ids(pieces) == {"v_site", "v_ok"}
    take = library.long_take("测试车", 3.0)
    assert take is not None and take.item_id in {"v_site", "v_ok"}


def test_enforce_blocks_rejected_and_expired(pool, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_GATE", "enforce")
    lib = Library.load("测试车")
    licensing.set_license(lib, ["site"], review="rejected", reviewer="me")
    lib.source("ok").license_expires = time.time() - DAY
    lib.save()
    assert library.fill("测试车", 6.0) == []
    assert library.long_take("测试车", 3.0) is None


def test_set_license_and_scope(pool, monkeypatch):
    monkeypatch.setattr(config, "LICENSE_GATE", "enforce")
    lib = Library.load("测试车")
    n = licensing.set_license(lib, ["off"], status="allowed", scope="background", proof="邮件 2026-10-30",
                              expires=time.time() + DAY, review="approved", reviewer="me")
    assert n == 1
    lib.save()
    again = Library.load("测试车")
    off = again.source("off")
    assert (off.license_status, off.license_proof, off.review_status) == ("allowed", "邮件 2026-10-30", "approved")
    assert "v_off" in _ids(library.fill("测试车", 12.0))
    with pytest.raises(ValueError):
        licensing.set_license(again, ["off"], status="bogus")


def test_unsourced_still_defaults(pool):
    book = licensing.LicenseBook(pool)
    official = Item(id="g1", kind="still", path="x", official=True, license="车企官网:https://x")
    third = Item(id="g2", kind="still", path="x", license="检索命中:https://y")
    assert book.license(official).status == "restricted"
    assert book.license(third).status == "unverified"


def test_old_library_json_loads_without_license_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    d = Library.dir_for("老池子")
    d.mkdir(parents=True)
    (d / "library.json").write_text(json.dumps({
        "subject": "老池子", "sources": [{"url": "https://www.bilibili.com/video/BV1xx411c7mD", "id": "a"}],
        "items": []}), encoding="utf-8")
    lib = Library.load("老池子")
    assert licensing.license_of_source(lib.sources[0]).status == "unverified"


def test_summary_counts_blocked(pool):
    s = licensing.summary(pool)
    assert s["sources"] == {"限制使用": 1, "未核验": 1, "允许使用": 1}
    assert s["blocked_if_enforced"] == 1


def test_cli_license_updates_and_lists(pool, capsys):
    rc = library._cli(["license", "测试车", "--source", "off", "--status", "allowed",
                       "--expires", "2099-01-01", "--proof", "邮件"])
    assert rc == 0
    assert Library.load("测试车").source("off").license_status == "allowed"
    assert "允许使用" in capsys.readouterr().out
    assert library._cli(["license", "测试车", "--source", "nope"]) == 1


def test_materials_used_record_and_reverse_lookup(pool, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "proj", raising=False)
    store: dict[str, object] = {}
    monkeypatch.setattr(db, "read_json", lambda pid, name, default=None: store.get((pid, name), default))
    monkeypatch.setattr(db, "write_json", lambda pid, name, data: store.__setitem__((pid, name), data))
    monkeypatch.setattr(config, "LICENSE_GATE", "warn")
    pieces = library.fill("测试车", 12.0)
    store[("p1", "beats/001.json")] = [p.__dict__ for p in pieces] + [
        library.Piece("bg", "still", "x", 0.0, 2.0).__dict__]
    specs = [ShotSpec(idx=1, visual_zh="x", chapter="外观", duration=12.0)]

    rec = footage.materials_used("p1", specs)
    assert len(rec["pieces"]) == len(pieces)  # 卡片底图 "bg" 不记
    assert {s["source_url"] for s in rec["sources"]} == {
        "https://cdn.example.com/launch.m3u8", "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1yy411c7mD"}
    assert [s["license_status"] for s in rec["uncleared"]] == ["unverified"]
    assert store[("p1", "materials_used.json")] == rec
    assert footage.projects_using("https://www.bilibili.com/video/BV1xx411c7mD", ["p1", "p2"]) == ["p1"]
    assert footage.projects_using("https://nope", ["p1"]) == []


def _note(monkeypatch, pieces):
    store = {("p1", "beats/001.json"): pieces}
    monkeypatch.setattr(db, "read_json", lambda pid, name, default=None: store.get((pid, name), default))
    specs = [ShotSpec(idx=1, visual_zh="x", chapter="外观", duration=12.0)]
    return footage.footage_source_note("p1", specs, "车型")


def _piece(platform, length, kind="still", third_party=False, item_id="x"):
    return {"item_id": item_id, "kind": kind, "platform": platform, "length": length,
            "third_party": third_party}


def test_source_note_names_galleries_not_official(monkeypatch):
    """吉利星愿实跑:素材全来自汽车之家、懂车帝,简介却写"品牌官方宣传片与官方图片"。"""
    note = _note(monkeypatch, [_piece("dongchedi", 3.0), _piece("autohome", 5.0),
                               _piece("autohome", 4.0)])
    assert note == "画面素材:汽车之家图库、懂车帝图库,仅作车型展示"
    assert "官方" not in note


def test_source_note_says_official_only_with_evidence(monkeypatch):
    note = _note(monkeypatch, [_piece("press", 6.0), _piece("site", 3.0, kind="video"),
                               _piece("bilibili", 2.0, kind="video"),
                               _piece("bilibili", 1.0, kind="video", third_party=True)])
    assert note == "画面素材:品牌官方图片、品牌官方视频、哔哩哔哩视频,仅作车型展示"
    # 老记录没有 third_party / platform 字段:不能算官方
    old = {"item_id": "x", "kind": "video", "length": 3.0}
    assert _note(monkeypatch, [old]) == "画面素材:网络公开素材,仅作车型展示"


def test_source_note_ignores_card_background_and_empty_project(monkeypatch):
    assert _note(monkeypatch, [_piece("", 3.0, item_id="bg")]) == ""
    assert _note(monkeypatch, []) == ""


@pytest.mark.parametrize("car_source,expected", [
    ("official_cutout", "画面素材:懂车帝图库,仅作车型展示"),
    ("ai_generated", "画面素材:懂车帝图库,仅作车型展示;封面车型图为 AI 生成示意,以官方图为准"),
])
def test_compose_writes_source_note_from_actual_footage(monkeypatch, tmp_path, car_source, expected):
    from types import SimpleNamespace

    from app.categories import load_profile
    from app.pipeline import cover_art, explainer, packaging

    store: dict[tuple[str, str], object] = {("p1", "beats/001.json"): [_piece("dongchedi", 3.0)]}
    monkeypatch.setattr(db, "read_json", lambda pid, name, default=None: store.get((pid, name), default))
    monkeypatch.setattr(db, "write_json", lambda pid, name, data: store.__setitem__((pid, name), data))
    monkeypatch.setattr(db, "get_project", lambda pid: {"subject": ""})
    monkeypatch.setattr(db, "get_shots", lambda pid, stage: [{"idx": 1, "final_path": "x.mp4"}])
    monkeypatch.setattr(config, "project_dir", lambda pid: tmp_path)
    result = SimpleNamespace(ok=True, output_path=tmp_path / "out.mp4", to_dict=dict, warnings=[])
    monkeypatch.setattr(explainer.render_mod, "compose", lambda *a, **k: result)
    monkeypatch.setattr(explainer.outro, "build", lambda *a: None)
    monkeypatch.setattr(explainer.intro, "build", lambda *a: None)
    monkeypatch.setattr(explainer, "_make_packaging", lambda *a: None)
    monkeypatch.setattr(explainer, "_make_cover_b", lambda *a, **k: None)
    monkeypatch.setattr(explainer, "measured_features", lambda *a: {})
    monkeypatch.setattr(packaging, "load", lambda pid: None)
    monkeypatch.setattr(cover_art, "pick_title", lambda pkg, title: title)
    art = {"path": str(tmp_path / "cover.jpg"), "car_source": car_source}
    monkeypatch.setattr(cover_art, "build", lambda *a, **k: art)

    specs = [ShotSpec(idx=1, visual_zh="x", chapter="外观", duration=3.0)]
    payload = explainer.compose("p1", specs, load_profile("auto_long"), "标题")
    assert payload["source_note"] == expected
    assert store[("p1", "render.json")]["source_note"] == expected
