"""素材池:按车型先把真实素材攒好、看过、打好标签,取材时直接从池子里挑。

为什么要有素材池
----------------
旧的取材是"每个镜头现搜现下":拿分镜的检索词去 B 站搜,在搜到的长视频里
**按固定起点**切 6 秒,再抽一帧质检。实测小米 YU7 一项目,10 个镜头只有
3 个用上真实素材 —— 不是网上没有,是切的位置不对:宣传片 15% 处可能正好是
字幕卡,评测视频 40% 处可能是主持人口播。质检毙掉之后换个起点再赌一次,
又慢又贵,赌输就退回 AI。

素材池把顺序倒过来:
1. **攒**:按车型找一批源(品牌官方号视频、宣传片、实拍、手动贴的链接、
   汽车之家官图),整条下载或分窗下载;
2. **切**:ffmpeg 场景检测,在**原片自己的剪辑点**切开 —— 每一段都是
   完整的一个镜头,不会切在转场中间;
3. **看**:每段抽中间帧问一次视觉模型,打上视角(前脸 / 侧面 / 车尾 / 行驶 /
   内饰 / 中控屏 / 细节)、是不是这台车、有没有字幕海报等问题;
4. **挑**:取材时按镜头要的视角,从标好的段里拼出一个够长的连续窗口。

池子按车型存在 `data/library/<车型>/`,跨项目复用:同一台车做第二条视频时,
攒和看的成本是零。

增量构建
--------
`build()` 每次只做还没做的事:没下的源下、没打标签的段打标签、没抓的官图抓。
有时长预算(`LIBRARY_BUILD_BUDGET`),超时就停在当前进度,下次接着做。
中途任何一步失败都只是这个源 / 这一段没有,不抛异常。

手动链接
--------
自动搜索搜不到的好素材(抖音官方号、某条 B 站评测的某一段),用
`python -m app.pipeline.library add 小米YU7 <链接>...` 加进来,下次 build 时下载。
抖音只能走这条路:抖音没有可用的站内搜索,而且抖音素材剪回抖音容易被判搬运。
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..categories import CategoryProfile, Vocab, load_profile
from ..providers import clips as clips_mod
from ..providers import images, official, platform_video
from . import vision_verdict

LIB_ROOT_NAME = "library"


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
#: 解说轨的"够用":可用视频秒数 + 可用官图 × STILL_SECONDS 达到它,就不再给视频段
#: 打标签。背景只要是这台车、画面干净,一条片子 4 分钟的背景量就够轮换
ENOUGH_USABLE_SECONDS = 240.0
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
    #: 不打标签的原因(source_rejected = 所在源的样本废段太多)
    skipped: str = ""

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def usable(self) -> bool:
        return self.tagged and self.target != "no" and self.problem not in BLOCKING \
            and self.view not in ("people", "text", "other", "")


@dataclass
class Library:
    subject: str
    sources: list[Source] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    discovered_at: float = 0.0
    gallery_at: float = 0.0
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
            updated_at=float(raw.get("updated_at") or 0.0),
            category=str(raw.get("category") or ""),
            vlm_calls=int(raw.get("vlm_calls") or 0),
        )

    def save(self) -> None:
        """写盘。后台构建拿着的是旧副本,期间用命令行 `add` 加的链接
        会被它覆盖掉 —— 所以写之前把盘上多出来的源并进来。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        on_disk = Library.load(self.subject)
        mine = {s.url for s in self.sources}
        self.sources += [s for s in on_disk.sources if s.url not in mine]
        self.updated_at = time.time()
        payload = {
            "subject": self.subject,
            "discovered_at": self.discovered_at,
            "gallery_at": self.gallery_at,
            "updated_at": self.updated_at,
            "category": self.category or on_disk.category,
            "vlm_calls": self.vlm_calls,
            "sources": [asdict(s) for s in self.sources],
            "items": [asdict(i) for i in self.items],
        }
        tmp = self.dir / "library.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.dir / "library.json")

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
            "usable_by_view": dict(sorted(views.items(), key=lambda kv: -kv[1])),
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


# --------------------------------------------------------------------------
# 攒:发现源
# --------------------------------------------------------------------------
def english_name(subject: str, aliases: Sequence[str]) -> str:
    """YouTube 用英文名搜。别名里有带拉丁字母且含主体型号的(Xiaomi YU7)就用它。"""
    latin = re.findall(r"[A-Za-z0-9]+", subject)
    code = [t for t in latin if re.search(r"\d", t) or t.isupper()]
    for a in aliases:
        if re.search(r"[A-Za-z]", a) and code and all(c.lower() in a.lower() for c in code):
            return a
    brand = next((a for a in aliases if re.fullmatch(r"[A-Za-z][A-Za-z ]+", a)), "")
    return " ".join(x for x in (brand.split()[0] if brand else "", *latin) if x) or subject


def discovery_queries(subject: str, en: str, vocab: Vocab) -> tuple[list[str], list[str]]:
    """(中文检索词, 英文检索词)。挑的都是"主体多人少"的视频类型,模板在 profile 里。"""
    zh = [q.format(subject=subject, en=en) for q in vocab.discovery_zh]
    en_q = [q.format(subject=subject, en=en) for q in vocab.discovery_en]
    return zh, en_q


