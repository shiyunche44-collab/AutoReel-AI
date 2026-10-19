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
from ..providers import clips as clips_mod
from ..providers import images, official, platform_video
from . import vision_verdict

LIB_ROOT_NAME = "library"

#: 视角标签。比镜头的 angle 细:取材时一个 angle 可以接受几种 view
VIEWS: tuple[str, ...] = (
    "exterior_front", "exterior_side", "exterior_45", "rear", "driving", "aerial",
    "interior", "screen", "seat", "detail", "people", "text", "other",
)
_EXTERIOR = {"exterior_front", "exterior_side", "exterior_45", "rear", "driving", "aerial"}
_INTERIOR = {"interior", "screen", "seat"}

#: 镜头 angle → 可接受的 view,按偏好排序(第一个是最对口的)
ANGLE_VIEWS: dict[str, tuple[str, ...]] = {
    "exterior_45": ("exterior_45", "exterior_front", "exterior_side", "driving", "aerial"),
    "rear": ("rear", "driving"),
    "interior": ("interior", "seat", "screen"),
    "screen_spec": ("screen", "interior"),
    "detail": ("detail",),
}

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
GALLERY_PER_CATEGORY = 12
#: 一次交给 LLM 结构化的描述条数。太多会超上下文、也更容易漏编号
TAG_BATCH = 12
#: 隔多久重新搜一次新源(新车上市后官方号会陆续发片)
REDISCOVER_SECONDS = 7 * 24 * 3600

#: 拦掉的问题(与 clip_resolver 的质检口径一致;text_overlay 只记不拦)
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
            "untagged": sum(1 for i in self.items if not i.tagged),
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
    return url


def platform_of(url: str) -> str:
    host = platform_video.domain_of(url)
    for key, name in (("bilibili", "bilibili"), ("b23.tv", "bilibili"), ("youtu", "youtube"),
                      ("douyin", "douyin"), ("ixigua", "douyin"), ("vimeo", "vimeo")):
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


def discovery_queries(subject: str, en: str) -> tuple[list[str], list[str]]:
    """(中文检索词, 英文检索词)。挑的都是"车多人少"的视频类型。"""
    zh = [f"{subject} 宣传片", f"{subject} 官方", f"{subject} 实拍 外观 内饰",
          f"{subject} 静态体验", f"{subject} 试驾 实拍"]
    en_q = [f"{en} official", f"{en} exterior interior", f"{en} walkaround"]
    return zh, en_q


def discover(lib: Library, aliases: Sequence[str], errors: list[str], *,
             max_sources: int = MAX_AUTO_SOURCES) -> int:
    """搜一轮新源加进池子。官方号的视频优先。返回新增的源数。"""
    subject = lib.subject
    hits: list[platform_video.PlatformHit] = []
    if aliases:
        hits += official.platform_official(subject, aliases, platforms=config.MEDIA_PLATFORMS,
                                           errors=errors)
    zh_q, en_q = discovery_queries(subject, english_name(subject, aliases))
    zh_platforms = [p for p in config.MEDIA_PLATFORMS if p == "bilibili"]
    en_platforms = [p for p in config.MEDIA_PLATFORMS if p == "youtube"]
    for q in zh_q:
        hits += platform_video.search(q, platforms=zh_platforms, errors=errors)
    for q in en_q:
        hits += platform_video.search(q, platforms=en_platforms, errors=errors)
    for h in hits:
        h.official = h.official or bool(aliases and platform_video.is_official(h, aliases))
    ranked = rank_sources(_dedupe_hits(hits), subject)

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


#: 标题里出现这些词的源不要:游戏 / 模组画面、新闻事件、渲染猜想、车模玩具 ——
#: 视觉模型很可能把游戏里的 YU7 也认成 YU7。
JUNK_TITLE_WORDS = (
    "beamng", "模组", "游戏", "gta", "地平线", "forza", "极品飞车", "神力科莎",
    "assetto", "gran turismo", "赛车计划", "手办", "车模", "乐高", "lego", "模型",
    "渲染", "申报图", "猜想", "曝光", "谍照", "老鼠", "事故", "起火", "维权",
    "二手车", "系统更新", "ota升级", "ota 升级", "投屏", "教程", "怎么",
)
#: 标题里出现这些词的排前面:广告、宣传片、实拍展示,画面里车多人少
SHOWCASE_TITLE_WORDS = (
    "宣传片", "广告", "tvc", "官方", "official", "commercial", "debut", "film",
    "实拍", "静态", "外观", "内饰", "walkaround", "exterior", "interior", "首发",
)
#: 官方号的长视频(发布会)也收:分窗下载,发布会里放的宣传片正是好素材
OFFICIAL_MAX_SECONDS = 3 * 3600


