"""② 脚本生成 + 合规 lint。

lint 是这个模块存在的核心理由。解说视频的两类翻车:
- **编造数据**:模型写出一个材料里没有的参数。
- **违反广告法**:绝对化用语("最强""第一""顶级")会让投放被拒。

两者都在这里被机械拦截,而不是指望模型自觉。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from ..categories import CategoryProfile, Vocab
from ..models import Fact, ScriptSegment
from .timeline import narration_budget_chars

#: 阿拉伯数字/百分号/单位 —— 出现即视为"数据型陈述",必须有事实引用
_NUMERIC_RE = re.compile(r"\d")

#: 年款型号(如 "2026 款"、"2024 款")不是性能参数,不要求事实引用。
#: 这类数字若也拦截,lint 会产生大量噪声,使用者会习惯性忽略它 ——
#: 那才是真正危险的:一个总在喊狼来了的告警等于没有告警。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def has_numeric_claim(text: str) -> bool:
    """判断文本是否含**需要事实支撑**的数据型陈述。

    剥离年款年份后再判断,其余数字(参数、价格、续航、加速等)一律要求引用。
    """
    if not text:
        return False
    stripped = _YEAR_RE.sub("", text)
    return bool(_NUMERIC_RE.search(stripped))


class LintIssue:
    __slots__ = ("segment_idx", "severity", "message")

    def __init__(self, segment_idx: int, severity: str, message: str) -> None:
        self.segment_idx = segment_idx
        self.severity = severity
        self.message = message

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "segment_idx": self.segment_idx,
            "severity": self.severity,
            "message": self.message,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"[{self.severity}] seg{self.segment_idx}: {self.message}"


SYSTEM = """你是中文{domain}(/{category})品类的短视频解说编导。

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或任何解释文字。
2. `segments` 数组,每项: {{"section": 段落名, "text": 解说词, "fact_refs": ["事实ID"]}}
   - segments 的数量与顺序必须严格对应给定的段落序列。
   - text 为口播文案,口语化、短句、有画面感,不要用书面语长句。
3. **事实引用是强制的**:text 中每出现一个数字({number_examples}),
   必须在 fact_refs 中列出支撑该数字的事实 ID。没有事实支撑的数字一律不许写。
   可以改写成不含数字的定性表述,而不是硬写。
4. **本次内容方向**:
{theme_intent}
{theme_rules}
{scope_policy}
5. **数字之外的硬红线**(所有方向都适用,不得因为方向而放宽):
   - 严禁编造任何未在事实清单中出现的信息,包括非数字的参数名与技术名词。
   - 禁止绝对化用语与无法证实的承诺。
   - 只讲{subject_noun}/这个品牌本身,不评价其他无关产品。
6. `title`:8-16 字的视频标题,要有吸引力但不得夸大。
{title_policy}
7. 字数:全片约 {chars} 字,平均每镜约 {per_shot} 字。
8. **黄金三秒**:第 1 段的第一句话必须在 18 字以内(约 3 秒读完),直接抛出一个反常识、
   争议或最硬的一个结论/数字,让人想听下去。不许用"大家好""今天来聊聊""欢迎""这期视频"
   这类开场白,不许先自我介绍或交代背景。

可用事实清单(只能用这里的 ID,已标注层级):
{facts}

段落序列(必须一一对应):
{plan}

必需关键词: {keywords}

