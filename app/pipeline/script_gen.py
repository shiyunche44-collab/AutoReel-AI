"""② 脚本生成 + 合规 lint。

lint 是这个模块存在的核心理由。解说视频的两类翻车:
- **编造数据**:模型写出一个材料里没有的参数。
- **违反广告法**:绝对化用语("最强""第一""顶级")会让投放被拒。

两者都在这里被机械拦截,而不是指望模型自觉。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

from ..categories import CategoryProfile, Slot, Vocab
from ..models import Fact, ScriptSegment
from .grounding import arabicize, numbers_in
from .timeline import narration_budget_chars

#: 阿拉伯数字/百分号/单位 —— 出现即视为"数据型陈述",必须有事实引用
_NUMERIC_RE = re.compile(r"\d")

#: 年款型号(如 "2026 款"、"2024 款")不是性能参数,不要求事实引用。
#: 这类数字若也拦截,lint 会产生大量噪声,使用者会习惯性忽略它 ——
#: 那才是真正危险的:一个总在喊狼来了的告警等于没有告警。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

#: 型号代号(YU7、SU7、X5、V6s)里的数字是名字不是参数。只认"1–4 个字母 + 1–2 位数字",
#: 这样 "CLTC643""752V""700TOPS" 这类真参数不会被误剥
_MODEL_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{1,2}(?![\d.])[A-Za-z]*")


#: 广告法绝对化用语:所有品类共享的红线
AD_ABSOLUTE_TERMS = ("最强", "第一", "顶级", "永久", "唯一", "100%")


def forbidden_in(text: str, profile: CategoryProfile) -> list[str]:
    """文本命中的禁用词(品类禁用词 + 广告法绝对化用语)。"第一次""第一眼"这类不算。"""
    plain = _BENIGN_FIRST_RE.sub("", text or "")
    terms = dict.fromkeys((*profile.forbidden_terms, *AD_ABSOLUTE_TERMS))
    return [t for t in terms if t and t in plain]


def has_numeric_claim(text: str) -> bool:
    """判断文本是否含**需要事实支撑**的数据型陈述。

    剥离年款年份后再判断,其余数字(参数、价格、续航、加速等)一律要求引用。
    """
    if not text:
        return False
    # 中文数量词("五百九十三公里")同样是数据,先统一成阿拉伯数字再判断
    stripped = _MODEL_CODE_RE.sub("", _YEAR_RE.sub("", arabicize(text)))
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
   - 数值一律写阿拉伯数字(如 26.35 万、593 公里、4.3 秒),字幕直接显示它,配音会自动转成读法。
3. **事实引用是强制的**:text 中每出现一个数字({number_examples}),
   必须在 fact_refs 中列出支撑该数字的事实 ID。没有事实支撑的数字一律不许写。
   可以改写成不含数字的定性表述,而不是硬写。
4. **本次内容方向**:
{theme_intent}
{theme_rules}
{scope_policy}
5. **数字之外的硬红线**(所有方向都适用,不得因为方向而放宽):
   - 严禁编造任何未在事实清单中出现的信息,包括非数字的参数名与技术名词。
     讲原理、讲取舍只能用下方「可用原理清单」里的说法,并把它的 k_ ID 写进 fact_refs;
     原理清单不含数字,句子里的数字仍然只能来自事实清单。
   - 禁止绝对化用语与无法证实的承诺。
   - {scope_line}
6. `title`:8-16 字的视频标题,要有吸引力但不得夸大。
{title_policy}
7. 字数:全片约 {chars} 字,平均每镜约 {per_shot} 字。
8. **黄金三秒**:第 1 段的第一句话必须在 18 字以内(约 3 秒读完),直接抛出一个反常识、
   争议或最硬的一个结论/数字,让人想听下去。不许用"大家好""今天来聊聊""欢迎""这期视频"
   这类开场白,不许先自我介绍或交代背景。{angle_policy}
9. **主论点** `thesis`:先定一句话,再照着它写全片。
   - 不超过 30 字,是一个**判断**而不是话题(「{subject}适合既要又要的人」而不是「{subject}的配置」);
   - 全片每一段都要为它服务:每段开头能接住它,最后一段必须**原样包含这句话**;
   - 不要写成广告语(「闭眼入」「太香了」这类),要写成有取舍的判断。
10. **开头三段式,结尾给行动**:
   - 开头三段分开写,各司其职:① **先给冲突** —— 一句带判断的反常识话
     (「看起来更贵的那台,反而更划算」这种),不是「XX 值不值得买」这种平的问题;
     ② **立刻兑现** —— 把上一段抛出的那个数字或判断摆出来,不许绕;
     ③ **建立可信度** —— 一句话说明每个数字都能查到出处,全片只说这一次。
   - 结尾要给**行动**,不只给总结:最后一段必须有一句提问(「你在纠结哪款?评论区说」)
     或一句下期预告(「下期做同价位横评」),让人知道下一步做什么。

可用事实清单(只能用这里的 ID,已标注层级;来源 derived 是由其它事实推算的体感数字):
{facts}

可用原理清单(讲"为什么"和"代价"时引用,ID 以 k_ 开头):
{principles}

段落序列(必须一一对应):
{plan}

必需关键词: {keywords}

必需键: segments, title, thesis"""

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