def rank_sources(hits: Sequence[platform_video.PlatformHit], subject: str
                 ) -> list[platform_video.PlatformHit]:
    """过滤 + 排序:官方号 → 广告 / 宣传片 / 实拍类标题 → 其余;口播类和垃圾标题不要。"""
    out: list[platform_video.PlatformHit] = []
    for h in hits:
        title = h.title.lower()
        limit = OFFICIAL_MAX_SECONDS if h.official else platform_video.MAX_SOURCE_SECONDS
        if h.duration and not (platform_video.MIN_SOURCE_SECONDS <= h.duration <= limit):
            continue
        if h.title and not platform_video.title_relevant(h.title, subject):
            continue
        if any(w in title for w in JUNK_TITLE_WORDS):
            continue
        # 口播 / 直播类 rank 只是排后面,这里直接不要:攒素材要的是车。官方发布会除外
        if not h.official and any(w in title for w in platform_video._TALKY_WORDS):
            continue
        out.append(h)

    def _key(h: platform_video.PlatformHit) -> tuple[int, int]:
        showcase = any(w in h.title.lower() for w in SHOWCASE_TITLE_WORDS)
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
                   per_category: int = GALLERY_PER_CATEGORY) -> int:
    """汽车之家官图 → 池子里的图。返回新增张数。"""
    try:
        refs = official.autohome_images(lib.subject, per_category=per_category, errors=errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"素材池官图抓取失败:{str(exc)[:120]}")
        return 0
    have = {i.url for i in lib.items if i.kind == "still"}
    still_dir = lib.dir / "stills"
    added = 0
    for ref in refs:
        if ref.url in have:
            continue
        item_id = "img_" + short_id(ref.url)
        dest = still_dir / f"{item_id}.jpg"
        try:
            images.download(ref.url, dest, headers={
                "User-Agent": clips_mod.BROWSER_UA,
                "Referer": "https://car.autohome.com.cn/",
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"官图下载失败({ref.url[:60]}):{str(exc)[:100]}")
            continue
        frame = _thumb(dest, lib.dir / "frames" / f"{item_id}.jpg")
        lib.items.append(Item(
            id=item_id, kind="still", path=str(dest), frame=str(frame or dest), url=ref.url,
            platform="autohome", official=True, origin="library:autohome",
            license=ref.license, view=_GALLERY_VIEW.get(ref.angle, ""),
        ))
        added += 1
    lib.gallery_at = time.time()
    return added


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
def tag_prompt(subject: str) -> str:
    return (
        f"这张图来自「{subject}」相关的视频或图库。只依据图片内容回答,不要猜。"
        "注意同品牌的不同车系(如小米 SU7 与 YU7)是不同车型。请用文字逐条说明:\n"
        "1. 画面主体是什么;有车的话是什么品牌和车型,依据是什么(车标、车尾字样、外形),"
        "看不出来就直说看不出来;\n"
        "2. 拍的是哪个角度:车头正面、侧面、前 45 度、车尾、行驶中、航拍俯视、"
        "车内座舱、中控屏或仪表、座椅、局部特写(轮毂、车灯、门把手等),"
        "还是人物、文字卡、其它;\n"
        "3. 车是否完整在画面内,画面是否清晰;\n"
        "4. 有没有对白 / 解说字幕(画面下方的句子)、角落台标、水印、大段文字;"
        "是不是宣传海报、网页 / App 截图,或者是在拍大屏幕 / 电视里放的画面;\n"
        "5. 画面里有没有人,人占画面多大。\n"
        "最后用一句话概括这个画面。"
    )


TAG_SYSTEM = """你把视觉模型对若干图片的描述转成素材标签。目标车型是「{subject}」。

只依据描述里**明确写出**的内容,保守,不要推理补全:
- target:描述明确认出是目标车型(同品牌同车系;年款、版本不同仍算)→ "yes";
  明确认出是**别的品牌或别的车系** → "no";没点名车型 → "unknown"。
  描述里视觉模型自己给的"是 / 不是目标车型"结论一律忽略,只看它对车标、字样、外形的描述。
- view,取最贴切的一个:
  exterior_front(车头正面)、exterior_side(正侧面)、exterior_45(前 45 度 / 前侧)、
  rear(车尾 / 后 45 度)、driving(行驶中的外观)、aerial(航拍俯视)、
  interior(座舱全景)、screen(中控屏 / 仪表 / HUD)、seat(座椅 / 后排 / 后备厢)、
  detail(轮毂、车灯、门把手、充电口等局部特写)、
  people(主体是人)、text(主体是文字卡 / 参数表 / 片头片尾)、other(风景、别的东西)。
- problem,按顺序取第一条命中的:target 为 "no" → "not_target_model";
  描述里没有任何车辆或车辆部件 → "no_subject"(座舱、局部特写都算有车);
  明确说车被画面边缘切掉(局部特写不算)→ "cropped";明确说模糊、过暗、看不清 → "unclear";
  宣传海报(大标题字、代言人摆拍)、网页 / App 截图,或车是**显示在大屏 / 屏幕 / 电视里**的
  翻拍画面(发布会舞台大屏、前景有观众)→ "poster";
  画面下方有对白或解说**字幕**(一行行句子)→ "subtitle";
  只有角落的台标、水印、小字标注 → "text_overlay";都没有 → ""。
- desc:一句中文概括画面(20 字以内),例如"白色 YU7 前 45 度停在沙漠公路"。
- 描述是报错、拒答或为空 → target "unknown",view "other",problem "unclear"。

输出 JSON 对象 {{"verdicts": [...]}},每张一项,字段 index(与输入编号一致)、
target、view、problem、desc。"""


def tag_items(lib: Library, todo: Sequence[Item], llm: Any, errors: list[str], *,
              deadline: float = 0.0) -> int:
    """给还没看过的段 / 图打标签。返回打上的个数。超过 `deadline` 就停。"""
    done = 0
    prompt = tag_prompt(lib.subject)
    system = TAG_SYSTEM.format(subject=lib.subject)
    for k in range(0, len(todo), TAG_BATCH):
        if deadline and time.monotonic() > deadline:
            break
        batch = [i for i in todo[k:k + TAG_BATCH] if i.frame and Path(i.frame).is_file()]
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
        for item, v in zip(slots, verdicts):
            if not v:
                continue
            apply_tag(item, v)
            done += 1
        lib.save()
    return done


def apply_tag(item: Item, v: dict[str, Any]) -> None:
    target = str(v.get("target") or "unknown").lower()
    item.target = target if target in ("yes", "no", "unknown") else "unknown"
    view = str(v.get("view") or "other").lower()
    item.view = view if view in VIEWS else "other"
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
          errors: list[str] | None = None, log: Any = None) -> dict[str, Any]:
    """增量构建素材池。返回统计。全程不抛异常。"""
    errors = errors if errors is not None else []
    say = log or (lambda msg: None)
    budget = config.LIBRARY_BUILD_BUDGET if budget is None else budget
    started = time.monotonic()
    deadline = started + budget if budget else 0.0

    def _late() -> bool:
        return bool(deadline) and time.monotonic() > deadline

    lib = Library.load(subject)
    aliases: list[str] = []
    if profile is not None:
        key, spec = official.detect_brand(subject, profile.official_sites, hint=brand)
        aliases = official.brand_aliases(key, spec, brand) if (key or brand) else []

    try:
        if time.time() - lib.discovered_at > REDISCOVER_SECONDS:
            n = discover(lib, aliases, errors, max_sources=max_sources)
            say(f"发现新源 {n} 个")
            lib.save()
        if not lib.gallery_at:
            n = ingest_gallery(lib, errors)
            say(f"官图 {n} 张")
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
            n = tag_items(lib, todo, llm, errors, deadline=deadline)
            say(f"打标签 {n} 个")
    except Exception as exc:  # noqa: BLE001 - 素材池是补充,失败不阻断
        errors.append(f"素材池构建异常:{type(exc).__name__}: {str(exc)[:200]}")
    finally:
        lib.save()
    stats = lib.stats()
    stats["elapsed"] = round(time.monotonic() - started, 1)
    stats["finished"] = not _late() and not stats["sources_pending"] and not _tag_queue(lib)
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
    (长段更可能是完整的展示镜头,而不是快剪)。"""
    out: list[Item] = [i for i in lib.items if i.kind == "still" and not i.tagged and i.frame]
    by_src: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video" and i.frame:
            by_src.setdefault(i.source, []).append(i)
    for segs in by_src.values():
        tagged = sum(1 for i in segs if i.tagged)
        room = MAX_TAGGED_PER_SOURCE - tagged
        if room <= 0:
            continue
        fresh = sorted((i for i in segs if not i.tagged), key=lambda i: -i.length)
        out += fresh[:room]
    return out


# --------------------------------------------------------------------------
# 挑:按镜头取候选
# --------------------------------------------------------------------------
def _group(view: str) -> str:
    if view in _EXTERIOR:
        return "ext"
    if view in _INTERIOR:
        return "int"
    return "any" if view == "detail" else "none"


def windows(items: Sequence[Item], seconds: float, angle: str) -> list[tuple[Item, float]]:
    """从同一个文件里按时间排好的段,拼出能撑满 `seconds` 的连续窗口。

    窗口从一个对口视角的好段开头,往后接同一大类(外观 / 内饰)的好段;
    夹在中间的极短段(闪切、转场,没打标签)可以跨过。返回 (首段, 窗口里第一段的占比)。
    """
    wanted = ANGLE_VIEWS.get(angle, ())
    segs = sorted(items, key=lambda i: i.start)
    out: list[tuple[Item, float]] = []
    for k, first in enumerate(segs):
        if not first.usable or first.view not in wanted:
            continue
        group = _group(first.view)
        covered = first.length
        prev_end = first.end
        for nxt in segs[k + 1:]:
            if covered >= seconds:
                break
            if nxt.start - prev_end > 0.5:
                break
            if nxt.tagged:
                g = _group(nxt.view)
                if not nxt.usable or (g != group and "any" not in (g, group)):
                    break
            elif nxt.length >= MIN_TAG_SECONDS:
                break   # 没看过的长段不敢用
            covered = nxt.end - first.start
            prev_end = nxt.end
        if covered >= seconds:
            out.append((first, min(1.0, first.length / seconds)))
    return out


def candidates(subject: str, angle: str, *, seconds: float | None = None,
               exclude: Sequence[tuple[str, float, float]] = ()) -> list[clips_mod.ClipCandidate]:
    """给一个镜头挑候选:视频窗口在前,官图在后。

    排序:官方源 → 认出是这台车的 → 首段视角最对口的 → 首段越长越好(少快剪)。
    `exclude` 是已经用掉的 (文件, 起, 止),与它重叠的窗口不再给。
    """
    seconds = float(seconds or config.SHOT_GRID_SECONDS)
    lib = Library.load(subject)
    if not lib.items or angle not in ANGLE_VIEWS:
        return []
    wanted = ANGLE_VIEWS[angle]
    by_file: dict[str, list[Item]] = {}
    for i in lib.items:
        if i.kind == "video":
            by_file.setdefault(i.path, []).append(i)

    scored: list[tuple[tuple, clips_mod.ClipCandidate]] = []
    for path, segs in by_file.items():
        if not Path(path).is_file():
            continue
        for first, share in windows(segs, seconds, angle):
            a, b = first.start, first.start + seconds
            if any(p == path and a < e and s < b for p, s, e in exclude):
                continue
            key = (_dirt(first), not first.official, first.target != "yes",
                   wanted.index(first.view), -share)
            scored.append((key, _to_candidate(first, path)))
    for i in lib.items:
        if i.kind == "still" and i.usable and i.view in wanted and Path(i.path).is_file():
            if any(p == i.path for p, _, _ in exclude):
                continue
            key = (_dirt(i), True, i.target != "yes", wanted.index(i.view), 0.0)
            scored.append((key, _to_candidate(i, i.path)))
    videos = sorted((x for x in scored if x[1].kind == "video"), key=lambda x: x[0])
    stills = sorted((x for x in scored if x[1].kind == "still"), key=lambda x: x[0])
    return [c for _, c in videos + stills]


def _dirt(item: Item) -> int:
    """画面干净程度:干净 0、角标水印 1、烧录字幕 2。字幕会和成片自己的旁白、
    字幕打架,比角落台标严重得多,所以排最后但不拦。"""
    dirt = {"": 0, "text_overlay": 1, "subtitle": 2}.get(item.problem, 1)
    # 主持人出镜同样抢戏:描述里点名有人在讲解 / 介绍的,按字幕同级处理
    if re.search(r"主持|讲解|介绍|解说|博主|自述|演示", item.desc or ""):
        dirt = max(dirt, 2)
    return dirt


def _to_candidate(item: Item, path: str) -> clips_mod.ClipCandidate:
    return clips_mod.ClipCandidate(
        url=path, page_url=item.url, origin=item.origin or "library", title=item.desc,
        license=item.license, kind=item.kind, view=item.view, start=item.start,
        official=item.official, local_path="", dirt=_dirt(item),
    )


MATCH_SYSTEM = """你在给解说视频的一个镜头挑真实素材。给你镜头想要的画面,和一组候选画面的描述
(每条前面是编号、视角、出处)。挑出能用的,按合适程度排序。

