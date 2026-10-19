"""平台视频 Provider:B 站 / YouTube / 抖音(西瓜)按关键词搜索 + 分段下载。

为什么用 yt-dlp
---------------
目录里没有视频检索 connector,而车的真实视频绝大多数在这几个平台上:
官方宣传片、试驾、静态评测。yt-dlp 一个包覆盖三家的解析与签名,
而且支持**只下需要的那几秒**(`download_ranges`),不必把十几分钟的
评测整条拉下来。用途是个人学习、不商用。

三家的接入方式不一样
--------------------
- **B 站**:直接调站内搜索接口(和 yt-dlp 的 `bilisearch` 用的是同一个),
  因为 yt-dlp 的搜索只回视频地址,拿不到标题 / UP 主 / 时长 —— 而这三样
  正是过滤和判定官方号要用的。接口失败时退回 `bilisearch`。
- **YouTube**:`ytsearchN:` 的 flat 结果自带标题、频道、时长、认证标记。
  国内需要在 `.env` 配 `YTDLP_PROXY`。
- **抖音 / 西瓜**:yt-dlp 没有这两家的搜索,只能先用 Bing `site:` 站内检索
  拿到视频页,再交给 yt-dlp 下。抖音下载多半要 cookie,所以是**实验性**来源:
  失败就安静跳过,不影响别的平台。

所有函数都不向上抛网络异常 —— 素材层的第一原则是降级而不是阻断。
"""
from __future__ import annotations

import html
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .. import config
from . import webassets

#: 超过这个时长的多是直播回放 / 长访谈,截到车的概率很低
MAX_SOURCE_SECONDS = 40 * 60
#: 太短的多是预告或表情包,一个 6 秒镜头都切不满
MIN_SOURCE_SECONDS = 8

#: 标题里出现这些词的视频往后排:口播、直播、开箱画面里人比车多
_TALKY_WORDS = ("直播", "口播", "开箱", "访谈", "采访", "vlog", "podcast", "reaction",
                "回放", "合集", "盘点", "吐槽", "抽奖")

#: 官方号的名字特征。命中其一且名字里含品牌,才算官方 —— 光有"汽车"二字的
#: 自媒体太多,不能单凭它判。
_OFFICIAL_MARKERS = ("官方", "汽车", "official", "motors", "motor", "auto", "global",
                     "china", "中国", "集团")


class PlatformError(RuntimeError):
    """平台检索或下载失败。调用方应降级。"""


@dataclass
class PlatformHit:
    """一条平台视频的元数据(还没下载)。"""
    platform: str
    url: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    #: 平台给的认证标记(B 站机构认证 / YouTube 认证频道)
    verified: bool = False
    official: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 打分与过滤
# --------------------------------------------------------------------------
def is_official(hit: PlatformHit, brand_aliases: Iterable[str]) -> bool:
    """判定上传者是不是品牌官方号。

    规则:上传者名里**含品牌名**,并且(平台认证过 或 名字带"官方 / 汽车 /
    Official / Motors"之类)。只看品牌名会把"比亚迪车友会"判成官方,
    只看认证会把别的品牌的官方号也算进来。
    """
    name = (hit.uploader or "").lower().replace(" ", "")
    if not name:
        return False
    aliases = [a.lower().replace(" ", "") for a in brand_aliases if a and len(a) >= 2]
    if not any(a in name for a in aliases):
        return False
    if hit.verified:
        return True
    return any(m in name for m in _OFFICIAL_MARKERS)


def title_relevant(title: str, subject: str) -> bool:
    """车辆镜头要求标题里提到车型。

    拉丁文主体复用维基那套词元匹配;中文主体按"主体名去掉空格后的
    前若干字"做包含判断 —— B 站标题多是中文,这样最稳。
    """
    if not subject:
        return True
    low = re.sub(r"<[^>]+>", "", title).lower().replace(" ", "")
    tokens = webassets._significant_tokens(subject)
    if tokens:
        return webassets._title_matches(title, tokens)
    core = re.sub(r"\s+", "", subject).lower()
    # "比亚迪汉 EV" → 先试整串,再试去掉尾部英文后缀的中文部分
    candidates = [core, re.sub(r"[a-z0-9]+$", "", core)]
    return any(c and c in low for c in candidates)


