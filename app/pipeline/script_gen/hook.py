"""开场第一句:候选评审用的提示词与开场 lint。"""
from __future__ import annotations

import re
from typing import Sequence
from ...categories import CategoryProfile
from ...models import ScriptSegment
from .. import variety

from .lint_base import LintIssue, forbidden_in, has_numeric_claim


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


HOOK_SYSTEM = """你是中文短视频的开场编导。给「{subject}」这期解说写 {n} 版**第一句话**,
每版写法要明显不同(反常识 / 有人会反对的立场 / 最硬的一个结论或数字 / 买车人的真实场景冲突),
不要只换几个字。

要求:
- 每句 {max_chars} 字以内,口语,直接给判断;不许用"大家好""今天来聊聊""这期视频"这类开场白;
- 句子里的数字只能来自下面的事实清单,并把事实 ID 写进 fact_refs;没把握就写不含数字的判断;
- 禁止绝对化用语(最强、第一、顶级、唯一)。
{extra}
可用事实清单:
{facts}

只输出 JSON:{{"candidates": [{{"text": "第一句", "fact_refs": ["事实ID"]}}]}}"""

JUDGE_SYSTEM = """你是短视频开场评审。只看第一句话能不能让刷到的人停下来、想听下去,按下面标准给每版打 1-10 分:
- 前 3 秒有没有抛出判断、反差或冲突(而不是告诉观众这期讲什么);
- 普通买车人一听就懂,不靠术语;
- 不夸大、不像广告。
{extra}
只输出 JSON:{{"scores": [{{"i": 序号(从 0 开始), "score": 分数, "why": "一句理由"}}]}}"""


def _hook_ok(text: str, refs: Sequence[str], profile: CategoryProfile, fact_ids: set[str]) -> bool:
    if not text or len(text) > HOOK_MAX_CHARS + 4 or any(f in text for f in HOOK_FILLER):
        return False
    if forbidden_in(text, profile):
        return False
    # 带数字的开场必须指得出事实:否则全片第一句就是编的
    return not has_numeric_claim(text) or any(r in fact_ids for r in refs)


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


#: 立场的痕迹:取舍、转折、对比、站队。钩子一个都没有,多半是"选题说明"而不是观点
_STANCE_WORDS = ("但", "却", "反而", "其实", "不是", "只有", "才", "多花", "少花",
                 "贵", "省", "亏", "值", "代价", "换来", "比", "输", "赢", "不如", "宁可",
                 "与其", "错", "坑", "真正", "偏偏", "凭什么")


def stance_lint(segments: Sequence[ScriptSegment],
                opening: variety.Opening | None = None) -> list[LintIssue]:
    """开场是不是一个立场(warning)。

    车评的钩子是立场 + 悬念 + 能被反驳:「别急着订车,先搞清楚买哪版」是选题说明,
    观众知道了讲什么,但没有理由留下。这里只看有没有取舍 / 转折 / 对比的痕迹,判据粗,只报 warning。
    """
    if not segments or not _is_hook_section(segments[0].section):
        return []
    if opening is not None and not opening.stance:
        # 问题开场 / 场景冷开:第一句本来就不是判断句
        return []
    text = segments[0].text or ""
    if any(w in text for w in _STANCE_WORDS):
        return []
    return [LintIssue(segments[0].idx, "warning",
                      "开场像选题说明,不像立场:给一个有取舍、有人会不同意的判断"
                      f"(「多花的那几万买的不是配置,是省心」这种):{first_sentence(text)}")]
