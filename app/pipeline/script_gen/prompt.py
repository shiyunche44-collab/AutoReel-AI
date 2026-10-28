"""生成脚本用的提示词模板与事实整理。"""
from __future__ import annotations

from typing import Any, Mapping, Sequence
from ...categories import CategoryProfile, Vocab
from ...models import Fact
from .. import variety


SYSTEM = """你是中文{domain}(/{category})品类的短视频解说编导。

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或任何解释文字。
2. `segments` 数组,每项: {{"section": 段落名, "text": 解说词, "fact_refs": ["事实ID"]}}
   - segments 的数量与顺序必须严格对应给定的段落序列。
   - text 为口播文案,口语化、短句、有画面感,不要用书面语长句。
   - 数值一律写阿拉伯数字(如 26.35 万、593 公里、4.3 秒),字幕直接显示它,配音会自动转成读法。
   - 清单里标了「对比[某车]」的事实只许写在竞品对比段;其他段落既不要引用它,也不要转述它的数字。
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
   - 部件名、技术名照抄事实原文,不许把两个部件并成一个词。
   - 来源 derived 的数字是推算的,句子里要带「约 / 粗算」;事实写了测试条件或区间,就一起说出来。
   - 标了「硬料」的事实是内行观众最想听的,开场或卖点段至少用一条。
   - {scope_line}
6. `title`:8-16 字的视频标题,要有吸引力但不得夸大。
{title_policy}
7. 字数:全片约 {chars} 字,平均每镜约 {per_shot} 字。
8. **黄金三秒**:第 1 段的第一句话必须在 18 字以内(约 3 秒读完),直接抛出一个反常识、
   争议或最硬的一个结论/数字,让人想听下去。不许用"大家好""今天来聊聊""欢迎""这期视频"
   这类开场白,不许先自我介绍或交代背景。{angle_policy}
   钩子要是**立场**,不是选题:「先搞清楚{subject}买哪版」只是告诉观众这期讲什么;
   「多花的那几万,买的不是配置,是省心」才是钩子 —— 有取舍、有人会不同意。
9. **主论点** `thesis`:先定一句话,再照着它写全片。
   - 不超过 30 字,是一个**判断**而不是话题(「{subject}适合既要又要的人」而不是「{subject}的配置」);
   - 全片每一段都要为它服务:每段开头能接住它,最后一段必须**原样包含这句话**;
   - 不要写成广告语(「闭眼入」「太香了」这类),要写成有取舍的判断。
{rule10}
11. **每个数字后面跟一句"所以呢"**:参数 → 对观众意味着什么 → 对谁有用。
   - 「宽 1996 毫米」不够;要接一句它对谁意味着什么(老小区窄车位、地库拐弯要先量一量)。
   - 含义只能来自三种:换算成日常场景(多久、多远、够几次)、跟同级或另一个版本比、
     点出适合或不适合哪类人。不许凭空臆断体验和缺点,换算里的数字同样要有事实支撑。
12. **口播要像人说话**:
   - 每句话必须完整,不要写「是不折腾。」「能确定的是。」这种半截话;
   - 不要在片子里告诉观众你缺数据(「我没有可靠数字」「不乱说」):没依据的内容整段不写,换成有事实的;
   - 「不是X,是Y」「代价是」「这是取舍」这类句式全片各不超过 3 次,换着说。

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


#: 争议类事实的主题标签,与 intake.CONTROVERSY_TOPIC 一致
CONTROVERSY_TOPIC = "争议"

#: heading 段落最多几个字(章节条上要放得下)
HEADING_MAX_CHARS = 12


def _heading_rule(profile: CategoryProfile | None, plan: Sequence[str]) -> str:
    """段落序列里有要脚本给章节名的段落时,告诉模型多输出一个 heading 键。"""
    if profile is None:
        return ""
    names = list(dict.fromkeys(s for s in plan if profile.section(s).heading))
    if not names:
        return ""
    return (f"segments 里段落「{'、'.join(names)}」的每一项多给一个 `heading` 键:"
            f"这一章的标题({HEADING_MAX_CHARS} 字以内),会显示在章节条和章节转场上。")


def _hook_rule(hook: str, locked: bool = False) -> str:
    """热点选题带来的开场钩子:给模型当第一句的参考,不强制照抄。

    `locked`:这句是写了几版、评审挑出来的(`choose_hook`),第一句就用它。"""
    hook = (hook or "").strip()
    if not hook:
        return ""
    if locked:
        return (f"第 1 段第一句用这句(几版开场里评审挑出来的):「{hook}」。"
                "可以顺口微调措辞,但别换意思;后面紧接着给出支撑它的证据。")
    return (f"选题策划给的开场钩子:「{hook}」。第 1 段第一句可以直接用它,也可以写得更好;"
            "但它里面的数字同样要有事实支撑,找不到就改成不含数字的说法。")


def _theme_rules(theme, facts: Sequence[Fact], extra: Sequence[str] = (),
                 variant: Mapping[str, Any] | None = None) -> str:
    rules = list(theme.rules_for(variety.opening(variant, theme).id,
                                 variety.ending(variant, theme).id)) if theme is not None else []
    if any(f.topic == QUALITY_TOPIC and f.enabled for f in facts):
        rules.append(_QUALITY_RULE)
    rules.extend(r for r in extra if r)
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
    """进脚本的事实:去掉被剔除的(门禁 A 取消勾选、旧年款)和推算源已被剔除的推算事实。"""
    enabled = {f.id for f in facts if f.enabled}
    return [f for f in facts if f.enabled
            and not (f.source_kind == "derived" and f.source_query and f.source_query not in enabled)]


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
