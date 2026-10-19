"""免费图库 Provider:Pexels + Pixabay + Coverr(视频)/ Unsplash(图片)。

定位:**非车画面**的第二梯队。开场钩子、城市夜景、车展人流、充电桩这类
氛围镜头,视频平台搜不到合适的时候,图库里的空镜画质高、没水印、没台标,
比 AI 生成快也更真实。车辆镜头不走这里 —— 图库里的车几乎不可能是目标车型。

每家都要免费 API Key(`.env` 的 PEXELS_API_KEY / PIXABAY_API_KEY /
COVERR_API_KEY / UNSPLASH_ACCESS_KEY),没填的那一家自动跳过。图库检索词要用英文,中文命中率很差。
"""
from __future__ import annotations

import urllib.parse
from typing import Any

from .. import config
from . import webassets
from .clips import ClipCandidate
from .images import ImageRef

PEXELS_VIDEO_API = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_API = "https://api.pexels.com/v1/search"
PIXABAY_VIDEO_API = "https://pixabay.com/api/videos/"
PIXABAY_PHOTO_API = "https://pixabay.com/api/"
COVERR_VIDEO_API = "https://api.coverr.co/videos"
UNSPLASH_PHOTO_API = "https://api.unsplash.com/search/photos"

_TIMEOUT = 25


