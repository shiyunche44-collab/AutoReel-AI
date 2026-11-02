"""火山方舟 Agent Plan 专业数据集(MCP):中国汽车车型配置库、中国汽车品牌销量数据。

走官方 MCP(streamable HTTP,单个工具 dataPro_search),用方舟 Agent Plan 的 Key。
配置库一次查询返回全部在售版本,与汽车之家逐字段抽查一致,但比非官方接口稳;
销量库给精确月销量(懂车帝榜单只有「约 X 万」),口径未标注,正文里照实写「口径未标注」。

计费:查到数据 12 AFP/次;不在支持范围的查询不计费。
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
    return re.sub(r"\s+", " ", name).strip()


def parse_items(items: list[dict[str, Any]], subject: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """配置库返回 → ([(参数名, {版本名: 取值})], 版本名列表)。两种返回形态都认;优先只留在售。"""
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
        data = search(f"汽车车型配置库:{subject} 全部在售版本 {_SPEC_FIELDS}")
        items = data.get("items") or []
        if not items:
            raise DataproError(data.get("msg") or "专业数据集没有这个车系")
        if data.get("has_more"):
            raise DataproError("专业数据集版本太多,一次没取全")
        rows, trims = parse_items(items, subject)
        if not rows or not trims:
            raise DataproError("专业数据集参数表是空的")
    except Exception as exc:  # noqa: BLE001 - 失败只记下来,由调用方降级
        return RawDoc(tool=CONFIG_TOOL, query=subject, retrieved_at=date, text="", ok=False,
                      error=str(exc))
    return RawDoc(tool=CONFIG_TOOL, query=subject, retrieved_at=date,
                  text=car_specs.render(subject, rows, trims, date, source="火山方舟专业数据集·车型配置库"))


def parse_sales(items: list[dict[str, Any]]) -> dict[str, list[tuple[str, int]]]:
    """销量行 → {车系: [(YYYY-MM, 销量)]},按月份新到旧。"""
    out: dict[str, list[tuple[str, int]]] = {}
    for it in items:
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
        data = search(f"汽车品牌销量数据:{subject} 全国 最近{months}个月 月销量")
        series = parse_sales(data.get("items") or [])
        if not series:
            raise DataproError(data.get("msg") or "专业数据集没有这个车系的销量")
    except Exception as exc:  # noqa: BLE001
        return RawDoc(tool=SALES_TOOL, query=subject, retrieved_at=date, text="", ok=False,
                      error=str(exc))
    lines = [f"{subject} 全国月销量(火山方舟专业数据集·汽车品牌销量数据,{date} 取数,口径未标注):"]
    for name, rows in series.items():
        lines.append(f"- {name}:" + ";".join(f"{ym} {n}辆" for ym, n in rows[:months]))
    for miss in data.get("missing_entities") or []:
        lines.append(f"- {miss}:数据集无此车系销量")
    return RawDoc(tool=SALES_TOOL, query=subject, retrieved_at=date, text="\n".join(lines))
