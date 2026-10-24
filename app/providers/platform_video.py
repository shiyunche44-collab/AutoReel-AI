"""平台视频 Provider:B 站 / YouTube / 抖音(西瓜)/ TikTok 按关键词搜索 + 分段下载。

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
- **TikTok**:主通路是**品牌官方号主页**(yt-dlp `tiktok:user`,账号名单在品类
  YAML 里人工核对);关键词检索借 DuckDuckGo `site:tiktok.com`,遇到人机验证
  就跳过。TikTok 源本身就是竖屏,正好铺满 9:16。国内需要 `YTDLP_PROXY`。

所有函数都不向上抛网络异常 —— 素材层的第一原则是降级而不是阻断。
"""
from __future__ import annotations

import html
import os
import re
import shutil
import time
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
#: 自媒体太多,不能单凭它判。品类专属的(汽车的 motors、auto……)由调用方传 `markers`
_OFFICIAL_MARKERS = ("官方", "official", "global", "china", "中国", "集团")


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
def is_official(hit: PlatformHit, brand_aliases: Iterable[str],
                markers: Iterable[str] = ()) -> bool:
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
    return any(m in name for m in (*_OFFICIAL_MARKERS, *markers))


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
    if any(c and c in low for c in candidates):
        return True
    # 中文主体里的型号(YU7、L9、ET5)单独也认:YouTube 标题是英文的
    # ("Xiaomi YU7 | Debut"),只认"小米yu7"整串会把它们全部当成不相关。
    # 只认字母 + 数字混写的型号,"EV" 这种通用词不算。
    codes = re.findall(r"[a-z]+\d+[a-z0-9]*|\d+[a-z]+[a-z0-9]*", core)
    spaced = re.sub(r"<[^>]+>", "", title).lower()
    return any(re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", spaced) for c in codes)


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


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
            # 热点选题要按播放量和发布时间筛
            extra={"play": _as_int(v.get("play")), "pubdate": _as_int(v.get("pubdate"))},
        ))
    return hits


def search_bilibili(query: str, *, limit: int = 10, order: str = "",
                    since_days: int = 0) -> list[PlatformHit]:
    """`order`:B 站排序参数,空 = 综合;"click" 最多播放,"pubdate" 最新发布。
    `since_days`:只要最近 N 天发布的视频(0 = 不限)。"""
    import uuid

    import httpx

    params = {"search_type": "video", "keyword": query, "page": 1}
    if order:
        params["order"] = order
    if since_days:
        now = int(time.time())
        params.update(pubtime_begin_s=now - since_days * 86400, pubtime_end_s=now)
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


# --------------------------------------------------------------------------
# B 站官方号投稿:按 UP 主列视频
# --------------------------------------------------------------------------
# 关键词搜索只能捞到官方号的两三条(小米YU7 搜 8 个词,官方号的只有 5 条),
# 而「小米汽车」号下标题带 YU7 的投稿有 300 多条 —— 宣传片、测试实拍、功能演示,
# 全是确认是这台车、没有别人台标的画面。所以官方素材直接按号列投稿。
_BILI_USER_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
_BILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"
_BILI_SPACE_API = "https://api.bilibili.com/x/space/wbi/arc/search"
#: wbi 签名的混淆表(B 站前端写死的常量,和 yt-dlp 用的同一张)
_WBI_TABLE = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9,
              42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1,
              60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)
def bili_dm_params() -> dict[str, Any]:
    """空间接口要的浏览器指纹(和 yt-dlp 同一套造法)。不带回 412;**写死成固定串**
    连发几次就被风控成 -352,所以每次随机。"""
    import base64
    import json
    import random
    import string

    def junk(lo: int, hi: int) -> str:
        raw = "".join(random.choices(string.printable, k=random.randint(lo, hi))).encode()
        return base64.b64encode(raw)[:-2].decode()

    r1, r2 = random.randint(0, 113), random.randint(0, 513)
    top = random.randint(0, 100)
    inter = {"ds": [], "wh": [2 * 1920 + 2 * 1080 + 3 * r1, 4 * 1920 - 1080 + r1, r1],
             "of": [3 * top + r2, 4 * top + 2 * r2, r2]}
    return {"dm_img_list": "[]", "dm_img_str": junk(16, 64), "dm_cover_img_str": junk(32, 128),
            "dm_img_inter": json.dumps(inter, separators=(",", ":"))}


