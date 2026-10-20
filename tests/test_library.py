"""素材池:存取、切段、拼窗口、挑候选。全部离线。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.pipeline import library as lib_mod
from app.pipeline.library import Item, Library, Source


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _seg(n: int, start: float, end: float, view: str = "exterior_45", *, path: str = "",
         target: str = "unknown", problem: str = "", tagged: bool = True,
         official: bool = False) -> Item:
    return Item(id=f"s{n}", kind="video", path=path, frame="f.jpg", source="src",
                url="https://www.bilibili.com/video/BV1xx411c7mD", start=start, end=end,
                view=view, target=target, problem=problem, tagged=tagged,
                official=official, origin="library:bilibili")


# --------------------------------------------------------------------------
def test_slug_ignores_spaces_and_case():
    assert lib_mod.slug("小米 YU7") == lib_mod.slug("小米yu7") == "小米yu7"


def test_normalise_url_strips_tracking():
    n = lib_mod.normalise_url
    assert n("https://m.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333&vd_source=abc") \
        == "https://www.bilibili.com/video/BV1xx411c7mD"
    assert n("https://www.bilibili.com/video/BV1xx411c7mD?p=2&spm=1") \
        == "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
    assert n("https://youtu.be/abcdefghijk?si=x") == "https://www.youtube.com/watch?v=abcdefghijk"
    assert n("https://www.youtube.com/watch?feature=share&v=abcdefghijk") \
        == "https://www.youtube.com/watch?v=abcdefghijk"
    assert n("https://www.douyin.com/video/7512345678901234567?previous_page=app") \
        == "https://www.douyin.com/video/7512345678901234567"


def test_english_name_prefers_alias_with_model_code():
    aliases = ["小米汽车", "小米SU7", "Xiaomi SU7", "Xiaomi YU7", "Xiaomi EV"]
    assert lib_mod.english_name("小米YU7", aliases) == "Xiaomi YU7"
    assert lib_mod.english_name("小米YU7", []) == "YU7"


def test_download_windows():
    assert lib_mod.download_windows(120) == [(0.0, 120)]
    long = lib_mod.download_windows(3600)
    assert len(long) == lib_mod.WINDOWS_PER_LONG_SOURCE
    assert long[0][0] >= 3600 * 0.08 - 0.01
    assert long[-1][0] + long[-1][1] <= 3600 * 0.92 + 0.01


def test_split_segments_trims_edges_and_drops_slivers():
    segs = lib_mod.split_segments([2.0, 2.3, 5.0, 99.0], 8.0)
    assert segs == [(0.1, 1.9), (2.4, 4.9), (5.1, 7.9)]


def test_apply_tag_normalises():
    item = _seg(1, 0, 3, view="")
    lib_mod.apply_tag(item, {"target": "NO", "view": "Rear", "problem": "", "desc": "x"})
    assert item.target == "no" and item.view == "rear"
    assert item.problem == "not_target_model" and not item.usable
    lib_mod.apply_tag(item, {"target": "maybe", "view": "spaceship", "problem": "none"})
    assert item.target == "unknown" and item.view == "other" and item.problem == ""


def test_usable_rules():
    assert _seg(1, 0, 3).usable
    assert not _seg(1, 0, 3, tagged=False).usable
    assert not _seg(1, 0, 3, problem="poster").usable
    assert not _seg(1, 0, 3, view="people").usable
    # 台标只记不拦
    assert _seg(1, 0, 3, problem="text_overlay").usable


# --------------------------------------------------------------------------
# 拼窗口
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 存取与挑选
# --------------------------------------------------------------------------
def test_save_load_roundtrip(data_dir):
    lib = Library(subject="小米YU7")
    assert lib.add_source(Source(url="https://youtu.be/abcdefghijk", official=True))
    assert not lib.add_source(Source(url="https://www.youtube.com/watch?v=abcdefghijk",
                                     title="t", added_by="manual"))
    lib.items.append(_seg(1, 0, 3))
    lib.save()
    back = Library.load("小米 yu7")
    assert len(back.sources) == 1
    src = back.sources[0]
    assert src.title == "t" and src.official and src.added_by == "manual" and src.id
    assert back.items[0].view == "exterior_45"


def test_save_keeps_sources_added_meanwhile(data_dir):
    stale = Library.load("小米YU7")
    lib_mod.add_links("小米YU7", ["https://www.bilibili.com/video/BV1xx411c7mD"])
    stale.items.append(_seg(1, 0, 3))
    stale.save()
    back = Library.load("小米YU7")
    assert [s.url for s in back.sources] == ["https://www.bilibili.com/video/BV1xx411c7mD"]
    assert back.sources[0].added_by == "manual"
    assert len(back.items) == 1


def test_add_links_retries_failed(data_dir):
    lib = Library(subject="小米YU7")
    lib.add_source(Source(url="https://www.bilibili.com/video/BV1xx411c7mD", status="failed",
                          error="403"))
    lib.save()
    assert lib_mod.add_links("小米YU7", ["https://www.bilibili.com/video/BV1xx411c7mD",
                                         "not a url"]) == []
    assert Library.load("小米YU7").sources[0].status == "pending"


def _write_lib(data_dir: Path, items: list[Item]) -> None:
    lib = Library(subject="小米YU7", items=items)
    lib.save()


def test_tag_queue_caps_per_source(data_dir, monkeypatch):
    monkeypatch.setattr(lib_mod, "MAX_TAGGED_PER_SOURCE", 2)
    items = [_seg(n, n * 3, n * 3 + (1 + n * 0.1), tagged=False) for n in range(5)]
    items.append(Item(id="img", kind="still", path="p", frame="f", tagged=False))
    lib = Library(subject="x", items=items)
    queue = lib_mod._tag_queue(lib)
    assert [i.id for i in queue] == ["img", "s4", "s3"]   # 图全打,视频长段优先


def test_build_async_is_single_flight(monkeypatch):
    import threading

    gate = threading.Event()
    calls: list[str] = []

    def _slow(subject, **k):
        calls.append(subject)
        gate.wait(5)

    monkeypatch.setattr(lib_mod, "build", _slow)
    assert lib_mod.build_async("小米YU7")
    assert not lib_mod.build_async("小米 yu7")
    assert lib_mod.is_building("小米YU7")
    gate.set()
    for t in threading.enumerate():
        if t.name.startswith("library-"):
            t.join(5)
    assert calls == ["小米YU7"]
    assert not lib_mod.is_building("小米YU7")


def test_rank_sources_filters_junk_and_prefers_showcase():
    from app.providers.platform_video import PlatformHit

    def h(title, dur=90, official=False, uploader="up"):
        return PlatformHit(platform="bilibili", url=f"https://b/{title}", title=title,
                           duration=dur, official=official, uploader=uploader)

    hits = [
        h("小米YU7 车主吐槽"),
        h("BeamNG精致模组分享:小米 YU7 Series"),
        h("小米YU7被老鼠咬趴窝"),
        h("小米yu7宣传片"),
        h("Xiaomi YU7 | Debut", official=True),
        h("小米YU7 提车作业"),
        h("一起回顾小米YU7预发布全程", dur=3483, official=True),
        h("小米YU7 直播回放", dur=3483),
        h("理想L9 宣传片"),
    ]
    got = [x.title for x in lib_mod.rank_sources(hits, "小米YU7")]
    assert got == ["Xiaomi YU7 | Debut", "一起回顾小米YU7预发布全程", "小米yu7宣传片",
                   "小米YU7 提车作业"]


def test_junk_words_do_not_hit_common_names():
    from app.providers.platform_video import PlatformHit

    hits = [PlatformHit(platform="youtube", url=f"https://y/{t}", title=t, duration=90)
            for t in ("Tesla Model Y official film", "Toyota bZ3X 宣传片")]
    assert len(lib_mod.rank_sources(hits, "Model Y")) == 1
    assert len(lib_mod.rank_sources(hits, "丰田bZ3X")) == 1


def test_dirt_counts_presenter_like_subtitle():
    assert lib_mod._dirt(_seg(1, 0, 3)) == 0
    assert lib_mod._dirt(_seg(1, 0, 3, problem="text_overlay")) == 1
    host = _seg(1, 0, 3)
    host.desc = "男士在YU7前45度处介绍大灯"
    assert lib_mod._dirt(host) == 2
    model = _seg(1, 0, 3)
    model.desc = "红裙女子站在红色SUV旁"
    assert lib_mod._dirt(model) == 0


# --------------------------------------------------------------------------
# 打标签省钱:按源止损、够用即停、重复画面去重
# --------------------------------------------------------------------------
def _frame(path: Path, seed: int) -> str:
    from PIL import Image

    im = Image.new("L", (90, 80))
    im.putdata([((x * (seed + 3) + y * (seed * 7 + 1)) * 37) % 256
                for y in range(80) for x in range(90)])
    im.save(path)
    return str(path)


def test_reject_sources_skips_rest_of_mostly_waste_source(monkeypatch):
    monkeypatch.setattr(lib_mod, "EARLY_STOP_SAMPLE", 4)
    bad = [_seg(n, n, n + 2, problem="no_subject") for n in range(3)]
    bad.append(_seg(3, 3, 5, problem="subtitle"))     # 字幕段能裁,不算废
    rest = [_seg(n, n, n + 2, tagged=False) for n in range(10, 13)]
    lib = Library(subject="x", items=bad + rest)
    assert lib_mod.reject_sources(lib) == 3           # 3/4 废 ≥ 70%
    assert all(i.skipped == "source_rejected" for i in rest)
    assert lib_mod._tag_queue(lib) == []


def test_reject_sources_keeps_source_with_subtitles_only(monkeypatch):
    monkeypatch.setattr(lib_mod, "EARLY_STOP_SAMPLE", 4)
    segs = [_seg(n, n, n + 2, problem="subtitle") for n in range(4)]
    segs.append(_seg(9, 9, 11, tagged=False))
    lib = Library(subject="x", items=segs)
    assert lib_mod.reject_sources(lib) == 0
    assert [i.id for i in lib_mod._tag_queue(lib)] == ["s9"]


def test_reject_sources_waits_for_full_sample(monkeypatch):
    monkeypatch.setattr(lib_mod, "EARLY_STOP_SAMPLE", 4)
    segs = [_seg(n, n, n + 2, problem="no_subject") for n in range(3)]
    segs.append(_seg(9, 9, 11, tagged=False))
    assert lib_mod.reject_sources(Library(subject="x", items=segs)) == 0


def test_tag_queue_puts_samples_of_every_source_first(monkeypatch):
    monkeypatch.setattr(lib_mod, "EARLY_STOP_SAMPLE", 2)
    a = [_seg(n, n, n + 2 + n * 0.1, tagged=False) for n in range(4)]
    b = [_seg(n, n, n + 2 + n * 0.1, tagged=False) for n in range(10, 14)]
    for i in b:
        i.source = "other"
    queue = lib_mod._tag_queue(Library(subject="x", items=a + b))
    assert [i.id for i in queue] == ["s3", "s2", "s13", "s12", "s1", "s0", "s11", "s10"]


def test_duplicate_frames_inherit_tags_without_vlm(tmp_path):
    lib_mod._dhash.cache_clear()
    f1 = _frame(tmp_path / "a.png", 1)
    f2 = _frame(tmp_path / "b.png", 1)    # 同一画面
    f3 = _frame(tmp_path / "c.png", 5)    # 不同画面
    tagged = _seg(1, 0, 3, view="rear", target="yes")
    tagged.frame, tagged.desc = f1, "车尾"
    twin = _seg(2, 5, 8, tagged=False)
    twin.frame = f2
    other = _seg(3, 9, 12, tagged=False)
    other.frame = f3
    lib = Library(subject="x", items=[tagged, twin, other])
    assert [i.id for i in lib_mod._tag_queue(lib)] == ["s3"]
    assert lib_mod.inherit_duplicates(lib) == 1
    assert twin.tagged and twin.view == "rear" and twin.desc == "车尾"
    assert not other.tagged


def test_tag_items_stops_videos_when_enough(monkeypatch):
    calls: list[int] = []

    def _judge(urls, *a, **k):
        calls.append(len(urls))
        return [{"target": "yes", "view": "exterior_45", "problem": ""} for _ in urls], [], {}

    monkeypatch.setattr(lib_mod.vision_verdict, "judge", _judge)
    monkeypatch.setattr(lib_mod.images.cli, "upload_temp_url", lambda p: "u")
    monkeypatch.setattr(lib_mod, "TAG_BATCH", 2)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Library, "save", lambda self: None)
    items = [_seg(n, n * 10, n * 10 + 10, tagged=False) for n in range(6)]
    lib = Library(subject="x", items=items)
    done = lib_mod.tag_items(lib, list(items), None, [], enough_seconds=15)
    assert calls == [2]                     # 第一批 20 秒就够了
    assert done == 2 and lib.vlm_calls == 2
    assert lib.stats()["vlm_calls"] == 2
