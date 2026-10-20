"""① 事实收集:原始材料 → 结构化可追溯事实。

两步:
1. 从 iFinD / 企查查 拉原始文本(`providers.facts`)。
2. 让 LLM 把原始文本抽成**原子事实**,每条挂上来源工具、查询串、取数日期。

第 2 步是整个平台可信度的地基。汽车解说最容易翻车的地方是模型凭印象
编出一个"看起来很像真的"参数(702km 写成 720km、3.8 秒写成 3.5 秒),
所以脚本里的每个数字都必须能指回一条 fact —— 后续 `script_gen.compliance_lint`
会对没有 fact 引用的数字直接报错。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .. import db
from ..categories import CategoryProfile, Vocab
from ..models import Fact
from ..providers import facts as facts_provider
from . import script_gen

# ---------------------------------------------------------------------------
# 事实层级判定
# ---------------------------------------------------------------------------
# iFinD 的证券类接口天然只能回答"公司"层面的问题(注册资本、上市日期、
# 法定代表人、财报)。把它们当产品参数用,会产出"这台车的证券代码是 002594"
# 这种荒谬文案。因此事实必须分层,脚本必须按层取用。
_COMPANY_HINTS = (
    "注册资本", "法定代表人", "法人代表", "注册地址", "办公地址", "成立日期",
    "上市日期", "证券代码", "股票代码", "证券简称", "统一社会信用代码",
    "企业名称", "所属行业", "经营范围", "公司简介", "控股股东",
    "实际控制人", "员工人数", "分支机构", "专利", "商标", "年报",
    # 品牌历史与产品战略方向
    "技术路线", "研发投入",
)
#: 产品层的通用词。品类专属的(汽车的续航、马力……)在 profile 的 vocab.product_hints
_PRODUCT_HINTS = (
    "容量", "功率", "配置", "选装", "官方指导价", "售价", "尺寸", "屏幕",
    "配色", "版本", "上市", "改款", "保养", "投诉", "召回",
)
_INDUSTRY_HINTS = (
    "行业", "市场", "渗透率", "销量排行", "整体", "全国", "同比",
    "环比", "份额", "政策", "补贴",
    # 市场观察方向
    "格局", "竞争", "排名", "保有量", "市场集中度",
)


def classify_scope(claim: str, topic: str = "", vocab: Vocab | None = None) -> str:
    """判定一条事实属于 company / product / industry 哪一层。

    顺序有意为之:先判产品,再判行业,最后兜底为公司 ——
    因为产品参数才是解说视频真正需要的,宁可多标成 product,
    也不能把一条产品参数误判成公司信息而被脚本忽略。
    """
    text = f"{claim} {topic}"
    vocab = vocab or Vocab()
    if any(h in text for h in (*_PRODUCT_HINTS, *vocab.product_hints)):
        return "product"
    if any(h in text for h in (*_INDUSTRY_HINTS, *vocab.industry_hints)):
        return "industry"
    if any(h in text for h in _COMPANY_HINTS):
        return "company"
    return "company"


def summarise_scopes(facts: list[Fact]) -> dict[str, int]:
    counts = {"company": 0, "product": 0, "industry": 0}
    for f in facts:
        counts[f.scope] = counts.get(f.scope, 0) + 1
    return counts


def product_gap_warning(facts: list[Fact], profile: CategoryProfile,
                        theme_name: str | None = None) -> str | None:
    """该方向所需的事实层级严重不足时给出明确警告。

    与其让脚本拿错层级的信息硬凑,不如明确告诉使用者:当前数据源覆盖不到
    这个方向要讲的东西,请在门禁 A 手动补充。

    判定按方向的 `required_scopes` 而非写死 product —— 品牌历史方向整片
    主体是 company/industry 事实,product 少于 3 条完全正常,不该报警。
    """
    theme = profile.theme_spec(theme_name)
    counts = summarise_scopes(facts)
    required = theme.required_scopes or ("product",)

    gaps = [s for s in required if counts.get(s, 0) < 3]
    if not gaps:
        return None

    scope_cn = {"product": "产品级", "company": "公司级", "industry": "行业级"}
    detail = "、".join(f"{scope_cn.get(s, s)} {counts.get(s, 0)} 条" for s in required)
    missing_cn = "、".join(scope_cn.get(g, g) for g in gaps)

    return (
        f"「{theme.label}」方向需要 {detail},其中{missing_cn}不足 3 条。"
        f"当前事实来源覆盖不到该方向要讲的内容 —— 相关段落只能写定性描述,"
        f"给不出带数字的结论。请在门禁 A 手动补充,或换一个数据覆盖更好的方向。"
    )


def collect_raw(
    profile: CategoryProfile,
    subject: str,
    project_dir: Path,
    theme: str | None = None,
) -> facts_provider.CollectionResult:
    """按**当前方向**声明的来源列表收集原始材料。

    不同方向要的素材完全不同:迭代史要跨年的改款通稿,市场观察要份额排名,
    真实成本要保险保养。走一份平铺的来源列表,会让每个方向都拿回一堆用不上的
    公司财经新闻,反而把该方向真正需要的挤掉。
    """
    return facts_provider.collect(
        list(profile.sources_for(theme)),
        subject,
        topic=industry_topic(profile),
        project_dir=project_dir,
        queries=profile.fact_queries,
        company=company_of(profile, subject),
    )


def industry_topic(profile: CategoryProfile) -> str:
    """行业指标检索用的行业名。"""
    return profile.vocab.industry_topic or profile.label


def company_of(profile: CategoryProfile, subject: str) -> str:
    """企查查按公司名查。品牌配了 `company`(工商全称)就用它,否则退回主体名 ——
    企查查的模糊检索拿型号名也常能命中,只是准度差一些。"""
    from ..providers import official

    _, spec = official.detect_brand(subject, profile.official_sites)
    names = tuple(spec.get("company", ())) if spec else ()
    return names[0] if names else subject


EXTRACT_SYSTEM = """You extract atomic, verifiable facts from Chinese business/financial raw material about a product.