def discover(lib: Library, aliases: Sequence[str], errors: list[str], *,
             max_sources: int = MAX_AUTO_SOURCES, tiktok_handles: Sequence[str] = ()) -> int:
    """搜一轮新源加进池子。官方号的视频优先。返回新增的源数。"""
    subject = lib.subject
    vocab = vocab_of(lib)
    en = english_name(subject, aliases)
    hits: list[platform_video.PlatformHit] = []
    if aliases:
        hits += official.platform_official(subject, aliases, platforms=config.MEDIA_PLATFORMS,
                                           errors=errors, markers=vocab.official_markers)
    if tiktok_handles and "tiktok" in config.MEDIA_PLATFORMS:
        hits += official.tiktok_official(subject, tiktok_handles, names=[en],
                                         aliases=aliases, errors=errors)
    zh_q, en_q = discovery_queries(subject, en, vocab)
    zh_platforms = [p for p in config.MEDIA_PLATFORMS if p == "bilibili"]
    en_platforms = [p for p in config.MEDIA_PLATFORMS if p in ("youtube", "tiktok")]
    for q in zh_q:
        hits += platform_video.search(q, platforms=zh_platforms, errors=errors)
    for q in en_q:
        hits += platform_video.search(q, platforms=en_platforms, errors=errors)
    for h in hits:
        h.official = h.official or bool(aliases and platform_video.is_official(
            h, aliases, markers=vocab.official_markers))
    ranked = rank_sources(_dedupe_hits(hits), subject, names=[en], vocab=vocab)

    have_auto = sum(1 for s in lib.sources if s.added_by == "auto")
    added = 0
    for h in ranked:
        if have_auto + added >= max_sources:
            break
        added += lib.add_source(Source(
            url=h.url, platform=h.platform, title=h.title, uploader=h.uploader,
            duration=h.duration, official=h.official,
        ))
    lib.discovered_at = time.time()
    return added


#: 标题里出现这些词的源不要:游戏画面、新闻事件、渲染猜想、玩具手办。
#: 品类专属的(汽车的 forza、车模……)在 profile 的 vocab.junk_words,两边合并
JUNK_TITLE_WORDS = (
    "游戏", "手办", "乐高", "lego", "渲染", "猜想", "曝光", "谍照", "事故", "起火",
    "维权", "系统更新", "投屏", "教程", "怎么",
)
#: 标题里出现这些词的排前面:广告、宣传片、实拍展示,画面里主体多人少。
#: 品类专属的在 vocab.showcase_words
SHOWCASE_TITLE_WORDS = (
    "宣传片", "广告", "tvc", "官方", "official", "commercial", "debut", "film",
    "实拍", "首发",
)
#: 官方号的长视频(发布会)也收:分窗下载,发布会里放的宣传片正是好素材
OFFICIAL_MAX_SECONDS = 3 * 3600


def rank_sources(hits: Sequence[platform_video.PlatformHit], subject: str, *,
                 names: Sequence[str] = (), vocab: Vocab | None = None
                 ) -> list[platform_video.PlatformHit]:
    """过滤 + 排序:官方号 → 广告 / 宣传片 / 实拍类标题 → 其余;口播类和垃圾标题不要。

    `names` 是车型的其他叫法(英文名):YouTube / TikTok 文案是英文的,
    "比亚迪海豹06" 认不出 "BYD SEAL 06"。
    """
    vocab = vocab or Vocab()
    junk = (*JUNK_TITLE_WORDS, *vocab.junk_words)
    showcase_words = (*SHOWCASE_TITLE_WORDS, *vocab.showcase_words)
    out: list[platform_video.PlatformHit] = []
    for h in hits:
        title = h.title.lower()
        limit = OFFICIAL_MAX_SECONDS if h.official else platform_video.MAX_SOURCE_SECONDS
        if h.duration and not (platform_video.MIN_SOURCE_SECONDS <= h.duration <= limit):
            continue
        if h.title and not any(platform_video.title_relevant(h.title, n)
                               for n in (subject, *names) if n):
            continue
        if any(w in title for w in junk):
            continue
        # 口播 / 直播类 rank 只是排后面,这里直接不要:攒素材要的是车。官方发布会除外
        if not h.official and any(w in title for w in platform_video._TALKY_WORDS):
            continue
        out.append(h)

    def _key(h: platform_video.PlatformHit) -> tuple[int, int]:
        showcase = any(w in h.title.lower() for w in showcase_words)
        return (0 if h.official else 1, 0 if showcase else 1)

    return sorted(out, key=_key)


def _dedupe_hits(hits: Iterable[platform_video.PlatformHit]) -> list[platform_video.PlatformHit]:
    seen: dict[str, platform_video.PlatformHit] = {}
    for h in hits:
        key = normalise_url(h.url)
        if key in seen:
            seen[key].official = seen[key].official or h.official
            continue
        seen[key] = h
    return list(seen.values())


def add_links(subject: str, urls: Iterable[str]) -> list[str]:
    """手动加链接。返回新加的地址。"""
    lib = Library.load(subject)
    added: list[str] = []
    for url in urls:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            continue
        if lib.add_source(Source(url=url, platform=platform_of(url), added_by="manual")):
            added.append(normalise_url(url))
        else:
            # 已有但失败过的,手动再加一次就重试
            for s in lib.sources:
                if s.url == normalise_url(url) and s.status == "failed":
                    s.status, s.error = "pending", ""
    lib.save()
    return added


#: 当图片直接收的扩展名
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
#: 一个详情页最多收几张(电商详情页动辄几十张长图,前面的主图最有用)
PAGE_IMAGES_MAX = 20