#: 对比事实的主题标签:一条事实里写清本车与对比车同一维度的取值。
#: 这类事实只能用在竞品对比段,混进本车的参数面板就是把别家的数字安到本车头上。
COMPARE_TOPIC = "对比"

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


#: 横评时替换 SYSTEM 第 5 条"只讲这台车"那一句
ROUNDUP_SCOPE_LINE = ("只讲车单里的这几台车;每段只讲段落序列里〔〕标注的那台,"
                      "它的数字只能引用这台车名下的事实,不许把 A 车的参数安到 B 车头上。")


def roster_line(subjects: Sequence[dict[str, str]]) -> str:
    """车单一行:小米YU7(25-30万)、特斯拉Model Y。"""
    return "、".join(f"{s['name']}({s['group']})" if s.get("group") else s["name"]
                    for s in subjects)


def roundup_facts_payload(facts: Sequence[Fact], subjects: Sequence[dict[str, str]],
                          line) -> str:
    """横评的事实清单按车分组(车单顺序),没有车名的共用事实放最后。"""
    blocks = []
    for s in subjects:
        own = [line(f) for f in facts if f.subject == s["name"]]
        head = f"## {s['name']}" + (f"({s['group']})" if s.get("group") else "")
        blocks.append(head + "\n" + ("\n".join(own) or "(这台车没有可用事实:只能写定性描述)"))
    names = {s["name"] for s in subjects}
    shared = [line(f) for f in facts if f.subject not in names]
    if shared:
        blocks.append("## 共用\n" + "\n".join(shared))
    return "\n\n".join(blocks)


def usable_facts(facts: Sequence[Fact]) -> list[Fact]:
    """进脚本的事实:去掉推算源已被剔除(门禁 A 取消勾选)的推算事实。"""
    enabled = {f.id for f in facts if f.enabled}
    return [f for f in facts
            if not (f.source_kind == "derived" and f.source_query and f.source_query not in enabled)]


def matched_principles(profile: CategoryProfile, facts: Sequence[Fact]):
    """事实清单里提到了的原理条目 —— 没提到的技术不讲,免得讲了这台车根本没有的东西。"""
    text = "\n".join(f.claim for f in facts if f.enabled)
    return [p for p in profile.principles if p.matches(text)]


def _angle_policy(angle: str) -> str:
    """选题角度规则。无角度时返回空串,SYSTEM 渲染结果与旧版逐字一致。"""
    if not angle:
        return ""
    return (
        f"\n9. **选题角度**:本期选题是「{angle}」。\n"
        "   - 第 1 段的钩子与 title 必须围绕这个角度,让观众一眼知道这期在聊这件事;"
        "但第一句不许照抄角度原句,仍须 18 字以内。\n"
        "   - 角度原句里的绝对化用语(「第一」「最强」等)不许照搬:排名写成带引用的"
        "具体名次(如「9 月销量榜第 1 名」),其余换成合规说法。\n"
        "   - 正文仍按段落序列展开,但每段优先讲与角度相关的事实,最后一段回扣角度给出结论。\n"
        "   - 角度里提到的事件/数字同样必须有事实支撑;事实清单里找不到的,只能定性地提,"
        "不许为了贴合角度编造。"
    )


