"""参数配置表:汽车之家车型库里每个在售版本的参数(整备质量、悬架、轮胎、电芯、离地间隙…)。

新闻检索几乎拿不到这些:通稿只写续航、零百、价格,车迷最先问的「多重」「什么悬架」
「轮胎多宽」「电芯谁家的」「后备厢多大」都要靠配置表。

懂车帝的参数页对脚本返回空页、易车配置页要过验证码,都不碰;汽车之家的车型库接口
直接给 JSON,用它。非官方接口,随时可能改版:任何一步失败都返回 ok=False 的 RawDoc,
不拖垮事实采集。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from . import browser_fetch, webassets
from .facts import RawDoc

TOOL = "autohome.specs"
_FIND_API = "https://www.autohome.com.cn/ashx/AjaxIndexCarFind.ashx"
_PARAM_API = "https://carif.api.autohome.com.cn/Car/v3/Param_ListBySpecIdList.ashx"
_HEADERS = {"User-Agent": webassets.BROWSER_UA, "Referer": "https://www.autohome.com.cn/"}
#: 车型库里「在售」的版本状态
_ON_SALE = 20
#: 不进正文的参数:价格在售清单里已有;其余要么人人一样(车体结构、挡位),要么只是噪音(电机型号)
_SKIP = {
    "车型名称", "厂商指导价(元)", "厂商", "能源类型", "车身结构", "车门开启方式", "车门数(个)",
    "简称", "挡位个数", "变速箱类型", "车体结构", "助力类型", "驻车制动类型", "快充功能",
    "高压快充", "慢充接口位置", "快充接口位置", "电池快充时间(小时)", "电动机总功率(kW)",
    "电动机总马力(Ps)", "电动机总扭矩(N·m)", "长度(mm)", "宽度(mm)", "高度(mm)",
    "前电动机型号", "后电动机型号", "电能当量燃料消耗量(L/100km)",
}
#: 「整备质量(kg)」→ 名字 + 单位;数字后面接上单位,下游的数字核对才认得出维度
_NAME_UNIT = re.compile(r"^(.*?)[(（]([^()（）]+)[)）]$")
_UNIT_ALIAS = {"Ps": "匹", "s": "秒", "m": "米", "个": "", "°": "°"}
#: 参数名本身说不清是什么的,换个说法
_RENAME = {"电动机": "电动机马力"}


class SpecsError(RuntimeError):
    pass


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    import httpx

    resp = httpx.get(url, params=params, headers=_HEADERS, timeout=15)
    raw = resp.content
    if resp.status_code >= 400:
        full = str(resp.request.url)
        if not browser_fetch.should_fallback(full, resp.status_code):
            raise SpecsError(f"汽车之家 HTTP {resp.status_code}")
        # 被认出是脚本:换真浏览器再取一次
        try:
            raw = browser_fetch.fetch(full, referer=_HEADERS["Referer"])  # type: ignore[assignment]
        except browser_fetch.BrowserFetchError as exc:
            raise SpecsError(f"汽车之家 HTTP {resp.status_code};浏览器兜底:{exc}") from exc
    # 找车接口是 UTF-8;参数接口声明 gb2312、实际有超出 gb2312 的字(「·」),按 gb18030 解
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    data = json.loads(text)
    if data.get("returncode") not in (0, None):
        raise SpecsError(f"汽车之家返回 {data.get('message')}")
    return data.get("result") or {}


def _norm(name: str) -> str:
    return re.sub(r"[\s·\-]", "", name or "").lower()


def _brand_ids(subject: str, brands: list[dict[str, Any]]) -> list[int]:
    """主题里写了品牌名(「小米YU7」里的「小米」)的品牌;长名字优先。"""
    s = _norm(subject)
    hits = []
    for b in brands:
        short = _norm(b.get("name", "")).removesuffix("汽车")
        if len(short) >= 2 and short in s:
            hits.append((len(short), int(b["id"])))
    return [i for _, i in sorted(hits, reverse=True)]


def _brand_from_rank(subject: str) -> str:
    """主题没写品牌(「Model Y」)时,从懂车帝榜单上找这台车的品牌名。"""
    from . import car_lineup, trends

    for kind in ("sales", "hot"):
        try:
            for item in trends.dongchedi_rank(kind, count=50):
                if car_lineup._match_score(subject, item.car) == 2:
                    m = re.search(r":(\S+) ", item.title)
                    if m:
                        return m.group(1)
        except Exception:  # noqa: BLE001 - 榜单不可用就算了
            pass
    return ""


def series_id(subject: str) -> tuple[int, str]:
    """车名 → (汽车之家车系 ID, 车系名)。按品牌找车系,车系名匹配规则和在售清单一致。"""
    from .car_lineup import _match_score

    brands = (_get_json(_FIND_API, {"type": 1}).get("branditems") or [])
    ids = _brand_ids(subject, brands)
    if not ids:
        ids = _brand_ids(_brand_from_rank(subject), brands)
    best: tuple[int, int, str] = (0, 0, "")
    for bid in ids:
        for factory in _get_json(_FIND_API, {"type": 3, "value": bid}).get("factoryitems") or []:
            for s in factory.get("seriesitems") or []:
                score = _match_score(subject, s.get("name", ""))
                if score > best[0]:
                    best = (score, int(s["id"]), s["name"])
        if best[0] == 2:
            break
    if not best[0]:
        raise SpecsError(f"汽车之家里找不到车系「{subject}」")
    return best[1], best[2]


def on_sale_specs(series: int) -> list[tuple[int, str]]:
    """车系 → [(版本 ID, 「2026款 四驱GT」)]:在售的;一个在售的都没有就取最新年款。"""
    years = _get_json(_FIND_API, {"type": 5, "value": series}).get("yearitems") or []
    items = [s for y in years for s in y.get("specitems") or []]
    live = [s for s in items if s.get("state") == _ON_SALE]
    if not live and years:
        live = years[0].get("specitems") or []
    return [(int(s["id"]), s["name"]) for s in live]


def _value(item: dict[str, Any]) -> str:
    """一格的取值:多数在 value,带选装的(悬架、轮胎、驱动方式)在 sublist。"""
    v = str(item.get("value") or "").strip()
    if not v:
        v = "、".join(str(s.get("subvalue") or "").strip() for s in item.get("sublist") or []
                     if str(s.get("subvalue") or "").strip())
    return "" if v in ("-", "无", "暂无") else v


def parse_params(result: dict[str, Any], names: dict[int, str]) -> list[tuple[str, dict[str, str]]]:
    """参数接口 → [(参数名带单位, {版本名: 取值})],只留有值的参数和在 names 里的版本。"""
    rows = []
    for group in result.get("paramtypeitems") or []:
        for p in group.get("paramitems") or []:
            name = str(p.get("name") or "").strip()
            if not name or name in _SKIP or any(name == r[0] for r in rows):
                continue  # 「CLTC纯电续航」在基本参数和电池两组各出现一次
            vals = {names[int(v["specid"])]: _value(v) for v in p.get("valueitems") or []
                    if int(v.get("specid") or 0) in names}
            vals = {k: v for k, v in vals.items() if v}
            if vals:
                rows.append((name, vals))
    return rows


def _with_unit(name: str, value: str) -> tuple[str, str]:
    """(「整备质量(kg)」, 「2200」) → (「整备质量」, 「2200kg」)。区间、文字不加单位。"""
    m = _NAME_UNIT.match(name)
    if not m:
        return name, value
    label, unit = m.group(1).strip(), _UNIT_ALIAS.get(m.group(2).strip(), m.group(2).strip())
    label = _RENAME.get(label, label)
    if re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", value):
        return label, f"{value}{unit}"
    return label, value


def render(series: str, rows: list[tuple[str, dict[str, str]]], trims: list[str],
           date: str) -> str:
    """给事实抽取读的正文:一个参数一行,取值相同的版本合并;所有版本一样就写「全系」。"""
    lines = [f"{series} 参数配置表(汽车之家车型库,{date} 取数,在售 {len(trims)} 个版本:"
             + "、".join(trims) + "):"]
    for name, vals in rows:
        groups: dict[str, list[str]] = {}
        for trim in trims:
            if trim in vals:
                label, v = _with_unit(name, vals[trim])
                groups.setdefault(v, []).append(trim)
        if not groups:
            continue
        if len(groups) == 1 and len(next(iter(groups.values()))) == len(trims):
            text = f"全系 {next(iter(groups))}"
        else:
            text = ";".join(f"{'、'.join(ts)} {v}" for v, ts in groups.items())
        lines.append(f"- {label}:{text}")
    return "\n".join(lines)


def specs(subject: str, project_dir=None) -> RawDoc:  # noqa: ARG001 - 与其它来源同签名
    date = time.strftime("%Y-%m-%d")
    try:
        sid, name = series_id(subject)
        trims = on_sale_specs(sid)
        if not trims:
            raise SpecsError(f"汽车之家「{name}」没有版本")
        names = {i: n for i, n in trims}
        result = _get_json(_PARAM_API, {"speclist": ",".join(str(i) for i in names),
                                        "_appid": "cms"})
        rows = parse_params(result, names)
        if not rows:
            raise SpecsError(f"汽车之家「{name}」参数表是空的")
    except Exception as exc:  # noqa: BLE001 - 非官方接口,失败只记下来
        return RawDoc(tool=TOOL, query=subject, retrieved_at=date, text="", ok=False, error=str(exc))
    return RawDoc(tool=TOOL, query=subject, retrieved_at=date,
                  text=render(name, rows, list(names.values()), date))
