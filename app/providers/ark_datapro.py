"""火山方舟 Agent Plan 专业数据集(MCP):中国汽车车型配置库、中国汽车品牌销量数据。

走官方 MCP(streamable HTTP,单个工具 dataPro_search),用方舟 Agent Plan 的 Key。
配置库与汽车之家逐字段抽查一致,但比非官方接口稳;版本多时一次给不全,要按完整车型名补查。
销量库给精确月销量(懂车帝榜单只有「约 X 万」),返回里有统计口径就照写,没有写「口径未标注」。

计费:查到数据 12 AFP/次;不在支持范围的查询不计费,所以车名可以挨个试。

接口背后是模型路由:同一句话两次的字段名和形态都可能不同,字段点得太多也可能被判超范围。
解析要认几种写法,查询用官方样例的「汽车配置:」开头。
任何一步失败都返回 ok=False 的 RawDoc,不拖垮事实采集。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .. import config
from . import car_specs
from .facts import RawDoc

CONFIG_TOOL = "ark.datapro.specs"
SALES_TOOL = "ark.datapro.sales"
_URL = "https://datapro.hqd.cn-beijing.volces.com/mcp"
_PROTOCOL = "2025-03-26"
#: 每次查询要的字段:接口只返回 query 里点名的字段
_SPEC_FIELDS = ("整备质量 前后悬架类型 前后轮胎规格 电芯品牌 电池容量 驱动方式 "
                "CLTC纯电续航 百公里加速 轴距 离地间隙 后备厢容积 最大功率 最大扭矩")
#: 车型身份字段,不当参数
_IDENTITY = {"车型编码", "品牌", "厂商", "车系", "车型名称", "年款", "销售状态", "指导价", "级别",
             "能源类型", "车型", "车型信息"}
_MARK = re.compile(r"[●○]")
#: 数据集按官方子品牌收录(「银河星愿」),选题常写母品牌(「吉利星愿」)。原名优先,查不到才换
_BRAND_ALIASES = {"吉利": ("吉利银河", "银河")}
#: 一次没取全时按完整车型名补查,每查一个 12 AFP;缺得比这还多就放弃,交给下一个来源
_MAX_BACKFILL = 3


class DataproError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.ARK_API_KEY)


def _post(client: Any, headers: dict[str, str], body: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
    resp = client.post(_URL, json=body, headers=headers, timeout=60)
    if resp.status_code >= 400:
        raise DataproError(f"专业数据集 HTTP {resp.status_code}")
    text = resp.text
    if "id" not in body or not text.strip():
        return None, resp
    if text.lstrip().startswith("{"):
        return json.loads(text), resp
    for line in text.splitlines():  # SSE
        if line.startswith("data:"):
            return json.loads(line[5:].strip()), resp
    raise DataproError("专业数据集返回无法解析")


def search(query: str) -> dict[str, Any]:
    """dataPro_search 一次调用(含握手),返回 structuredContent。"""
    import httpx

    if not config.ARK_API_KEY:
        raise DataproError("未配置 ARK_API_KEY")
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "X-Agent-Plan-Key": config.ARK_API_KEY}
    with httpx.Client() as client:
        _, resp = _post(client, headers, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "aimovice", "version": "0"}}})
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            headers["Mcp-Session-Id"] = sid
        _post(client, headers, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        data, _ = _post(client, headers, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "dataPro_search", "arguments": {"query": query}}})
    result = (data or {}).get("result") or {}
    if result.get("isError"):
        raise DataproError("专业数据集返回错误")
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        raise DataproError("专业数据集没有结构化返回")
    if payload.get("code") not in (0, None):
        raise DataproError(f"专业数据集 code={payload.get('code')} {payload.get('msg')}")
    return payload


def _names(subject: str) -> list[str]:
    """查询要试的车名:原名在前,再试子品牌写法。"""
    names = [subject]
    for brand, aliases in _BRAND_ALIASES.items():
        if subject.startswith(brand) and not subject.startswith(aliases):
            rest = subject[len(brand):].strip()
            names += [f"{alias}{rest}" for alias in aliases]
    return names


def _search_names(subject: str, query: str) -> tuple[str, dict[str, Any]]:
    """按 `_names` 依次查(`query` 里的 {name} 换成车名),返回第一个有数据的车名和结果。"""
    miss = ""
    for name in _names(subject):
        try:
            data = search(query.format(name=name))
        except DataproError as exc:
            miss = str(exc)
            continue
        if data.get("items"):
            return name, data
        miss = str(data.get("msg") or "")
    raise DataproError(miss or "专业数据集没有这个车系")


def _backfill(name: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """这次查询的全部版本;有没返回的,按接口给的完整车型名补查。补不全就报错,不交半张表。"""
    items = list(data.get("items") or [])
    if not data.get("has_more"):
        return items
    rest = data.get("unreturned_model_names") or []
    if not rest or len(rest) > _MAX_BACKFILL:
        raise DataproError("专业数据集版本太多,一次没取全")
    for model in rest:
        # 带上查到数据的车名才稳;还不行就只报完整车型名(接口 hint 的写法,但返回字段会少些)
        extra = (search(f"汽车配置:{name} {model} {_SPEC_FIELDS}").get("items")
                 or search(f"汽车配置:{model}").get("items"))
        if not extra:
            raise DataproError(f"专业数据集补查不到「{model}」")
        items += extra
    return items


def _expand(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """配置参数是版本列表的形态拆成一行一个版本(车系名在外层的车型信息里)。"""
    out: list[dict[str, Any]] = []
    for it in items:
        params = it.get("配置参数")
        if isinstance(params, list):
            series = (it.get("车型信息") or {}).get("车系", "")
            out += [{"车系": series, **p} for p in params if isinstance(p, dict)]
        else:
            out.append(it)
    return out


def _same_series(items: list[dict[str, Any]], subject: str) -> list[dict[str, Any]]:
    """只留这个车系的版本:检索是模糊的,别的车的参数混进来比没有数据更糟。"""
    from .car_lineup import _match_score

    kept = []
    for it in items:
        series = _flat(it).get("车系", "")
        if not series or _match_score(subject, series):
            kept.append(it)
    return kept


def _flat(d: Any, prefix: str = "") -> dict[str, str]:
    """嵌套结构拍平成 {参数名: 值};{value, unit} 写成「名(单位)」→ 数值,和汽车之家同一种命名。"""
    out: dict[str, str] = {}
    if isinstance(d, dict):
        if "value" in d and set(d) <= {"value", "unit"}:
            name = f"{prefix}({d['unit']})" if d.get("unit") else prefix
            return {name: str(d["value"])}
        for k, v in d.items():
            out.update(_flat(v, k))
    elif d not in (None, ""):
        out[prefix] = str(d)
    return out


def _clean(v: str) -> str:
    # 选项用 ●/○ 标在值后面、以 / 连接;和汽车之家一样改用「、」连
    return _MARK.sub("", re.sub(r"[●○]/", "、", v)).strip()


def _trim_name(item: dict[str, str], subject: str) -> str:
    name = item.get("车型名称") or item.get("车型") or ""
    for s in sorted({item.get("车系", ""), subject}, key=len, reverse=True):
        if s and s in name:
            name = name.replace(s, "")
    # 有的形态把年款单列:版本名不带年款,同车多年款时就撞名了
    year = item.get("年款", "")
    if year and year not in name:
        name = f"{year} {name}"
    return re.sub(r"\s+", " ", name).strip()


def parse_items(items: list[dict[str, Any]], subject: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """配置库返回 → ([(参数名, {版本名: 取值})], 版本名列表)。两种返回形态都认;优先只留在售。"""
    items = _expand(items)
    flats = []
    for it in items:
        f = _flat(it)
        # 嵌套形态的车型名在「车型」下,拍平后键为车型
        if f.get("销售状态", "在售") == "在售" or not any(
                x.get("销售状态") == "在售" for x in (_flat(i) for i in items)):
            flats.append(f)
    trims: list[str] = []
    rows: dict[str, dict[str, str]] = {}
    for f in flats:
        trim = _trim_name(f, subject)
        if not trim or trim in trims:
            continue
        trims.append(trim)
        for k, v in f.items():
            if k in _IDENTITY or "指导价" in k:
                continue
            val = _clean(v)
            if val and val not in ("-", "无", "暂无"):
                rows.setdefault(k, {})[trim] = val
    return list(rows.items()), trims


def specs(subject: str, project_dir=None) -> RawDoc:  # noqa: ARG001 - 与其它来源同签名
    date = time.strftime("%Y-%m-%d")
    try:
        try:
            name, data = _search_names(subject, "汽车配置:{name} 全部在售版本 " + _SPEC_FIELDS)
        except DataproError:
            # 点名字段的长查询偶尔整句被判超范围(不计费);短查询字段少些,总比没有参数表好
            name, data = _search_names(subject, "汽车配置:{name} 全部在售版本")
        items = _same_series(_expand(_backfill(name, data)), subject)
        if not items:
            raise DataproError("专业数据集返回的不是这个车系")
        rows, trims = parse_items(items, subject)
        if not rows or not trims:
            raise DataproError("专业数据集参数表是空的")
    except Exception as exc:  # noqa: BLE001 - 失败只记下来,由调用方降级
        return RawDoc(tool=CONFIG_TOOL, query=subject, retrieved_at=date, text="", ok=False,
                      error=str(exc))
    return RawDoc(tool=CONFIG_TOOL, query=subject, retrieved_at=date,
                  text=car_specs.render(subject, rows, trims, date, source="火山方舟专业数据集·车型配置库"))


def _sales_month(row: dict[str, Any]) -> str:
    return str(row.get("月份") or row["统计月份"])


def _sales_count(row: dict[str, Any]) -> int:
    return int(row["零售量"] if "零售量" in row else row["销量"])


def _sales_bases(items: list[dict[str, Any]]) -> dict[str, str]:
    """{月份: 统计口径}。同一次返回里各月口径可能不同(零售上牌 / 销量),不能写成一个。"""
    out: dict[str, str] = {}
    for it in items:
        for row in it.get("销售数据") or ():
            try:
                if row.get("统计口径"):
                    out[_sales_month(row)] = str(row["统计口径"])
            except KeyError:
                continue
    return out


def parse_sales(items: list[dict[str, Any]]) -> dict[str, list[tuple[str, int]]]:
    """销量行 → {车系: [(YYYY-MM, 销量)]},按月份新到旧。平铺和嵌套(销售数据)两种形态都认。"""
    out: dict[str, list[tuple[str, int]]] = {}
    for it in items:
        if isinstance(it.get("销售数据"), list):
            cond = it.get("查询条件") or {}
            name = cond.get("品牌车系") or cond.get("品牌/车系") or it.get("车系") or ""
            for row in it["销售数据"]:
                try:
                    out.setdefault(name, []).append((_sales_month(row), _sales_count(row)))
                except (KeyError, TypeError, ValueError):
                    continue
            continue
        try:
            ym = f"{int(it['年份'])}-{int(it['月份']):02d}"
            out.setdefault(it["车系"], []).append((ym, int(it["销量"])))
        except (KeyError, TypeError, ValueError):
            continue
    return {k: sorted(v, reverse=True) for k, v in out.items()}


def sales(subject: str, months: int = 6, project_dir=None) -> RawDoc:  # noqa: ARG001
    """全国月销量(精确值)。查询里的车系名要用「品牌+车系」(小米SU7);缺失的车系照实写出。"""
    date = time.strftime("%Y-%m-%d")
    try:
        _, data = _search_names(subject, "汽车销量:{name} 全国 最近"
                                + f"{months}个月 月销量")
        series = parse_sales(data.get("items") or [])
        if not series:
            raise DataproError(data.get("msg") or "专业数据集没有这个车系的销量")
    except Exception as exc:  # noqa: BLE001
        return RawDoc(tool=SALES_TOOL, query=subject, retrieved_at=date, text="", ok=False,
                      error=str(exc))
    bases = _sales_bases(data.get("items") or [])
    note = "各月口径写在括号里" if bases else "口径未标注"
    lines = [f"{subject} 全国月销量(火山方舟专业数据集·汽车品牌销量数据,{date} 取数,{note}):"]
    for name, rows in series.items():
        months_text = [f"{ym} {n}辆" + (f"({bases[ym]})" if ym in bases else "")
                       for ym, n in rows[:months]]
        lines.append(f"- {name}:" + ";".join(months_text))
    for miss in data.get("missing_entities") or []:
        lines.append(f"- {miss}:数据集无此车系销量")
    return RawDoc(tool=SALES_TOOL, query=subject, retrieved_at=date, text="\n".join(lines))