def _portrait_policy(portrait: bool) -> str:
    """竖屏短视频的额外规则。横屏返回空串,SYSTEM 渲染结果与旧版逐字一致。"""
    if not portrait:
        return ""
    return (
        "\n- **竖屏短视频**:观众是在手机上刷到的,划走只要一秒。\n"
        "   - 第 1 段第一句就抛出冲突或结论(价格、反差、一个最硬的数字),不铺垫、不寒暄。\n"
        "   - 每段只讲一件事,句子更短;能用一个数字说清的不用一句形容词。\n"
        "   - 全片字数(Target total characters)是**硬上限**:选了 90 秒就是 90 秒,"
        "宁可少讲一个参数也不要超;每段只挑最硬的一两个数字。\n"
        "   - 不讲「下面我们来看」「接下来聊聊」这类过渡句,直接进内容。\n"
        "   - title 要能在手机上一眼看完:18 字以内,可以用冒号分成两半(画面上会拆成两行)。"
    )


def generate(
    *,
    subject: str,
    profile: CategoryProfile,
    facts: Sequence[Fact],
    plan: Sequence[str],
    duration: int,
    llm,
    theme=None,
    angle: str = "",
    slots: Sequence[Slot] | None = None,
    subjects: Sequence[dict[str, str]] | None = None,
    portrait: bool = False,
) -> tuple[list[ScriptSegment], str, str]:
    """生成解说脚本。返回 (段落列表, 标题, 主论点)。

    `angle`:选题角度(热点选题)。为空时 prompt 与无角度版本逐字一致。
    `slots` / `subjects`:多车横评的段落位(段落 + 车名)与车单。不传时 prompt 与单车版逐字一致。
    `portrait`:竖屏项目多一组短视频规则;横屏时 prompt 与旧版逐字一致。
    """
    roundup = bool(subjects)
    angle = (angle or "").strip()
    vocab = profile.vocab
    budget = narration_budget_chars(duration)
    shot_count = max(1, len(plan))
    scope_label = {"product": "product", "company": "company", "industry": "industry"}
    facts = usable_facts(facts)
    def _line(f: Fact) -> str:
        tags = scope_label.get(f.scope, "company")
        if f.topic == QUALITY_TOPIC:
            tags += " · topic=quality"
        if f.compare_with:
            tags += f" · 对比[{f.compare_with}]"
        return (f"- {f.id} [{tags}] | {f.claim} | "
                f"口径:{f.basis or '未注明'} | 来源:{f.source_kind or f.source_tool} | "
                f"可信度:{f.confidence}")

    if roundup:
        facts_payload = roundup_facts_payload(facts, subjects or (), _line)
    else:
        facts_payload = "\n".join(_line(f) for f in facts)
    facts_payload = facts_payload or "(无可用事实:请完全避免写出任何具体数字)"
    if roundup and slots:
        plan_lines = [f"{s.section}〔{s.subject}〕" if s.subject else s.section for s in slots]
    else:
        plan_lines = list(plan)
    principles_payload = "\n".join(
        f"- {p.id} [{' / '.join(p.terms)}] 原理:{p.explain}"
        + (f" 代价/边界:{p.tradeoff}" if p.tradeoff else "")
        for p in matched_principles(profile, facts)
    ) or "(无)"

    user = (
        f"Product subject: {subject}\n"
        f"Category: {profile.label}\n"
        f"Script style: {profile.script_style or '口语化,克制,有画面感'}\n"
        f"Total duration: {duration} seconds\n"
        f"Target total characters: {budget}\n"
        + (f"Topic angle: {angle}\n" if angle else "")
        + (f"车单:{roster_line(subjects or ())}\n" if roundup else "")
        + "\n"
        f"可用事实清单:\n{facts_payload}\n\n"
        f"段落序列:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_lines))
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
            principles=principles_payload,
            plan="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan_lines)),
            scope_line=(ROUNDUP_SCOPE_LINE if roundup else
                        f"只讲{vocab.subject_noun}/这个品牌本身,不评价其他无关产品。"),
            keywords=", ".join(profile.visual_keywords[:4]),
            theme_intent=f"   {theme.intent}" if theme is not None and theme.intent
            else "   按品类惯例组织叙事。",
            theme_rules=_theme_rules(theme, facts),
            scope_policy=_scope_policy(theme, vocab),
            title_policy=_title_policy(theme) if theme is not None else _TITLE_POLICY_PRODUCT,
            angle_policy=_angle_policy(angle) + _portrait_policy(portrait),
            subject=subject,
        ),
        user,
        required_keys=["segments", "title", "thesis"],
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
    thesis = str(data.get("thesis") or "").strip()
    return segments, title, thesis


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


