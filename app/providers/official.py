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
   专门把官方号的视频捞上来。TikTok 例外:没有可用的站内搜索,改为直接拉
   YAML `tiktok:` 里人工核对过的官方号主页(`tiktok_official`)。
2. **车企官网 / 媒体中心**:域名声明在品类 YAML 的 `official_sites` 里。
   车型页靠 Bing `site:域名 车型` 站内检索定位,不写死页面地址 —— 官网改版
   换路径很常见,站内检索不受影响。页面里抓视频直链、嵌入播放器和大图。
3. **汽车之家 / 懂车帝官图库**:同样先站内检索定位车系页,再按图库分类页
   抓图(外观 / 内饰 / 细节分别对应镜头视角)。

⚠️ 2、3 都是解析网页,对方改版就可能失效。所以每个站点各写一个小函数、
全部"解析不出来 = 没结果",坏了单独修,不影响别的渠道。
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .. import config
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
                      errors: list[str] | None = None,
                      markers: Sequence[str] = ()) -> list[platform_video.PlatformHit]:
    """补搜一轮官方号视频,只返回判定为官方的结果。"""
    if not aliases:
        return []
    primary = aliases[0]
    queries = [f"{primary}官方 {subject}", f"{subject} 官方宣传片"]
    # TikTok 不靠"上传者名含品牌"判官方(叫 bydglobal 的号发的是油画),走 tiktok_official
    platforms = [p for p in (config.MEDIA_PLATFORMS if platforms is None else platforms)
                 if p != "tiktok"]
    out: list[platform_video.PlatformHit] = []
    seen: set[str] = set()
    for q in queries:
        for hit in platform_video.search(q, platforms=platforms, limit=limit, errors=errors):
            if hit.url in seen:
                continue
            seen.add(hit.url)
            if platform_video.is_official(hit, aliases, markers):
                hit.official = True
                out.append(hit)
    return platform_video.rank(out)


#: 判断"文案对不对题"时不算数的词:品牌号每条文案都带品牌名
_GENERIC_TOKENS = {"ev", "car", "cars", "the", "new", "auto", "official", "global", "motors"}


def tiktok_on_topic(title: str, subject: str, names: Sequence[str] = (),
                    aliases: Sequence[str] = ()) -> bool:
    """品牌号的一条文案是不是在说这台车。

    比通用的 `title_relevant` 严:后者两个词元命中一半就算,而品牌号的文案
    **每条**都带品牌名,"nio ET9" 只靠 "nio" 就会把 ES9、ET5 全收进来。
    这里把品牌词去掉,要求剩下的型号词**全部**以整词出现;中文主体则要求
    整串出现(TikTok 文案很少有中文,宁可漏也不错收)。
    """
    low = title.lower()
    brand = {a.lower() for a in aliases if re.fullmatch(r"[A-Za-z]{3,}", a or "")}
    for name in (subject, *names):
        toks = [t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
                if t not in brand and t not in _GENERIC_TOKENS]
        if toks and all(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low)
                        for t in toks):
            return True
    core = re.sub(r"\s+", "", subject or "").lower()
    return bool(core) and core in re.sub(r"\s+", "", low)


