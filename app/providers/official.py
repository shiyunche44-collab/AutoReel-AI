"""官方素材 Provider:品牌官方号 / 车企官网与媒体中心 / 汽车之家·懂车帝官图库。

为什么单列一层、而且排在最前
----------------------------
"画面里是不是**这台**车"是解说视频最容易翻车的地方。普通 UP 主的视频
可能拍的是别的年款、改装车,检索图可能是同名的别家车;而官方素材 ——
厂家宣传片、官网车型页、汽车之家/懂车帝的"官图"分类 —— 拍的一定是这台车,
画质也最好。所以车辆镜头先找官方,找不到才轮到普通素材。

三条渠道
--------
1. **平台品牌官方号**:不需要逐品牌配置。在平台搜索结果上看上传者:
   名字含品牌名、并且被平台认证或带"官方 / 汽车 / Official"字样,就算官方。
   见 `platform_video.is_official`。这里额外补一轮"{品牌}官方 {车型}"检索,
   专门把官方号的视频捞上来。
2. **车企官网 / 媒体中心**:域名声明在品类 YAML 的 `official_sites` 里。
   车型页靠 Bing `site:域名 车型` 站内检索定位,不写死页面地址 —— 官网改版
   换路径很常见,站内检索不受影响。页面里抓视频直链、嵌入播放器和大图。
3. **汽车之家 / 懂车帝官图库**:同样先站内检索定位车系页,再按图库分类页
   抓图(外观 / 内饰 / 细节分别对应镜头视角)。

⚠️ 2、3 都是解析网页,对方改版就可能失效。所以每个站点各写一个小函数、
全部"解析不出来 = 没结果",坏了单独修,不影响别的渠道。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable, Sequence

from . import clips, platform_video, webassets
from .images import ImageRef

#: 页面上明显不是车照的图:站点 logo、图标、头像、二维码、占位图
_IMG_JUNK = ("logo", "icon", "avatar", "qrcode", "qr_code", "qr-", "sprite", "btn",
             "button", "arrow", "placeholder", "loading", "blank", "favicon", "share",
             "wechat", "weibo", "douyin_", "badge", "banner_small")

_IMG_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()]+?\.(?:jpe?g|png|webp)(?:\?[^\s"'<>()]*)?""",
    re.IGNORECASE,
)
_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']+)["']""", re.IGNORECASE
)


# --------------------------------------------------------------------------
# 品牌识别
# --------------------------------------------------------------------------
def detect_brand(subject: str, official_sites: dict[str, dict[str, tuple[str, ...]]],
                 hint: str = "") -> tuple[str, dict[str, tuple[str, ...]]]:
    """返回 (品牌键, 配置)。识别不出返回 ("", {})。

    `hint` 是 LLM 解析出来的品牌名(可能是"比亚迪"也可能是"BYD"),
    先用它匹配,再用主体名匹配。别名取最长命中,避免"智界"这类
    多家共用的短词抢先。
    """
    best: tuple[int, str] = (0, "")
    for text in (hint, subject):
        low = (text or "").lower().replace(" ", "")
        if not low:
            continue
        for key, spec in official_sites.items():
            for alias in (key, *spec.get("aliases", ())):
                a = alias.lower().replace(" ", "")
                if len(a) >= 2 and a in low and len(a) > best[0]:
                    best = (len(a), key)
        if best[1]:
            break
    if not best[1]:
        return "", {}
    return best[1], official_sites[best[1]]


def brand_aliases(brand_key: str, spec: dict[str, tuple[str, ...]], hint: str = "") -> list[str]:
    out = [a for a in (hint, brand_key, *spec.get("aliases", ())) if a]
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------
# 1. 平台品牌官方号
# --------------------------------------------------------------------------
def platform_official(subject: str, aliases: Sequence[str], *,
                      platforms: Iterable[str] | None = None, limit: int = 8,
                      errors: list[str] | None = None) -> list[platform_video.PlatformHit]:
    """补搜一轮官方号视频,只返回判定为官方的结果。"""
    if not aliases:
        return []
    primary = aliases[0]
    queries = [f"{primary}官方 {subject}", f"{subject} 官方宣传片"]
    out: list[platform_video.PlatformHit] = []
    seen: set[str] = set()
    for q in queries:
        for hit in platform_video.search(q, platforms=platforms, limit=limit, errors=errors):
            if hit.url in seen:
                continue
            seen.add(hit.url)
            if platform_video.is_official(hit, aliases):
                hit.official = True
                out.append(hit)
    return platform_video.rank(out)


# --------------------------------------------------------------------------
# 2. 车企官网 / 媒体中心
# --------------------------------------------------------------------------
def extract_images(body: str, page_url: str) -> list[str]:
    """从页面里抽大图地址。og:image 排最前 —— 那是站方自己挑的头图。"""
    found: list[str] = []
    for raw in (*_OG_IMAGE_RE.findall(body), *_IMG_URL_RE.findall(body.replace("\\/", "/"))):
        url = raw.strip()
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = urllib.parse.urljoin(page_url, url)
        if not url.startswith(("http://", "https://")):
            continue
        name = url.lower().rsplit("/", 1)[-1]
        if any(j in name for j in _IMG_JUNK) or any(j in url.lower() for j in ("/icon", "/logo")):
            continue
        if _looks_tiny(url):
            continue
        if url not in found:
            found.append(url)
    return found


_SIZE_IN_URL_RE = re.compile(r"(?<![0-9])(\d{2,4})x(\d{2,4})(?![0-9])")


def _looks_tiny(url: str) -> bool:
    """URL 里带尺寸(如 120x90)且两边都很小的,是缩略图,做不了镜头。"""
    m = _SIZE_IN_URL_RE.search(url)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    return 0 < max(w, h) < 400


def site_media(subject: str, spec: dict[str, tuple[str, ...]], *, pages_per_site: int = 2,
               errors: list[str] | None = None) -> tuple[list[clips.ClipCandidate], list[ImageRef]]:
    """车企官网 / 媒体中心:站内检索车型页,抓视频与大图。"""
    videos: list[clips.ClipCandidate] = []
    stills: list[ImageRef] = []
    for domain in (*spec.get("sites", ()), *spec.get("media", ())):
        try:
            pages = webassets.bing_web(f"site:{domain} {subject}", limit=pages_per_site + 3)
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"官网站内检索失败({domain}):{str(exc)[:100]}")
            continue
        host = domain.split("/", 1)[0]
        pages = [p for p in pages if host in platform_video.domain_of(p)][:pages_per_site]
        for page in pages:
            body = clips.fetch_page(page)
            if not body:
                continue
            for link in clips.extract_direct_links(body):
                videos.append(clips.ClipCandidate(
                    url=link, page_url=page, origin="official:site", title=page,
                    license=f"车企官网:{page}", official=True,
                ))
            for embed in clips.extract_embeds(body):
                plat = "bilibili" if "bilibili" in embed else "youtube"
                videos.append(clips.ClipCandidate(
                    url=embed, page_url=page, origin=f"official:{plat}", title=page,
                    license=f"车企官网嵌入:{page}", official=True, platform=plat,
                ))
            for img in extract_images(body, page)[:8]:
                stills.append(ImageRef(
                    url=img, origin="official:site", note=page[:120],
                    license=f"车企官网:{page}", source_page=page,
                ))
    return _dedupe(videos), _dedupe(stills)


# --------------------------------------------------------------------------
# 3. 汽车之家 / 懂车帝官图库
# --------------------------------------------------------------------------
#: 汽车之家图库分类页:series/{车系ID}-{分类}.html。分类号与视角的对应:
#: 1 外观、10 中控方向盘、3 车厢座椅、12 其它细节。
AUTOHOME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("1", "exterior_45"), ("10", "interior"), ("3", "interior"), ("12", "detail"),
)
_AUTOHOME_SERIES_RE = re.compile(
    r"(?:car\.autohome\.com\.cn/pic/series(?:-s\d+)?/|www\.autohome\.com\.cn/|"
    r"car\.autohome\.com\.cn/config/series/)(\d{2,6})(?:[-/.]|$)"
)
_AUTOHOME_IMG_RE = re.compile(
    r"(?:https?:)?//car\d?\.autoimg\.cn/[^\s\"'<>()]+?\.(?:jpe?g|png|webp)", re.IGNORECASE
)
#: 缩略图文件名带尺寸前缀(如 480x360_0_q95_c42_autohomecar__xxx.jpg),去掉即原图
_AUTOHOME_THUMB_PREFIX_RE = re.compile(r"/\d+x\d+_[^/]*?(autohomecar__)")


def autohome_series_id(urls: Iterable[str]) -> str:
    for u in urls:
        m = _AUTOHOME_SERIES_RE.search(u)
        if m:
            return m.group(1)
    return ""


def parse_autohome_images(body: str) -> list[str]:
    out: list[str] = []
    for raw in _AUTOHOME_IMG_RE.findall(body):
        url = ("https:" + raw) if raw.startswith("//") else raw
        url = _AUTOHOME_THUMB_PREFIX_RE.sub(r"/\1", url)
        if any(j in url.lower() for j in _IMG_JUNK):
            continue
        if url not in out:
            out.append(url)
    return out


def autohome_images(subject: str, *, per_category: int = 6,
                    errors: list[str] | None = None) -> list[ImageRef]:
    # 检索失败直接抛给 gallery_images 记录:返回空列表会被磁盘缓存 24 小时
    pages = webassets.bing_web(f"site:autohome.com.cn {subject} 图片", limit=8)
    series = autohome_series_id(pages)
    if not series:
        return []
    refs: list[ImageRef] = []
    for cat, angle in AUTOHOME_CATEGORIES:
        page = f"https://car.autohome.com.cn/pic/series/{series}-{cat}.html"
        try:
            body = webassets._http_text(page, referer="https://www.autohome.com.cn/")
        except Exception:  # noqa: BLE001
            continue
        for url in parse_autohome_images(body)[:per_category]:
            refs.append(ImageRef(
                url=url, origin="official:autohome", angle=angle,
                note=f"汽车之家官图 · 车系 {series}",
                license=f"汽车之家图库:{page}", source_page=page,
            ))
    return _dedupe(refs)


_DCD_SERIES_RE = re.compile(r"dongchedi\.com/auto/series/(\d{2,8})")
_DCD_IMG_RE = re.compile(
    r"(?:https?:)?(?:\\u002F|/){2}p\d+-dcd[\w-]*\.byteimg\.com(?:\\u002F|/)[^\s\"'<>()]+",
    re.IGNORECASE,
)


def parse_dongchedi_images(body: str) -> list[str]:
    out: list[str] = []
    for raw in _DCD_IMG_RE.findall(body):
        url = raw.replace("\\u002F", "/")
        url = ("https:" + url) if url.startswith("//") else url
        if _looks_tiny(url) or any(j in url.lower() for j in _IMG_JUNK):
            continue
        if url not in out:
            out.append(url)
    return out


def dongchedi_images(subject: str, *, limit: int = 12,
                     errors: list[str] | None = None) -> list[ImageRef]:
    # 检索失败直接抛给 gallery_images 记录:返回空列表会被磁盘缓存 24 小时
    pages = webassets.bing_web(f"site:dongchedi.com {subject} 图片", limit=8)
    targets: list[str] = []
    m = next((_DCD_SERIES_RE.search(p) for p in pages if _DCD_SERIES_RE.search(p)), None)
    if m:
        targets.append(f"https://www.dongchedi.com/auto/series/{m.group(1)}/images")
    targets += [p for p in pages if "dongchedi.com" in p][:2]
    refs: list[ImageRef] = []
    for page in dict.fromkeys(targets):
        try:
            body = webassets._http_text(page, referer="https://www.dongchedi.com/")
        except Exception:  # noqa: BLE001
            continue
        for url in parse_dongchedi_images(body):
            refs.append(ImageRef(
                url=url, origin="official:dongchedi", note="懂车帝图库",
                license=f"懂车帝图库:{page}", source_page=page,
            ))
            if len(refs) >= limit:
                return _dedupe(refs)
    return _dedupe(refs)


def gallery_images(subject: str, *, errors: list[str] | None = None) -> list[ImageRef]:
    """官图库合并:汽车之家(按视角分好类)在前,懂车帝补位。结果缓存 24 小时。"""
    out: list[ImageRef] = []
    for kind, fn in (("autohome", autohome_images), ("dongchedi", dongchedi_images)):
        try:
            out += webassets._cached(f"official_{kind}", subject, 40,
                                     lambda fn=fn: fn(subject, errors=errors))
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"{kind} 官图抓取失败:{str(exc)[:100]}")
    return out


# --------------------------------------------------------------------------
def _dedupe(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        out.append(it)
    return out