#: "第一感觉""第一次""第一步"是叙述顺序,不是广告法说的排名
#: "第一类人""第一种情况""第一档"是列举,同样不是排名
_BENIGN_FIRST_RE = re.compile(r"第一(?=感|眼|印象|次|步|句|段|时间|反应|排|个问题|类|种|档|条|项)")

#: 竞品 / 对比类章节的识别词。段落名里出现这些字,这段就是在跟别人比。
_COMPARISON_SECTION_WORDS = ("竞品", "对比", "横评", "比较", "比一比")

#: 厂商口径的标志:把结论归给厂商,或归给厂商里的人。
#:
#: 为什么要拦:厂商自己做的"消费者选择调查"拿来证明本车比对手强,
#: 等于替厂商站台。实测见过"卢伟冰说,对比试驾后多数消费者选了YU7"被当横评论据 ——
#: 车评引用它,掉的是自己的可信度。这类话在**对比章节**里一律不算论据。
_VENDOR_CLAIM_MARKERS = (
    "官方表示", "官方称", "官方说", "官方宣称", "官方口径", "官方数据",
    "厂商表示", "厂商称", "厂商说", "厂家表示", "厂家称", "品牌方",
    "调研显示", "调研结果", "问卷显示", "调查显示", "消费者调研", "调研数据",
    "CEO", "总裁", "董事长", "高管", "创始人",
)

#: 人名 + 说话的引述句式:"卢伟冰说,……"。
#:
#: 动词后必须紧跟标点,否则「这类车说白了就是」会把"这类车"当人名。
_PERSON_QUOTE_RE = re.compile(r"([一-龥]{2,4})(说|表示|称|指出|坦言)\s*[，,：:]")

#: 不是人名的常见词,别当引述主体
_NOT_A_NAME = frozenset({
    "现在", "其实", "所以", "但是", "如果", "这个", "那个", "我们", "你们",
    "他们", "有人", "大家", "已经", "可以", "应该", "不能", "不会", "就是",
    "还是", "以及", "或者", "不过", "而且", "然后", "例如", "比如", "通常",
    "一般", "基本", "真的", "确实", "当然", "因此", "由于", "为了", "关于",
    "针对", "按照", "根据", "通过", "经过", "随着", "作为", "这么", "那么",
    "怎么", "为什么", "多少", "如何", "所谓", "别看", "别忘", "要知", "要说",
})


def _is_comparison_section(section: str) -> bool:
    """这一段是不是在跟别的东西比。"""
    return any(w in (section or "") for w in _COMPARISON_SECTION_WORDS)


def vendor_attribution(text: str) -> str:
    """文本里的厂商口径。返回命中的那截,没有则返回空串。"""
    hit = next((m for m in _VENDOR_CLAIM_MARKERS if m in text), None)
    if hit:
        return hit
    for m in _PERSON_QUOTE_RE.finditer(text or ""):
        if m.group(1) not in _NOT_A_NAME:
            return m.group(0).strip()
    return ""


#: 口径词之后多远内出现的数字算"这个口径下的数"(如 "CLTC续航643公里")
_BASIS_WINDOW = 40


#: 黄金三秒:第一句的字数上限(约 3 秒读完,按 5.8 字/秒)与禁用的开场套话
HOOK_MAX_CHARS = 18
#: 开场套话。命中即判定为"没抛钩子" —— 这些是自我介绍,不是钩子。
#:
#: 收得比直觉宽:模型实际写出来的是"朋友们好,今天聊小米YU7",只禁"今天来聊"会整条漏掉。
#: 宁可多拦一次让人改句,也不让"大家好"这种开场进成片。
HOOK_FILLER = ("大家好", "大伙好", "朋友们好", "朋友好", "各位好", "各位朋友",
               "今天来聊", "今天聊", "今天我们", "今天带大家", "今天给大家",
               "欢迎", "这期视频", "本期视频", "这期我们", "哈喽", "哈啰", "各位")


def first_sentence(text: str) -> str:
    """脚本的第一句(到第一个句末标点为止)。"""
    return re.split(r"[。!?!?;;\n]", (text or "").strip(), maxsplit=1)[0].strip()


#: 开场段的识别词。**只有第一段是开场时**才套用黄金三秒的规矩 ——
#: 「使用成本」「品牌历史」这类方向的第一段本来就不是钩子,套上去是误伤。
_HOOK_SECTION_WORDS = ("开场", "钩子", "hook", "intro")