def tiktok_official(subject: str, handles: Sequence[str], *, names: Sequence[str] = (),
                    aliases: Sequence[str] = (),
                    errors: list[str] | None = None) -> list[platform_video.PlatformHit]:
    """拉品牌 TikTok 官方号最近的视频,只留文案里提到这台车的。

    账号是 YAML 里人工核对过的,所以直接算官方。品牌号什么车都发,
    文案对题是必须的(`tiktok_on_topic`):`names` 是车型的其他叫法(英文名),
    TikTok 文案几乎全是英文,只认中文主体会把它们全部筛掉。
    """
    out: list[platform_video.PlatformHit] = []
    for handle in handles:
        try:
            hits = platform_video.tiktok_account(handle)
        except Exception as exc:  # noqa: BLE001 - 一个账号挂了不影响别的
            if errors is not None:
                errors.append(f"tiktok 官方号 @{handle} 失败:{str(exc)[:120]}")
            continue
        for h in hits:
            if h.title and tiktok_on_topic(h.title, subject, names, aliases):
                h.official = True
                out.append(h)
    return out


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
#: 1 外观、10 中控方向盘、3 车厢座椅、12 其它细节、53 官图(厂家发布的
#: 宣传照,视角不定,不标)。55 车展不要:人比车多。
AUTOHOME_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("1", "exterior_45"), ("10", "interior"), ("3", "interior"), ("12", "detail"),
    ("53", ""),
)
#: 汽车之家选车对比页用的全车系目录(品牌 → 厂商 → 车系),GBK 编码的 JS。
#: 按车型名查车系 ID 靠它,不靠搜索引擎 —— cn.bing 会无视 `site:`,
#: 查询里带"图片"二字时只回小米官网首页(小米 YU7 项目实测)。
AUTOHOME_CATALOG_URL = "https://car.autohome.com.cn/javascript/NewSpecCompare.js"
_CATALOG_TTL_SECONDS = 7 * 24 * 3600
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


def parse_autohome_catalog(text: str) -> list[tuple[str, str, str]]:
    """全车系目录 → [(车系 ID, 品牌名, 车系名)]。解析不出来返回空。"""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        brands = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    out: list[tuple[str, str, str]] = []
    for brand in brands if isinstance(brands, list) else []:
        for maker in brand.get("List") or []:
            for series in maker.get("List") or []:
                if series.get("I") and series.get("N"):
                    out.append((str(series["I"]), str(brand.get("N") or ""), str(series["N"])))
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def match_autohome_series(subject: str, catalog: Sequence[tuple[str, str, str]]) -> str:
    """按车型名在目录里找车系 ID。

    依次认:车系名(或"品牌 + 车系名")与主体完全相同 → 主体里包含的最长车系名
    ("比亚迪汉 EV" 认"比亚迪汉")→ 包含主体的最短车系名(主体只写了"YU7")。
    车系名至少 2 个字,免得"汉"这类单字到处命中。
    """
    want = _norm(subject)
    if not want:
        return ""
    names: list[tuple[str, str]] = []
    for sid, brand, name in catalog:
        for full in (_norm(name), _norm(brand) + _norm(name)):
            if len(full) >= 2:
                names.append((full, sid))
    exact = [sid for full, sid in names if full == want]
    if exact:
        return exact[0]
    inside = [(len(full), sid) for full, sid in names if full in want]
    if inside:
        return max(inside)[1]
    around = [(len(full), sid) for full, sid in names if want in full]
    return min(around)[1] if around else ""


def autohome_catalog() -> list[tuple[str, str, str]]:
    """取全车系目录,磁盘缓存 7 天(新车上市才会变)。失败抛 WebAssetError。"""
    from .. import config

    path = config.DATA_DIR / "_cache" / "autohome_catalog.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload.get("ts", 0) < _CATALOG_TTL_SECONDS and payload.get("rows"):
            return [tuple(r) for r in payload["rows"]]  # type: ignore[misc]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    raw = webassets._http_bytes(AUTOHOME_CATALOG_URL, referer="https://car.autohome.com.cn/")
    rows = parse_autohome_catalog(raw.decode("gbk", errors="replace"))
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "rows": rows}, ensure_ascii=False),
                        encoding="utf-8")
    return rows


def find_autohome_series(subject: str, errors: list[str] | None = None) -> str:
    """车型名 → 汽车之家车系 ID。先查目录,目录不可用时退回站内检索。"""
    try:
        sid = match_autohome_series(subject, autohome_catalog())
        if sid:
            return sid
    except Exception as exc:  # noqa: BLE001
        if errors is not None:
            errors.append(f"汽车之家车系目录不可用:{str(exc)[:100]}")
    # 不带"图片"二字:带了 cn.bing 只回品牌官网
    return autohome_series_id(webassets.bing_web(f"site:autohome.com.cn {subject}", limit=10))


def autohome_images(subject: str, *, per_category: int = 6,
                    errors: list[str] | None = None) -> list[ImageRef]:
    # 检索失败直接抛给 gallery_images 记录:返回空列表会被磁盘缓存 24 小时
    series = find_autohome_series(subject, errors)
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