def rank(hits: list[PlatformHit], *, subject: str = "", require_subject: bool = False
         ) -> list[PlatformHit]:
    """过滤 + 排序:官方优先,口播类靠后,时长不合适的丢掉。"""
    out: list[PlatformHit] = []
    for h in hits:
        if h.duration and not (MIN_SOURCE_SECONDS <= h.duration <= MAX_SOURCE_SECONDS):
            continue
        # 没有标题的(抖音、B 站 yt-dlp 兜底结果)判断不了,保留但排在后面
        if require_subject and h.title and not title_relevant(h.title, subject):
            continue
        out.append(h)

    def _key(h: PlatformHit) -> tuple[int, int, int]:
        talky = any(w in h.title.lower() for w in _TALKY_WORDS)
        return (0 if h.official else 1, 0 if h.title else 1, 1 if talky else 0)

    return sorted(out, key=_key)   # sorted 是稳定排序:同档保留平台给的相关度顺序


def segment_starts(duration: float, seconds: float, count: int = 3) -> list[float]:
    """一条长视频切给多个镜头时的起点。

    避开片头(logo、口播开场)和片尾(结束卡、求三连),只在 15%–70% 区间取。
    时长未知时从第 5 秒起切 —— 大多数宣传片前几秒是黑场或 logo。
    """
    if duration <= 0:
        return [5.0]
    usable_end = max(0.0, duration - seconds)
    if usable_end <= 0:
        return [0.0]
    lo, hi = duration * 0.15, min(duration * 0.70, usable_end)
    if hi <= lo:
        return [round(min(lo, usable_end), 2)]
    if count <= 1:
        return [round(lo, 2)]
    step = (hi - lo) / (count - 1)
    return [round(lo + step * i, 2) for i in range(count)]


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------
_BILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"


def parse_bili_duration(raw: Any) -> float:
    """B 站搜索结果的时长是 "4:35" / "1:02:03" 字符串。"""
    if isinstance(raw, (int, float)):
        return float(raw)
    parts = str(raw or "").strip().split(":")
    try:
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    except ValueError:
        return 0.0


def parse_bili_results(payload: dict[str, Any]) -> list[PlatformHit]:
    data = payload.get("data") or {}
    hits: list[PlatformHit] = []
    for v in data.get("result") or []:
        if not isinstance(v, dict):
            continue
        url = str(v.get("arcurl") or "")
        if not url and v.get("bvid"):
            url = f"https://www.bilibili.com/video/{v['bvid']}"
        if not url:
            continue
        hits.append(PlatformHit(
            platform="bilibili",
            url=url.replace("http://", "https://"),
            title=html.unescape(re.sub(r"<[^>]+>", "", str(v.get("title") or ""))),
            uploader=str(v.get("author") or ""),
            duration=parse_bili_duration(v.get("duration")),
            # 搜索结果里没有 UP 主认证字段,官方判定靠名字;
            # `is_union_video` 等字段与认证无关,不拿来凑数
            verified=False,
        ))
    return hits


def search_bilibili(query: str, *, limit: int = 10) -> list[PlatformHit]:
    import uuid

    import httpx

    params = {"search_type": "video", "keyword": query, "page": 1}
    headers = {
        "User-Agent": webassets.BROWSER_UA,
        "Referer": "https://search.bilibili.com/",
        # 没有 buvid3 会被风控成 -412。和 yt-dlp 一样随机造一个即可
        "Cookie": f"buvid3={uuid.uuid4()}infoc",
    }
    try:
        resp = httpx.get(_BILI_SEARCH_API, params=params, headers=headers, timeout=20)
        payload = resp.json()
        if payload.get("code") not in (0, None):
            raise PlatformError(f"B 站搜索返回 code={payload.get('code')}")
        hits = parse_bili_results(payload)
    except Exception as exc:  # noqa: BLE001 - 接口改版 / 风控时退回 yt-dlp
        try:
            hits = [PlatformHit(platform="bilibili", url=u)
                    for u in _ytdlp_flat_urls(f"bilisearch{limit}:{query}")]
        except Exception as exc2:  # noqa: BLE001
            # 必须抛出而不是返回空:空结果会被磁盘缓存 24 小时,
            # 网络修好之后也一直显示"没搜到"
            raise PlatformError(f"B 站搜索不可用:{str(exc)[:80]} / {str(exc2)[:80]}") from exc2
    return hits[:limit]