def _is_hook_section(section: str) -> bool:
    """这一段是不是开场。段落名没给出来时,按开场处理(第一段默认就是开场)。"""
    s = (section or "").strip().lower()
    if not s:
        return True
    return any(w in s for w in _HOOK_SECTION_WORDS)


def hook_lint(segments: Sequence[ScriptSegment]) -> list[LintIssue]:
    """开场第一句太长或是套话 → **error**。

    这条以前只是 warning,结果模型照写不误(实测成片开场就是"朋友们好,今天聊…")。
    黄金三秒是脚本层唯一的硬性结构约束,必须在进分镜前拦住,而不是等人用眼睛找。

    只对**开场段**生效:成本、品牌这类方向的第一段本来就不是钩子。
    """
    if not segments:
        return []
    first_seg = segments[0]
    if not _is_hook_section(first_seg.section):
        return []
    first = first_sentence(first_seg.text)
    issues: list[LintIssue] = []
    # 字数按"念出来要多久"算:空格不占时间,数字一位算一个音
    spoken = len(re.sub(r"\s+", "", first))
    if spoken > HOOK_MAX_CHARS:
        issues.append(LintIssue(first_seg.idx, "error",
                                f"开场第一句 {spoken} 字,超过黄金三秒的 {HOOK_MAX_CHARS} 字:"
                                f"{first}(请直接抛结论或硬数字)"))
    for term in HOOK_FILLER:
        if term in first:
            issues.append(LintIssue(first_seg.idx, "error",
                                    f"开场第一句是套话「{term}」,应直接抛钩子:{first}"))
            break
    return issues


def depth_lint(
    segments: Sequence[ScriptSegment],
    profile: CategoryProfile,
    facts: Sequence[Fact],
) -> list[LintIssue]:
    """技术深度检查:空话(挡)、该讲原理的段落没讲(提醒)、数字没带口径(提醒)。

    合规 lint 管"不许错",这里管"不够专业":只念参数、用"很猛"代替论据,
    观众听完不知道这台车为什么好、好在哪、代价是什么。

    空洞评价是 **error**:profile 的 script_rules 把它列为硬规则
    ("不许用'很猛'这类空洞评价代替论据"),而且它是成片里最容易被观众听出来的
    一类偷懒 —— 模型写"这个数据很猛"是因为拿不出论据,与其放过去让人在门禁 A
    里逐条找,不如在这里直接拦住。其余两条是 warning。
    """
    vocab = profile.vocab
    principle_ids = {p.id for p in profile.principles}
    by_id = {f.id: f for f in facts}
    issues: list[LintIssue] = []

    for seg in segments:
        text = seg.text or ""
        hit = next((t for t in vocab.filler_terms if t in text), None)
        if hit:
            issues.append(LintIssue(seg.idx, "error",
                                    f"空洞评价「{hit}」:说清楚比什么、好在哪,或删掉:{text[:40]}"))
        if has_numeric_claim(text):
            said = numbers_in(text)
            missing = []
            for r in seg.fact_refs:
                claim = by_id[r].claim if r in by_id else ""
                for t in vocab.basis_terms:
                    pos = claim.find(t)
                    if pos < 0 or t in text:
                        continue
                    # 只有句子里的数字恰好是这个口径下的数,才需要说口径(讲价格不用说 CLTC)
                    if numbers_in(claim[pos:pos + len(t) + _BASIS_WINDOW]) & said:
                        missing.append(t)
            if missing:
                issues.append(LintIssue(seg.idx, "warning",
                                        f"数字没带口径:引用的事实是 {missing[0]} 口径,"
                                        f"句子里应说明(如「{missing[0]} 标称」):{text[:40]}"))

    # 无出处的技术名词:原理库认得这个词,但事实里根本没提 —— 多半是模型
    # 凭印象补的配置("空悬""四驱"),这台车未必有
    fact_text = "\n".join(f.claim for f in facts if f.enabled).lower()
    for seg in segments:
        low = (seg.text or "").lower()
        ghost = next((t for p in profile.principles for t in p.terms
                      if t.lower() in low and t.lower() not in fact_text), None)
        if ghost:
            issues.append(LintIssue(seg.idx, "warning",
                                    f"「{ghost}」不见于任何事实,这台车未必有这项配置:{seg.text[:40]}"))

    available = bool(matched_principles(profile, facts))
    for section in vocab.depth_sections:
        segs = [s for s in segments if s.section == section]
        if not segs or not available:
            continue
        if not any(r in principle_ids for s in segs for r in s.fact_refs):
            issues.append(LintIssue(segs[0].idx, "warning",
                                    f"「{section}」只列了参数,没有引用任何原理(k_)讲清为什么"
                                    "和代价 —— 技术深度不够"))
    return issues


