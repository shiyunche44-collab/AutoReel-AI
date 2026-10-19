"""真实视频片段 Provider:维基共享资源 + 车媒文章页内嵌视频。

为什么需要它
------------
成片里车的外观一直是 AI 画出来的。参考图能锁住 H3 的主体,但锁不住
"这台车真实开起来是什么样";而**外观镜头用真实片段**还有第二重收益:
H3 走 account credits(不吃套餐额度),每个成片镜头 $0.48 + 15–30 分钟
墙钟,换成真实片段等于直接省掉。

为什么没有视频检索 connector
----------------------------
`connector__matrix__*` 目录里只有视频**生成**与视频**理解**,没有视频检索。
所以真实片段只能自建抓取,分两条路:

- **V1 维基共享资源**(`commons_search_videos`)—— 免密钥、授权明确、命名规整,
  可以按文件名过滤。但车型视频存量很少,通常只能覆盖一两个镜头。
- **V2 车媒文章页**(`page_video_urls`)—— `images.search` 返回的 `link` 就是
  车媒文章页,页面里常常直接带 mp4 / m3u8 直链。实测命中率不高
  (`tests/probe_clip_sources.py` 探到 2/14),而且余下的多是 YouTube / B 站
  iframe,拿不到直链。所以 V2 是**补充**不是主力,取不到就安静地少几个镜头。

B 站 / YouTube / 抖音这类平台视频走 `platform_video.py`(yt-dlp)。这里的
V2 抓到的 iframe 也不再丢弃:`page_media()` 会把嵌入的 B 站 / YouTube 播放器
还原成视频页地址,交给 yt-dlp 去下。

拉流一律走 ffmpeg —— 它原生支持 HLS(m3u8)、webm 转码与定长裁剪,
所以 V1/V2 两条路都收敛到同一个函数,不需要额外依赖。
"""
from __future__ import annotations

import re
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from . import webassets

#: 匿名 UA 会被部分 CDN 403 掉,必须带
USER_AGENT = webassets.USER_AGENT
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

#: 页面里的视频直链。刻意写成宽松形态:车媒的地址常见两种变形 ——
#: 协议相对(`//cdn/a.mp4`)和转义过的 JSON 串(`http:\/\/cdn\/a.mp4`)。
#: 只认绝对 https 会漏掉一大半,然后误判成"这站不给直链"。
#: 中间那段刻意允许 `\/` 作为普通字符,否则转义串会在第一个斜杠处断掉。
DIRECT_LINK_RE = re.compile(
    r"(?:https?:)?(?:\\?/){2}(?:[^\"'\s<>\\]|\\/)+?\.(?:mp4|m3u8)(?:[^\"'\s<>]*)",
    re.IGNORECASE,
)

#: `<video src=...>` / `<source src=...>` 标签,兜住属性里带协议相对地址的情况
_TAG_SRC_RE = re.compile(
    r"<(?:video|source)[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE
)

#: 只有 iframe / 需要签名解密,拿不到直链的站点。提前跳过,别浪费请求
_UNREACHABLE_HOSTS = (
    "youtube.com", "youtu.be", "bilibili.com", "zhihu.com",
    "tiktok.com", "facebook.com", "instagram.com", "v.qq.com",
)

_HTTP_TIMEOUT = 25

#: 横屏素材 → 9:16:完整前景 + 模糊铺底。与静图镜头(render.still_filter)同一套构图
VERTICAL_FILTER = (
    f"split[a][b];"
    f"[a]scale={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT}:force_original_aspect_ratio=increase,"
    f"crop={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT},boxblur=24:2,setsar=1[bg];"
    f"[b]scale={config.OUTPUT_WIDTH}:{config.OUTPUT_HEIGHT}:force_original_aspect_ratio=decrease,"
    f"setsar=1[fg];"
    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=24,format=yuv420p"
)


class ClipError(RuntimeError):
    """抓取或转码片段失败。调用方应降级而不是中断流水线。"""


@dataclass
class ClipCandidate:
    """一条可用作成片镜头的真实视频片段。"""
    url: str
    #: 来源文章页 —— 直链是 CDN 临时地址,事后靠这个追溯出处
    page_url: str = ""
    #: 来源标记:clip_commons / clip_media
    origin: str = "clip_commons"
    title: str = ""
    license: str = ""
    author: str = ""
    #: 原始时长(秒);0 表示未知
    duration: float = 0.0
    #: 视角标签:exterior / interior / rear / detail
    view: str = ""
    #: 下载/转码后的本地路径
    local_path: str = ""
    #: 素材形态:video = 视频片段;still = 静态图片(落地时做成推拉动效镜头)
    kind: str = "video"
    #: 平台视频的截取起点(秒)。同一条长视频按不同起点切给多个镜头
    start: float = 0.0
    #: 平台名(bilibili / youtube / douyin)。非空表示 `url` 是**视频页**而不是
    #: 直链,落地要走 yt-dlp
    platform: str = ""
    #: 是否来自官方渠道(品牌官方号 / 车企官网 / 官图库)
    official: bool = False
    #: 上传者 / 频道名,用于判定官方号与报告追溯
    uploader: str = ""


