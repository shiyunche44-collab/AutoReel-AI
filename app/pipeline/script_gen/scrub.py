"""清掉越界引用:非对比段的对比事实、厂商口径。"""
from __future__ import annotations

import re
from typing import Sequence
from ...models import Fact, ScriptSegment


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
_COMPARISON_SECTION_WORDS = ("竞品", "对比", "横评", "比较", "比一比", "对决")

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


def strip_foreign_comparison_refs(
    segments: list[ScriptSegment], facts: Sequence[Fact],
) -> list[ScriptSegment]:
    """去掉非对比段里引用的对比事实。

    LLM 常把讲别家车的那条事实(「极氪 7X 800V」)挂在开场、三电性能这些本车段落上,
    再被 split_segments 摊到每一拍,lint 规则 9 就一口气报几十条。
    源头剥掉:引用本来就不该出现在这里。剥完若句里还带数字,会被「数字没有事实引用」拦下,
    交给人在门禁 A 处理,而不是静默放行。
    """
    rivals = {f.id for f in facts if f.compare_with}
    if not rivals:
        return segments
    for seg in segments:
        if not _is_comparison_section(seg.section):
            seg.fact_refs = [r for r in seg.fact_refs if r not in rivals]
    return segments


def vendor_attribution(text: str) -> str:
    """文本里的厂商口径。返回命中的那截,没有则返回空串。"""
    hit = next((m for m in _VENDOR_CLAIM_MARKERS if m in text), None)
    if hit:
        return hit
    for m in _PERSON_QUOTE_RE.finditer(text or ""):
        if m.group(1) not in _NOT_A_NAME:
            return m.group(0).strip()
    return ""