#: 结论段的识别词。主论点要在这里落地。
_CONCLUSION_SECTION_WORDS = ("结论",)


def _is_conclusion_section(section: str) -> bool:
    return any(w in (section or "") for w in _CONCLUSION_SECTION_WORDS)


def thesis_lint(segments: Sequence[ScriptSegment], thesis: str) -> list[LintIssue]:
    """结论段有没有回扣主论点。

    主论点(全片那句判断)是视频的脊椎:每一段为它服务,最后一段把它落地。
    提示词要求结论段**原样包含**这句话,所以这里是精确匹配。
    """
    if not thesis.strip():
        return []
    ends = [s for s in segments if _is_conclusion_section(s.section)]
    if not ends:
        return []
    if any(thesis in (s.text or "") for s in ends):
        return []
    return [LintIssue(ends[-1].idx, "warning",
                     f"结论段没有回扣主论点「{thesis}」——"
                     f"全片每一段都为它服务,最后一段要把它落地"
                     f"(原样写进结论即可):{ends[-1].text[:40]}")]


#: 结尾要给行动:一句提问,或一句下期预告 / 互动引导
_ENDING_ACTION = ("?", "？", "评论区", "留言", "私信", "下期", "下一期", "下一集",
                  "想看", "关注", "扣1", "打在评论区")


def ending_lint(segments: Sequence[ScriptSegment]) -> list[LintIssue]:
    """结尾要给行动,不只给总结。

    复盘原话:「现在结尾停在『标准版闭眼入』,没有下一步。补一句提问
    (你在纠结哪台?评论区说车型,我下期做横评)或下期预告 —— 评论区互动和连看
    都靠这一句。」

    只报 warning:结尾怎么写是风格问题,不该拦住出片。
    """
    if not segments:
        return []
    last = segments[-1]
    text = last.text or ""
    if any(w in text for w in _ENDING_ACTION):
        return []
    return [LintIssue(last.idx, "warning",
                     "结尾只有总结,没有下一步:补一句提问(你在纠结哪款?评论区说)"
                     "或下期预告(下期做同价位横评)—— 评论区互动和连看都靠它")]


def duplicate_lint(
    segments: Sequence[ScriptSegment],
    facts: Sequence[Fact],
) -> list[LintIssue]:
    """重复检测:同一条事实被多个章节反复引用、同一个数字在多段重复出现。

    车评最常见的观感问题是"这段刚才不是讲过了吗"。实测典型:同一张价格表在
    「定位与价格」和「版本选择」各讲一遍,后者只是复读,没有新增信息。

    这里只报 warning:价格在「定位」和「版本选择」里各出现一次是合理的,
    真正的重复靠**跨章节引用同一条事实**来判定更准 —— 同一个章节里连续几段
    引用同一条事实是正常的(那一段的参数本来就出自它)。
    """
    issues: list[LintIssue] = []
    by_id = {f.id: f for f in facts}

    # 1. 同一条事实被几个不同章节引用
    seen: dict[str, list[int]] = {}
    for seg in segments:
        for ref in seg.fact_refs:
            seen.setdefault(ref, []).append(seg.idx)
    for ref, idxs in seen.items():
        secs = sorted({segments[i].section for i in idxs if i < len(segments)})
        if len(secs) >= 2:
            claim = by_id[ref].claim[:26] if ref in by_id else ref
            issues.append(LintIssue(idxs[0], "warning",
                                    f"同一条事实被「{'、'.join(secs)}」多个章节引用"
                                    f"({claim}):这几段是不是在重说同一件事?"))

    # 2. 同一个数字在多段重复出现。跳过个位数(3 块屏、3 米轴距不算重复)
    #    和年款年份(2026 款会出现在好几段)。
    hits: dict[str, list[int]] = {}
    for seg in segments:
        for n in numbers_in(seg.text or ""):
            if len(n) < 2 or _YEAR_RE.fullmatch(n):
                continue
            hits.setdefault(n, []).append(seg.idx)
    for n, idxs in hits.items():
        if len(idxs) >= 3:
            where = "、".join(str(i + 1) for i in idxs)
            issues.append(LintIssue(idxs[0], "warning",
                                    f"数字 {n} 在第 {where} 段反复出现,考虑删掉重复的一次"))
    return issues