def wbi_mixin_key(img_url: str, sub_url: str) -> str:
    """nav 接口给的两张图的文件名拼起来,按混淆表重排取前 32 位。"""
    def stem(u: str) -> str:
        return u.rsplit("/", 1)[-1].split(".")[0]
    raw = stem(img_url) + stem(sub_url)
    return "".join(raw[i] for i in _WBI_TABLE if i < len(raw))[:32]


def wbi_sign(params: dict[str, Any], mixin: str, *, now: int | None = None) -> dict[str, Any]:
    """给参数加 `wts` + `w_rid`。值里的 !'()* 要先去掉(B 站前端也这么做)。"""
    import hashlib

    signed = dict(params, wts=int(time.time() if now is None else now))
    clean = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in signed.items()}
    query = urllib.parse.urlencode(sorted(clean.items()))
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


def _bili_client() -> Any:
    import httpx

    client = httpx.Client(headers={"User-Agent": webassets.BROWSER_UA,
                                   "Referer": "https://space.bilibili.com/",
                                   "Origin": "https://space.bilibili.com"}, timeout=20)
    client.get("https://www.bilibili.com/")  # 种 buvid3 等匿名 cookie
    return client


def parse_bili_users(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """用户搜索结果 → [{mid, name, verified}]。`official_verify.type` 1 = 机构认证。"""
    out: list[dict[str, Any]] = []
    for u in (payload.get("data") or {}).get("result") or []:
        if not isinstance(u, dict) or not u.get("mid"):
            continue
        verify = u.get("official_verify") or {}
        out.append({"mid": int(u["mid"]), "name": html.unescape(str(u.get("uname") or "")),
                    "verified": _as_int(verify.get("type")) == 1})
    return out


def bilibili_users(name: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """按名字搜 UP 主。失败抛 PlatformError(调用方降级)。"""
    import uuid

    import httpx

    try:
        resp = httpx.get(_BILI_USER_SEARCH, params={"search_type": "bili_user", "keyword": name},
                         headers={"User-Agent": webassets.BROWSER_UA,
                                  "Referer": "https://search.bilibili.com/",
                                  "Cookie": f"buvid3={uuid.uuid4()}infoc"}, timeout=20)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"B 站用户搜索不可用:{str(exc)[:80]}") from exc
    if payload.get("code") not in (0, None):
        raise PlatformError(f"B 站用户搜索返回 code={payload.get('code')}")
    return parse_bili_users(payload)[:limit]


def parse_bili_space(payload: dict[str, Any], uploader: str = "") -> list[PlatformHit]:
    """空间投稿接口 → PlatformHit。官方号列出来的,`verified` 由调用方定。"""
    vlist = (((payload.get("data") or {}).get("list") or {}).get("vlist")) or []
    hits: list[PlatformHit] = []
    for v in vlist:
        if not isinstance(v, dict) or not v.get("bvid"):
            continue
        hits.append(PlatformHit(
            platform="bilibili", url=f"https://www.bilibili.com/video/{v['bvid']}",
            title=html.unescape(str(v.get("title") or "")),
            uploader=str(v.get("author") or uploader),
            duration=parse_bili_duration(v.get("length")),
            extra={"play": _as_int(v.get("play")), "pubdate": _as_int(v.get("created"))},
        ))
    return hits


#: 空间接口的风控很敏感:翻页之间歇一下;被拦(412 / -352)就换一个新会话再试
BILI_SPACE_PAGE_GAP = 1.0
BILI_SPACE_RETRIES = 2
BILI_SPACE_RETRY_WAIT = 3.0


def bilibili_uploads(mid: int, keyword: str, *, uploader: str = "", pages: int = 2,
                     page_size: int = 50) -> list[PlatformHit]:
    """一个 UP 主标题含 `keyword` 的投稿(新的在前)。失败抛 PlatformError。"""
    last: Exception | None = None
    for attempt in range(BILI_SPACE_RETRIES):
        if attempt:
            time.sleep(BILI_SPACE_RETRY_WAIT)
        try:
            return _bilibili_uploads_once(mid, keyword, uploader=uploader, pages=pages,
                                          page_size=page_size)
        except PlatformError as exc:
            last = exc
    raise last or PlatformError("B 站空间接口不可用")


def _bilibili_uploads_once(mid: int, keyword: str, *, uploader: str, pages: int,
                           page_size: int) -> list[PlatformHit]:
    client = None
    try:
        client = _bili_client()
        wbi = client.get(_BILI_NAV_API).json()["data"]["wbi_img"]
        mixin = wbi_mixin_key(wbi["img_url"], wbi["sub_url"])
        hits: list[PlatformHit] = []
        for pn in range(1, pages + 1):
            if pn > 1:
                time.sleep(BILI_SPACE_PAGE_GAP)
            params = wbi_sign({"mid": mid, "ps": page_size, "pn": pn, "keyword": keyword,
                               "order": "pubdate", "order_avoided": "true", "platform": "web",
                               "tid": 0, "web_location": "333.1387", "special_type": "",
                               "index": 0, **bili_dm_params()}, mixin)
            resp = client.get(_BILI_SPACE_API, params=params,
                              headers={"Referer": f"https://space.bilibili.com/{mid}/video",
                                       "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8"})
            if resp.status_code != 200:
                raise PlatformError(f"B 站空间接口 HTTP {resp.status_code}")
            payload = resp.json()
            if payload.get("code") not in (0, None):
                if hits:  # 后面的页被拦:前面拿到的照样用
                    break
                raise PlatformError(f"B 站空间接口返回 code={payload.get('code')}")
            got = parse_bili_space(payload, uploader)
            hits += got
            count = _as_int(((payload.get("data") or {}).get("page") or {}).get("count"))
            if len(got) < page_size or len(hits) >= count:
                break
    except PlatformError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"B 站空间接口不可用:{str(exc)[:80]}") from exc
    finally:
        if client is not None:
            client.close()
    return hits


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