def add_images(subject: str, refs: Iterable[str], *, category: str = "",
               errors: list[str] | None = None) -> int:
    """手动加图:图片地址、本地图片文件,或商品详情页 / 官网产品页(抓页面里的大图)。
    入池后还没打标签,下次 build 时打 —— 认不出是目标产品的不当背景。返回新增张数。

    京东 / 天猫这类页面反爬重,抓不到就把图另存到本地再加。
    """
    errors = errors if errors is not None else []
    lib = Library.load(subject)
    if category:
        lib.category = category
    have = {i.url for i in lib.items if i.kind == "still"}
    added = 0
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        is_url = ref.startswith(("http://", "https://"))
        if not is_url and not Path(ref).is_file():
            errors.append(f"不是图片地址也不是本地文件:{ref[:80]}")
            continue
        path_part = ref.split("?", 1)[0].lower()
        if not is_url or path_part.endswith(_IMAGE_EXTS):
            urls, page = [ref if is_url else str(Path(ref).resolve())], ""
        else:
            body = clips_mod.fetch_page(ref)
            urls = official.extract_images(body, ref)[:PAGE_IMAGES_MAX] if body else []
            page = ref
            if not urls:
                errors.append(f"页面里没抓到图(可能有反爬,把图存到本地再加):{ref[:80]}")
                continue
        for url in urls:
            if url in have:
                continue
            item = _add_still(lib, url, errors, platform="manual", referer=page,
                              license=f"手动添加:{page or ref}")
            if item is not None:
                have.add(url)
                added += 1
    lib.save()
    return added


# --------------------------------------------------------------------------
# 攒 + 切:下载与场景检测
# --------------------------------------------------------------------------
def download_windows(duration: float) -> list[tuple[float, float]]:
    """要下载的 (起点, 时长)。短源整条下;长源在 8%–92% 区间均匀取几个窗
    (片头片尾多是 logo、口播、求三连)。"""
    if 0 < duration <= WHOLE_MAX_SECONDS:
        return [(0.0, duration)]
    if duration <= 0:
        return [(0.0, WHOLE_MAX_SECONDS)]
    lo, hi = duration * 0.08, duration * 0.92 - WINDOW_SECONDS
    n = WINDOWS_PER_LONG_SOURCE
    step = (hi - lo) / (n - 1) if n > 1 and hi > lo else 0.0
    return [(round(lo + step * k, 2), WINDOW_SECONDS) for k in range(n if step else 1)]


