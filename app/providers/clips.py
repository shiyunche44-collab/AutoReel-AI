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

⚠️ 为什么不用 yt-dlp:它能拿下 YouTube,但要装第三方包,而且 ToS 边界模糊。
这里的需求是"个人学习、不商用",大可不必把平台默认路径押在它上面。
需要时再单独装,不必成为运行前提。

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
    if not page_url or _is_unreachable(page_url):
        return []
    try:
        import httpx

        resp = httpx.get(page_url, headers={"User-Agent": BROWSER_UA},
                         timeout=timeout, follow_redirects=True)
        body = resp.text
    except Exception:  # noqa: BLE001 - 任何抓取失败都只是"这个源没有"
        return []

    urls: list[str] = []
    for raw in (*DIRECT_LINK_RE.findall(body), *_TAG_SRC_RE.findall(body)):
        link = _normalise_link(raw)
        if link and link not in urls:
            urls.append(link)
    return urls


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

    args: list[str] = [
        _ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-user_agent", BROWSER_UA,
    ]
    if referer:
        args += ["-headers", f"Referer: {referer}\r\n"]
    if loop:
        args += ["-stream_loop", "-1"]
    args += [
        "-i", url,
        "-t", str(length),
        # 只压高度、不裁剪:画幅统一交给渲染层的 normalise_clip,
        # 这里再裁一遍会把车切掉(16:9 裁 9:16 会丢掉左右两侧)。
        "-vf", "scale=-2:1080,fps=24,setsar=1",
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