def compliance_lint(
    segments: Sequence[ScriptSegment],
    profile: CategoryProfile,
    facts: Sequence[Fact],
    theme=None,
    thesis: str = "",
) -> list[LintIssue]:
    """机械校验脚本。error 级问题必须修复后才能进入分镜阶段。"""
    issues: list[LintIssue] = []
    known_ids = {f.id for f in facts}
    by_id = {f.id: f for f in facts}
    principle_ids = {p.id for p in profile.principles}
    forbidden = list(profile.forbidden_terms)
    if theme is not None:
        forbidden.extend(theme.extra_forbidden_terms)
    allow_critique = bool(getattr(theme, "allow_subject_critique", False))
    theme_roundup = bool(getattr(theme, "roundup", False))
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

        # 1. 数字必须有事实引用(年款年份除外)。原理条目不含数字,只引原理不算有出处
        fact_refs = [r for r in seg.fact_refs if r not in principle_ids]
        if has_numeric_claim(text) and not fact_refs:
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
            if ref not in known_ids and ref not in principle_ids:
                issues.append(
                    LintIssue(seg.idx, "error", f"引用了不存在的事实 ID:{ref}")
                )

        # 3. 品类禁用词 + 方向禁用词(广告法红线)
        text_ad = _BENIGN_FIRST_RE.sub("", text)
        for term in forbidden:
            if term in text_ad:
                issues.append(
                    LintIssue(seg.idx, "error", f"出现品类禁用词「{term}」:{text[:40]}...")
                )

        # 4. 通用绝对化用语(所有品类共享的红线)
        for term in AD_ABSOLUTE_TERMS:
            if term in text_ad:
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

        # 7. 多车横评:讲 A 车的段落不许引用 B 车的事实(参数张冠李戴)。
        #    开场 / 结论这类共用段(subject 为空)可以引用任何一台车。
        if seg.subject:
            others = sorted({f.subject for f in cited if f.subject and f.subject != seg.subject})
            if others:
                issues.append(
                    LintIssue(seg.idx, "error",
                              f"第 {seg.idx + 1} 段讲{seg.subject},却引用了"
                              f"{'、'.join(others)}的事实。请改成引用{seg.subject}自己的事实,"
                              "或把比较挪到开场 / 结论")
                )

        # 8. 对比章节不许拿厂商口径当论据。
        #    厂商自己做的消费者选择调查,拿来证明本车比对手好,是替厂商站台;
        #    车评引用它掉的是自己的可信度。改用可比参数(售价/续航/加速/配置)。
        if _is_comparison_section(seg.section):
            vendor = vendor_attribution(text)
            if vendor:
                issues.append(
                    LintIssue(
                        seg.idx,
                        "error",
                        f"对比章节拿厂商口径当论据「{vendor}」:{text[:40]}..."
                        "请换成可比参数(售价/续航/加速/配置),或删掉这句",
                    )
                )

        # 9. 对比事实(讲的是别家车的那条数字)只能出现在竞品对比段。
        #    拿到「版本选择」段里讲,就是把别家的参数安到本车头上。
        #    横评方向不管这事:那里每台车的事实本来就带车名,规则 7 已经管住了张冠李戴。
        if not theme_roundup and not _is_comparison_section(seg.section):
            for ref in seg.fact_refs:
                f = by_id.get(ref)
                if f is not None and f.compare_with:
                    issues.append(
                        LintIssue(
                            seg.idx,
                            "error",
                            f"这段引用了讲{f.compare_with}的对比事实({f.id}),"
                            "只能用在竞品对比段 —— 别家的数字不能出现在本车的段落里",
                        )
                    )
                    break

    issues.extend(hook_lint(segments))
    issues.extend(depth_lint(segments, profile, facts))
    issues.extend(duplicate_lint(segments, facts))
    issues.extend(thesis_lint(segments, thesis))
    issues.extend(ending_lint(segments))
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
    plan: Sequence[str] | Sequence[Slot],
) -> list[ScriptSegment]:
    """把段落数量对齐到分镜计划 —— 数量不一致会导致画面与旁白错位。

    `plan` 也可以是段落位(`Slot`):对齐后按位置写回每段讲哪台车 ——
    以段落位为准,不信 LLM 返回的车名。"""
    slots = [p if isinstance(p, Slot) else Slot(str(p)) for p in plan]
    plan = [s.section for s in slots]
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
        # LLM 偶尔整篇写中文数字:字幕全是汉字,lint 的引用检查也会被绕过
        seg.text = arabicize(seg.text)
        seg.subject = slots[i].subject if i < len(slots) else ""
    return out