宽松原则 —— 真实素材比 AI 生成的可信,只要主体对得上就该用:
- 角度、景别、光线、颜色、天气、时间、地点背景(影棚 / 赛道 / 公路 / 雪山)、动静、
  具体是哪个部件(要中控屏给了座椅,同属座舱)都**不是**排除理由,只影响排序;
- 只排除主体明显对不上的:要两车同框而候选只有一台、要特定人物而候选没人或反之、
  要外观给了内饰或反之、候选描述里明确是别的车型;
- 同样合适时,画面干净(没有字幕、没有主持人)的排前面。

先想清楚这个镜头有没有**硬性要求**(几台车、有没有人、是外观还是车内),写进 need;
再逐条对照,只挑满足硬性要求的。
输出 JSON 对象 {"need": "一句话硬性要求", "picks": [编号, ...]},最多 8 个;
一个都不能用就输出 {"need": "...", "picks": []}。"""

#: 交给 LLM 挑的候选上限:按池子自己的排序取前若干条
MATCH_POOL = 20


def match_intent(cands: Sequence[clips_mod.ClipCandidate], intent: str, llm: Any,
                 errors: list[str]) -> list[clips_mod.ClipCandidate] | None:
    """按镜头意图从候选里挑、重排。文本对文本(候选描述是入池时打的),
    比逐镜头让视觉模型判"对不对题"稳定得多,也便宜得多。

    返回挑中的候选(可能为空 = 都不合适);没有 LLM、没有意图或调用失败返回 None,
    调用方据此保留原排序。
    """
    if llm is None or not intent or not cands:
        return None
    pool = list(cands[:MATCH_POOL])
    lines = []
    for n, c in enumerate(pool, start=1):
        tags = [c.view or "-", "官方" if c.official else "", "图片" if c.kind == "still" else "视频",
                {1: "有台标", 2: "有字幕或主持人"}.get(c.dirt, "")]
        lines.append(f"{n}. [{' / '.join(t for t in tags if t)}] {c.title or '(无描述)'}")
    user = f"镜头想要的画面:{intent}\n\n候选:\n" + "\n".join(lines)
    try:
        data = llm.structured(MATCH_SYSTEM, user, required_keys=["picks"], temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"素材池按意图挑选失败,按原排序:{str(exc)[:120]}")
        return None
    out: list[clips_mod.ClipCandidate] = []
    for raw in data.get("picks") or []:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(pool) and pool[n - 1] not in out:
            out.append(pool[n - 1])
    # LLM 只管"能不能用";顺序先看画面干净程度 —— 它会按字面贴合度排
    # ("棚拍"对"影棚"),把带主持人和字幕的评测画面排到干净的官方广告前面
    return sorted(out, key=lambda c: c.dirt)


def has_usable_video(subject: str) -> bool:
    """池子里有没有能用的视频段(文件还在)。"""
    return any(i.kind == "video" and i.usable and Path(i.path).is_file()
               for i in Library.load(subject).items)


def is_library(candidate: clips_mod.ClipCandidate) -> bool:
    return candidate.origin.startswith("library")


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
                                     description="车型素材池:构建、加链接、查看")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="增量构建(发现源、下载、切段、打标签、抓官图)")
    b.add_argument("subject")
    b.add_argument("--brand", default="")
    b.add_argument("--budget", type=float, default=None, help="时长预算(秒),0 不限")
    b.add_argument("--max-sources", type=int, default=MAX_AUTO_SOURCES)
    b.add_argument("--no-llm", action="store_true", help="不调 LLM(只认视觉模型自己的 JSON)")
    a = sub.add_parser("add", help="手动加视频链接")
    a.add_argument("subject")
    a.add_argument("urls", nargs="+")
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

    from ..categories import load_profile

    llm = None
    if not args.no_llm:
        from .orchestrator import _llm

        llm = _llm()
    errors: list[str] = []
    stats = build(args.subject, profile=load_profile("auto"), brand=args.brand, llm=llm,
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