#: 懂车帝站内搜索接口:回的 JSON 里带 series_id / series_name
DCD_SEARCH_API = ("https://www.dongchedi.com/motor/searchapi/search_content/"
                  "?keyword={q}&offset=0&count=10&cur_tab=1")


def find_dongchedi_series(subject: str) -> str:
    """车型名 → 懂车帝车系 ID。走站内搜索接口,车系名与主体最接近的优先。"""
    body = webassets._http_text(DCD_SEARCH_API.format(q=urllib.parse.quote(subject)),
                                referer="https://www.dongchedi.com/")
    found: list[tuple[str, str]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("series_name") or ""
            if node.get("series_id") and name:
                found.append((str(node["series_id"]), str(name)))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    try:
        _walk(json.loads(body))
    except (json.JSONDecodeError, ValueError):
        return ""
    return match_autohome_series(subject, [(sid, "", name) for sid, name in found])


def dongchedi_images(subject: str, *, limit: int = 12,
                     errors: list[str] | None = None) -> list[ImageRef]:
    """懂车帝图库。PC 版图库页是前端渲染的空壳,要抓手机版。
    图多是 640 宽的文章配图,只做汽车之家的补位。"""
    series = find_dongchedi_series(subject)
    if not series:
        return []
    refs: list[ImageRef] = []
    for page in (f"https://m.dongchedi.com/auto/series/{series}/images",):
        try:
            body = webassets._http_text(page, referer="https://m.dongchedi.com/")
        except Exception:  # noqa: BLE001
            continue
        for url in parse_dongchedi_images(body):
            url = html.unescape(url)
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
            out += webassets._cached(f"official_{kind}_v2", subject, 60,
                                     lambda fn=fn: fn(subject, errors=errors))
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"{kind} 官图抓取失败:{str(exc)[:100]}")
    return out


# --------------------------------------------------------------------------
# 4. 品牌官网 + 图片检索(非汽车品类的静图来源)
# --------------------------------------------------------------------------
#: 图片检索的查询词:官方主图、详情页图是电商和品牌站自己拍的产品照
PRODUCT_IMAGE_QUERIES: tuple[str, ...] = ("{subject} 官方 主图", "{subject} 详情页")


def product_images(subject: str, spec: dict[str, tuple[str, ...]] | None = None, *,
                   per_query: int = 12, errors: list[str] | None = None) -> list[ImageRef]:
    """品牌官网大图(站内检索产品页)+ Bing 大图检索。

    不直接爬京东 / 天猫(反爬重、地址常变),电商详情页靠手动 `library add` 贴链接。
    检索结果里混着别家的同类产品,入池后照常打标签,认不出是目标产品的不当背景。
    """
    out: list[ImageRef] = []
    if spec and (spec.get("sites") or spec.get("media")):
        try:
            _, stills = site_media(subject, spec, errors=errors)
            out += stills
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"品牌官网抓图失败:{str(exc)[:100]}")
    for q in PRODUCT_IMAGE_QUERIES:
        query = q.format(subject=subject)
        try:
            out += webassets.bing_images_cached(query, limit=per_query)
        except Exception as exc:  # noqa: BLE001
            if errors is not None:
                errors.append(f"图片检索失败({query}):{str(exc)[:100]}")
    return _dedupe(out)


@dataclass(frozen=True)
class Gallery:
    """一个静图来源。`fetch(subject, spec, errors)` 返回 ImageRef;`referer` 下载时带上。"""
    fetch: Callable[..., list[ImageRef]]
    referer: str = ""


#: profile 的 `galleries` 里可以写的名字
GALLERIES: dict[str, Gallery] = {
    "autohome": Gallery(lambda subject, spec, errors: autohome_images(
        subject, per_category=12, errors=errors),
                        referer="https://car.autohome.com.cn/"),
    "dongchedi": Gallery(lambda subject, spec, errors: dongchedi_images(subject, errors=errors),
                         referer="https://www.dongchedi.com/"),
    "product_images": Gallery(lambda subject, spec, errors: product_images(
        subject, spec, errors=errors)),
}


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
