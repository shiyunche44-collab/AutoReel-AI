"""事实数据来源:同花顺 iFinD(金融/行业)+ 企查查(工商/合规)。

设计:本模块只做**原始材料收集**,不做结论抽取。
抽取成原子事实(带 claim / source_tool / retrieved_at)是 `script_gen` 的职责 ——
由 LLM 阅读原始文本后产出结构化事实,每条都能回溯到具体工具与查询串。

这样分工的理由:把"检索"和"断言"分开,才能保证脚本里的每个数字都挂着来源,
而不是模型凭记忆编一个看起来很像的参数。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cli

# Connector 工具名常量(严禁在别处硬编码字符串)
IFIND = {
    "stock_summary": "connector__thsifind__get_stock_summary",
    "security_indicators": "connector__thsifind__get_security_indicators",
    "search_securities": "connector__thsifind__search_securities",
    "search_news": "connector__thsifind__search_news",
    "search_edb": "connector__thsifind__search_edb",
    "fetch_edb": "connector__thsifind__fetch_edb",
}
QCC = {
    "company_profile": "connector__qichacha__get_company_profile",
    "trademark_info": "connector__qichacha__get_trademark_info",
    "advertising_review": "connector__qichacha__get_advertising_review",
    "product_recall": "connector__qichacha__get_product_recall",
    "product_spot_check": "connector__qichacha__get_product_spot_check",
    "counterfeit_cosmetics": "connector__qichacha__get_counterfeit_cosmetics",
}


@dataclass
class RawDoc:
    """一次 Connector 调用的原始结果。"""

    tool: str
    query: str
    retrieved_at: str
    text: str
    raw_path: str | None = None
    ok: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "query": self.query,
            "retrieved_at": self.retrieved_at,
            "text": self.text,
            "raw_path": self.raw_path,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class CollectionResult:
    docs: list[RawDoc] = field(default_factory=list)

    @property
    def context(self) -> str:
        """拼接成给 LLM 阅读的原始素材上下文。"""
        parts = []
        for i, doc in enumerate(self.docs, 1):
            if not doc.ok or not doc.text:
                continue
            parts.append(
                f"### 素材 {i}\n"
                f"- 来源工具:{doc.tool}\n"
                f"- 查询串:{doc.query}\n"
                f"- 取数日期:{doc.retrieved_at}\n\n"
                f"{doc.text}"
            )
        return "\n\n".join(parts)

    @property
    def failures(self) -> list[RawDoc]:
        return [d for d in self.docs if not d.ok]


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------
#: 业务信封里的噪声键,剥掉后正文更干净,省 token 也更不容易误导模型。
_ENVELOPE_KEYS = ("code", "msg", "message", "subCode", "subMsg", "success", "status")

#: 递归深度上限,只用来防病态自嵌套,不是用来限制真实嵌套层数。
#: 同花顺新闻的真实形态就已经有 7 层:
#:   list[{"type":"text","text":"<json>"}] → 信封 → {"data": "<json数组>"} → 文章 dict
#: 曾经设成 6,结果每篇文章都被守卫截成空串,空串又退化成 "data: ",
#: 让整个来源"成功"地返回零素材。宁可放宽,也不要在这里砍掉真实内容。
_MAX_DEPTH = 30

#: 正文可能藏在这些键下(同花顺/企查查的实际返回形态)
_PAYLOAD_KEYS = ("data", "answer", "content", "text", "result", "items", "list", "rows")

#: 少于这么多字符的正文视为"没拿到东西"。宁可记失败让人看见,
#: 也不要拿一个 "data: " 之类的残渣冒充成功素材。
_MIN_USEFUL_CHARS = 20


def _strip_envelope(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _ENVELOPE_KEYS}


def _flatten(result: Any, _depth: int = 0) -> str:
    """把 Connector 的各种返回形态压成纯文本,供 LLM 阅读。

    实测返回常有两层包装:
        {"code":0,"message":"ok","result":{"code":1,"msg":"success","data":"...正文..."}}
    直接丢给 LLM 会浪费大量 token 在信封上,且"code":1 容易被误读成失败。
    这里逐层剥壳,并且支持 data 字段本身还是一个 JSON 字符串的情况。
    """
    if _depth > _MAX_DEPTH:
        return ""
    if result is None:
        return ""
    if isinstance(result, str):
        text = result.strip()
        if text.startswith(("{", "[")):
            # 可能是被字符串化的 JSON,尝试再剥一层
            try:
                return _flatten(json.loads(text), _depth + 1)
            except (json.JSONDecodeError, ValueError):
                return text
        return text
    if isinstance(result, (int, float, bool)):
        return str(result)
    if isinstance(result, list):
        parts = [_flatten(item, _depth + 1) for item in result]
        return "\n".join(p for p in parts if p)
    if isinstance(result, dict):
        clean = _strip_envelope(result)
        saw_payload_key = False
        for key in _PAYLOAD_KEYS:
            if key in clean:
                saw_payload_key = True
                inner = _flatten(clean[key], _depth + 1)
                if inner:
                    return inner
        # 认出了正文键但压不出正文 → 说明上游确实没给内容,返回空串让调用方
        # 记为失败。绝不能退化成 "data: " 这种键值拼接:那会让一个空响应
        # 看起来像成功,而事实层会照常往下走,最终用 0 条产品事实写出一篇
        # 看似正常的脚本 —— 这正是之前"4 个来源全部成功"的假象来源。
        if saw_payload_key:
            return ""
        if not clean:
            return ""
        return "\n".join(
            f"{k}: {_flatten(v, _depth + 1)}" for k, v in clean.items()
        )
    return str(result)


def invoke(
    tool: str,
    args: dict[str, Any],
    *,
    query_label: str,
    project_dir: Path | None = None,
    timeout: int = 120,
) -> RawDoc:
    """调用一个事实来源,落盘原始返回,返回 RawDoc。

    单个来源失败不抛出 —— 事实层必须"尽力而为",某个数据源不可用时
    流水线仍应继续(计划风险 #6 的降级路径)。
    """
    today = time.strftime("%Y-%m-%d")
    try:
        result = cli.call(tool, args, timeout=timeout)
        text = _flatten(result)
        if len(text.strip()) < _MIN_USEFUL_CHARS:
            doc = RawDoc(
                tool=tool, query=query_label, retrieved_at=today, text="",
                ok=False,
                error=f"Connector 返回空正文(仅 {len(text.strip())} 字符)",
            )
        else:
            doc = RawDoc(tool=tool, query=query_label, retrieved_at=today, text=text)
    except cli.ConnectorError as exc:
        doc = RawDoc(
            tool=tool, query=query_label, retrieved_at=today, text="",
            ok=False, error=exc.message,
        )

    if project_dir:
        raw_dir = project_dir / "facts_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_]+", "_", tool.split("__")[-1])[:40]
        path = raw_dir / f"{slug}_{int(time.time() * 1000)}.json"
        try:
            path.write_text(
                json.dumps(doc.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            doc.raw_path = path.name
        except OSError:
            pass
    return doc


# --------------------------------------------------------------------------
# 具体来源
# --------------------------------------------------------------------------
def ifind_stock_summary(subject: str, project_dir: Path | None = None) -> RawDoc:
    query = f"{subject} 公司简介 主营业务 所属行业"
    return invoke(IFIND["stock_summary"], {"query": query},
                  query_label=query, project_dir=project_dir)


def ifind_financials(subject: str, project_dir: Path | None = None,
                     market: str = "A股") -> RawDoc:
    query = f"{subject} 最新一期 营业收入 净利润 毛利率 销量"
    return invoke(IFIND["security_indicators"], {"market": market, "query": query},
                  query_label=query, project_dir=project_dir)


def ifind_news(subject: str, project_dir: Path | None = None,
               size: int = 8, days: int = 540) -> RawDoc:
    """公司/销量/财报向的新闻检索。

    窗口从 180 天放宽到 540 天:车型上市/改款的硬参数(续航、售价、配置)
    往往只出现在发布当天的报道里,半年之后就不���抓了,而这恰恰是解说
    视频最需要的素材 —— 时效性由抽取层按报道日期把关,不在检索层一刀切。
    """
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 新车 发布 销量 财报 重大事件"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_product_news(subject: str, project_dir: Path | None = None,
                       size: int = 10, days: int = 540) -> RawDoc:
    """产品参数向的新闻检索 —— 车型硬数据的主要来源。

    为什么必须和 ifind_news 分开:把"销量 财报 重大事件"和"续航 售价 配置"
    混在一条查询里,检索引擎会把结果整体偏向公司财经新闻,车型参数反而被
    挤掉。实测同一车型下,产品向查询能稳定带出续航/加速/售价/电耗/配置。
    """
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 车型 参数 续航 加速 售价 配置 上市"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_industry_indicator(topic: str, project_dir: Path | None = None) -> RawDoc:
    """检索行业经济指标(先搜指标名,拿到指标清单供 LLM 引用)。"""
    query = f"{topic} 相关的行业经济指标有哪些"
    return invoke(IFIND["search_edb"], {"query": query},
                  query_label=query, project_dir=project_dir)


# ---------------------------------------------------------------------------
# 内容方向专属来源
#
# 每条查询只表达**一个意图** —— 这是本模块最关键的经验,踩过坑:
# 把"续航 售价 配置"和"销量 财报"混进一条查询,检索引擎会把结果整体拉回
# 公司财经新闻,车型参数被挤掉。实测拆分后产品级事实从 2 条升到 19 条。
# 下面每条查询都刻意与公司/销量向的 `ifind_news` 保持单词级别的区分。
# ---------------------------------------------------------------------------


def ifind_iteration_news(subject: str, project_dir: Path | None = None,
                         size: int = 10, days: int = 1500) -> RawDoc:
    """车型迭代史方向:历代车型、改款、换代。

    窗口比其他来源宽得多(1500 天 ≈ 4 年):迭代史天然跨多年,540 天只够
    捞回最近一次改款的发布通稿,讲不出"这条线怎么演进来的"。
    """
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 历代车型 改款 换代 停产 升级"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_compare_news(subject: str, project_dir: Path | None = None,
                       size: int = 10, days: int = 540) -> RawDoc:
    """横向对比方向:竞品、同级车型、差异。"""
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 对比 竞品 同级 差异 相比"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_cost_news(subject: str, project_dir: Path | None = None,
                    size: int = 10, days: int = 540) -> RawDoc:
    """真实使用成本方向:能耗、保险、保养、折旧。

    注意与 `ifind_news` 的"销量 财报"完全不相干 —— 这里要的是持有成本,
    不是公司经营数据。
    """
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 用车成本 保险 保养 电费 油耗 保值率"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_tech_news(subject: str, project_dir: Path | None = None,
                    size: int = 10, days: int = 540) -> RawDoc:
    """技术与设计拆解方向:平台、三电、智驾、底盘调校。"""
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{subject} 技术 架构 三电 智驾 平台 底盘 调校"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def ifind_market_news(topic: str, project_dir: Path | None = None,
                      size: int = 10, days: int = 540) -> RawDoc:
    """市场观察方向:份额、排名、行业格局、渗透率。"""
    end = time.strftime("%Y-%m-%d")
    start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    query = f"{topic} 销量 市场份额 排名 行业格局 渗透率"
    return invoke(
        IFIND["search_news"],
        {"query": query, "size": size, "time_start": start, "time_end": end},
        query_label=query, project_dir=project_dir,
    )


def qcc_profile(company: str, project_dir: Path | None = None) -> RawDoc:
    return invoke(QCC["company_profile"], {"searchKey": company},
                  query_label=company, project_dir=project_dir)


def qcc_trademark(company: str, project_dir: Path | None = None) -> RawDoc:
    return invoke(QCC["trademark_info"], {"searchKey": company},
                  query_label=company, project_dir=project_dir)


def qcc_advertising(company: str, project_dir: Path | None = None) -> RawDoc:
    return invoke(QCC["advertising_review"], {"searchKey": company},
                  query_label=company, project_dir=project_dir)


#: 品类 Profile 里可以直接引用的来源名 → 函数
SOURCES = {
    "ifind.stock_summary": ifind_stock_summary,
    "ifind.financials": ifind_financials,
    "ifind.news": ifind_news,
    "ifind.product_news": ifind_product_news,
    "ifind.industry_indicator": ifind_industry_indicator,
    # 内容方向专属来源(一条查询一个意图,理由见上方注释)
    "ifind.iteration_news": ifind_iteration_news,
    "ifind.compare_news": ifind_compare_news,
    "ifind.cost_news": ifind_cost_news,
    "ifind.tech_news": ifind_tech_news,
    "ifind.market_news": ifind_market_news,
    "qcc.profile": qcc_profile,
    "qcc.trademark": qcc_trademark,
    "qcc.advertising": qcc_advertising,
}


def collect(
    source_names: list[str],
    subject: str,
    *,
    topic: str = "",
    project_dir: Path | None = None,
) -> CollectionResult:
    """按品类 Profile 声明的来源列表依次收集原始材料。"""
    result = CollectionResult()
    for name in source_names:
        fn = SOURCES.get(name)
        if fn is None:
            result.docs.append(
                RawDoc(tool=name, query=subject, retrieved_at=time.strftime("%Y-%m-%d"),
                       text="", ok=False, error=f"未知事实来源: {name}")
            )
            continue
        if name == "ifind.industry_indicator" and topic:
            result.docs.append(fn(topic, project_dir))
        else:
            result.docs.append(fn(subject, project_dir))
    return result


def health_check() -> dict[str, bool]:
    """连通性自检:iFinD 与企查查各探一个最轻的调用。"""
    status: dict[str, bool] = {}
    try:
        cli.call(IFIND["stock_summary"], {"query": "测试连通性"}, timeout=30, retries=0)
        status["ifind"] = True
    except Exception:
        status["ifind"] = False
    try:
        cli.call(QCC["company_profile"], {"searchKey": "测试"}, timeout=30, retries=0)
        status["qichacha"] = True
    except Exception:
        status["qichacha"] = False
    return status