# --------------------------------------------------------------------------
# V1:维基共享资源视频
# --------------------------------------------------------------------------
def commons_search_videos(query: str, *, limit: int = 6) -> list[ClipCandidate]:
    """在维基共享资源里搜车型视频。

    走 `prop=videoinfo`,取转码后的 derivatives 而不是原文件 —— 原文件动辄
    几百 MB 的 webm/ogv,而 derivatives 里有 480p/720p 的轻量版本。
    """
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:video {query}",
        "gsrnamespace": "6",           # File 命名空间
        "gsrlimit": str(limit * 2),    # 多取一些,过滤会砍掉不少
        "prop": "videoinfo",
        "viprop": "url|size|mime|derivatives|extmetadata",
    }
    try:
        data = webassets._http_json(
            f"{webassets.COMMONS_API}?{urllib.parse.urlencode(params)}", timeout=40
        )
    except webassets.WebAssetError:
        return []
    return _commons_candidates(data, query, limit)


def _commons_candidates(data: dict[str, Any], query: str, limit: int) -> list[ClipCandidate]:
    pages = (data.get("query") or {}).get("pages") or {}
    tokens = webassets._significant_tokens(query)
    out: list[ClipCandidate] = []
    for page in pages.values():
        infos = page.get("videoinfo") or []
        if not infos:
            continue
        info = infos[0]
        title = str(page.get("title", "")).replace("File:", "").strip()
        # 复用图片侧的标题过滤:图表/示意图/徽标对视频同样没意义
        if tokens and not webassets._title_matches(title, tokens):
            continue
        src = _best_derivative(info.get("derivatives") or []) or info.get("url")
        if not src:
            continue
        meta = info.get("extmetadata") or {}

        def _meta(key: str) -> str:
            raw = (meta.get(key) or {}).get("value", "")
            return re.sub(r"<[^>]+>", "", str(raw)).strip()

        out.append(ClipCandidate(
            url=webassets._strip_tracking(str(src)),
            page_url=str(info.get("descriptionurl") or page.get("title", "")),
            origin="clip_commons",
            title=title,
            license=_meta("LicenseShortName") or "见来源页",
            author=_meta("Artist")[:120],
            duration=float(info.get("duration") or 0.0),
        ))
        if len(out) >= limit:
            break
    return out


def _best_derivative(derivatives: Sequence[dict[str, Any]]) -> str:
    """从转码列表里挑一个"够用又不至于几百 MB"的版本。

    按分辨率降序取第一个 ≤720p 的;全都没有就退而取最低的。
    mp4 优先于 webm —— Windows 上省掉一次转码。
    """
    if not derivatives:
        return ""
    scored: list[tuple[int, int, str]] = []
    for d in derivatives:
        src = str(d.get("src") or "")
        if not src:
            continue
        height = int(d.get("height") or 0)
        mp4_bonus = 0 if src.lower().split("?")[0].endswith((".mp4", ".m3u8")) else 1
        scored.append((height, mp4_bonus, src))
    if not scored:
        return ""
    small = [s for s in scored if 0 < s[0] <= 720]
    pool = small or sorted(scored)[:1]
    pool.sort(key=lambda s: (-s[0], s[1]))
    return pool[0][2]


def commons_search_videos_cached(query: str, *, limit: int = 6) -> list[ClipCandidate]:
    return webassets._cached(
        "commons_video", query, limit,
        lambda: commons_search_videos(query, limit=limit),
        factory=ClipCandidate,
    )


# --------------------------------------------------------------------------
# V2:车媒文章页内嵌视频
# --------------------------------------------------------------------------
def page_video_urls(page_url: str, *, timeout: int = _HTTP_TIMEOUT) -> list[str]:
    """抓文章页,返回里面能直接下载的视频地址。取不到就返回空列表。

    这一层是"尽力而为":车媒页面里嵌 YouTube 是常态,实测直链命中率很低。
    拿不到不是错误,是正常结果 —— 调用方据此少排几个镜头,其余回退生成。
    """
    return page_media(page_url, timeout=timeout)[0]


def page_media(page_url: str, *, timeout: int = _HTTP_TIMEOUT) -> tuple[list[str], list[str]]:
    """抓一次页面,同时返回 (视频直链, 嵌入的平台视频页地址)。

    嵌入的 B 站 / YouTube 播放器过去被当成"拿不到"直接丢掉;现在有 yt-dlp,
    把它们还原成视频页地址就能下。
    """
    body = fetch_page(page_url, timeout=timeout)
    if not body:
        return [], []
    return extract_direct_links(body), extract_embeds(body)


def fetch_page(page_url: str, *, timeout: int = _HTTP_TIMEOUT) -> str:
    """抓页面 HTML;平台页本身(iframe 宿主)和任何失败都返回空串。"""
    if not page_url or _is_unreachable(page_url):
        return ""
    try:
        import httpx

        resp = httpx.get(page_url, headers={"User-Agent": BROWSER_UA},
                         timeout=timeout, follow_redirects=True)
        return resp.text
    except Exception:  # noqa: BLE001 - 任何抓取失败都只是"这个源没有"
        return ""