#: 一拍最多多少字。按实测 TTS 语速 4.8 字/秒,28 字 ≈ 5.8 秒。
#:
#: 为什么在脚本层就拆:复盘要求「每 5–6 秒必须有一次画面变化」,而**一段脚本 = 一拍**
#: (`explainer.build_beats` 一段一版式)。拍太长,这一拍里就只剩一张画面撑着 ——
#: 实测成片 24 拍平均 8 秒一拍,后半程好几拍是同一张参数面板杵着不走。
SHOOT_CHARS_MAX = 28
#: 竖屏一拍的字数上限:约 4–5 秒换一次画面(竖屏刷得更快)
PORTRAIT_SHOOT_CHARS_MAX = 22
#: 拆出来的碎块太短就别单独成拍(2 个字的一拍纯属浪费画面)
MIN_PART_CHARS = 8
#: 句子边界(标点跟着前一句)
_SENT_END = "。!?!?;；\n"
#: 次级边界:逗号、顿号、冒号
_CLAUSE_END = "，,、：:"


def _split_keep(text: str, chars: str) -> list[str]:
    """按 `chars` 里的标点断句,标点留在前半句。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in chars:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


#: 拆完落在句尾的半个标点 —— 字幕末尾挂着逗号不像话,换成句号
_TRAILING_MARKS = "，,、；;：:"


def _split_text(text: str, max_chars: int) -> list[str]:
    """把一段话切成每块不超过 `max_chars` 字的几块,尽量断在句末标点上。"""
    parts: list[str] = []
    for sent in _split_keep(text, _SENT_END):
        if len(sent) <= max_chars:
            parts.append(sent)
            continue
        # 一句太长:再按逗号切
        for clause in _split_keep(sent, _CLAUSE_END):
            if len(clause) <= max_chars:
                parts.append(clause)
                continue
            # 连逗号都没有(纯数字长串):只能硬切,宁可切坏也不能留一个 30 秒的拍
            for k in range(0, len(clause), max_chars):
                parts.append(clause[k:k + max_chars])
    # 太短的碎块并到相邻块:一个 3 字的拍既撑不住画面也不像人话。
    # 先并再换标点 —— 并回去的逗号仍是逗号,不会冒出「我的判断是。YU7……」
    merged: list[str] = []
    for p in parts:
        if merged and (len(p) < MIN_PART_CHARS or len(merged[-1]) < MIN_PART_CHARS) \
                and len(merged[-1]) + len(p) <= max_chars:
            merged[-1] += p
            continue
        merged.append(p)
    if len(merged) > 1 and len(merged[-1]) < MIN_PART_CHARS and \
            len(merged[-2]) + len(merged[-1]) <= max_chars:
        merged[-2] += merged[-1]
        merged.pop()
    # 切在逗号上的块,句尾那个逗号换成句号:字幕末尾挂半个标点不像话,
    # TTS 也需要一个明确的断句。
    return [(p[:-1] + "。" if p and p[-1] in _TRAILING_MARKS else p) for p in merged]


def split_segments(segments: Sequence[ScriptSegment],
                   max_chars: int = SHOOT_CHARS_MAX) -> list[ScriptSegment]:
    """把过长的段落拆成句子级的小段 —— 一段 = 一拍,拍太长就没有画面变化。

    拆在句末标点;一句太长再拆到逗号。拆出来的每一段保留原来的段落名、
    车名和事实引用(引用跟着数字走,拆开也不丢出处)。

    开场段也拆:复盘要的「0–3 秒先给冲突、3–10 秒立刻兑现承诺」正好对应
    开场的第一句和第二句,拆开这两句才分得开。拆完第一段仍然是钩子段
    (`hook_lint` 只看第一段,黄金三秒的规矩不变)。
    """
    out: list[ScriptSegment] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        parts = _split_text(text, max_chars) if len(text) > max_chars else [text]
        for p in parts:
            out.append(ScriptSegment(idx=len(out), section=seg.section, text=p,
                                     fact_refs=list(seg.fact_refs), subject=seg.subject))
    return out
