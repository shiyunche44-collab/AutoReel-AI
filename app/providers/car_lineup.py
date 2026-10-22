"""在售版本清单:懂车帝车型库的「在售」列表(年款、版本名、厂商指导价、驱动形式)。

新闻检索拿不准的三件事,这里一次说清:
- 哪些版本**还在卖**:2025 款的「标准版」到 2026 年改名成了「后驱长续航版」,
  新闻里两个名字混着用,脚本很容易把停售版本当成推荐;
- 每个版本的**厂商指导价**;
- **驱动形式**(后驱 / 四驱)—— 车迷最先问的一项,新闻通稿反而很少写。

列表里的续航标签不可靠(实测 2026 款标准版标着 835KM,官方是 643 公里),不取。
非官方接口,随时可能改版:任何一步失败都返回 ok=False 的 RawDoc,不拖垮事实采集。
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import webassets
from .facts import RawDoc

TOOL = "dongchedi.lineup"
_CAR_LIST_API = "https://www.dongchedi.com/motor/pc/car/series/car_list"
_SEARCH_API = "https://www.dongchedi.com/motor/searchapi/search_content/"
_HEADERS = {"User-Agent": webassets.BROWSER_UA, "Referer": "https://www.dongchedi.com/"}
#: 版本标签里的驱动形式
_DRIVES = ("后驱", "前驱", "四驱")
#: 搜索结果里车友圈链接带的车系 ID
_SERIES_IN_SEARCH = re.compile(r"series_id=(\d+)\\u0026series_name=([^\"\\&]+)")


class LineupError(RuntimeError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[\s·\-]", "", name or "").lower()


def _match_score(subject: str, series_name: str) -> int:
    """2 = 同一个车系(「特斯拉Model Y」对「Model Y」也算),1 = 一方包含另一方,0 = 不是。

    包含只算次优:「小米YU7」也包含在「小米YU7 GT」里,有同名车系时要选准的那个。
    """
    s, n = _norm(subject), _norm(series_name).replace("车友圈", "")
    if not n:
        return 0
    if n == s or s.endswith(n) and len(n) >= 3:
        return 2
    return 1 if n in s or s in n else 0


def series_id(subject: str) -> tuple[int, str]:
    """车名 → (懂车帝车系 ID, 车系名)。先查销量 / 热度榜(有缓存),再查搜索。"""
    import httpx

    from . import trends

    best: tuple[int, int, str] = (0, 0, "")
    for kind in ("sales", "hot"):
        try:
            for item in trends.dongchedi_rank(kind, count=50):
                sid = item.url.rsplit("/", 1)[-1] if item.url else ""
                score = _match_score(subject, item.car) if sid.isdigit() else 0
                if score > best[0]:
                    best = (score, int(sid), item.car)
        except Exception:  # noqa: BLE001 - 榜单不可用就走搜索
            pass
    if best[0] == 2:
        return best[1], best[2]
    resp = httpx.get(_SEARCH_API, params={"keyword": subject, "offset": 0, "count": 10,
                                          "cur_tab": 1, "search_source": "pc"},
                     headers=_HEADERS, timeout=15)
    if resp.status_code >= 400:
        raise LineupError(f"懂车帝搜索 HTTP {resp.status_code}")
    counts: dict[tuple[int, str], tuple[int, int]] = {}
    for sid, name in _SERIES_IN_SEARCH.findall(resp.text):
        score = _match_score(subject, name) if sid != "0" else 0
        if score:
            key = (int(sid), name.replace("车友圈", ""))
            counts[key] = (score, counts.get(key, (0, 0))[1] + 1)
    if counts:
        return max(counts, key=counts.get)
    if best[0]:
        return best[1], best[2]
    raise LineupError(f"懂车帝里找不到车系「{subject}」")


def parse_car_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """car_list 的「在售」页 → [{year, series, name, price, drive}]。"""
    tabs = (payload.get("data") or {}).get("tab_list") or []
    tab = next((t for t in tabs if t.get("tab_key") == "online_all"), None)
    rows = []
    for cell in (tab or {}).get("data") or []:
        info = cell.get("info") or {}
        if not info.get("car_id"):
            continue  # 年款分组标题
        tags = info.get("tags") or []
        rows.append({
            "year": info.get("year"),
            "series": info.get("series_name") or "",
            "name": info.get("car_name") or info.get("name") or "",
            "price": info.get("official_price_str") or "",
            "drive": next((t for t in tags if t in _DRIVES), ""),
        })
    return rows


def render(series: str, rows: list[dict[str, Any]], date: str) -> str:
    """给事实抽取读的正文:一个版本一行,再加一行版本总数。"""
    lines = [f"{series} 在售版本(懂车帝车型库,{date} 取数,厂商指导价):"]
    for r in rows:
        bits = [f"厂商指导价 {r['price']}" if r["price"] else "", r["drive"]]
        lines.append(f"- {r['year']}款 {r['series']} {r['name']}:" + ",".join(b for b in bits if b))
    years = sorted({r["year"] for r in rows if r["year"]})
    lines.append(f"{series} 目前在售 {len(rows)} 个版本,横跨 "
                 + "、".join(f"{y}款" for y in years) + ";不在这个列表里的版本已停售。")
    return "\n".join(lines)


def lineup(subject: str, project_dir=None) -> RawDoc:  # noqa: ARG001 - 与其它来源同签名
    import httpx

    date = time.strftime("%Y-%m-%d")
    try:
        sid, name = series_id(subject)
        resp = httpx.get(_CAR_LIST_API, params={"series_id": sid, "city_name": ""},
                         headers=_HEADERS, timeout=15)
        if resp.status_code >= 400:
            raise LineupError(f"懂车帝车型列表 HTTP {resp.status_code}")
        rows = parse_car_list(resp.json())
        if not rows:
            raise LineupError(f"懂车帝「{name}」没有在售版本")
    except Exception as exc:  # noqa: BLE001 - 非官方接口,失败只记下来
        return RawDoc(tool=TOOL, query=subject, retrieved_at=date, text="", ok=False, error=str(exc))
    return RawDoc(tool=TOOL, query=subject, retrieved_at=date,
                  text=render(rows[0]["series"] or name, rows, date))