def parse_yt_entries(info: dict[str, Any]) -> list[PlatformHit]:
    hits: list[PlatformHit] = []
    for e in info.get("entries") or []:
        if not isinstance(e, dict):
            continue
        url = str(e.get("url") or e.get("webpage_url") or "")
        if url and not url.startswith("http") and e.get("id"):
            url = f"https://www.youtube.com/watch?v={e['id']}"
        if not url:
            continue
        hits.append(PlatformHit(
            platform="youtube",
            url=url,
            title=str(e.get("title") or ""),
            uploader=str(e.get("channel") or e.get("uploader") or ""),
            duration=float(e.get("duration") or 0.0),
            verified=bool(e.get("channel_is_verified")),
        ))
    return hits


def search_youtube(query: str, *, limit: int = 10) -> list[PlatformHit]:
    """失败时抛出(由 `search()` 记进错误),不返回空 —— 空结果会被缓存。"""
    try:
        info = _ytdlp_extract(f"ytsearch{limit}:{query}", flat=True)
    except Exception as exc:  # noqa: BLE001
        hint = "" if config.YTDLP_PROXY else ";国内访问需在 .env 配 YTDLP_PROXY"
        raise PlatformError(f"YouTube 搜索不可用{hint}:{str(exc)[:120]}") from exc
    return parse_yt_entries(info)[:limit]


_DOUYIN_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:douyin\.com/video/\d+|ixigua\.com/\d+)", re.IGNORECASE
)


def search_douyin(query: str, *, limit: int = 6) -> list[PlatformHit]:
    """抖音 / 西瓜:Bing 站内检索拿视频页。实验性。"""
    hits: list[PlatformHit] = []
    failures: list[str] = []
    for site in ("douyin.com/video", "ixigua.com"):
        try:
            urls = webassets.bing_web(f"site:{site} {query}", limit=limit)
        except Exception as exc:  # noqa: BLE001
            failures.append(str(exc)[:80])
            continue
        for u in urls:
            m = _DOUYIN_URL_RE.match(u)
            if m and all(h.url != m.group(0) for h in hits):
                hits.append(PlatformHit(platform="douyin", url=m.group(0)))
    if len(failures) == 2:
        raise PlatformError(f"抖音/西瓜站内检索不可用:{failures[0]}")
    return hits[:limit]


_SEARCHERS = {
    "bilibili": search_bilibili,
    "youtube": search_youtube,
    "douyin": search_douyin,
}


def search(query: str, *, platforms: Iterable[str] | None = None, limit: int = 8,
           errors: list[str] | None = None) -> list[PlatformHit]:
    """按平台优先级依次搜索,合并结果(平台顺序即优先级)。结果缓存 24 小时。"""
    out: list[PlatformHit] = []
    for name in (platforms if platforms is not None else config.MEDIA_PLATFORMS):
        fn = _SEARCHERS.get(name)
        if fn is None:
            continue
        try:
            got = _cached_hits(name, query, limit, lambda fn=fn: fn(query, limit=limit))
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"{name} 搜索失败({query}):{str(exc)[:120]}")
            continue
        out.extend(got)
    return out


def _cached_hits(platform: str, query: str, limit: int, fetch: Any) -> list[PlatformHit]:
    return webassets._cached(f"pv_{platform}", query, limit, fetch, factory=PlatformHit)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
class _SilentLogger:
    """yt-dlp 即使 quiet 也会把 ERROR 打到控制台。失败原因已经由调用方写进
    clip_report.json,控制台再刷一遍只会淹没服务日志。"""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def _base_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "logger": _SilentLogger(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "http_headers": {"User-Agent": webassets.BROWSER_UA},
    }
    if config.YTDLP_PROXY:
        opts["proxy"] = config.YTDLP_PROXY
    cookiefile = _cookie_file()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if config.FFMPEG_PATH:
        opts["ffmpeg_location"] = str(Path(config.FFMPEG_PATH).parent)
        _ffmpeg_on_path(Path(config.FFMPEG_PATH).parent)
    return opts