_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def scene_cuts(path: str | Path) -> list[float]:
    """原片自己的剪辑点(秒)。缩到 320 宽再算,一条三分钟 1080p 几秒钟就跑完。"""
    proc = subprocess.run(
        [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
         "-an", "-vf", f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    return sorted({round(float(t), 3) for t in _PTS_RE.findall(proc.stderr or "")})


def split_segments(cuts: Sequence[float], duration: float) -> list[tuple[float, float]]:
    """剪辑点 → 段。每段两头各让 0.1 秒,避开转场那一两帧。"""
    edges = [0.0, *[c for c in cuts if 0 < c < duration], duration]
    out: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        a2, b2 = a + 0.1, b - 0.1
        if b2 - a2 > 0.2:
            out.append((round(a2, 3), round(b2, 3)))
    return out


def extract_frame(video: str | Path, at: float, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return dest if proc.returncode == 0 and dest.is_file() else None


def ingest_source(lib: Library, src: Source, errors: list[str]) -> int:
    """下载一个源、按剪辑点切段、抽帧。返回新增的段数。失败把源标成 failed。"""
    raw_dir = lib.dir / "raw"
    frame_dir = lib.dir / "frames"
    try:
        if not src.duration or not src.title:
            info = platform_video.probe(src.url)
            src.duration = src.duration or float(info.get("duration") or 0.0)
            src.title = src.title or str(info.get("title") or "")
            src.uploader = src.uploader or str(info.get("uploader") or info.get("channel") or "")
        added = 0
        for k, (start, length) in enumerate(download_windows(src.duration)):
            try:
                raw = platform_video.download_segment(
                    src.url, raw_dir / f"{src.id}_{k}.mp4", start=start, seconds=length,
                    timeout_hint=600)
            except platform_video.PlatformError as exc:
                errors.append(f"素材池下载失败({src.url} 窗 {k}):{str(exc)[:160]}")
                continue
            dur = clips_mod.probe_duration(raw)
            if dur <= 0:
                continue
            segs = split_segments(scene_cuts(raw), dur)
            for n, (a, b) in enumerate(segs):
                item_id = f"{src.id}_{k}_{n:03d}"
                frame = ""
                if b - a >= MIN_TAG_SECONDS:
                    got = extract_frame(raw, (a + b) / 2, frame_dir / f"{item_id}.jpg")
                    frame = str(got) if got else ""
                lib.items.append(Item(
                    id=item_id, kind="video", path=str(raw), frame=frame, source=src.id,
                    url=src.url, platform=src.platform or platform_of(src.url),
                    official=src.official,
                    origin=f"library:{src.platform or platform_of(src.url)}",
                    license=f"{src.platform}:{src.uploader or '未知上传者'}",
                    start=a, end=b,
                ))
                added += 1
        if not added:
            raise platform_video.PlatformError("没有下载到任何可用的片段")
        src.status, src.error = "done", ""
        return added
    except Exception as exc:  # noqa: BLE001 - 一个源失败只是少一个源
        src.status, src.error = "failed", str(exc)[:200]
        errors.append(f"素材池源失败({src.url}):{str(exc)[:160]}")
        return 0


def ingest_gallery(lib: Library, errors: list[str], *,
                   spec: dict[str, tuple[str, ...]] | None = None) -> int:
    """profile 声明的静图来源(汽车之家官图、品牌官网 + 图片检索……)→ 池子里的图。
    返回新增张数。"""
    have = {i.url for i in lib.items if i.kind == "still"}
    added = 0
    for name in profile_of(lib).galleries:
        gallery = official.GALLERIES.get(name)
        if gallery is None:
            errors.append(f"未知静图来源:{name}")
            continue
        try:
            refs = gallery.fetch(lib.subject, spec or {}, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"素材池静图抓取失败({name}):{str(exc)[:120]}")
            continue
        for ref in refs:
            if ref.url in have:
                continue
            item = _add_still(lib, ref.url, errors, platform=name, referer=gallery.referer,
                              official=name != "product_images" or ref.origin.startswith(
                                  "official"),
                              license=ref.license, view=_GALLERY_VIEW.get(ref.angle, ""))
            if item is not None:
                have.add(ref.url)
                added += 1
    lib.gallery_at = time.time()
    return added


def _add_still(lib: Library, url: str, errors: list[str], *, platform: str, referer: str = "",
               official: bool = False, license: str = "", view: str = "") -> Item | None:
    """下载一张图进池子(未打标签)。失败记一条错误,返回 None。"""
    item_id = "img_" + short_id(url)
    dest = lib.dir / "stills" / f"{item_id}.jpg"
    headers = {"User-Agent": clips_mod.BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    try:
        if url.startswith(("http://", "https://")):
            images.download(url, dest, headers=headers)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(url).read_bytes())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"静图下载失败({url[:60]}):{str(exc)[:100]}")
        return None
    frame = _thumb(dest, lib.dir / "frames" / f"{item_id}.jpg")
    item = Item(
        id=item_id, kind="still", path=str(dest), frame=str(frame or dest), url=url,
        platform=platform, official=official, origin=f"library:{platform}",
        license=license, view=view,
    )
    lib.items.append(item)
    return item


#: 官图分类自带的视角,作为打标签前的初值(打标签会覆盖成更细的)
_GALLERY_VIEW = {"exterior_45": "exterior_45", "interior": "interior", "detail": "detail"}


def _thumb(src: Path, dest: Path) -> Path | None:
    """官图原图动辄几 MB,打标签只要一张 1280 宽的。"""
    try:
        from PIL import Image

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
            im.save(dest, "JPEG", quality=85)
        return dest
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# 看:打标签
# --------------------------------------------------------------------------
def tag_prompt(subject: str, vocab: Vocab) -> str:
    return (
        f"这张图来自「{subject}」相关的视频或图库。只依据图片内容回答,不要猜。"
        f"{vocab.tag_variant_note}请用文字逐条说明:\n"
        f"1. {vocab.tag_identify}\n"
        f"2. 拍的是哪个角度:{vocab.tag_angles}\n"
        f"3. {vocab.subject_short}是否完整在画面内,画面是否清晰;\n"
        "4. 有没有对白 / 解说字幕(画面下方的句子)、角落台标、水印、大段文字;"
        "是不是宣传海报、网页 / App 截图,或者是在拍大屏幕 / 电视里放的画面;\n"
        "5. 画面里有没有人,人占画面多大。\n"
        "最后用一句话概括这个画面。"
    )


TAG_SYSTEM = """你把视觉模型对若干图片的描述转成素材标签。目标{label}是「{subject}」。

只依据描述里**明确写出**的内容,保守,不要推理补全:
- target:{target_rule}
  描述里视觉模型自己给的"是 / 不是目标{label}"结论一律忽略,只看它对{target_cues}的描述。
- view,取最贴切的一个:
{view_guide}
  people(主体是人)、text(主体是文字卡 / 参数表 / 片头片尾)、other(风景、别的东西)。
- problem,按顺序取第一条命中的:target 为 "no" → "not_target_model";
  {no_subject}
  明确说{short}被画面边缘切掉(局部特写不算)→ "cropped";明确说模糊、过暗、看不清 → "unclear";
  宣传海报(大标题字、代言人摆拍)、网页 / App 截图,或{short}是**显示在大屏 / 屏幕 / 电视里**的
  翻拍画面(发布会舞台大屏、前景有观众)→ "poster";
  画面下方有对白或解说**字幕**(一行行句子)→ "subtitle";
  只有角落的台标、水印、小字标注 → "text_overlay";都没有 → ""。
- desc:一句中文概括画面(20 字以内),例如"{desc_example}"。
- 描述是报错、拒答或为空 → target "unknown",view "other",problem "unclear"。

输出 JSON 对象 {{"verdicts": [...]}},每张一项,字段 index(与输入编号一致)、
target、view、problem、desc。"""


def tag_system(subject: str, vocab: Vocab) -> str:
    return TAG_SYSTEM.format(
        subject=subject, label=vocab.subject_label, short=vocab.subject_short,
        target_rule=vocab.tag_target_rule, target_cues=vocab.tag_target_cues,
        view_guide=vocab.tag_view_guide, no_subject=vocab.tag_no_subject,
        desc_example=vocab.tag_desc_example)


def tag_items(lib: Library, todo: Sequence[Item], llm: Any, errors: list[str], *,
              deadline: float = 0.0, enough_seconds: float = 0.0) -> int:
    """给还没看过的段 / 图打标签。返回打上的个数(含继承重复画面的)。

    超过 `deadline` 就停;`enough_seconds` > 0 时,可用素材够了就不再打视频段。
    每批之后检查各源的样本,废段太多的源剩下的段直接跳过(见 `reject_sources`)。
    """
    done = inherit_duplicates(lib)
    vocab = vocab_of(lib)
    prompt = tag_prompt(lib.subject, vocab)
    system = tag_system(lib.subject, vocab)
    queue = list(todo)
    while queue:
        if deadline and time.monotonic() > deadline:
            break
        if enough_seconds and usable_seconds(lib) >= enough_seconds:
            queue = [i for i in queue if i.kind == "still"]
        batch = [i for i in queue[:TAG_BATCH] if not i.tagged and not i.skipped
                 and i.frame and Path(i.frame).is_file()]
        queue = queue[TAG_BATCH:]
        urls: list[str] = []
        slots: list[Item] = []
        for item in batch:
            try:
                urls.append(images.cli.upload_temp_url(item.frame))
                slots.append(item)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"素材池帧上传失败({item.id}):{str(exc)[:100]}")
        if not urls:
            continue
        verdicts, _, _ = vision_verdict.judge(urls, prompt, system, llm, errors, what="素材")
        lib.vlm_calls += len(urls)
        for item, v in zip(slots, verdicts):
            if not v:
                continue
            apply_tag(item, v, vocab.all_views)
            done += 1
        done += inherit_duplicates(lib)
        reject_sources(lib)
        lib.save()
    return done


def _has_host(item: Item) -> bool:
    """描述里点名有人在讲解 / 介绍(评测博主口播)。"""
    return bool(re.search(r"主持|讲解|介绍|解说|博主|自述|演示", item.desc or ""))


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


def reject_sources(lib: Library) -> int:
    """样本打够、废段占比太高的源:剩下没打标签的段标记跳过。返回新跳过的段数。"""
    by_src: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.source:
            by_src.setdefault(i.source, []).append(i)
    n = 0
    for segs in by_src.values():
        tagged = [i for i in segs if i.tagged]
        if len(tagged) < EARLY_STOP_SAMPLE:
            continue
        if sum(1 for i in tagged if is_waste(i)) / len(tagged) < EARLY_STOP_RATIO:
            continue
        for i in segs:
            if not i.tagged and not i.skipped:
                i.skipped = "source_rejected"
                n += 1
    return n


@functools.lru_cache(maxsize=8192)
def _dhash(path: str) -> int | None:
    """代表帧的 64 位差值哈希。读不了返回 None(= 不参与去重)。"""
    try:
        from PIL import Image

        with Image.open(path) as im:
            px = im.convert("L").resize((9, 8)).tobytes()
    except (OSError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | int(px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def _same_frame(a: Item, b: Item) -> bool:
    if a.source != b.source or not a.frame or not b.frame:
        return False
    ha, hb = _dhash(a.frame), _dhash(b.frame)
    return ha is not None and hb is not None and bin(ha ^ hb).count("1") <= DUP_BITS


def inherit_duplicates(lib: Library) -> int:
    """同源里和已打标签的段几乎同一画面的未打段,直接照抄标签,不花视觉调用。"""
    tagged: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.tagged:
            tagged.setdefault(i.source, []).append(i)
    n = 0
    for i in lib.items:
        if i.kind != "video" or i.tagged or i.skipped or not i.frame:
            continue
        twin = next((t for t in tagged.get(i.source, ()) if _same_frame(i, t)), None)
        if twin is None:
            continue
        i.view, i.target, i.problem, i.desc = twin.view, twin.target, twin.problem, twin.desc
        i.tagged = True
        tagged[i.source].append(i)
        n += 1
    return n


def apply_tag(item: Item, v: dict[str, Any], views: Sequence[str]) -> None:
    target = str(v.get("target") or "unknown").lower()
    item.target = target if target in ("yes", "no", "unknown") else "unknown"
    view = str(v.get("view") or "other").lower()
    item.view = view if view in views else "other"
    problem = str(v.get("problem") or "").lower()
    item.problem = "" if problem in ("", "none") else problem
    if item.target == "no":
        item.problem = "not_target_model"
    item.desc = str(v.get("desc") or "")[:60]
    item.tagged = True


# --------------------------------------------------------------------------
# 构建入口
# --------------------------------------------------------------------------
def build(subject: str, *, profile: Any = None, brand: str = "", llm: Any = None,
          budget: float | None = None, max_sources: int = MAX_AUTO_SOURCES,
          errors: list[str] | None = None, log: Any = None,
          enough_seconds: float | None = None) -> dict[str, Any]:
    """增量构建素材池。返回统计。全程不抛异常。

    `enough_seconds`:可用素材够这么多秒就不再给视频段打标签。默认
    `ENOUGH_USABLE_SECONDS`(背景不挑画面);传 0 不设上限。
    """
    errors = errors if errors is not None else []
    if enough_seconds is None:
        enough_seconds = ENOUGH_USABLE_SECONDS if getattr(profile, "is_explainer", False) else 0.0
    say = log or (lambda msg: None)
    budget = config.LIBRARY_BUILD_BUDGET if budget is None else budget
    started = time.monotonic()
    deadline = started + budget if budget else 0.0

    def _late() -> bool:
        return bool(deadline) and time.monotonic() > deadline

    lib = Library.load(subject)
    aliases: list[str] = []
    tiktok_handles: tuple[str, ...] = ()
    spec: dict[str, tuple[str, ...]] = {}
    if profile is not None:
        lib.category = profile.name
        key, spec = official.detect_brand(subject, profile.official_sites, hint=brand)
        aliases = official.brand_aliases(key, spec, brand) if (key or brand) else []
        tiktok_handles = tuple(spec.get("tiktok", ()))

    try:
        if time.time() - lib.discovered_at > REDISCOVER_SECONDS:
            n = discover(lib, aliases, errors, max_sources=max_sources,
                         tiktok_handles=tiktok_handles)
            say(f"发现新源 {n} 个")
            lib.save()
        if not lib.gallery_at:
            n = ingest_gallery(lib, errors, spec=spec)
            say(f"静图 {n} 张")
            lib.save()
        # 手动加的源先下;同类里官方号优先
        pending = sorted((s for s in lib.sources if s.status == "pending"),
                         key=lambda s: (s.added_by != "manual", not s.official))
        for src in pending:
            if _late():
                break
            n = ingest_source(lib, src, errors)
            say(f"源 {src.title[:30] or src.url}:{n} 段")
            lib.save()
        todo = _tag_queue(lib)
        if todo and not _late():
            n = tag_items(lib, todo, llm, errors, deadline=deadline,
                          enough_seconds=enough_seconds)
            say(f"打标签 {n} 个")
    except Exception as exc:  # noqa: BLE001 - 素材池是补充,失败不阻断
        errors.append(f"素材池构建异常:{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        lib.save()
    stats = lib.stats()
    stats["elapsed"] = round(time.monotonic() - started, 1)
    enough = bool(enough_seconds) and usable_seconds(lib) >= enough_seconds
    stats["finished"] = not _late() and not stats["sources_pending"] and (
        enough or not _tag_queue(lib))
    return stats


_running: set[str] = set()
_running_lock = threading.Lock()


def build_async(subject: str, **kwargs: Any) -> bool:
    """后台线程里构建。同一车型已经在建就不重复起。返回是否起了新线程。

    分镜阶段调用:用户审脚本、分镜的这几分钟正好拿来攒素材,
    门禁 A 确认时取材直接用池子里已有的,不在确认请求里等下载。
    """
    key = slug(subject)
    with _running_lock:
        if key in _running:
            return False
        _running.add(key)

    def _run() -> None:
        try:
            build(subject, **kwargs)
        finally:
            with _running_lock:
                _running.discard(key)

    threading.Thread(target=_run, name=f"library-{key}", daemon=True).start()
    return True


def is_building(subject: str) -> bool:
    with _running_lock:
        return slug(subject) in _running


def _tag_queue(lib: Library) -> list[Item]:
    """待打标签的:图全打;视频每个源最多 MAX_TAGGED_PER_SOURCE 段,长段优先
    (长段更可能是完整的展示镜头,而不是快剪)。

    视频先排每个源的前 EARLY_STOP_SAMPLE 段(样本),再排各源剩下的 ——
    样本打完就能判出整源都是口播的源,剩下的不用再花钱。
    跳过的段、和同源里已打标签 / 已在队列的段同一画面的段不进队列。
    """
    out: list[Item] = [i for i in lib.items if i.kind == "still" and not i.tagged and i.frame]
    by_src: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.frame:
            by_src.setdefault(i.source, []).append(i)
    samples: list[Item] = []
    rest: list[Item] = []
    for segs in by_src.values():
        tagged = sum(1 for i in segs if i.tagged)
        room = MAX_TAGGED_PER_SOURCE - tagged
        if room <= 0:
            continue
        kept = [i for i in segs if i.tagged]
        fresh: list[Item] = []
        for i in sorted((i for i in segs if not i.tagged and not i.skipped),
                        key=lambda i: -i.length):
            if len(fresh) >= room:
                break
            if any(_same_frame(i, k) for k in kept):
                continue
            kept.append(i)
            fresh.append(i)
        head = max(0, EARLY_STOP_SAMPLE - tagged)
        samples += fresh[:head]
        rest += fresh[head:]
    return out + samples + rest


# --------------------------------------------------------------------------
# 挑:素材干净程度
# --------------------------------------------------------------------------
def _dirt(item: Item) -> int:
    """画面干净程度:干净 0、角标水印 1、烧录字幕 2。字幕会和成片自己的旁白、
    字幕打架,比角落台标严重得多,所以排最后但不拦。"""
    dirt = {"": 0, "text_overlay": 1, "subtitle": 2}.get(item.problem, 1)
    # 主持人出镜同样抢戏:描述里点名有人在讲解 / 介绍的,按字幕同级处理
    if _has_host(item):
        dirt = max(dirt, 2)
    return dirt




# --------------------------------------------------------------------------
# 铺:横屏解说轨的背景画面
# --------------------------------------------------------------------------
@dataclass
class Piece:
    """背景里的一小段:视频按 [start, start+length) 截,图片做推拉。"""
    item_id: str
    kind: str
    path: str
    start: float
    length: float
    #: 截取时裁掉画面底部的比例(烧录字幕段裁掉字幕区再放大),0 = 不裁
    crop_bottom: float = 0.0


#: 一段最长用几秒就换下一段 —— 信息解说的背景是 2–4 秒一刀的节奏,
#: 一个镜头拖太久反而像卡住了
FILL_MAX_PIECE = 4.5
#: 短于这个的段不用(闪白、转场)
FILL_MIN_PIECE = 1.2


#: 从源片第 0 秒开始的段,跳过开头这么多秒(黑场淡入)
SOURCE_FADE_IN = 0.8
#: 帧平均亮度低于它就当黑场(宣传片开头结尾的黑底片名卡,打标签时常被放过)
DARK_LUMA = 28


@functools.lru_cache(maxsize=4096)
def _luma(path: str) -> float:
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as im:
            return float(ImageStat.Stat(im.convert("L").resize((64, 36))).mean[0])
    except (OSError, ValueError):
        return 255.0  # 读不了就不拦


def is_dark(item: Item) -> bool:
    """背景用不了的黑场:大面积黑底 + 一行小字的片名卡。叠上面板后整屏发黑。"""
    probe = item.frame or (item.path if item.kind == "still" else "")
    return bool(probe) and Path(probe).is_file() and _luma(probe) < DARK_LUMA


#: 烧录字幕段当背景时裁掉画面底部的比例。字幕几乎都在下方五分之一,
#: 成片自己的字幕和章节条也盖在这一带,裁掉不损失要看的东西
SUBTITLE_CROP = 0.22


def _croppable(item: Item) -> bool:
    """只有烧录字幕、没有主持人的视频段:裁掉底部就是干净画面。"""
    return item.kind == "video" and item.problem == "subtitle" and not _has_host(item)


def _fill_tier(item: Item, used: int) -> int:
    """非官方来源、车型没确认(target 不是 yes)的素材整体排到确认过的后面,
    池子里确认过的够用时就不会选到它们(YU7 池里混的 SU7 片段多是这类)。"""
    base = _fill_tier_base(item, used)
    if not item.official and item.target != "yes":
        return base + 10
    return base


def _fill_tier_base(item: Item, used: int) -> int:
    """取用的先后档:干净视频 → 带角标水印的视频(与官图同档) → 官图 → 裁掉字幕的视频 → 用过的干净视频 →
    主持人出镜的视频 → 其余。主持人裁不掉,会和成片自己的旁白打架,排最后。"""
    if item.kind == "video":
        if _dirt(item) == 0:
            return 0 if not used else 3
        if _dirt(item) == 1:  # 角落台标 / 第三方水印:干净视频不够才用
            return 1 if not used else 3
        if _croppable(item):
            return 2 if not used else 5
        return 4 if not used else 6
    return 1 if not used else 5


#: 铺背景时的"长得差不多":比打标签去重(DUP_BITS)宽 —— 同角度的几张棚拍官图、
#: 同一镜头切出来的前后段都算一簇,用掉一张就当整簇都用过
FILL_DUP_BITS = 10
#: 相邻多样性看最近几刀(跨节拍):这几刀里出现过的簇往后排
RECENT_PIECES = 6


def _probe(item: Item) -> str:
    return item.frame or (item.path if item.kind == "still" else "")


def near_dup_groups(items: Sequence[Item], bits: int = FILL_DUP_BITS) -> dict[str, str]:
    """item_id → 所在簇的代表 id。跨来源比代表帧的 dHash,贪心入簇;
    没有代表帧(读不了)的条目自成一簇。"""
    heads: list[tuple[str, int]] = []
    out: dict[str, str] = {}
    for i in items:
        probe = _probe(i)
        h = _dhash(probe) if probe and Path(probe).is_file() else None
        if h is None:
            out[i.id] = i.id
            continue
        head = next((hid for hid, hh in heads if bin(h ^ hh).count("1") <= bits), None)
        if head is None:
            heads.append((i.id, h))
            head = i.id
        out[i.id] = head
    return out


def fill(subject: str, seconds: float, *, hint: str = "",
         used: dict[str, int] | None = None, recent: list[str] | None = None) -> list[Piece]:
    """给一个节拍铺背景:从这台车的池子里挑干净片段**连拼**到 `seconds`。

    和 `candidates()` 的根本区别:这里不要求画面对上旁白,也不要求单段撑满时长 ——
    背景只需要"是这台车、画面干净"。快剪广告里 1.3–3 秒的短段正好拿来连拼,
    不再因为凑不满 6 秒窗口而被浪费。

    `hint`(profile 的背景偏好,汽车是 exterior / interior / driving)只参与排序。`used` 是全片共享的
    已用计数(item_id → 次数),调用方跨节拍传同一个字典,尽量不重复。
    计数按**近重复簇**算(`near_dup_groups`):同角度的几张官图用掉一张,其余也算用过。
    `recent` 是全片最近用过的 item_id(调用方跨节拍传同一个列表,这里就地追加):
    和最近几刀同簇的、和上一刀同视角的往后排 —— 只是排序,池子不够时照样铺满。
    返回空列表表示池子里什么都没有。
    """
    seconds = float(seconds)
    used = used if used is not None else {}
    recent = recent if recent is not None else []
    lib = Library.load(subject)
    vocab = vocab_of(lib)
    want = vocab.hint_views(hint)
    pool = [i for i in lib.items if i.usable and Path(i.path).is_file() and (
        (i.kind == "video" and i.length >= FILL_MIN_PIECE)
        or (i.kind == "still" and i.view in vocab.angle_of_view)) and not is_dark(i)]

    group = near_dup_groups(pool)
    by_id = {i.id: i for i in lib.items}
    members: dict[str, list[str]] = {}
    for iid, g in group.items():
        members.setdefault(g, []).append(iid)

    def group_used(i: Item) -> int:
        return sum(used.get(m, 0) for m in members.get(group[i.id], [i.id]))

    def rank(i: Item, recent_groups: set[str], last_view: str) -> tuple:
        # 认出是这台车的排在没认出的前面:没认出的常是同品牌别的车型(YU7 池子里
        # 混着 SU7 的赛道活动),背景里出现别的车比画面不对口严重得多
        n = group_used(i)
        return (_fill_tier(i, n), i.target != "yes", group[i.id] in recent_groups,
                bool(want) and i.view not in want, bool(last_view) and i.view == last_view, n,
                _dirt(i), not i.official, -i.length)

    out: list[Piece] = []
    covered = 0.0
    left = list(pool)
    while left and covered < seconds - 0.05:
        window = recent[-RECENT_PIECES:]
        recent_groups = {group.get(r, r) for r in window}
        last = by_id.get(window[-1]) if window else None
        i = min(left, key=lambda x: rank(x, recent_groups, last.view if last else ""))
        left.remove(i)
        remaining = seconds - covered
        if i.kind == "still":
            take = min(remaining, FILL_MAX_PIECE)
            start = 0.0
        else:
            # 源片开头的那一段几乎都是黑场淡入(打标签抽的是段中间的帧,看不出来)
            skip = SOURCE_FADE_IN if i.start < 0.5 else 0.0
            length = i.length - skip
            take = min(length, remaining, FILL_MAX_PIECE)
            # 别留一截过短的尾巴:够长就把它吃进这一段
            if remaining - take < FILL_MIN_PIECE:
                take = min(length, remaining)
            start = i.start + skip
        if take < min(FILL_MIN_PIECE, remaining):
            continue
        out.append(Piece(item_id=i.id, kind=i.kind, path=i.path, start=start, length=round(take, 3),
                         crop_bottom=SUBTITLE_CROP if _croppable(i) else 0.0))
        used[i.id] = used.get(i.id, 0) + 1
        recent.append(i.id)
        del recent[:-RECENT_PIECES]
        covered += take
    if out and covered < seconds - 0.05:
        # 池子太小铺不满:把最后一段拉长到结尾(图片推拉 / 视频截到原片允许的长度)
        last = out[-1]
        last.length = round(last.length + (seconds - covered), 3)
    return out


# --------------------------------------------------------------------------
# 预览:联系表
# --------------------------------------------------------------------------
def contact_sheet(subject: str, dest: str | Path, *, usable_only: bool = False,
                  cols: int = 6, cell: int = 320) -> Path | None:
    """把池子里的帧拼成一张大图,每格标视角、车型判定和问题,方便人工过一遍。"""
    from PIL import Image, ImageDraw, ImageFont

    lib = Library.load(subject)
    items = [i for i in lib.items if i.frame and Path(i.frame).is_file()
             and (i.usable or not usable_only)]
    if not items:
        return None
    rows = (len(items) + cols - 1) // cols
    h = int(cell * 9 / 16) + 44
    sheet = Image.new("RGB", (cols * cell, rows * h), "white")
    draw = ImageDraw.Draw(sheet)
    font = None
    for name in ("msyh.ttc", "simhei.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(name, 14)
            break
        except OSError:
            continue
    for n, item in enumerate(items):
        x, y = (n % cols) * cell, (n // cols) * h
        try:
            with Image.open(item.frame) as im:
                im = im.convert("RGB")
                im.thumbnail((cell - 4, int(cell * 9 / 16)))
                sheet.paste(im, (x + 2, y + 2))
        except OSError:
            continue
        color = (0, 128, 0) if item.usable else ((160, 160, 160) if not item.tagged else (200, 0, 0))
        mark = "✓" if item.usable else ("?" if not item.tagged else "✗")
        line1 = f"{mark} {item.id[-12:]} {item.view or '-'} {item.target}"
        line2 = (item.problem + " " if item.problem else "") + item.desc
        draw.text((x + 4, y + int(cell * 9 / 16) + 4), line1, fill=color, font=font)
        draw.text((x + 4, y + int(cell * 9 / 16) + 22), line2[:24], fill=(60, 60, 60), font=font)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest)
    return dest


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pipeline.library",
                                     description="素材池:构建、加链接、查看")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="增量构建(发现源、下载、切段、打标签、抓官图)")
    b.add_argument("subject")
    b.add_argument("--brand", default="")
    b.add_argument("--category", default="", help="品类 profile 名,默认 DEFAULT_CATEGORY")
    b.add_argument("--budget", type=float, default=None, help="时长预算(秒),0 不限")
    b.add_argument("--max-sources", type=int, default=MAX_AUTO_SOURCES)
    b.add_argument("--no-llm", action="store_true", help="不调 LLM(只认视觉模型自己的 JSON)")
    a = sub.add_parser("add", help="手动加视频链接")
    a.add_argument("subject")
    a.add_argument("urls", nargs="+")
    ai = sub.add_parser("add-images", help="手动加图:图片地址、本地图片,或商品详情页链接")
    ai.add_argument("subject")
    ai.add_argument("refs", nargs="+")
    ai.add_argument("--category", default="", help="品类 profile 名(新池子要给)")
    rt = sub.add_parser("retag", help="清掉已有标签,下次 build 重新打(改了打标签规则之后用)")
    rt.add_argument("subject")
    s = sub.add_parser("show", help="看统计")
    s.add_argument("subject")
    sh = sub.add_parser("sheet", help="导出联系表图片")
    sh.add_argument("subject")
    sh.add_argument("out", nargs="?", default="")
    sh.add_argument("--usable", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "add":
        added = add_links(args.subject, args.urls)
        print(f"新加 {len(added)} 个:" + ", ".join(added))
        return 0
    if args.cmd == "add-images":
        errs: list[str] = []
        n = add_images(args.subject, args.refs, category=args.category, errors=errs)
        print(f"新加 {n} 张图,下次 build 时打标签")
        for e in errs:
            print(" -", e)
        return 0
    if args.cmd == "retag":
        lib = Library.load(args.subject)
        n = 0
        for i in lib.items:
            if i.tagged:
                i.tagged, n = False, n + 1
        lib.save()
        print(f"已清掉 {n} 个标签,运行 build 重新打")
        return 0
    if args.cmd == "show":
        print(json.dumps(Library.load(args.subject).stats(), ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "sheet":
        out = args.out or str(Library.dir_for(args.subject) / "sheet.png")
        got = contact_sheet(args.subject, out, usable_only=args.usable)
        print(got or "池子里还没有帧")
        return 0

    from ..categories import default_category

    llm = None
    if not args.no_llm:
        from .orchestrator import _llm

        llm = _llm()
    errors: list[str] = []
    profile = load_profile(args.category or default_category())
    stats = build(args.subject, profile=profile, brand=args.brand, llm=llm,
                  budget=args.budget, max_sources=args.max_sources, errors=errors,
                  log=lambda m: print(m, flush=True))
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    if errors:
        print(f"\n{len(errors)} 条错误(前 15 条):")
        for e in errors[:15]:
            print(" -", e)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