Rules:
1. Output ONE JSON object only. No markdown fences, no commentary.
2. `facts` is an array. Each element: {{claim, source_tool, source_query, retrieved_at, confidence, topic}}
   - claim: ONE factual sentence in Chinese. Include concrete numbers ONLY if they appear verbatim in the material.
   - source_tool / source_query / retrieved_at: copy EXACTLY from the material header for that fact.
   - confidence: "high" if a specific number or official record; "medium" if descriptive; "low" if vague.
   - topic: a short label like {topic_examples}.
3. NEVER invent, extrapolate, round, or estimate a number. If the material does not state it, do not create the fact.
4. Keep 8-25 facts, ordered from most product-relevant to least.
5. Required keys: facts

RECENCY RULES (critical for product specifications):
6. News material carries a 日期 field per article. A product specification ({spec_examples})
   is only valid for the CURRENT model if the article is recent. Today is given in the user message.
7. If a product-spec fact comes from an article OLDER than 12 months, you MUST either
   (a) drop it, or (b) keep it but state the article's year explicitly in the claim
       (e.g. "{year_example}") and set confidence to "low".
   Never present an old article's numbers as the current configuration.
8. When the same specification appears in several articles with different values, prefer the most
   recent article and mention the value's year. Do not average them and do not pick silently.
9. Company-level facts (注册资本 / 法定代表人 / 证券代码 / 财报) are useful context but must never be
   phrased as product attributes."""

#: 质量记录类事实的主题标签。脚本按它套"只能说成品牌曾…"的规则
QUALITY_TOPIC = script_gen.QUALITY_TOPIC

#: 材料里有召回 / 抽检 / 假冒记录时追加的抽取规则
QUALITY_RULE = """
10. Material from product recall / spot-check / counterfeit registries describes the BRAND or COMPANY,
    not necessarily this exact product. Keep only records from the last 3 years that concern this
    product category; phrase each claim with the record's date and the product named in the record
    (e.g. "2024 年 3 月,该品牌一款婴儿推车因折叠锁扣缺陷被召回"). Set topic to "quality".
    If the registry returned nothing relevant, output NO fact for it — never write "no recall records"."""

EXTRACT_USER = """Product subject: {subject}
Category: {category}
Today: {today}

RAW MATERIAL
------------
{context}
------------