def _get_json(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    import httpx

    resp = httpx.get(url, headers=headers or {}, timeout=_TIMEOUT, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------
# 解析(纯函数,便于用样例 JSON 测)
# --------------------------------------------------------------------------
def _pick_pexels_file(files: list[dict[str, Any]]) -> str:
    """挑一个 ≤1080p 的 mp4,竖屏优先。原片 4K 动辄上百 MB,用不上。"""
    scored: list[tuple[int, int, str]] = []
    for f in files:
        link = str(f.get("link") or "")
        if not link or "mp4" not in str(f.get("file_type") or "video/mp4"):
            continue
        w, h = int(f.get("width") or 0), int(f.get("height") or 0)
        if max(w, h) > 1920:
            continue
        portrait = 0 if h >= w else 1
        scored.append((portrait, -max(w, h), link))
    if not scored:
        return ""
    return sorted(scored)[0][2]


def parse_pexels_videos(payload: dict[str, Any]) -> list[ClipCandidate]:
    out: list[ClipCandidate] = []
    for v in payload.get("videos") or []:
        link = _pick_pexels_file(v.get("video_files") or [])
        if not link:
            continue
        out.append(ClipCandidate(
            url=link,
            page_url=str(v.get("url") or ""),
            origin="stock:pexels",
            title=str(v.get("url") or "").rstrip("/").rsplit("/", 1)[-1][:80],
            license="Pexels License",
            author=str((v.get("user") or {}).get("name") or ""),
            duration=float(v.get("duration") or 0.0),
        ))
    return out


def parse_pixabay_videos(payload: dict[str, Any]) -> list[ClipCandidate]:
    out: list[ClipCandidate] = []
    for h in payload.get("hits") or []:
        videos = h.get("videos") or {}
        link = ""
        for size in ("large", "medium", "small"):
            item = videos.get(size) or {}
            if item.get("url") and int(item.get("height") or 0) <= 1920:
                link = str(item["url"])
                break
        if not link:
            continue
        out.append(ClipCandidate(
            url=link,
            page_url=str(h.get("pageURL") or ""),
            origin="stock:pixabay",
            title=str(h.get("tags") or "")[:80],
            license="Pixabay Content License",
            author=str(h.get("user") or ""),
            duration=float(h.get("duration") or 0.0),
        ))
    return out


def parse_coverr_videos(payload: dict[str, Any]) -> list[ClipCandidate]:
    """Coverr 竖屏素材少,竖屏的排前面;付费素材跳过。"""
    rows: list[tuple[int, ClipCandidate]] = []
    for h in payload.get("hits") or []:
        if h.get("is_premium"):
            continue
        link = str((h.get("urls") or {}).get("mp4") or "")
        if not link:
            continue
        slug = str(h.get("slug") or h.get("base_filename") or h.get("id") or "")
        rows.append((0 if h.get("is_vertical") else 1, ClipCandidate(
            url=link,
            page_url=f"https://coverr.co/videos/{slug}" if slug else "",
            origin="stock:coverr",
            title=str(h.get("title") or "").strip()[:80],
            license="Coverr License",
            duration=float(h.get("duration") or 0.0),
        )))
    return [c for _, c in sorted(rows, key=lambda r: r[0])]


def parse_pexels_photos(payload: dict[str, Any]) -> list[ImageRef]:
    out: list[ImageRef] = []
    for p in payload.get("photos") or []:
        src = p.get("src") or {}
        url = src.get("portrait") or src.get("large2x") or src.get("original")
        if not url:
            continue
        out.append(ImageRef(
            url=str(url), origin="stock:pexels",
            note=str(p.get("alt") or "")[:120],
            license="Pexels License",
            author=str(p.get("photographer") or ""),
            width=p.get("width"),
            source_page=str(p.get("url") or ""),
        ))
    return out


def parse_pixabay_photos(payload: dict[str, Any]) -> list[ImageRef]:
    out: list[ImageRef] = []
    for h in payload.get("hits") or []:
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        out.append(ImageRef(
            url=str(url), origin="stock:pixabay",
            note=str(h.get("tags") or "")[:120],
            license="Pixabay Content License",
            author=str(h.get("user") or ""),
            width=h.get("imageWidth"),
            source_page=str(h.get("pageURL") or ""),
        ))
    return out


def parse_unsplash_photos(payload: dict[str, Any]) -> list[ImageRef]:
    out: list[ImageRef] = []
    for p in payload.get("results") or []:
        urls = p.get("urls") or {}
        # regular = 宽 1080 的 jpg,竖屏够用;raw/full 动辄十几 MB
        url = urls.get("regular") or urls.get("full")
        if not url:
            continue
        out.append(ImageRef(
            url=str(url), origin="stock:unsplash",
            note=str(p.get("alt_description") or p.get("description") or "")[:120],
            license="Unsplash License",
            author=str((p.get("user") or {}).get("name") or ""),
            width=min(int(p.get("width") or 0), 1080) or None,
            source_page=str((p.get("links") or {}).get("html") or ""),
        ))
    return out


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
def available() -> bool:
    return bool(config.PEXELS_API_KEY or config.PIXABAY_API_KEY
                or config.COVERR_API_KEY or config.UNSPLASH_ACCESS_KEY)


def search_videos(query: str, *, limit: int = 6, errors: list[str] | None = None
                  ) -> list[ClipCandidate]:
    """合并:Pexels 在前(竖屏素材多),Pixabay、Coverr 补位。"""
    out: list[ClipCandidate] = []
    if config.PEXELS_API_KEY:
        def _pexels() -> list[ClipCandidate]:
            qs = urllib.parse.urlencode({"query": query, "per_page": limit,
                                         "orientation": "portrait"})
            return parse_pexels_videos(_get_json(
                f"{PEXELS_VIDEO_API}?{qs}", headers={"Authorization": config.PEXELS_API_KEY}))
        out += _guard("Pexels 视频", query, errors,
                      lambda: webassets._cached("pexels_v", query, limit, _pexels,
                                                factory=ClipCandidate))
    if config.PIXABAY_API_KEY:
        def _pixabay() -> list[ClipCandidate]:
            qs = urllib.parse.urlencode({"key": config.PIXABAY_API_KEY, "q": query[:100],
                                         "per_page": max(3, limit)})
            return parse_pixabay_videos(_get_json(f"{PIXABAY_VIDEO_API}?{qs}"))
        out += _guard("Pixabay 视频", query, errors,
                      lambda: webassets._cached("pixabay_v", query, limit, _pixabay,
                                                factory=ClipCandidate))
    if config.COVERR_API_KEY:
        def _coverr() -> list[ClipCandidate]:
            qs = urllib.parse.urlencode({"query": query, "page_size": max(3, limit),
                                         "urls": "true"})
            return parse_coverr_videos(_get_json(
                f"{COVERR_VIDEO_API}?{qs}",
                headers={"Authorization": f"Bearer {config.COVERR_API_KEY}"}))
        out += _guard("Coverr 视频", query, errors,
                      lambda: webassets._cached("coverr_v", query, limit, _coverr,
                                                factory=ClipCandidate))
    return out


def search_photos(query: str, *, limit: int = 6, errors: list[str] | None = None
                  ) -> list[ImageRef]:
    out: list[ImageRef] = []
    if config.PEXELS_API_KEY:
        def _pexels() -> list[ImageRef]:
            qs = urllib.parse.urlencode({"query": query, "per_page": limit,
                                         "orientation": "portrait"})
            return parse_pexels_photos(_get_json(
                f"{PEXELS_PHOTO_API}?{qs}", headers={"Authorization": config.PEXELS_API_KEY}))
        out += _guard("Pexels 图片", query, errors,
                      lambda: webassets._cached("pexels_p", query, limit, _pexels))
    if config.PIXABAY_API_KEY:
        def _pixabay() -> list[ImageRef]:
            qs = urllib.parse.urlencode({"key": config.PIXABAY_API_KEY, "q": query[:100],
                                         "image_type": "photo", "orientation": "vertical",
                                         "per_page": max(3, limit)})
            return parse_pixabay_photos(_get_json(f"{PIXABAY_PHOTO_API}?{qs}"))
        out += _guard("Pixabay 图片", query, errors,
                      lambda: webassets._cached("pixabay_p", query, limit, _pixabay))
    if config.UNSPLASH_ACCESS_KEY:
        def _unsplash() -> list[ImageRef]:
            qs = urllib.parse.urlencode({"query": query, "per_page": limit,
                                         "orientation": "portrait"})
            return parse_unsplash_photos(_get_json(
                f"{UNSPLASH_PHOTO_API}?{qs}",
                headers={"Authorization": f"Client-ID {config.UNSPLASH_ACCESS_KEY}",
                         "Accept-Version": "v1"}))
        out += _guard("Unsplash 图片", query, errors,
                      lambda: webassets._cached("unsplash_p", query, limit, _unsplash))
    return out


def _guard(label: str, query: str, errors: list[str] | None, fn: Any) -> list:
    try:
        return list(fn())
    except Exception as exc:  # noqa: BLE001 - 图库挂了只是少一个来源
        if errors is not None:
            errors.append(f"{label}检索失败({query}):{str(exc)[:120]}")
        return []
