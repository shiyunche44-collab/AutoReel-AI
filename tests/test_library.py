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
def test_window_from_one_long_segment():
    got = lib_mod.windows([_seg(1, 0.1, 7.9)], 6, "exterior_45")
    assert [(w.id, round(share, 2)) for w, share in got] == [("s1", 1.0)]


def test_window_joins_same_group_segments():
    segs = [_seg(1, 0.1, 2.9, "exterior_front"), _seg(2, 3.1, 5.9, "driving"),
            _seg(3, 6.1, 8.9, "exterior_side")]
    got = lib_mod.windows(segs, 6, "exterior_45")
    assert [w.id for w, _ in got] == ["s1"]   # s2 起只剩 5.8 秒,不够


def test_window_breaks_on_group_change_and_bad_segment():
    inside = [_seg(1, 0.1, 2.9), _seg(2, 3.1, 9.9, "interior")]
    assert lib_mod.windows(inside, 6, "exterior_45") == []
    poster = [_seg(1, 0.1, 2.9), _seg(2, 3.1, 9.9, problem="poster")]
    assert lib_mod.windows(poster, 6, "exterior_45") == []
    # 细节特写两边都能接
    detail = [_seg(1, 0.1, 2.9), _seg(2, 3.1, 9.9, "detail")]
    assert [w.id for w, _ in lib_mod.windows(detail, 6, "exterior_45")] == ["s1"]


def test_window_skips_short_untagged_flash_but_not_long_untagged():
    flash = [_seg(1, 0.1, 2.9), _seg(2, 3.1, 3.5, tagged=False), _seg(3, 3.7, 9.0)]
    assert [w.id for w, _ in lib_mod.windows(flash, 6, "exterior_45")] == ["s1"]
    unseen = [_seg(1, 0.1, 2.9), _seg(2, 3.1, 9.0, tagged=False)]
    assert lib_mod.windows(unseen, 6, "exterior_45") == []


def test_window_requires_matching_first_view():
    assert lib_mod.windows([_seg(1, 0, 8, "interior")], 6, "exterior_45") == []
    assert lib_mod.windows([_seg(1, 0, 8, "screen")], 6, "screen_spec")


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


def test_candidates_rank_and_exclude(data_dir):
    a = data_dir / "a.mp4"
    b = data_dir / "b.mp4"
    img = data_dir / "i.jpg"
    for f in (a, b, img):
        f.write_bytes(b"x")
    still = Item(id="img1", kind="still", path=str(img), tagged=True, view="exterior_45",
                 official=True, origin="library:autohome")
    _write_lib(data_dir, [
        _seg(1, 0.1, 7.9, "exterior_side", path=str(a)),
        _seg(2, 0.1, 7.9, "exterior_45", path=str(b), official=True, target="yes"),
        _seg(3, 8.1, 15.9, "exterior_45", path=str(b), official=True),
        still,
    ])
    got = lib_mod.candidates("小米YU7", "exterior_45", seconds=6)
    assert [(Path(c.url).name, c.start) for c in got] == [
        ("b.mp4", 0.1), ("b.mp4", 8.1), ("a.mp4", 0.1), ("i.jpg", 0.0)]
    assert got[0].kind == "video" and got[-1].kind == "still"
    assert all(lib_mod.is_library(c) for c in got)

    used = [(str(b), 0.1, 6.1)]
    got = lib_mod.candidates("小米YU7", "exterior_45", seconds=6, exclude=used)
    assert [(Path(c.url).name, c.start) for c in got][0] == ("b.mp4", 8.1)


def test_candidates_empty_without_library(data_dir):
    assert lib_mod.candidates("没建过的车", "exterior_45") == []
    _write_lib(data_dir, [_seg(1, 0, 8, path=str(data_dir / "gone.mp4"))])
    assert lib_mod.candidates("小米YU7", "exterior_45") == []   # 文件没了
    assert lib_mod.candidates("小米YU7", "unknown_angle") == []


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


def test_candidates_prefer_clean_over_logo_over_subtitle(data_dir):
    files = {n: data_dir / f"{n}.mp4" for n in ("sub", "logo", "clean")}
    for f in files.values():
        f.write_bytes(b"x")
    _write_lib(data_dir, [
        _seg(1, 0, 8, path=str(files["sub"]), problem="subtitle", official=True, target="yes"),
        _seg(2, 0, 8, path=str(files["logo"]), problem="text_overlay", official=True),
        _seg(3, 0, 8, path=str(files["clean"])),
    ])
    got = [Path(c.url).stem for c in lib_mod.candidates("小米YU7", "exterior_45", seconds=6)]
    assert got == ["clean", "logo", "sub"]


class _FakeLLM:
    def __init__(self, picks=None, boom=False):
        self.picks, self.boom, self.calls = picks, boom, []

    def structured(self, system, user, **k):
        self.calls.append(user)
        if self.boom:
            raise RuntimeError("down")
        return {"picks": self.picks}


def _cand(n):
    from app.providers.clips import ClipCandidate

    return ClipCandidate(url=f"F:/lib/{n}.mp4", origin="library:bilibili", title=f"画面{n}",
                         view="exterior_45", start=float(n))


def test_match_intent_reorders_and_filters():
    cands = [_cand(n) for n in range(4)]
    llm = _FakeLLM(picks=[3, "1", 99, "x", 3])
    got = lib_mod.match_intent(cands, "夕阳下静止的 YU7", llm, [])
    assert [c.title for c in got] == ["画面2", "画面0"]
    assert "夕阳下静止的 YU7" in llm.calls[0] and "1. [exterior_45 / 视频] 画面0" in llm.calls[0]


def test_match_intent_none_means_keep_order():
    cands = [_cand(0)]
    assert lib_mod.match_intent(cands, "x", None, []) is None
    assert lib_mod.match_intent(cands, "", _FakeLLM([1]), []) is None
    errors: list[str] = []
    assert lib_mod.match_intent(cands, "x", _FakeLLM(boom=True), errors) is None
    assert errors
    assert lib_mod.match_intent(cands, "x", _FakeLLM(picks=[]), []) == []


def test_dirt_counts_presenter_like_subtitle():
    assert lib_mod._dirt(_seg(1, 0, 3)) == 0
    assert lib_mod._dirt(_seg(1, 0, 3, problem="text_overlay")) == 1
    host = _seg(1, 0, 3)
    host.desc = "男士在YU7前45度处介绍大灯"
    assert lib_mod._dirt(host) == 2
    model = _seg(1, 0, 3)
    model.desc = "红裙女子站在红色SUV旁"
    assert lib_mod._dirt(model) == 0


def test_match_intent_orders_clean_first():
    cands = [_cand(n) for n in range(3)]
    cands[0].dirt, cands[1].dirt, cands[2].dirt = 2, 0, 1
    got = lib_mod.match_intent(cands, "x", _FakeLLM(picks=[1, 2, 3]), [])
    assert [c.title for c in got] == ["画面1", "画面2", "画面0"]
