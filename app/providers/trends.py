"""实时热点:给「热点选题」提供原始素材。

四路来源,各自独立失败:
- 懂车帝:月销量榜(rank_data_type=11)与热度榜(=1,按关注度)—— 只有汽车有
- 新闻:走现有 iFinD search_news 通道(direct 模式下是 MiniMax 网络搜索);
  没有公开榜单的品类(母婴),标题带「榜」的新闻就是它的榜单信号
- 抖音热榜:公开热搜,只留与本品类相关的词条
- B 站:近 N 天按播放量排序的品类视频

查询词、过滤词按品类走 profile 的 `trends:`(categories.TrendSpec);
下面的模块常量是汽车口径,`collect()` 不传 spec 时沿用。

非官方接口随时可能改版。所以单路挂掉只记进 errors,不影响别的来源;
空结果不进缓存,免得接口修好后仍长时间显示"没有热点"。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable

from . import cli, webassets
from .facts import IFIND

#: 磁盘缓存 2 小时:热点是按小时变的,不能沿用素材检索的 24 小时
CACHE_TTL = 2 * 3600

_DCD_RANK_API = "https://www.dongchedi.com/motor/pc/car/rank_data"
#: rank_data_type:11 = 月销量榜,1 = 热度榜(关注度)。2026-10 实测确认
_DCD_RANK_TYPES = {"sales": 11, "hot": 1}
_DOUYIN_HOT_API = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"

NEWS_QUERIES = (
    "新车 上市 发布会 汽车",
    "汽车 官降 降价 限时优惠",
    "汽车 召回 事故 OTA 智驾",
)
BILI_QUERIES = ("新车 评测", "汽车 试驾")

#: 判断一条热搜跟车有没有关系的通用词;品牌/车系名由懂车帝榜单动态补充
AUTO_WORDS = (
    "汽车", "新车", "车型", "车企", "电车", "电动车", "新能源", "智驾", "辅助驾驶", "自动驾驶",
    "续航", "充电", "换电", "车展", "SUV", "轿车", "提车", "交付", "销量", "召回", "降价",
    "官降", "OTA", "增程", "混动", "4S店", "车主",
)
#: 常见品牌兜底:懂车帝不可用时抖音过滤仍能工作
AUTO_BRANDS = (
    "比亚迪", "特斯拉", "小米汽车", "小米SU7", "小米YU7", "理想", "蔚来", "小鹏", "问界", "华为",
    "吉利", "长城", "哈弗", "奇瑞", "长安", "零跑", "极氪", "领克", "埃安", "广汽", "大众",
    "丰田", "本田", "宝马", "奔驰", "奥迪", "保时捷", "智界", "享界", "尊界", "阿维塔", "深蓝",
    "极越", "岚图", "腾势", "仰望", "方程豹", "星途", "坦克", "魏牌", "鸿蒙智行", "乐道", "萤火虫",
)


class TrendError(RuntimeError):
    """单个热点来源不可用。"""


@dataclass
class TrendItem:
    #: 来源:dongchedi.sales / dongchedi.hot / news / douyin / bilibili
    source: str
    #: model = 车型榜单条目,event = 新闻/热搜事件,video = 视频
    kind: str
    title: str
    #: 能确定的车系名(榜单条目才有)
    car: str = ""
    #: 量化热度的文字描述:"月销 50359" / "热度 1.2 万" / "播放 35 万"
    metric: str = ""
    url: str = ""
    date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def _cache_path(key: str):
    from .. import config

    d = config.DATA_DIR / "_cache" / "trends"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}.json"


def _cached(key: str, fetch: Callable[[], list[TrendItem]]) -> list[TrendItem]:
    path = _cache_path(key)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload.get("ts", 0) <= CACHE_TTL and payload.get("items"):
            return [TrendItem(**d) for d in payload["items"]]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    items = fetch()
    if items:
        try:
            path.write_text(json.dumps({"ts": time.time(), "items": [i.to_dict() for i in items]},
                                       ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return items


def _wan(n: int) -> str:
    return f"{n / 10000:.1f} 万" if n >= 10000 else str(n)


# --------------------------------------------------------------------------
# 懂车帝
# --------------------------------------------------------------------------
def parse_dongchedi(payload: dict[str, Any], kind: str) -> list[TrendItem]:
    data = (payload or {}).get("data") or {}
    rows = data.get("list") or []
    # 销量榜是按月出的,标上统计月份("2026年09月"),别让 LLM 当成本月实时数
    months = data.get("sells_rank_month") or []
    period = str(months[0].get("text") or "") if kind == "sales" and months and isinstance(months[0], dict) else ""
    items = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("series_name"):
            continue
        name = str(r["series_name"]).strip()
        brand = str(r.get("brand_name") or "").strip()
        count = int(r.get("count") or 0)
        rank, last = int(r.get("rank") or 0), int(r.get("last_rank") or 0)
        move = ""
        if last > 0 and rank and last != rank:
            move = f",上期第 {last}"
        label = "月销" if kind == "sales" else "热度"
        items.append(TrendItem(
            source=f"dongchedi.{kind}",
            kind="model",
            title=f"{'销量' if kind == 'sales' else '热度'}榜第 {rank} 名:{brand} {name}{move}",
            car=name,
            metric=f"{label} {_wan(count)}",
            url=f"https://www.dongchedi.com/auto/series/{r['series_id']}" if r.get("series_id") else "",
            date=period or time.strftime("%Y-%m"),
        ))
    return items


def dongchedi_rank(kind: str = "sales", count: int = 20) -> list[TrendItem]:
    import httpx

    def fetch() -> list[TrendItem]:
        last_error = ""
        for attempt in range(2):
            # 连续请求偶尔回空列表(实测一次),隔一秒重试一次即可
            if attempt:
                time.sleep(1.0)
            try:
                return once()
            except TrendError as exc:
                last_error = str(exc)
        raise TrendError(last_error)

    def once() -> list[TrendItem]:
        resp = httpx.get(
            _DCD_RANK_API,
            params={"aid": 1839, "app_name": "auto_web_pc", "rank_data_type": _DCD_RANK_TYPES[kind],
                    "count": count, "offset": 0, "nation": 0},
            headers={"User-Agent": webassets.BROWSER_UA, "Referer": "https://www.dongchedi.com/"},
            timeout=15,
        )
        if resp.status_code >= 400:
            raise TrendError(f"懂车帝 HTTP {resp.status_code}")
        items = parse_dongchedi(resp.json(), kind)
        if not items:
            raise TrendError("懂车帝榜单为空,接口可能改版")
        return items

    return _cached(f"dcd:{kind}:{count}", fetch)


# --------------------------------------------------------------------------
# 汽车新闻
# --------------------------------------------------------------------------
def parse_news(raw: Any) -> list[dict[str, Any]]:
    rows = raw.get("data") if isinstance(raw, dict) else raw
    return [r for r in rows or [] if isinstance(r, dict)]


def _qkey(queries: tuple[str, ...] | list[str]) -> str:
    """查询词进缓存 key:不同品类的热点不能串缓存。"""
    return hashlib.sha1("|".join(queries).encode("utf-8")).hexdigest()[:8]


def auto_news(days: int = 7, size: int = 8,
              queries: tuple[str, ...] | list[str] = NEWS_QUERIES) -> list[TrendItem]:
    queries = tuple(queries)

    def fetch() -> list[TrendItem]:
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
        items: list[TrendItem] = []
        seen: set[str] = set()
        errors = []
        for q in queries:
            try:
                raw = cli.call(IFIND["search_news"],
                               {"query": f"{q} {end[:7]}", "size": size,
                                "time_start": start, "time_end": end}, timeout=60, retries=1)
            except Exception as exc:  # noqa: BLE001 - 一条查询失败不连坐
                errors.append(str(exc)[:80])
                continue
            for r in parse_news(raw):
                title = str(r.get("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                items.append(TrendItem(
                    # 标题带「榜」的(电商品类榜、销量榜报道)单独标出来,给 LLM 时排前面
                    source="news", kind="rank" if "榜" in title else "event", title=title,
                    metric=str(r.get("snippet") or r.get("content") or "")[:80],
                    url=str(r.get("link") or r.get("url") or ""),
                    date=str(r.get("date") or r.get("publish_time") or "")[:10],
                ))
        if not items and errors:
            raise TrendError(f"新闻搜索失败:{errors[0]}")
        return items

    key = f"news:{days}:{time.strftime('%Y-%m-%d')}"
    # 汽车口径保持原 key,已有缓存继续可用
    return _cached(key if queries == NEWS_QUERIES else f"{key}:{_qkey(queries)}", fetch)


# --------------------------------------------------------------------------
# 抖音热榜
# --------------------------------------------------------------------------
def is_related(text: str, words: Iterable[str]) -> bool:
    t = str(text or "").upper()
    return any(w.upper() in t for w in words if w)


def is_auto_related(text: str, extra_words: tuple[str, ...] = ()) -> bool:
    return is_related(text, (*AUTO_WORDS, *AUTO_BRANDS, *extra_words))


def parse_douyin(payload: dict[str, Any], extra_words: tuple[str, ...] = (),
                 words: tuple[str, ...] | None = None) -> list[TrendItem]:
    """`words` = 品类过滤词(不传 = 汽车口径);`extra_words` 是榜单上动态补的名字。"""
    vocab = (*(AUTO_WORDS + AUTO_BRANDS if words is None else words), *extra_words)
    items = []
    for i, w in enumerate((payload or {}).get("word_list") or [], 1):
        if not isinstance(w, dict):
            continue
        word = str(w.get("word") or "").strip()
        if not word or not is_related(word, vocab):
            continue
        items.append(TrendItem(
            source="douyin", kind="event", title=word,
            metric=f"热搜第 {i} · 热度 {_wan(int(w.get('hot_value') or 0))}",
            url=f"https://www.douyin.com/search/{word}",
            date=time.strftime("%Y-%m-%d"),
        ))
    return items


def douyin_hot(extra_words: tuple[str, ...] = (),
               words: tuple[str, ...] | None = None) -> list[TrendItem]:
    """抖音热搜里与本品类相关的词条。整榜都无关时返回空列表(不算失败)。"""
    import httpx

    resp = httpx.get(_DOUYIN_HOT_API, timeout=15, headers={
        "User-Agent": webassets.BROWSER_UA, "Referer": "https://www.douyin.com/"})
    if resp.status_code >= 400:
        raise TrendError(f"抖音热榜 HTTP {resp.status_code}")
    payload = resp.json()
    if not payload.get("word_list"):
        raise TrendError(f"抖音热榜为空:status_code={payload.get('status_code')}")
    return parse_douyin(payload, extra_words, words)


# --------------------------------------------------------------------------
# B 站
# --------------------------------------------------------------------------
def bilibili_hot(days: int = 7, limit: int = 10,
                 queries: tuple[str, ...] | list[str] = BILI_QUERIES) -> list[TrendItem]:
    from . import platform_video

    queries = tuple(queries)

    def fetch() -> list[TrendItem]:
        cutoff = time.time() - days * 86400
        items: list[TrendItem] = []
        seen: set[str] = set()
        for q in queries:
            for hit in platform_video.search_bilibili(q, limit=20, order="click",
                                                       since_days=days):
                pub = int((hit.extra or {}).get("pubdate") or 0)
                if (pub and pub < cutoff) or hit.url in seen or not hit.title:
                    continue
                seen.add(hit.url)
                items.append(TrendItem(
                    source="bilibili", kind="video", title=hit.title,
                    metric=f"播放 {_wan(int((hit.extra or {}).get('play') or 0))}"
                           + (f" · UP {hit.uploader}" if hit.uploader else ""),
                    url=hit.url,
                    date=time.strftime("%Y-%m-%d", time.localtime(pub)) if pub else "",
                ))
        items.sort(key=lambda it: -_play_of(it))
        return items[:limit]

    key = f"bili:{days}:{limit}"
    return _cached(key if queries == BILI_QUERIES else f"{key}:{_qkey(queries)}", fetch)


def _play_of(item: TrendItem) -> float:
    m = item.metric.split("·")[0].replace("播放", "").strip()
    try:
        return float(m.replace("万", "")) * (10000 if "万" in m else 1)
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
ALL_SOURCES = ("dongchedi", "news", "douyin", "bilibili")


def collect(sources: tuple[str, ...] | list[str] | None = None, *, spec: Any = None,
            brands: Iterable[str] = ()) -> tuple[list[TrendItem], dict[str, str]]:
    """抓全部热点。返回 (条目, {来源: 错误信息})。单路失败不影响其他来源。

    `spec`:品类的 categories.TrendSpec;不传 = 汽车口径(模块常量)。
    `brands`:品类品牌词(profile.trend_brands),抖音过滤用。
    """
    sources = tuple(sources or ALL_SOURCES)
    if spec is not None and spec.rank != "dongchedi":
        sources = tuple(s for s in sources if s != "dongchedi")
    items: list[TrendItem] = []
    errors: dict[str, str] = {}

    def run(name: str, fn: Callable[[], list[TrendItem]]) -> list[TrendItem]:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001 - 隔离单路故障
            errors[name] = str(exc)[:160] or type(exc).__name__
            return []
        items.extend(got)
        return got

    rank: list[TrendItem] = []
    if "dongchedi" in sources:
        rank += run("dongchedi.sales", lambda: dongchedi_rank("sales"))
        rank += run("dongchedi.hot", lambda: dongchedi_rank("hot"))
    if spec is None:
        if "news" in sources:
            run("news", auto_news)
        if "douyin" in sources:
            # 榜单里的车系名补进汽车词表,抖音热搜"星愿交付破10万"这类才认得出来
            names = tuple({i.car for i in rank if len(i.car) >= 2})
            run("douyin", lambda: douyin_hot(names))
        if "bilibili" in sources:
            run("bilibili", bilibili_hot)
        return items, errors

    if "news" in sources and spec.news_queries:
        run("news", lambda: auto_news(queries=spec.news_queries))
    if "douyin" in sources and (spec.hot_words or brands):
        names = tuple({i.car for i in rank if len(i.car) >= 2})
        words = (*spec.hot_words, *brands)
        run("douyin", lambda: douyin_hot(names, words=words))
    if "bilibili" in sources and spec.bili_queries:
        # B站搜「安全座椅 测评」会混进汽车座椅、加湿器测评:标题也按品类词过滤一遍
        words = (*spec.hot_words, *brands)
        run("bilibili", lambda: [i for i in bilibili_hot(queries=spec.bili_queries)
                                 if not words or is_related(i.title, words)])
    # 榜单类条目(懂车帝 / 新闻里的榜单报道)排前面:LLM 只读前 80 条
    items.sort(key=lambda it: it.kind not in ("model", "rank"))
    return items, errors
