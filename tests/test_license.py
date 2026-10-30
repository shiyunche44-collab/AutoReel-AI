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