Extract the atomic facts now. Remember: no invented numbers, and product specifications must respect the recency rules."""


def extract_facts(
    context: str,
    *,
    subject: str,
    category: str,
    llm,
    source_docs: list[dict[str, Any]] | None = None,
    profile: CategoryProfile | None = None,
) -> list[Fact]:
    """把原始材料抽取为结构化事实。LLM 不可用时返回空列表(由上层决定降级)。"""
    if not context.strip():
        return []

    vocab = profile.vocab if profile is not None else Vocab()
    system = EXTRACT_SYSTEM.format(topic_examples=vocab.extract_topic_examples,
                                   spec_examples=vocab.extract_spec_examples,
                                   year_example=vocab.extract_year_example)
    if any(d.get("tool") in facts_provider.QUALITY_TOOLS for d in source_docs or []):
        system += QUALITY_RULE
    data = llm.structured(
        system,
        EXTRACT_USER.format(
            subject=subject,
            category=category,
            today=time.strftime("%Y-%m-%d"),
            context=context[:24000],
        ),
        required_keys=["facts"],
        temperature=0.2,
    )

    # 建立 (tool, query) -> raw_ref 的映射,便于回溯原始文件
    ref_index: dict[tuple[str, str], str | None] = {}
    for doc in source_docs or []:
        ref_index[(doc.get("tool", ""), doc.get("query", ""))] = doc.get("raw_path")

    facts: list[Fact] = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict) or not str(item.get("claim", "")).strip():
            continue
        tool = str(item.get("source_tool", "")).strip()
        query = str(item.get("source_query", "")).strip()
        confidence = str(item.get("confidence", "medium")).strip().lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        facts.append(
            Fact(
                claim=str(item["claim"]).strip(),
                source_tool=tool or "unknown",
                source_query=query,
                retrieved_at=str(item.get("retrieved_at") or time.strftime("%Y-%m-%d")),
                confidence=confidence,  # type: ignore[arg-type]
                raw_ref=ref_index.get((tool, query)),
                topic=str(item.get("topic", "")).strip(),
                scope=classify_scope(
                    str(item["claim"]).strip(), str(item.get("topic", "")).strip(), vocab
                ),
            )
        )
        if tool in facts_provider.QUALITY_TOOLS:
            # 召回 / 抽检 / 假冒记录是品牌级的,不是这一款产品的(见 QUALITY_RULE)
            facts[-1].scope, facts[-1].topic = "company", QUALITY_TOPIC
    return facts


def run(
    project_id: str,
    profile: CategoryProfile,
    subject: str,
    *,
    theme: str | None = None,
    llm=None,
) -> tuple[list[Fact], dict[str, Any]]:
    """完整的事实收集流程。返回 (事实列表, 采集报告)。"""
    project_dir = db_dir(project_id)
    collected = collect_raw(profile, subject, project_dir, theme)

    docs_payload = [d.to_dict() for d in collected.docs]
    db.write_json(project_id, "facts_raw.json", docs_payload)

    facts: list[Fact] = []
    error = None
    if llm is not None:
        try:
            facts = extract_facts(
                collected.context,
                subject=subject,
                category=profile.label,
                llm=llm,
                source_docs=docs_payload,
                profile=profile,
            )
        except Exception as exc:  # noqa: BLE001 - 事实层降级不应中断流水线
            error = str(exc)

    db.write_json(project_id, "facts.json", [f.model_dump() for f in facts])

    report = {
        "sources_requested": list(profile.sources_for(theme)),
        "sources_ok": [d.tool for d in collected.docs if d.ok],
        "sources_failed": [
            {"tool": d.tool, "error": d.error} for d in collected.failures
        ],
        "raw_chars": len(collected.context),
        "facts_extracted": len(facts),
        "scope_counts": summarise_scopes(facts),
        # 键名沿用 product_gap:project.html 与模板自检样例都依赖它,
        # 内容已按方向所需层级重新判定。
        "product_gap": product_gap_warning(facts, profile, theme),
        "extract_error": error,
    }
    db.write_json(project_id, "facts_report.json", report)
    return facts, report


def db_dir(project_id: str) -> Path:
    from .. import config

    return config.project_dir(project_id)


def health_check() -> dict[str, bool]:
    return facts_provider.health_check()
