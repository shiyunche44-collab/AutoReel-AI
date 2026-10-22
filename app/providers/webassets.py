"""维基共享资源 / 维基百科:真实车型照片的可靠来源。

为什么需要它:早期 `images.search` 的 payload 是畸形的(`queries` 发成了
字符串数组),检索永远返回空,平台拿不到真实车型照片,只能退化成 AI 编的
虚构车型 —— 文案讲汉、画面拍一台不存在的汉。payload 修好后检索已经能用,
但维基这条路依然值得保留:它**免密钥、免鉴权、授权明确**(多为 CC BY-SA)、
命名规整,可以按文件名精确过滤,稳定性比通用检索好。

⚠️ 别再写"search 整体不可用"这类结论 —— 那是把本地 payload 的畸形
误判成了工具故障。判断工具是否可用,要拿真实调用结果说话。

注意:维基条目更新有滞后,新车型的官方图可能缺失。找不到时返回空列表,
由调用方继续走下一级降级,不在这里硬造。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import browser_fetch
from .images import ImageRef

#: Wikimedia 要求请求方标明身份,匿名 UA 会被 403
USER_AGENT = "aimovice/1.0 (local AI video automation; single-user local tool)"

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKI_API = "https://en.wikipedia.org/w/api.php"

_TIMEOUT = 25


class WebAssetError(RuntimeError):
    """抓取公开素材失败。调用方应降级而不是中断流水线。"""


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
#: Wikimedia 有明确的 robot policy,短时间重复请求会被 403 限流
#: (实测连打几次 API + 下载就会中招)。所以查询结果落盘缓存,
#: 同一车型重复跑素材层时不再打扰对方。
_CACHE_TTL_SECONDS = 24 * 3600


def _cache_dir() -> Path:
    from .. import config

    d = config.DATA_DIR / "_cache" / "webassets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(kind: str, query: str) -> Path:
    digest = hashlib.sha1(f"{kind}:{query}".encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^\w一-鿿-]+", "_", str(query))[:32]
    return _cache_dir() / f"{kind}_{safe}_{digest}.json"


def _cache_read(path: Path) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - payload.get("ts", 0) > _CACHE_TTL_SECONDS:
        return None
    return payload.get("refs") or []


def _cache_write(path: Path, refs: list[ImageRef]) -> None:
    try:
        path.write_text(json.dumps(
            {"ts": time.time(), "refs": [asdict(r) for r in refs]},
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
    except OSError:
        pass


def _cached(kind: str, query: str, limit: int, fetch: Any, factory: Any = None) -> list:
    """带磁盘缓存的查询。缓存未命中时才真正发请求。

    `factory` 让别的模块复用同一套缓存约定(`clips.py` 的 ClipCandidate 就
    走这里)—— 缓存键、TTL、容错逻辑只有一份,不必各写各的。
    """
    factory = factory or ImageRef
    path = _cache_key(kind, query)
    hit = _cache_read(path)
    if hit is not None:
        refs = [factory(**{k: v for k, v in r.items() if k in factory.__annotations__})
                for r in hit]
        return refs[:limit]
    refs = fetch()
    _cache_write(path, refs)
    return refs[:limit]


def _http_json(url: str, *, timeout: int = _TIMEOUT) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise WebAssetError(f"HTTP {exc.code}: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise WebAssetError(f"{type(exc).__name__}: {exc}") from exc


def _strip_tracking(url: str) -> str:
    """去掉 API 返回的 utm_* 追踪参数,拿到干净的直链。"""
    parts = urllib.parse.urlsplit(url)
    query = [
        (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.startswith("utm_")
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


#: 文件名里出现这些词,基本可以断定不是产品照片(图表/示意图/地图…)
_NOT_A_PRODUCT_PHOTO = (
    "diagram", "chart", "graph", "map", "logo", "icon", "svg", "schematic",
    "sales share", "share by", "timeline", "poster", "stamp", "coat of arms",
    "vesicle", "diagram", "plot",
)


def _significant_tokens(subject: str) -> list[str]:
    """从主体名里取出有区分度的词(品牌 + 型号),用于过滤检索噪音。

    只在主体名是拉丁文时返回词元:中文主体在 Commons 上检索时,命中的
    文件名往往是英文的("2020 BYD Han EV front.jpg"),拿中文词元去匹配
    文件名会把正确答案全部误杀。反过来也不能用中文派生出英文变体 ——
    正则剥掉汉字后只剩 "EV" 这种毫无区分度的词,搜出来全是噪音。
    """
    text = str(subject)
    latin = sum(1 for c in text if "a" <= c.lower() <= "z")
    meaningful = sum(1 for c in text if not c.isspace())
    # 要求主体名**主体**是拉丁文。像 "比亚迪汉 EV" 这种只带两个字母的,
    # 剥掉汉字后剩下的 "EV" 没有区分度,拿它当词元会误杀全部正确答案。
    if meaningful == 0 or latin / meaningful < 0.5:
        return []
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", str(subject))
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    generic = {"ev", "car", "cars", "vehicle", "vehicles", "the", "auto",
               "model", "new", "energy"}
    strong = [t for t in tokens if t.lower() not in generic]
    return strong or tokens


def _title_matches(title: str, tokens: list[str]) -> bool:
    """文件名过滤。词元为空(中文主体)时只挡明显的非产品图。"""
    low = title.lower()
    if any(bad in low for bad in _NOT_A_PRODUCT_PHOTO):
        return False
    if not tokens:
        return True
    hits = sum(1 for t in tokens if t.lower() in low)
    return hits > 0 and hits >= max(1, len(tokens) // 2)


def _imageinfo_to_ref(page: dict[str, Any], query: str,
                      tokens: list[str] | None = None) -> ImageRef | None:
    infos = page.get("imageinfo") or []
    if not infos:
        return None
    info = infos[0]
    url = info.get("thumburl") or info.get("url")
    if not url:
        return None
    title = str(page.get("title", "")).replace("File:", "").strip()
    if not title.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return None
    if tokens is not None and not _title_matches(title, tokens):
        return None
    meta = info.get("extmetadata") or {}

    def _meta(key: str) -> str:
        raw = (meta.get(key) or {}).get("value", "")
        return re.sub(r"<[^>]+>", "", str(raw)).strip()

    return ImageRef(
        url=_strip_tracking(str(url)),
        origin="commons",
        note=f"{title} · {query}",
        license=_meta("LicenseShortName") or "见来源页",
        author=_meta("Artist")[:120],
        width=info.get("width"),
    )


def commons_search(query: str, *, limit: int = 8) -> list[ImageRef]:
    """在维基共享资源里按关键词搜照片。

    `query` 建议直接用车型名(如 "BYD Han"),维基的英文文件名很规整。
    结果会按标题过滤掉图表/示意图与明显不相关的文件 —— Commons 的
    全文检索对 "EV" 这类缩写特别不友好,不筛会混进销量图表甚至生物图。
    """
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",          # File: 命名空间
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": "1600",         # 取缩略图,避免几十 MB 的原图
    }
    data = _http_json(f"{COMMONS_API}?{urllib.parse.urlencode(params)}", timeout=40)
    pages = (data.get("query") or {}).get("pages") or {}
    tokens = _significant_tokens(query)
    refs: list[ImageRef] = []
    for page in pages.values():
        ref = _imageinfo_to_ref(page, query, tokens)
        if ref:
            refs.append(ref)
    return refs


def commons_search_cached(query: str, *, limit: int = 8) -> list[ImageRef]:
    return _cached("commons", query, limit,
                   lambda: commons_search(query, limit=limit))


def wikipedia_images(title: str, *, limit: int = 12) -> list[ImageRef]:
    """取维基百科条目里用到的车型照片。

    条目配图通常比关键词搜索更准 —— 编辑挑的就是有代表性的整车照。
    """
    params = {
        "action": "query",
        "format": "json",
        "titles": title,
        "prop": "pageimages|images",
        "piprop": "original",
        "imlimit": str(limit),
    }
    data = _http_json(f"{WIKI_API}?{urllib.parse.urlencode(params)}")
    pages = (data.get("query") or {}).get("pages") or {}
    refs: list[ImageRef] = []
    for page in pages.values():
        if page.get("missing"):
            return []
        original = page.get("original") or {}
        if original.get("source"):
            refs.append(ImageRef(
                url=_strip_tracking(str(original["source"])),
                origin="wikipedia",
                note=f"{title} 主图",
                license="见条目页",
                width=original.get("width"),
            ))
        for name in (page.get("images") or [])[:limit]:
            file_title = str(name.get("title", ""))
            if not file_title.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                continue
            file_params = {
                "action": "query", "format": "json",
                "titles": file_title, "prop": "imageinfo",
                "iiprop": "url|size|extmetadata", "iiurlwidth": "1600",
            }
            try:
                fdata = _http_json(f"{COMMONS_API}?{urllib.parse.urlencode(file_params)}")
            except WebAssetError:
                continue
            fpages = (fdata.get("query") or {}).get("pages") or {}
            for fpage in fpages.values():
                ref = _imageinfo_to_ref(fpage, title)
                if ref:
                    refs.append(ref)
    return refs

def collect(subject: str, *, limit: int = 8) -> tuple[list[ImageRef], list[str]]:
    """按顺序尝试多个维基入口,合并去重。返回 (参考图, 错误信息)。

    不在拿到第一份结果后就停 —— Commons 关键词搜索常常只给两三张,
    而参考图集需要 3-5 张覆盖不同视角,所以两个入口都要跑,合并到 limit 为止。
    """
    errors: list[str] = []
    refs: list[ImageRef] = []
    seen: set[str] = set()

    # 主体名原样使用。曾试过把中文主体正则"翻译"成英文再搜一遍,但汉字会被
    # 剥光只剩 "EV" 这种无区分度的词,搜出来全是销量图表和生物图 —— 反而更糟。
    # 实测 Commons 对中文查询本身就能命中英文文件名的真实照片。
    attempts: list[tuple[str, Any]] = [
        (f"commons:{subject}",
         lambda: commons_search_cached(subject, limit=limit)),
        (f"wikipedia:{subject}",
         lambda: _cached("wikipedia", subject, limit,
                         lambda: wikipedia_images(subject, limit=limit))),
    ]

    for label, fn in attempts:
        if len(refs) >= limit:
            break
        try:
            got = fn()
        except WebAssetError as exc:
            errors.append(f"{label} 抓取失败:{exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} 抓取失败:{type(exc).__name__}: {exc}")
            continue
        for ref in got:
            if len(refs) >= limit:
                break
            if ref.url in seen:
                continue
            seen.add(ref.url)
            refs.append(ref)
    return refs, errors


# --------------------------------------------------------------------------
# Bing:图片检索与站内网页检索
#
# 走 cn.bing.com 而不是 www.bing.com:国内直连、免密钥。两个用途:
# - `bing_images` 直接拿真实图片(做参考图,也做静图镜头);
# - `bing_web` 拿网页链接 —— 汽车之家 / 懂车帝官图页、车企官网车型页、
#   抖音/西瓜视频页都靠 `site:` 站内检索定位,不必逐站对接各家的搜索接口。
# 页面结构一改解析就会失效,所以一律"解析不出来 = 没结果",不抛异常。
# --------------------------------------------------------------------------
BING_BASE = "https://cn.bing.com"

#: 图片结果里每张图的元数据挂在 `<a class="iusc" m="{...}">` 上(HTML 转义过的 JSON)
_BING_IMG_META_RE = re.compile(r'class="iusc"[^>]*?\sm="([^"]+)"', re.IGNORECASE)
#: 网页结果:`<li class="b_algo"> ... <h2><a href="...">标题</a></h2>`
_BING_WEB_RE = re.compile(
    r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _http_text(url: str, *, timeout: int = _TIMEOUT, referer: str | None = None) -> str:
    """抓网页文本。用浏览器 UA:Bing 和车媒站会给匿名 UA 返回空壳页。"""
    return _http_bytes(url, timeout=timeout, referer=referer, as_text=True)  # type: ignore[return-value]


def _http_bytes(url: str, *, timeout: int = _TIMEOUT, referer: str | None = None,
                as_text: bool = False) -> bytes | str:
    """抓原始字节:编码不是 UTF-8 的页面(汽车之家的 GBK 脚本)由调用方自己解。"""
    import httpx

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    if referer:
        headers["Referer"] = referer
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        raise WebAssetError(f"{type(exc).__name__}: {exc}") from exc
    if resp.status_code >= 400:
        if browser_fetch.should_fallback(url, resp.status_code):
            # 403 / 412 / 429 这类是认出了脚本:换真浏览器再抓一次
            try:
                return browser_fetch.fetch(url, referer=referer, as_text=as_text)
            except browser_fetch.BrowserFetchError as exc:
                raise WebAssetError(f"HTTP {resp.status_code};浏览器兜底:{exc}") from exc
        raise WebAssetError(f"HTTP {resp.status_code}")
    return resp.text if as_text else resp.content


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def parse_bing_images(body: str, query: str = "") -> list[ImageRef]:
    """解析 Bing 图片结果页。纯函数,便于用样例 HTML 测。"""
    refs: list[ImageRef] = []
    seen: set[str] = set()
    for raw in _BING_IMG_META_RE.findall(body):
        try:
            meta = json.loads(html.unescape(raw))
        except (ValueError, TypeError):
            continue
        url = str(meta.get("murl") or "")
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        title = str(meta.get("t") or "")
        if any(bad in title.lower() for bad in _NOT_A_PRODUCT_PHOTO):
            continue
        seen.add(url)
        page = str(meta.get("purl") or "")
        refs.append(ImageRef(
            url=url,
            origin="bing",
            note=title[:120] or query,
            license=f"检索命中:{page or '未知来源页'}",
            source_page=page,
        ))
    return refs


def bing_images(query: str, *, limit: int = 20) -> list[ImageRef]:
    """Bing 图片检索。只取大图(`qft=+filterui:imagesize-large`),小图做不了镜头。"""
    params = {
        "q": query, "first": "0", "count": str(max(limit, 35)),
        "qft": "+filterui:imagesize-large", "mmasync": "1",
    }
    body = _http_text(f"{BING_BASE}/images/async?{urllib.parse.urlencode(params)}")
    return parse_bing_images(body, query)[:limit]


def bing_images_cached(query: str, *, limit: int = 20) -> list[ImageRef]:
    return _cached("bing_img", query, limit, lambda: bing_images(query, limit=limit))


def parse_bing_web(body: str) -> list[ImageRef]:
    """解析 Bing 网页结果。复用 ImageRef 只是为了走同一套磁盘缓存:
    `url` 放网页地址,`note` 放标题。"""
    out: list[ImageRef] = []
    seen: set[str] = set()
    for url, title in _BING_WEB_RE.findall(body):
        url = html.unescape(url)
        if url in seen:
            continue
        seen.add(url)
        out.append(ImageRef(url=url, origin="bing_web",
                            note=re.sub(r"<[^>]+>", "", html.unescape(title)).strip()[:120]))
    return out


def bing_web(query: str, *, limit: int = 10) -> list[str]:
    """Bing 网页检索,返回结果页地址列表。常配合 `site:` 使用。"""
    def _fetch() -> list[ImageRef]:
        params = {"q": query, "count": str(max(limit, 10)), "setlang": "zh-Hans"}
        return parse_bing_web(_http_text(f"{BING_BASE}/search?{urllib.parse.urlencode(params)}"))

    return [r.url for r in _cached("bing_web", query, limit, _fetch)]
