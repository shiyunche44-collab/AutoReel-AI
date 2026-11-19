"""素材池阶段 1:新标签字段、选料排序(1080p 动态视频优先、hook 挑大镜头)、重拉高清任务。

全部离线:平台解析 / 下载、切段、视觉打标签都换成假的。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config
from app.pipeline import library
from app.pipeline.library import footage_health, upgrade
from app.pipeline.library.model import Item, Library, Source
from app.providers import platform_video

SUBJECT = "测试车"
SID = "BV1xx411c7mD"
URL = f"https://www.bilibili.com/video/{SID}"
HD = {"width": 1920, "height": 1080}
SD = {"width": 854, "height": 480}


@pytest.fixture()
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")
    still = tmp_path / "s.jpg"
    still.write_bytes(b"x")
    return vid, still


def _video(id_: str, path: Path, start: float, end: float, **kw) -> Item:
    base = {"view": "exterior_45", "target": "yes", "official": True}
    base.update(kw)
    return Item(id=id_, kind="video", path=str(path), tagged=True, start=start, end=end, **base)


def _pool(items: list[Item]) -> None:
    lib = Library(subject=SUBJECT)
    lib.items = items
    lib.save()


# --------------------------------------------------------------------------
# 新字段:老 JSON 能读、属性口径
# --------------------------------------------------------------------------
def test_old_json_items_get_neutral_defaults(files):
    vid, _ = files
    d = Library.dir_for(SUBJECT)
    d.mkdir(parents=True)
    old = {"id": "x1", "kind": "video", "path": str(vid), "tagged": True, "view": "rear",
           "target": "yes", "start": 0, "end": 3}
    (d / "library.json").write_text(json.dumps({"sources": [{"url": URL, "id": SID}],
                                                "items": [old]}), encoding="utf-8")
    lib = Library.load(SUBJECT)
    item, src = lib.items[0], lib.sources[0]
    assert (item.shot, item.hero, item.motion, item.replaces) == ("", -1, -1.0, "")
    assert not item.dynamic and item.big_shot == -1.0 and not item.hd
    assert (src.upgraded_at, src.upgrade_error, src.upgraded_height) == (0.0, "", 0)


def test_item_hd_dynamic_and_big_shot():
    hd = Item(id="a", kind="video", path="", motion=0.4, hero=3, **HD)
    assert hd.hd and hd.dynamic and hd.big_shot == pytest.approx(1.2)
    calm = Item(id="b", kind="video", path="", motion=library.DYNAMIC_MOTION - 0.01, **SD)
    assert not calm.hd and not calm.dynamic and calm.big_shot == -1.0   # hero 没打过
    assert not Item(id="c", kind="still", path="", **HD).hd


def test_normalise_shot_accepts_codes_and_rejects_junk():
    assert library.normalise_shot(" Closeup ") == "closeup"
    assert library.normalise_shot("中景偏近") == "medium"
    assert library.normalise_shot("太空镜头") == ""


def test_official_title_naming_the_car_counts_unknown_as_target():
    lib = Library(subject="小米YU7")
    lib.sources = [Source(url=URL, id="named", official=True, title="小米YU7 官方首发片"),
                   Source(url=URL, id="other", official=True, title="小米SU7 赛道日"),
                   Source(url=URL, id="fan", official=False, title="小米YU7 提车")]
    titled = library.titled_sources(lib)
    assert titled == {"named"}

    def seg(source: str, target: str, official: bool = True) -> Item:
        return Item(id=f"{source}-{target}", kind="video", path="", source=source,
                    target=target, official=official)

    assert library.on_target(seg("named", "unknown"), titled)
    assert not library.on_target(seg("named", "no"), titled)        # 模型明确说不是
    assert not library.on_target(seg("other", "unknown"), titled)
    assert not library.on_target(seg("fan", "unknown", official=False), titled)
    assert library.on_target(seg("other", "yes"), titled)


def test_digit_only_model_needs_whole_name_in_title():
    assert library.title_names_subject("领克 07 EM-P 发布", "领克 07")
    assert not library.title_names_subject("领克 08 和它的 07 号配色", "领克 07")


# --------------------------------------------------------------------------
# 选料排序
# --------------------------------------------------------------------------
def test_fill_puts_confirmed_hd_dynamic_video_first(files):
    """默认 look 下也一样:确认本车的 1080p 动态视频排在静图、标清、静止镜头前面。"""
    vid, still = files
    _pool([
        Item(id="still1", kind="still", path=str(still), frame=str(still), view="exterior_45",
             target="yes", tagged=True),
        _video("sd", vid, 0, 4, motion=0.6, hero=3),                       # 尺寸不明
        _video("static", vid, 4, 8, motion=0.02, hero=3, **HD),            # 1080p 但不动
        _video("dyn", vid, 8, 12, motion=0.4, hero=1, **HD),
    ])
    assert library.fill(SUBJECT, 2.0, used={})[0].item_id == "dyn"
    assert library.fill(SUBJECT, 2.0, used={}, prefer_video=True)[0].item_id == "dyn"


def test_premium_needs_confirmed_target(files):
    """1080p 动态但视觉模型没认出是这台车(第三方源)的,不进第一档。"""
    vid, _ = files
    _pool([
        _video("guess", vid, 0, 4, target="unknown", official=False, logo_crop=0.0,
               motion=0.8, hero=3, **HD),
        _video("sure", vid, 4, 8, motion=0.2, hero=1, **HD),
    ])
    assert library.fill(SUBJECT, 2.0, used={})[0].item_id == "sure"


def test_prefer_video_keeps_stills_as_fallback(files):
    vid, still = files
    _pool([
        Item(id="still1", kind="still", path=str(still), frame=str(still), view="exterior_45",
             target="yes", tagged=True),
        _video("sub1", vid, 0, 10, problem="subtitle"),
    ])
    # 老排法:裁字幕的视频比官图低一档
    assert library.fill(SUBJECT, 2.0, used={}, prefer_video=False)[0].item_id == "still1"
    # 视频优先:还有没用过的视频,静图就排在后面
    first = library.fill(SUBJECT, 2.0, used={}, prefer_video=True)[0]
    assert first.item_id == "sub1" and first.crop_bottom == library.SUBTITLE_CROP
    # 视频都用过了,静图才顶上来
    assert library.fill(SUBJECT, 2.0, used={"sub1": 1},
                        prefer_video=True)[0].item_id == "still1"


def test_hook_picks_biggest_hero_times_motion(files):
    vid, _ = files
    _pool([
        _video("calm", vid, 0, 6, hero=0, motion=0.05, **HD),
        _video("mid", vid, 6, 12, hero=1, motion=0.3, **HD),
        _video("wow", vid, 12, 15, hero=3, motion=0.6, **HD),
        _video("fast_dull", vid, 15, 21, hero=0, motion=0.9, **HD),
    ])
    assert library.fill(SUBJECT, 2.0, used={}, role="hook")[0].item_id == "wow"
    # 用过了就让给下一个大镜头,不在开场反复用同一段
    assert library.fill(SUBJECT, 2.0, used={"wow": 1}, role="hook")[0].item_id == "mid"


def test_retired_segment_and_replacement_are_both_screened(files):
    """被高清段顶替的老段不再上屏;老段被拉黑时,顶替它的新段也跟着不要。"""
    vid, _ = files
    _pool([
        _video(f"{SID}_0_000", vid, 0, 4, skipped="replaced"),
        _video(f"{SID}_h0_000", vid, 0, 4, replaces=f"{SID}_0_000", **HD),
        _video("other", vid, 4, 8),
    ])
    ids = {p.item_id for p in library.fill(SUBJECT, 6.0, used={})}
    assert f"{SID}_0_000" not in ids and f"{SID}_h0_000" in ids


# --------------------------------------------------------------------------
# cookies 体检(不连网)
# --------------------------------------------------------------------------
def test_cookies_state_reports_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES", "")
    assert platform_video.cookies_state()["configured"] is False
    assert "未配置" in platform_video.cookies_state()["note"]
    monkeypatch.setattr(config, "YTDLP_COOKIES", str(tmp_path / "nope"))
    assert platform_video.cookies_state()["configured"] is False
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "bilibili.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "YTDLP_COOKIES", str(tmp_path / "c"))
    assert platform_video.cookies_state() == {"configured": True, "files": 1,
                                              "note": "已配置(1 份 cookies 文件)"}


def test_best_short_side_reads_formats():
    info = {"formats": [{"vcodec": "none", "height": 2160},
                        {"vcodec": "avc1", "width": 852, "height": 480},
                        {"vcodec": "hev1", "width": 1080, "height": 1920}]}
    assert platform_video.best_short_side(info) == 1080
    assert platform_video.best_short_side({"width": 1280, "height": 720}) == 720
    assert platform_video.best_short_side({}) == 0


def test_cookies_check_without_pool_or_network(files, fresh_db, monkeypatch):
    def boom(url):
        raise AssertionError("不该连网")

    monkeypatch.setattr(config, "YTDLP_COOKIES", "")
    monkeypatch.setattr(platform_video, "probe", boom)
    data = footage_health.check()           # 池子里没有官方投稿:不探
    assert data["configured"] is False
    assert all(data["platforms"][p]["error"] for p in footage_health.PLATFORMS)
    st = footage_health.status()
    assert st["cookies"]["configured"] is False and not st["low_res_only"]


def test_guest_only_bilibili_raises_banner(files, monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES", "")
    monkeypatch.setattr(footage_health, "low_res_share_cached", lambda: {"low_res_ratio": 0.8})
    footage_health.record_probe("bilibili", URL, 480)
    st = footage_health.status()
    assert st["low_res_only"] and st["best_height"]["bilibili"] == 480
    assert "cookies 未配置" in st["note"] and "80%" in st["note"]


# --------------------------------------------------------------------------
# 重拉高清(library.upgrade)
# --------------------------------------------------------------------------
class FakePlatform:
    """假的 B 站:probe 给 1080p,下载写一个够大的文件;切段 / 打标签也是假的。"""

    def __init__(self, monkeypatch, frame: Path) -> None:
        self.downloads: list[str] = []
        self.tag_calls = 0
        self.tag_ok = True
        self.fail: Exception | None = None
        self.frame = frame
        monkeypatch.setattr(platform_video, "probe", self.probe)
        monkeypatch.setattr(platform_video, "download_segment", self.download)
        monkeypatch.setattr(upgrade, "split_window", self.split)
        monkeypatch.setattr(upgrade, "tag_items", self.tag)
        monkeypatch.setattr(upgrade, "mark_official_duplicates", lambda lib: 0)
        monkeypatch.setattr(upgrade, "_pause", lambda seconds: None)
        monkeypatch.setattr(footage_health, "load_check", lambda: {"effective": True})
        monkeypatch.setattr(footage_health, "record_probe", lambda *a, **k: None)

    def probe(self, url: str) -> dict:
        return {"formats": [{"vcodec": "avc1", **HD}]}

    def download(self, url: str, out: Path, *, start: float, seconds: float,
                 timeout_hint: float = 0) -> Path:
        if self.fail is not None:
            raise self.fail
        self.downloads.append(out.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\0" * 30_000)
        return out

    def split(self, src: Source, raw: Path, k: int, frame_dir: Path, *, tag: str = "") -> list:
        return [Item(id=f"{src.id}_{tag}{k}_{n:03d}", kind="video", path=str(raw),
                     frame=str(self.frame), source=src.id, url=src.url, official=src.official,
                     start=a, end=b, motion=0.3, origin="library:bilibili", **HD)
                for n, (a, b) in enumerate(((0.1, 2.9), (3.1, 5.9)))]

    def tag(self, lib, items, llm, errors, *a, **k) -> int:
        self.tag_calls += 1
        if not self.tag_ok:
            return 0
        for i in items:
            i.tagged, i.view, i.target = True, "exterior_45", "yes"
        return len(items)


@pytest.fixture()
def low_pool(files, fresh_db, monkeypatch):
    vid, still = files
    lib = Library(subject=SUBJECT)
    lib.sources = [Source(url=URL, id=SID, platform="bilibili", official=True, status="done",
                          duration=60.0, title="测试车官方首发")]
    lib.items = [_video(f"{SID}_0_000", vid, 0, 3, source=SID, **SD),
                 _video(f"{SID}_0_001", vid, 3, 6, source=SID, **SD)]
    lib.save()
    return FakePlatform(monkeypatch, still)


def test_upgrade_replaces_low_res_source_once(low_pool):
    fake = low_pool
    assert [s.id for s in upgrade.candidates(Library.load(SUBJECT))] == [SID]

    out = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert out["result"] == "done" and out["height"] == 1080
    assert (out["added"], out["tagged"], out["retired"]) == (2, 2, 2)
    assert len(fake.downloads) == 1

    lib = Library.load(SUBJECT)
    old = {i.id: i for i in lib.items if "_h" not in i.id}
    new = {i.id: i for i in lib.items if "_h" in i.id}
    assert all(i.skipped == "replaced" for i in old.values())
    assert all(Path(i.path).is_file() for i in old.values())        # 老文件留着
    assert {i.replaces for i in new.values()} == set(old)
    assert all(i.usable and i.hd for i in new.values())
    src = lib.source(SID)
    assert src is not None and src.upgraded_height == 1080 and not src.upgrade_error

    # 第二次:不再是候选,不下载、不打标签,池子不变
    calls = fake.tag_calls
    again = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert again["result"] == "skip"
    assert upgrade.upgrade([SUBJECT], llm=object())["sources"] == 0
    assert len(fake.downloads) == 1 and fake.tag_calls == calls
    assert len(Library.load(SUBJECT).items) == 4


def test_upgrade_resumes_without_redownloading(low_pool):
    """中断在"下好了、还没打上标签"之后:重跑只补打标签,不重下;老段等新段打上标签才退役。"""
    fake = low_pool
    fake.tag_ok = False
    first = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert first["result"] == "partial" and first["retired"] == 0
    lib = Library.load(SUBJECT)
    assert not any(i.skipped == "replaced" for i in lib.items)
    assert "老段先留着" in (lib.source(SID).upgrade_error or "")

    fake.tag_ok = True
    second = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert second["result"] == "done" and second["retired"] == 2 and second["added"] == 0
    assert len(fake.downloads) == 1


def test_upgrade_reuses_raw_file_left_by_interrupted_run(low_pool):
    raw = Library.dir_for(SUBJECT) / "raw" / f"{SID}_h0.mp4"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"\0" * 30_000)
    out = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert out["result"] == "done" and low_pool.downloads == []


def test_upgrade_rate_limit_trips_cooldown_and_stops_platform(low_pool):
    from app.providers import cooldown

    low_pool.fail = platform_video.PlatformError("ERROR: HTTP Error 412: Precondition Failed")
    with pytest.raises(upgrade.Stop) as exc:
        upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert exc.value.platform == "bilibili" and exc.value.reason == "cooldown"
    with pytest.raises(cooldown.Cooling):
        cooldown.check(footage_health.cooldown_key("bilibili"))
    # 整轮重拉:撞了冷却就停下这个平台,不报错、不再请求
    low_pool.fail = None
    out = upgrade.upgrade([SUBJECT], llm=object())
    assert low_pool.downloads == [] and out["stopped"].get("bilibili")


def test_upgrade_low_platform_height_is_remembered(low_pool, monkeypatch):
    monkeypatch.setattr(low_pool, "probe", lambda url: {"formats": [{"vcodec": "avc1", **SD}]})
    monkeypatch.setattr(platform_video, "probe", low_pool.probe)
    monkeypatch.setattr(footage_health, "load_check", lambda: {"effective": True})
    out = upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert out["result"] == "low" and low_pool.downloads == []
    lib = Library.load(SUBJECT)
    assert not upgrade.needs_upgrade(lib, lib.source(SID))          # 一周内不再试


def test_upgrade_without_cookies_stops_bilibili(low_pool, monkeypatch):
    monkeypatch.setattr(platform_video, "probe",
                        lambda url: {"formats": [{"vcodec": "avc1", **SD}]})
    monkeypatch.setattr(footage_health, "load_check", lambda: {})
    with pytest.raises(upgrade.Stop) as exc:
        upgrade.upgrade_source(SUBJECT, SID, llm=object())
    assert exc.value.reason == "cookies" and low_pool.downloads == []


def test_submit_upgrade_is_single_flight(fresh_db):
    assert upgrade.submit_upgrade(SUBJECT) is not None
    assert upgrade.submit_upgrade("测试 车") is None          # 同一个池子
    assert upgrade.submit_upgrade(all_=True) is not None