_TIKTOK_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?tiktok\.com/@([\w.\-]+)/video/(\d+)", re.IGNORECASE
)
#: 官方号只看最近这么多条:品牌号发得勤,再往前多是旧车型
TIKTOK_FEED_LIMIT = 60
_DDG_HTML = "https://html.duckduckgo.com/html/"
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)


def tiktok_url(url: str) -> str:
    """TikTok 视频页规范化成 `https://www.tiktok.com/@号/video/ID`,不是视频页返回空。"""
    m = _TIKTOK_URL_RE.search(url)
    return f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}" if m else ""


def parse_tiktok_entries(info: dict[str, Any], handle: str = "") -> list[PlatformHit]:
    """yt-dlp `tiktok:user` 的 flat 结果。标题为空时用描述(TikTok 的"标题"就是文案)。"""
    hits: list[PlatformHit] = []
    for e in info.get("entries") or []:
        if not isinstance(e, dict):
            continue
        url = tiktok_url(str(e.get("url") or e.get("webpage_url") or ""))
        if not url and e.get("id") and handle:
            url = f"https://www.tiktok.com/@{handle}/video/{e['id']}"
        if not url:
            continue
        hits.append(PlatformHit(
            platform="tiktok",
            url=url,
            title=str(e.get("title") or e.get("description") or "").strip(),
            uploader=str(e.get("uploader") or e.get("channel") or handle),
            duration=float(e.get("duration") or 0.0),
        ))
    return hits