必需键: segments, title"""

#: 事实层级使用策略。
#:
#: 这一段是方向化最关键的一处。旧版本只有一套「严禁把 company 层事实当作
#: 产品卖点讲、公司背景最多开场一句话、title 必须指向产品而非公司」的规则 ——
#: 对**品牌历史与产品战略**、**市场观察**方向是反向错误的:那两个方向整片主体
#: 恰恰就是公司层与行业层事实,照搬旧规则会让脚本被自己的合规规则掐死。
_SCOPE_POLICY_PRODUCT = """   - `product` 层 = 产品本身({product_examples})。
   - `company` 层 = 公司工商与财务(注册资本、成立日期、法定代表人、注册地址、
     证券代码、财报、销量总量)。
   - `industry` 层 = 行业与市场(渗透率、份额、政策)。
   **严禁把 company 层事实当作产品卖点讲。** 不允许出现"{subject_noun}的证券代码是…"、
   "这家{company_noun}成立于…"这类内容。公司背景最多在开场用一句话带过,
   且不占用任何 `product` 段落。
   若某个段落(如"参数硬数据")没有 product 层事实可用,就写定性体验描述,
   **绝对不要**用 company 层信息去填。宁可这一段平淡,也不能讲错对象。"""

_SCOPE_POLICY_COMPANY = """   - `company` 层 = 公司工商与财务(注册资本、成立日期、法定代表人、经营范围、
     证券代码、财报、销量结构)。
   - `industry` 层 = 行业与市场(渗透率、份额、排名、政策与热点)。
   - `product` 层 = 产品参数({product_examples_short})。
   本方向的**主体就是 company 与 industry 层事实**,它们是正当素材,不是需要回避的内容。
   反过来,**严禁用 product 层参数去论证公司战略或行业结论** ——
   不允许出现"{spec_claim_example},所以公司的战略是…"这类跳跃推理。
   若某个段落没有 company/industry 层事实可用,就写定性描述,不要拿产品参数硬凑。"""

_TITLE_POLICY_PRODUCT = "   标题必须指向产品本身,而不是公司或行业。"
_TITLE_POLICY_COMPANY = "   标题可以指向品牌、公司或市场,但不得夸大。"


def _scope_policy(theme, vocab: Vocab) -> str:
    """按方向所依赖的事实层级选择策略段。无方向时按产品向。"""
    required = set(getattr(theme, "required_scopes", ("product",)) or ())
    policy = _SCOPE_POLICY_PRODUCT if not required or "product" in required         else _SCOPE_POLICY_COMPANY
    return policy.format(
        product_examples=vocab.product_examples,
        product_examples_short=vocab.product_examples_short,
        subject_noun=vocab.subject_noun, company_noun=vocab.company_noun,
        spec_claim_example=vocab.spec_claim_example,
    )


#: 质量记录(召回 / 抽检 / 假冒)类事实的主题标签,与 intake.QUALITY_TOPIC 一致
QUALITY_TOPIC = "quality"

#: 质量记录类事实的讲法。只在事实里有这类记录时追加
_QUALITY_RULE = ("topic=quality 的事实是品牌 / 公司名下的召回、抽检、假冒记录,"
                 "不是这一款产品本身的:只能说成\"该品牌曾……\"并带上记录里的时间和产品名,"
                 "不得说成这款产品有问题,也不得据此下\"不安全\"\"质量差\"这类结论。")


def _theme_rules(theme, facts: Sequence[Fact]) -> str:
    rules = list(theme.script_rules) if theme is not None else []
    if any(f.topic == QUALITY_TOPIC and f.enabled for f in facts):
        rules.append(_QUALITY_RULE)
    return "\n".join(f"   - {r}" for r in rules) if rules else "   (无方向专属规则)"


def _title_policy(theme) -> str:
    required = set(getattr(theme, "required_scopes", ()) or ())
    if required and "product" in required:
        return _TITLE_POLICY_PRODUCT
    return _TITLE_POLICY_COMPANY


def generate(
    *,
    subject: str,
    profile: CategoryProfile,
    facts: Sequence[Fact],
    plan: Sequence[str],
    duration: int,
    llm,
    theme=None,
) -> tuple[list[ScriptSegment], str]:
    """生成解说脚本。返回 (段落列表, 标题)。"""
    vocab = profile.vocab
    budget = narration_budget_chars(duration)
    shot_count = max(1, len(plan))
    scope_label = {"product": "product", "company": "company", "industry": "industry"}
    facts_payload = "\n".join(
        f"- {f.id} [{scope_label.get(f.scope, 'company')}"
        f"{' · topic=quality' if f.topic == QUALITY_TOPIC else ''}] | {f.claim} | "
        f"来源:{f.source_tool} | 可信度:{f.confidence}"
        for f in facts
    ) or "(无可用事实:请完全避免写出任何具体数字)"

    user = (
        f"Product subject: {subject}\n"
        f"Category: {profile.label}\n"
        f"Script style: {profile.script_style or '口语化,克制,有画面感'}\n"
        f"Total duration: {duration} seconds\n"
        f"Target total characters: {budget}\n\n"
        f"可用事实清单:\n{facts_payload}\n\n"
        f"段落序列:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan))
    )

    data = llm.structured(
        SYSTEM.format(
            domain=vocab.domain,
            number_examples=vocab.number_examples,
            subject_noun=vocab.subject_noun,
            category=profile.label,
            chars=budget,
            per_shot=max(20, budget // shot_count),
            facts=facts_payload,
            plan="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan)),
            keywords=", ".join(profile.visual_keywords[:4]),
            theme_intent=f"   {theme.intent}" if theme is not None and theme.intent
            else "   按品类惯例组织叙事。",
            theme_rules=_theme_rules(theme, facts),
            scope_policy=_scope_policy(theme, vocab),
            title_policy=_title_policy(theme) if theme is not None else _TITLE_POLICY_PRODUCT,
        ),
        user,
        required_keys=["segments", "title"],
        temperature=0.7,
    )

    segments: list[ScriptSegment] = []
    raw_segments = data.get("segments") or []
    for i, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        section = str(item.get("section", "")).strip()
        if not section:
            section = plan[i] if i < len(plan) else ""
        refs = item.get("fact_refs") or []
        if isinstance(refs, str):
            refs = [refs]
        segments.append(
            ScriptSegment(
                idx=len(segments),
                section=section,
                text=text,
                fact_refs=[str(r) for r in refs if str(r)],
            )
        )

    title = str(data.get("title") or "").strip() or f"{subject} 详解"
    return segments, title


# --------------------------------------------------------------------------
# 合规 lint
# --------------------------------------------------------------------------
#: 竞品标记词 —— 出现在段落里就说明这句在讲别人。
_COMPETITOR_MARKERS = ("竞品", "对手", "友商", "同级", "其他品牌", "别家")
#: 贬义词 —— 与竞品标记共现即构成"贬低竞品"。
#:
#: 刻意收窄:像"不如""吊打"这类词在中文汽车内容里经常是**客观比较**
#: ("同级的竞品在保值率上不如它"),把它们算作贬义会误伤对比方向本身。
#: 这里只放无歧义的侮辱性说法 —— 真正的红线是"点名别家 + 骂它"。
_DISPARAGING_TERMS = ("智商税", "劝退", "垃圾", "辣鸡", "韭菜", "坑爹")


#: 黄金三秒:第一句的字数上限(约 3 秒读完,按 5.8 字/秒)与禁用的开场套话
HOOK_MAX_CHARS = 18
HOOK_FILLER = ("大家好", "今天来聊", "今天我们", "欢迎", "这期视频", "本期视频", "哈喽", "各位")


def first_sentence(text: str) -> str:
    """脚本的第一句(到第一个句末标点为止)。"""
    return re.split(r"[。!?!?;;\n]", (text or "").strip(), maxsplit=1)[0].strip()


def hook_lint(segments: Sequence[ScriptSegment]) -> list[LintIssue]:
    """开场第一句太长或是套话 → warning(提示重写,不硬拦)。"""
    if not segments:
        return []
    first = first_sentence(segments[0].text)
    issues: list[LintIssue] = []
    if len(first) > HOOK_MAX_CHARS:
        issues.append(LintIssue(segments[0].idx, "warning",
                                f"开场第一句 {len(first)} 字,超过黄金三秒的 {HOOK_MAX_CHARS} 字:{first}"))
    for term in HOOK_FILLER:
        if term in first:
            issues.append(LintIssue(segments[0].idx, "warning",
                                    f"开场第一句是套话「{term}」,应直接抛钩子:{first}"))
            break
    return issues


def compliance_lint(
    segments: Sequence[ScriptSegment],
    profile: CategoryProfile,
    facts: Sequence[Fact],
    theme=None,
) -> list[LintIssue]:
    """机械校验脚本。error 级问题必须修复后才能进入分镜阶段。"""
    issues: list[LintIssue] = []
    known_ids = {f.id for f in facts}
    forbidden = list(profile.forbidden_terms)
    if theme is not None:
        forbidden.extend(theme.extra_forbidden_terms)
    allow_critique = bool(getattr(theme, "allow_subject_critique", False))
    # 规则 5 只在**产品是叙事主体**的方向生效。
    #
    # 它的触发词表里含"电池""充电""配置"这类词,而这些词在品牌叙事里同样高频
    # —— "这家车企最早是做电池起家的"会被判成"用公司事实讲产品参数",
    # 于是整个品牌历史方向被自己的 lint 掐死。品牌/市场方向的叙事主体本就是
    # company/industry 层,这条规则对它们没有意义,因此直接跳过。
    theme_requires_product = True if theme is None else (
        "product" in (theme.required_scopes or ("product",))
    )

    for seg in segments:
        text = seg.text or ""

        # 1. 数字必须有事实引用(年款年份除外)
        if has_numeric_claim(text) and not seg.fact_refs:
            issues.append(
                LintIssue(
                    seg.idx,
                    "error",
                    f"段落含数字但没有任何事实引用:{text[:40]}..."
                    "请补充 fact_refs,或改写为不含数字的定性表述",
                )
            )

        # 2. 引用必须指向真实存在的事实
        for ref in seg.fact_refs:
            if ref not in known_ids:
                issues.append(
                    LintIssue(seg.idx, "error", f"引用了不存在的事实 ID:{ref}")
                )

        # 3. 品类禁用词 + 方向禁用词(广告法红线)
        for term in forbidden:
            if term in text:
                issues.append(
                    LintIssue(seg.idx, "error", f"出现品类禁用词「{term}」:{text[:40]}...")
                )

        # 4. 通用绝对化用语(所有品类共享的红线)
        for term in ("最强", "第一", "顶级", "永久", "唯一", "100%"):
            if term in text:
                issues.append(
                    LintIssue(seg.idx, "error", f"出现广告法绝对化用语「{term}」")
                )

        # 5. 层级混用:用公司工商信息讲产品 —— 解说视频最典型的讲错对象。
        #    只在产品向方向生效,理由见 theme_requires_product 的注释。
        cited = [f for f in facts if f.id in seg.fact_refs]
        if theme_requires_product and cited and all(f.scope != "product" for f in cited):
            if any(keyword in text for keyword in profile.vocab.subject_words):
                issues.append(
                    LintIssue(
                        seg.idx,
                        "error",
                        "把公司层面事实当作产品参数来讲了"
                        f"(引用了 {len(cited)} 条 company/industry 事实)。"
                        "请改写为定性体验描述,或先补充产品级事实。",
                    )
                )

        # 6. 禁止贬低竞品 —— 对**所有方向**生效,不只 cost 方向。
        #    判别靠竞品标记词:允许讲本车缺点(allow_subject_critique)与
        #    不许贬低别人靠的是同一个句子里有没有点名第三方。
        if any(m in text for m in _COMPETITOR_MARKERS):
            for term in _DISPARAGING_TERMS:
                if term in text:
                    issues.append(
                        LintIssue(
                            seg.idx,
                            "error",
                            f"在评价其他品牌时使用了贬义词「{term}」:{text[:40]}..."
                            + ("本方向允许讲自身缺点,但" if allow_critique else "")
                            + "不得贬低竞品,请改写为客观的差异陈述",
                        )
                    )
                    break

    issues.extend(hook_lint(segments))
    return issues


def ensure_disclaimers(
    segments: list[ScriptSegment],
    profile: CategoryProfile,
) -> list[ScriptSegment]:
    """在最后一个段落追加免责声明(仅在缺失时)。"""
    if not profile.required_disclaimers or not segments:
        return segments
    last = segments[-1]
    missing = [d for d in profile.required_disclaimers if d not in last.text]
    if missing:
        last.text = last.text.rstrip() + "".join(f"({d})" for d in missing)
    return segments


def total_chars(segments: Iterable[ScriptSegment]) -> int:
    return sum(len(s.text or "") for s in segments)


def normalize_segments(
    segments: Sequence[ScriptSegment],
    plan: Sequence[str],
) -> list[ScriptSegment]:
    """把段落数量对齐到分镜计划 —— 数量不一致会导致画面与旁白错位。"""
    out = list(segments)
    # 段落过多:合并尾部
    while len(out) > len(plan):
        merged = out[-2].text.rstrip() + out[-1].text
        out[-2].text = merged
        out[-2].fact_refs = list(dict.fromkeys(out[-2].fact_refs + out[-1].fact_refs))
        out.pop()
    # 段落过少:复制最后一段占位,后续由用户/分镜阶段修正
    while len(out) < len(plan):
        template = out[-1] if out else ScriptSegment(idx=0, text="")
        out.append(
            ScriptSegment(
                idx=len(out),
                section=plan[len(out)] if len(out) < len(plan) else "",
                text=template.text,
                fact_refs=list(template.fact_refs),
            )
        )
    for i, seg in enumerate(out):
        seg.idx = i
    return out