def extract_direct_links(body: str) -> list[str]:
    urls: list[str] = []
    for raw in (*DIRECT_LINK_RE.findall(body), *_TAG_SRC_RE.findall(body)):
        link = _normalise_link(raw)
        if link and link not in urls:
            urls.append(link)
    return urls


#: 嵌入播放器 → 视频页。只认能还原出唯一视频 ID 的写法。
_EMBED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"player\.bilibili\.com/player\.html\?[^\"'\s<>]*?bvid=(BV[0-9A-Za-z]{10})"),
     "https://www.bilibili.com/video/{}"),
    (re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10})"),
     "https://www.bilibili.com/video/{}"),
    (re.compile(r"youtube(?:-nocookie)?\.com/embed/([0-9A-Za-z_-]{11})"),
     "https://www.youtube.com/watch?v={}"),
    (re.compile(r"youtu\.be/([0-9A-Za-z_-]{11})"),
     "https://www.youtube.com/watch?v={}"),
)


def extract_embeds(body: str) -> list[str]:
    out: list[str] = []
    for pattern, template in _EMBED_PATTERNS:
        for vid in pattern.findall(body):
            url = template.format(vid)
            if url not in out:
                out.append(url)
    return out


def _is_unreachable(url: str) -> bool:
    return any(h in url for h in _UNREACHABLE_HOSTS)


def _normalise_link(raw: str) -> str:
    """把协议相对地址、转义斜杠补成完整的 https 地址。"""
    link = raw.replace("\\/", "/").replace("\\u002F", "/").strip()
    if not link or link.startswith("data:"):
        return ""
    if link.startswith("//"):
        link = "https:" + link
    if not link.startswith(("http://", "https://")):
        return ""
    # 只认真的视频直链 —— 页面里同一张图的缩略图常和视频混在一起
    if not DIRECT_LINK_RE.search(link):
        return ""
    return link


# --------------------------------------------------------------------------
# 落地:ffmpeg 拉流 + 定长裁剪 + 统一封装
# --------------------------------------------------------------------------
def _ffmpeg() -> str:
    if not config.FFMPEG_PATH or not config.FFMPEG_PATH.is_file():
        raise ClipError("未找到 ffmpeg,无法处理真实视频片段")
    return str(config.FFMPEG_PATH)


def fetch_clip(url: str, dest: str | Path, *, seconds: float | None = None,
               referer: str | None = None, loop: bool = False,
               timeout: int = 600) -> Path:
    """把一条视频直链抓成定长、竖屏友好的本地 mp4。

    一律用 ffmpeg 而不是 httpx 直下,是为了用同一个函数覆盖三种源:
    m3u8(ffmpeg 原生 HLS)、webm/ogv(转码)、普通 mp4(定长裁剪)。
    少一条下载分支,少一类"这个源特殊处理一下"的 bug。

    `loop=True` 用于源比目标短的情况(`-stream_loop -1`),短片段循环补足
    到网格秒,否则渲染层归一化时会拿到一个短于 6 秒的片段,音画就会错位。
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    length = seconds if seconds and seconds > 0 else config.SHOT_GRID_SECONDS

    args: list[str] = [_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error"]
    # UA / Referer 是 http 协议的输入选项。本地文件(yt-dlp 先落地的分段)
    # 带上它们 ffmpeg 会直接报 "Option not found" 拒绝打开
    if str(url).startswith(("http://", "https://")):
        args += ["-user_agent", BROWSER_UA]
        if referer:
            args += ["-headers", f"Referer: {referer}\r\n"]
    if loop:
        args += ["-stream_loop", "-1"]
    args += [
        "-i", url,
        "-t", str(length),
        # 直接排成 9:16,但**不裁画面**:B 站 / YouTube 的车视频几乎都是 16:9,
        # 交给渲染层 normalise_clip 居中裁 9:16 只剩三分之一宽,车头车尾全被切掉,
        # 质检随即判 cropped 弃用 —— 真实素材通路会大面积空转。所以这里把完整
        # 画面放中间、上下用同一画面放大模糊铺满;竖屏源则正好铺满,不受影响。
        # 输出已是 1080×1920,后面 normalise_clip 的 scale+crop 等于空操作。
        "-vf", VERTICAL_FILTER,
        "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dest),
    ]
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise ClipError(f"片段抓取失败({url[:60]}):{(proc.stderr or '')[-300:]}")
    return dest


def probe_duration(path: str | Path) -> float:
    """探测本地片段时长;失败返回 0。"""
    from ..pipeline import timeline

    return timeline.probe_safe(Path(path), default=0.0)


def page_urls_from(refs: Iterable[Any], *, limit: int = 12) -> list[str]:
    """从 ImageRef 列表里挑出可抓的来源页地址(去重、跳过不可达站点)。"""
    out: list[str] = []
    for ref in refs:
        page = getattr(ref, "source_page", "") or ""
        if page and page not in out and not _is_unreachable(page):
            out.append(page)
            if len(out) >= limit:
                break
    return out