def _cookie_file() -> str | None:
    """`YTDLP_COOKIES` 可以是一个 cookies.txt,也可以是放了多份的文件夹
    (B 站、YouTube 各导出一份)。文件夹时合并成一份交给 yt-dlp。

    合并文件写在 data/ 下而不是原文件夹:yt-dlp 退出时会把 cookie **写回**
    cookiefile,不能让它覆盖用户导出的原件。
    """
    raw = config.YTDLP_COOKIES
    if not raw:
        return None
    src = Path(raw)
    if src.is_file():
        return str(src)
    if not src.is_dir():
        return None
    files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() == ".txt")
    if not files:
        return None
    lines = ["# Netscape HTTP Cookie File"]
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.startswith("# "):
                lines.append(line)
    merged = config.DATA_DIR / "_cookies_merged.txt"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(merged)


def _ffmpeg_on_path(ffmpeg_dir: Path) -> None:
    """把 ffmpeg 目录放进本进程的 PATH。

    ⚠️ 光给 `ffmpeg_location` 不够:yt-dlp 判断"能不能分段下载"时调的是
    `FFmpegFD.available()`,它**不读** `ffmpeg_location`、只查 PATH(源码里
    自带 Fixme)。ffmpeg 不在系统 PATH 时(本机就是),所有 `download_ranges`
    下载都报"ffmpeg is not installed",B 站 / YouTube 通路整条空转。
    """
    current = os.environ.get("PATH", "")
    if str(ffmpeg_dir).lower() not in current.lower().split(os.pathsep):
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + current


def _ytdlp_extract(url: str, *, flat: bool = False) -> dict[str, Any]:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise PlatformError("未安装 yt-dlp:pip install yt-dlp") from exc
    opts = _base_opts()
    opts["skip_download"] = True
    if flat:
        opts["extract_flat"] = "in_playlist"
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _ytdlp_flat_urls(query: str) -> list[str]:
    info = _ytdlp_extract(query, flat=True)
    return [str(e.get("url")) for e in info.get("entries") or []
            if isinstance(e, dict) and e.get("url")]


def probe(url: str) -> dict[str, Any]:
    """取单条视频的完整元数据(时长、标题、上传者)。失败抛 PlatformError。"""
    try:
        return _ytdlp_extract(url)
    except PlatformError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"解析失败({url[:60]}):{str(exc)[:200]}") from exc


#: 只要视频流:成片的声音是旁白 + BGM,原片音轨用不上,省一半流量。
#: 1080p 封顶,mp4 优先(Windows 上省一次转码)。
_MIN_SEGMENT_BYTES = 20_000

FORMAT = "bv*[height<=1080][ext=mp4]/bv*[height<=1080]/b[height<=1080]/bv*/b"


def download_segment(url: str, dest: str | Path, *, start: float, seconds: float,
                     timeout_hint: int = 300) -> Path:
    """只下载 [start, start+seconds] 这一段到 `dest`(扩展名由 yt-dlp 决定)。

    返回实际落盘的文件路径。失败抛 PlatformError。
    """
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import download_range_func
    except ImportError as exc:
        raise PlatformError("未安装 yt-dlp:pip install yt-dlp") from exc

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = dest.with_suffix("")
    for old in dest.parent.glob(stem.name + ".*"):
        old.unlink(missing_ok=True)

    opts = _base_opts()
    opts.update({
        "format": FORMAT,
        "outtmpl": str(stem) + ".%(ext)s",
        # 多切 1 秒余量:关键帧对齐会让实际起点前后漂一点
        "download_ranges": download_range_func(None, [(max(0.0, start), start + seconds + 1)]),
        "force_keyframes_at_cuts": False,
        "socket_timeout": min(60, timeout_hint),
    })
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"下载失败({url[:60]}):{str(exc)[:200]}") from exc

    files = sorted(
        (p for p in dest.parent.glob(stem.name + ".*")
         if p.suffix not in (".part", ".ytdl") and p.stat().st_size > 0),
        key=lambda p: p.stat().st_size, reverse=True,
    )
    if not files:
        raise PlatformError(f"下载后没有文件({url[:60]})")
    # 源站不支持 Range 或索引在文件尾时,分段下载会"成功"产出一个只有容器头的
    # 空壳(几百字节)。当成失败,让调用方换下一个候选。
    if files[0].stat().st_size < _MIN_SEGMENT_BYTES:
        raise PlatformError(f"分段下载得到空文件({url[:60]})")
    return files[0]


def domain_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()
