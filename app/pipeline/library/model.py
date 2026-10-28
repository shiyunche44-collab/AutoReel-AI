"""素材池数据模型:Source / Item / Library,以及按池子算的时长统计。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Sequence
from ... import config
from ...categories import CategoryProfile, Vocab, load_profile
from ...providers import platform_video
from .. import media_check


LIB_ROOT_NAME = "library"

#: 池文件写入锁:按车型一把。
#:
#: 素材池是**跨项目共享**的 —— 同一台车的两个项目各起一个后台构建线程,
#: 各自 load 一份旧副本、各自 save,后写的覆盖先写的,先写线程刚打上的
#: 标签就没了。写盘前按车型串行化。
_save_locks: dict[str, threading.Lock] = {}
_save_locks_guard = threading.Lock()


def _save_lock(subject: str) -> threading.Lock:
    """取某个车型的池写锁。"""
    with _save_locks_guard:
        lock = _save_locks.get(subject)
        if lock is None:
            lock = threading.Lock()
            _save_locks[subject] = lock
        return lock


def profile_of(lib: "Library") -> CategoryProfile:
    """建池的品类。旧池子没记品类的按汽车(当时只有汽车)。"""
    try:
        return load_profile(lib.category or "auto_long")
    except Exception:  # noqa: BLE001 - profile 被删了也不能让取材崩掉
        return load_profile("auto_long")


def vocab_of(lib: "Library") -> Vocab:
    return profile_of(lib).vocab

#: 素材视角(打标签的 view)、视角 → 镜头 angle、背景偏好分组都随品类变,
#: 在 profile 的 vocab 里(见 `vocab_of`)。池子记着自己是哪个品类建的。

#: 整条下载的上限。更长的源分窗下载,免得一场两小时的发布会占满硬盘
WHOLE_MAX_SECONDS = 6 * 60
#: 长源分几个窗、每窗多长
WINDOWS_PER_LONG_SOURCE = 8
WINDOW_SECONDS = 40.0
#: 场景检测阈值。0.3 是 ffmpeg 社区的常用值:硬切都能检出,慢推拉不会误切
SCENE_THRESHOLD = 0.3
#: 短于这个的段不打标签(闪白、转场),但允许夹在两个好段中间
MIN_TAG_SECONDS = 1.0
#: 每个源最多给多少段打标签。一条三分钟的快剪宣传片能切出上百段
MAX_TAGGED_PER_SOURCE = 60
#: 自动发现最多收多少个源
MAX_AUTO_SOURCES = 10
#: 官图每个分类抓多少张
#: 一次交给 LLM 结构化的描述条数。太多会超上下文、也更容易漏编号
TAG_BATCH = 12
#: 隔多久重新搜一次新源(新车上市后官方号会陆续发片)
REDISCOVER_SECONDS = 7 * 24 * 3600
#: 每个源先打这么多段当样本;样本里废段(拦掉的、主持人出镜的)占比 ≥ EARLY_STOP_RATIO
#: 就不再给这个源剩下的段打标签。B 站测评源常常整条都是主持人口播,
#: 实测小米 YU7 池子里四成视觉调用花在这类段上
EARLY_STOP_SAMPLE = 8
EARLY_STOP_RATIO = 0.7
#: 解说轨的"够用":**确认是这台车**的干净视频秒数(`confirmed_seconds`)达到它,就不再给
#: 视频段打标签。只算确认过的:没认出车型的第三方段凑够了秒数,背景照样会配错车
ENOUGH_USABLE_SECONDS = 240.0
#: 多车横评:每台车只占全片的一小段,轻量池够这么多秒就停
ROUNDUP_ENOUGH_SECONDS = 90.0
#: 官方号的源单独计数,不和第三方源抢 `MAX_AUTO_SOURCES` 的名额:
#: 官方画面一定是这台车、没有别人的台标,是背景的主力
MAX_OFFICIAL_SOURCES = 8
#: 其中官网视频最多几个(官网一页常有十来段短视频,都收会把官方号的宣传片挤掉)
MAX_SITE_SOURCES = 4
#: 官方源到这个数后,第三方源最多再收几个(只当补位)
OFFICIAL_ENOUGH_SOURCES = 4
THIRD_PARTY_WITH_OFFICIAL = 3
#: 「确认是这台车」的视频秒数低于它,不等满 7 天就重新找源(每次至少隔这么久)
CONFIRMED_VIDEO_TARGET = 180.0
REDISCOVER_THIN_SECONDS = 6 * 3600
#: 人工「重建素材池」两次找源之间至少隔这么久
REDISCOVER_MIN_GAP = 30 * 60
#: 多车横评:每台车最多拉几个平台视频源
ROUNDUP_MAX_SOURCES = 3
STILL_SECONDS = 4.0
#: 代表帧 dHash 的汉明距离不超过它算同一个画面(同源快剪里的重复镜头),只打一个
DUP_BITS = 6

#: 拦掉的问题(text_overlay 只记不拦)
BLOCKING = {"not_target_model", "no_subject", "cropped", "unclear", "poster"}


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------
@dataclass
class Source:
    """一个素材源:一条平台视频。"""
    url: str
    id: str = ""
    platform: str = ""
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    official: bool = False
    #: auto = 自动发现;manual = 手动贴的链接
    added_by: str = "auto"
    #: pending / done / failed
    status: str = "pending"
    error: str = ""
    #: 静态角标检测(logo_detect):checked = 下过结论;boxes = 归一化 [x0, y0, x1, y1]
    logo_checked: bool = False
    logo_boxes: list[list[float]] = field(default_factory=list)


@dataclass
class Item:
    """池子里的一段视频或一张图。"""
    id: str
    #: video / still
    kind: str
    #: 本地文件:视频是下载下来的原片(或原片的一个窗),图是原图
    path: str
    #: 打标签用的那一帧
    frame: str = ""
    #: 所属源的 id(官图为空)
    source: str = ""
    #: 出处页,用于追溯与 Referer
    url: str = ""
    platform: str = ""
    official: bool = False
    origin: str = ""
    license: str = ""
    #: 段在 `path` 里的起止秒
    start: float = 0.0
    end: float = 0.0
    #: 以下是打标签的结果;`tagged` 为 False 表示还没看过
    tagged: bool = False
    view: str = ""
    #: yes / no / unknown —— 是不是目标车型
    target: str = "unknown"
    desc: str = ""
    problem: str = ""
    #: 不打标签 / 不用的原因:source_rejected = 所在源的样本废段太多;
    #: frame_dark / frame_flat = 本地体检取的帧全黑 / 没内容(见 media_check.frame_verdict);
    #: logo_uncroppable = 第三方源的角标裁不掉;dup_official = 搬运的官方画面
    skipped: str = ""
    #: 技术参数(视频按所在的窗文件,图按原图);0 / 空 = 还没读过(老池子回填前)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    #: 视频流码率,kbps
    bitrate: int = 0
    vcodec: str = ""
    #: 本地体检的边缘方差中位数;-1 = 没体检过
    sharp: float = -1.0
    #: 按所在源检测到的角标,当背景时四边要收的比例(同 Piece.crop_edge);-1 = 没检测过
    logo_crop: float = -1.0
    #: 第三方段和某条官方段是同一画面时,那条官方段的 id(见 mark_official_duplicates)
    dup_of: str = ""

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def low_res(self) -> bool:
        """视频短边不到 720:放到 1080p 成片里糊成一片,不当背景。"""
        return self.kind == "video" and media_check.is_low_res_video(self.width, self.height)

    @property
    def low_bitrate(self) -> bool:
        """码率低于该分辨率 / 编码的下限:只排序靠后,不拦。"""
        return self.kind == "video" and media_check.is_low_bitrate(
            self.width, self.height, self.bitrate, self.vcodec)

    @property
    def small_still(self) -> bool:
        """静图长边不到 1280:不当全屏背景。"""
        return self.kind == "still" and media_check.is_small_still(self.width, self.height)

    @property
    def usable(self) -> bool:
        return self.tagged and self.target != "no" and self.problem not in BLOCKING \
            and self.view not in ("people", "text", "other", "") and not self.low_res \
            and not self.skipped


@dataclass
class Library:
    subject: str
    sources: list[Source] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    discovered_at: float = 0.0
    gallery_at: float = 0.0
    #: 抓过的静图来源名(profile 新加了来源,老池子下次构建时只补抓新的那几个)
    galleries_done: list[str] = field(default_factory=list)
    updated_at: float = 0.0
    #: 建池的品类(profile 名)。空 = 旧池子,按汽车处理
    category: str = ""
    #: 累计花了多少次视觉模型调用(打标签),给项目页看素材池的成本
    vlm_calls: int = 0

    # ---- 存取 ----
    @staticmethod
    def dir_for(subject: str) -> Path:
        return config.DATA_DIR / LIB_ROOT_NAME / slug(subject)

    @property
    def dir(self) -> Path:
        return self.dir_for(self.subject)

    @classmethod
    def load(cls, subject: str) -> "Library":
        path = cls.dir_for(subject) / "library.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(subject=subject)
        src_names = {f.name for f in fields(Source)}
        item_names = {f.name for f in fields(Item)}
        return cls(
            subject=subject,
            sources=[Source(**{k: v for k, v in s.items() if k in src_names})
                     for s in raw.get("sources") or []],
            items=[Item(**{k: v for k, v in i.items() if k in item_names})
                   for i in raw.get("items") or []],
            discovered_at=float(raw.get("discovered_at") or 0.0),
            gallery_at=float(raw.get("gallery_at") or 0.0),
            # 记这个字段之前的池子:抓过的就是当时唯一的汽车之家
            galleries_done=list(raw.get("galleries_done") or (
                ["autohome"] if raw.get("gallery_at") else [])),
            updated_at=float(raw.get("updated_at") or 0.0),
            category=str(raw.get("category") or ""),
            vlm_calls=int(raw.get("vlm_calls") or 0),
        )

    def save(self) -> None:
        """写盘。后台构建拿着的是旧副本,期间用命令行 `add` 加的链接
        会被它覆盖掉 —— 所以写之前把盘上多出来的源并进来。

        写盘按**车型串行**:池是跨项目共享的,同一台车的两个项目各起一个
        后台构建线程时,谁后写谁覆盖,先写线程刚打上的标签就没了。
        """
        with _save_lock(self.subject):
            self.dir.mkdir(parents=True, exist_ok=True)
            on_disk = Library.load(self.subject)
            mine = {s.url for s in self.sources}
            self.sources += [s for s in on_disk.sources if s.url not in mine]
            self.updated_at = time.time()
            payload = {
                "subject": self.subject,
                "discovered_at": self.discovered_at,
                "gallery_at": self.gallery_at,
                "galleries_done": self.galleries_done,
                "updated_at": self.updated_at,
                "category": self.category or on_disk.category,
                "vlm_calls": self.vlm_calls,
                "sources": [asdict(s) for s in self.sources],
                "items": [asdict(i) for i in self.items],
            }
            tmp = self.dir / "library.json.tmp"
            # 原子替换:读者要么看到旧内容,要么看到新内容,不会看到半截。
            # 池文件被截断的话,`Library.load` 会静默返回一个空池 ——
            # 整台车的素材标签就这么没了,而且查不出来。
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.dir / "library.json")

    # ---- 增改 ----
    def add_source(self, src: Source) -> bool:
        """按地址去重;已有的源只补全缺的元数据。返回是否新增。"""
        src.url = normalise_url(src.url)
        for old in self.sources:
            if old.url == src.url:
                old.official = old.official or src.official
                old.title = old.title or src.title
                old.uploader = old.uploader or src.uploader
                old.duration = old.duration or src.duration
                if src.added_by == "manual":
                    old.added_by = "manual"
                return False
        src.id = src.id or short_id(src.url)
        self.sources.append(src)
        return True

    def source(self, source_id: str) -> Source | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def stats(self) -> dict[str, Any]:
        videos = [i for i in self.items if i.kind == "video"]
        stills = [i for i in self.items if i.kind == "still"]
        views: dict[str, int] = {}
        for i in self.items:
            if i.usable:
                views[i.view] = views.get(i.view, 0) + 1
        sized = [i for i in videos if i.width and i.height]
        cover = coverage_seconds(self)
        return {
            "sources": len(self.sources),
            "sources_done": sum(1 for s in self.sources if s.status == "done"),
            "sources_failed": sum(1 for s in self.sources if s.status == "failed"),
            "sources_pending": sum(1 for s in self.sources if s.status == "pending"),
            "video_segments": len(videos),
            "video_usable": sum(1 for i in videos if i.usable),
            "stills": len(stills),
            "stills_usable": sum(1 for i in stills if i.usable),
            "untagged": sum(1 for i in self.items if not i.tagged and not i.skipped),
            "skipped": sum(1 for i in self.items if i.skipped),
            "vlm_calls": self.vlm_calls,
            "usable_seconds": round(usable_seconds(self), 1),
            "confirmed_seconds": round(confirmed_seconds(self), 1),
            "confirmed_video_seconds": round(confirmed_seconds(self, video_only=True), 1),
            "official_sources": sum(1 for s in self.sources if s.official),
            "usable_by_view": dict(sorted(views.items(), key=lambda kv: -kv[1])),
            "video_by_coverage": cover,
            "missing_coverage": [k for k, v in cover.items() if v < COVERAGE_MIN_SECONDS],
            # 清晰度:视频段按短边分档(没读到尺寸的不计),码率偏低只排序靠后
            "video_res": {
                "1080p+": sum(1 for i in sized if min(i.width, i.height) >= 1080),
                "720p": sum(1 for i in sized if 720 <= min(i.width, i.height) < 1080),
                "低清": sum(1 for i in sized if i.low_res),
            },
            "low_bitrate": sum(1 for i in videos if i.usable and i.low_bitrate),
            "small_stills": sum(1 for i in stills if i.small_still),
            # 台标检测:测过的源、带角标的源、裁不掉被整源弃用的段、第三方搬运官方画面的段
            "logo_checked": sum(1 for s in self.sources if s.logo_checked),
            "logo_sources": sum(1 for s in self.sources if s.logo_boxes),
            "logo_dropped": sum(1 for i in videos if i.skipped == "logo_uncroppable"),
            "dup_official": sum(1 for i in videos if i.skipped == "dup_official"),
        }


def slug(subject: str) -> str:
    """车型名 → 目录名。去空格、转小写:"小米 YU7" 与 "小米YU7" 是同一个池子。"""
    s = re.sub(r"\s+", "", subject or "").lower()
    s = re.sub(r"[^\w一-鿿-]+", "_", s)
    return s or "unknown"


def short_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def normalise_url(url: str) -> str:
    """去掉分享链接上的跟踪参数,同一条视频才能去重。B 站保留分 P 参数。"""
    url = url.strip()
    m = re.match(r"(https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+))"
                 r"/?(?:\?(?:.*&)?(p=\d+))?", url)
    if m:
        return m.group(1).replace("://m.", "://www.") + (f"?{m.group(2)}" if m.group(2) else "")
    m = re.match(r"https?://(?:www\.|m\.)?youtube\.com/watch\?(?:.*&)?v=([0-9A-Za-z_-]{11})", url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    m = re.match(r"https?://youtu\.be/([0-9A-Za-z_-]{11})", url)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    m = re.match(r"(https?://(?:www\.)?douyin\.com/video/\d+)", url)
    if m:
        return m.group(1)
    return platform_video.tiktok_url(url) or url


def platform_of(url: str) -> str:
    host = platform_video.domain_of(url)
    for key, name in (("bilibili", "bilibili"), ("b23.tv", "bilibili"), ("youtu", "youtube"),
                      ("douyin", "douyin"), ("ixigua", "douyin"), ("tiktok", "tiktok"),
                      ("vimeo", "vimeo")):
        if key in host:
            return name
    return "web"


def _has_host(item: Item) -> bool:
    """描述里点名有人在讲解 / 介绍(评测博主口播)。"""
    return bool(re.search(r"主持|讲解|介绍|解说|博主|主播|口播|出镜|对着镜头|评测员|自述|演示", item.desc or ""))


def is_waste(item: Item) -> bool:
    """打过标签但当背景也用不上的段。烧录字幕不算:解说轨裁掉底部照样能用。"""
    return item.tagged and (not item.usable or _has_host(item))


def usable_seconds(lib: Library) -> float:
    """池子里能当解说轨背景的总量(秒):可用视频的时长 + 可用官图 × STILL_SECONDS。"""
    total = 0.0
    angle_of_view = vocab_of(lib).angle_of_view
    for i in lib.items:
        if not i.usable or _has_host(i):
            continue
        if i.kind == "video":
            total += i.length if i.length >= FILL_MIN_PIECE else 0.0
        elif i.view in angle_of_view:
            total += STILL_SECONDS
    return total


def confirmed_seconds(lib: Library, *, video_only: bool = False) -> float:
    """池子里「确认是这台车、能放心当背景」的总量(秒):官方来源或认出是这台车,
    而且没有主持人出镜 / 第三方台标。开场拍只用这部分;它少了,背景就只能靠官图和没认出的片段。"""
    hosts = _host_sources(lib.items)
    angle_of_view = vocab_of(lib).angle_of_view
    total = 0.0
    for i in lib.items:
        if not i.usable or not _confirmed(i) or _hard_bad(i, hosts):
            continue
        if i.kind == "video":
            total += i.length if i.length >= FILL_MIN_PIECE else 0.0
        elif not video_only and i.view in angle_of_view:
            total += STILL_SECONDS
    return total


#: 门禁 A「先补素材」的提示线(只提示,不拦):确认视频秒数低于它,
#: 或品类必备视角里缺这么多个的视频
FOOTAGE_HINT_SECONDS = 300.0
FOOTAGE_HINT_MISSING_VIEWS = 2
#: 一个必备视角至少这么多秒确认视频才算"有"
COVERAGE_MIN_SECONDS = 8.0


def coverage_seconds(lib: Library) -> dict[str, float]:
    """品类必备视角(`vocab.coverage_views`)各有多少秒确认视频(口径同 `confirmed_seconds`)。"""
    groups = vocab_of(lib).coverage_views
    hosts = _host_sources(lib.items)
    out = {label: 0.0 for label in groups}
    for i in lib.items:
        if (i.kind != "video" or not i.usable or not _confirmed(i) or _hard_bad(i, hosts)
                or i.length < FILL_MIN_PIECE):
            continue
        for label, views in groups.items():
            if i.view in views:
                out[label] += i.length
    return {k: round(v, 1) for k, v in out.items()}


def footage_gaps(stats: dict[str, Any]) -> list[str]:
    """按 `Library.stats()` 判素材够不够,返回该提示的原因(空 = 够)。"""
    reasons = []
    secs = stats.get("confirmed_video_seconds") or 0.0
    if secs < FOOTAGE_HINT_SECONDS:
        reasons.append(f"确认是本车的视频只有 {int(secs)} 秒(建议 ≥ {int(FOOTAGE_HINT_SECONDS)})")
    missing = stats.get("missing_coverage") or []
    if len(missing) >= FOOTAGE_HINT_MISSING_VIEWS:
        reasons.append(f"缺{'、'.join(missing)}的视频")
    return reasons
#: 短于这个的段不用(闪白、转场)
FILL_MIN_PIECE = 1.2


def _confirmed(item: Item) -> bool:
    """开场 / 结论拍只用的素材:官方来源,或认出就是这台车的。"""
    return item.official or item.target == "yes"


def _third_party(item: Item) -> bool:
    """别家账号的视频(博主、媒体、搬运):只当正文拍的最后兜底。"""
    return item.kind == "video" and not item.official


def _third_party_ok(item: Item) -> bool:
    """第三方视频能进正文候选的前提:源跑过台标检测且能裁掉(`check_logos` 下了结论)。"""
    return item.logo_crop >= 0


#: 非官方来源里主持人出镜的段占比 ≥ 这个,整个源按"评测博主视频"处理,源里的段一律不当背景
#: (VLM 的描述常漏标:同一支视频里漏掉的段多半也有人入镜、带博主水印)
HOST_SOURCE_RATIO = 0.15


def _host_sources(items: Sequence[Item]) -> set[str]:
    total: dict[str, int] = {}
    hosts: dict[str, int] = {}
    for i in items:
        if i.kind != "video" or not i.tagged or i.official:
            continue
        total[i.source] = total.get(i.source, 0) + 1
        hosts[i.source] = hosts.get(i.source, 0) + (1 if _has_host(i) else 0)
    return {s for s, n in total.items() if s and hosts[s] / n >= HOST_SOURCE_RATIO}


def _hard_bad(item: Item, host_sources: set[str] = frozenset()) -> bool:
    """背景里宁可不用的素材:主持人出镜(裁不掉,和旁白打架)、评测博主视频里的段,
    或第三方的角标水印(别家账号的台标,发出去等于挂别人的名)。官方来源的角标不算。"""
    if _has_host(item) or (item.kind == "video" and item.source in host_sources):
        return True
    return item.kind == "video" and item.problem == "text_overlay" and not item.official