def tiktok_account(handle: str, *, limit: int = TIKTOK_FEED_LIMIT) -> list[PlatformHit]:
    """拉一个 TikTok 账号最近的视频(yt-dlp `tiktok:user`)。

    TikTok 没有能直接调的关键词搜索(网页搜索接口要签名),品牌官方号的主页
    却能稳定拉到,而且带文案、时长 —— 所以官方号是 TikTok 的主通路。
    账号名单写在品类 YAML 的 `official_sites.<品牌>.tiktok`,由人核对过:
    TikTok 上叫 `bydglobal` 的账号发的是油画,不能按名字猜。
    国内访问需要 `YTDLP_PROXY`。失败抛 PlatformError。
    """
    handle = handle.strip().lstrip("@")

    def _fetch() -> list[PlatformHit]:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            raise PlatformError("未安装 yt-dlp:pip install yt-dlp") from exc
        opts = _base_opts()
        opts.update(skip_download=True, extract_flat="in_playlist", playlistend=limit)
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.tiktok.com/@{handle}", download=False) or {}
        except Exception as exc:  # noqa: BLE001
            hint = "" if config.YTDLP_PROXY else ";国内访问需在 .env 配 YTDLP_PROXY"
            raise PlatformError(f"TikTok 账号 @{handle} 拉取失败{hint}:{str(exc)[:120]}") from exc
        return parse_tiktok_entries(info, handle)

    return _cached_hits("tiktok_user", handle.lower(), limit, _fetch)


def parse_ddg_results(body: str) -> list[tuple[str, str]]:
    """DuckDuckGo HTML 版结果 → [(目标地址, 标题)]。结果链接是 `/l/?uddg=<目标>` 跳转。"""
    out: list[tuple[str, str]] = []
    for href, title in _DDG_RESULT_RE.findall(body):
        href = html.unescape(href)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        target = qs.get("uddg", [href])[0]
        out.append((target, html.unescape(re.sub(r"<[^>]+>", "", title)).strip()))
    return out


def search_tiktok(query: str, *, limit: int = 8) -> list[PlatformHit]:
    """TikTok 关键词检索:借 DuckDuckGo 的 `site:tiktok.com`。尽力而为。

    Bing 在国内会被跳到 cn.bing,无视 `site:`,搜不到 TikTok;DuckDuckGo 能搜到,
    但请求一密就回人机验证页 —— 这时**直接放弃**(抛错,不缓存空结果),
    不去绕验证。拿不到结果时官方号通路(`tiktok_account`)照常工作。
    """
    url = f"{_DDG_HTML}?{urllib.parse.urlencode({'q': f'site:tiktok.com {query}', 'kl': 'us-en'})}"
    try:
        body = webassets._http_text(url)
    except Exception as exc:  # noqa: BLE001
        raise PlatformError(f"TikTok 检索(DuckDuckGo)不可用:{str(exc)[:80]}") from exc
    if "anomaly" in body.lower() and "result__a" not in body:
        raise PlatformError("TikTok 检索(DuckDuckGo)要求人机验证,本轮跳过")
    hits: list[PlatformHit] = []
    for target, title in parse_ddg_results(body):
        clean = tiktok_url(target)
        if clean and all(h.url != clean for h in hits):
            # 结果标题形如 "文案 | TikTok",去掉站名尾巴
            title = re.sub(r"\s*[|\-–]\s*TikTok\s*$", "", title)
            hits.append(PlatformHit(platform="tiktok", url=clean, title=title,
                                    uploader=_TIKTOK_URL_RE.search(clean).group(1)))
    return hits[:limit]


_SEARCHERS = {
    "bilibili": search_bilibili,
    "youtube": search_youtube,
    "douyin": search_douyin,
    "tiktok": search_tiktok,
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
    # YouTube 要先解一道 JS 题("n challenge")才给格式,否则报
    # "The page needs to be reloaded"。yt-dlp 默认只认 deno,本机装的是 node,
    # 要显式启用;解题脚本来自 yt-dlp-ejs(`pip install "yt-dlp[default]"`)。
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
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


#: 只要视频流:成片的声音是旁白 + BGM,原片音轨用不上,省一半流量。1080p 封顶。
_MIN_SEGMENT_BYTES = 20_000

FORMAT = "bv*[height<=1080]/b[height<=1080]/bv*/b"
#: 同分辨率里挑码率最高的流。yt-dlp 默认偏好 AV1,B 站同一条 1080p 的 AV1 只有
#: 1.7M、AVC 有 5.2M(小米官方号实测),按默认挑出来的片子发糊;AVC 解码也最省心
FORMAT_SORT = ["res:1080", "br", "vcodec"]


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
        "format_sort": FORMAT_SORT,
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
