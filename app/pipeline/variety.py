"""每个项目的变体:开头怎么起、结尾怎么收、中段按什么顺序讲。

同一台车连做几条,片型(theme)之外,开头和结尾的写法也不该永远是「冲突 → 兑现 → 出处」
+「评论区说」。建项目时一次性挑好写进项目(`projects.variant`),之后脚本提示词、段落
铺展和 lint 都按它走:

- `opening` / `ending`:从片型允许的集合里挑,避开同品类最近几条用过的;
- `order_seed`:片型里标了 `movable` 的相邻段落按它重排(见 `CategoryProfile.order_blocks`);
- `seed`:留给画面节奏(转场、镜头运动)用。

挑选用项目 ID 派生的种子,同一个项目重算结果不变。没有 variant 的老项目 = 默认变体
(片型列表里的第一个开头 / 结尾),提示词与以前逐字相同。
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

#: 没声明 openings / endings 的片型用这一对:就是变体出现之前写死在提示词里的那套
DEFAULT_OPENING = "conflict"
DEFAULT_ENDING = "action"

#: 避开最近几条项目用过的开头 / 结尾
RECENT_WINDOW = 4


@dataclass(frozen=True)
class Opening:
    id: str
    label: str
    #: 提示词第 10 条标题的前半句
    title: str
    #: 第 10 条里的开头要求(每行以三个空格缩进,接在标题下面)
    rule: str
    #: 开场要不要是立场(stance_lint)。问句、场景冷开本来就不是判断句
    stance: bool = True


@dataclass(frozen=True)
class Ending:
    id: str
    label: str
    title: str
    rule: str
    #: 结尾 lint:每组至少命中一个词。空 = 不查(判决式结尾的检查由 thesis_lint 管)
    markers: tuple[tuple[str, ...], ...] = ()
    #: markers 没命中时的提示
    hint: str = ""


#: 改写非默认变体时补的一句:段落说明里对开场 / 结尾的描述是按默认写法写的
_OVERRIDE = "\n     这一条优先于段落序列里对开场 / 结尾段的说明。"

OPENINGS: dict[str, Opening] = {o.id: o for o in (
    Opening(
        "conflict", "冲突三段式", "开头三段式",
        "   - 开头三段分开写,各司其职:① **先给冲突** —— 一句带判断的反常识话\n"
        "     (「看起来更贵的那台,反而更划算」这种),不是「XX 值不值得买」这种平的问题;\n"
        "     ② **立刻兑现** —— 把上一段抛出的那个数字或判断摆出来,不许绕;\n"
        "     ③ **建立可信度** —— 一句话说明每个数字都能查到出处,全片只说这一次。"),
    Opening(
        "question", "问题开场", "开头抛问题",
        "   - 第一句就是一个观众心里真实存在、自己也会去搜的问题(「冬天它到底能跑多远?」),\n"
        "     18 字以内,不许是「XX 值不值得买」这种空泛的问题;\n"
        "     第二句立刻给出答案或答案的方向,不许把答案拖到后面。" + _OVERRIDE,
        stance=False),
    Opening(
        "scene", "场景冷开", "开头用场景冷开",
        "   - 第一句是一个具体的用车画面:时间、地点、人在干什么(「周五晚上,服务区充电桩前排着队」),\n"
        "     不讲参数、不报车名以外的名词;场景里出现的数字同样要有事实支撑;\n"
        "     第二句把这个场景接到本期的判断上,让观众知道这期要回答什么。" + _OVERRIDE,
        stance=False),
    Opening(
        "myth", "破除说法", "开头先破一个说法",
        "   - 第一句复述一个流行说法(「都说增程是过渡技术」),只能说「网上 / 很多人 / 不少车主」,\n"
        "     不许编造具体是谁说的;第二句就用事实推翻或修正它,并引用那条事实。" + _OVERRIDE),
    Opening(
        "number", "硬数字开场", "开头砸一个硬数字",
        "   - 第一句就是全片最硬的那一个数字加一句判断(「10 分钟补 400 公里,才是它最贵的地方」),\n"
        "     这个数字必须引用事实;第二句说清这个数字对谁意味着什么。" + _OVERRIDE),
)}

#: 默认结尾的标记词:一句提问,或一句下期预告 / 互动引导
_ACTION_MARKERS = ("?", "？", "评论区", "留言", "私信", "下期", "下一期", "下一集",
                   "想看", "关注", "扣1", "打在评论区")

ENDINGS: dict[str, Ending] = {e.id: e for e in (
    Ending(
        "action", "提问或预告", "结尾给行动",
        "   - 结尾要给**行动**,不只给总结:最后一段必须有一句提问(「你在纠结哪款?评论区说」)\n"
        "     或一句下期预告(「下期做同价位横评」),让人知道下一步做什么。",
        markers=(_ACTION_MARKERS,),
        hint="结尾只有总结,没有下一步:补一句提问(你在纠结哪款?评论区说)"
             "或下期预告(下期做同价位横评)—— 评论区互动和连看都靠它"),
    Ending(
        "pick_side", "二选一", "结尾让观众站队",
        "   - 最后一句抛一个二选一给观众(「你选 A 还是 B?评论区说」):两个选项必须是片子里讲过的\n"
        "     两个版本 / 两台车 / 两种用法,不许出现片子里没讲过的东西。" + _OVERRIDE,
        markers=(("还是", "选哪", "选 A", "哪一个", "哪个"), ("?", "？", "评论区")),
        hint="结尾要抛一个二选一(你选 A 还是 B?评论区说),两个选项都得是片子里讲过的"),
    Ending(
        "next_episode", "下期预告", "结尾预告下期",
        "   - 最后一句绑定一个具体的下期(「下期把它和 XX 放一起,只比冬天续航」):下期内容要从本期\n"
        "     自然延伸出来、真的做得出来,不许只喊「关注我」。" + _OVERRIDE,
        markers=(("下期", "下一期", "下一集", "下次"),),
        hint="结尾要预告一个具体的下期(下期把它和 XX 放一起比 XX)"),
    Ending(
        "verdict", "一句判决", "结尾一句话判决",
        "   - 最后一句就是主论点原文,前面最多一句铺垫;不加提问、不加预告,干净收住。" + _OVERRIDE),
    Ending(
        "decision_card", "决策卡", "结尾给决策卡",
        "   - 结尾是一张能截图带走的决策卡:3 个最关键的数字(都要引用事实)、2 类适合的人、\n"
        "     1 个下单时机或等待理由(只能来自事实;没有就给不含时间的判断),一句一项,短句。" + _OVERRIDE,
        markers=(("适合", "如果你", "推荐"),),
        hint="决策卡结尾要点出适合哪两类人(适合……的人)"),
)}


def opening(variant: Mapping[str, Any] | None, theme=None) -> Opening:
    key = (variant or {}).get("opening") or default_opening(theme)
    return OPENINGS.get(str(key), OPENINGS[DEFAULT_OPENING])


def ending(variant: Mapping[str, Any] | None, theme=None) -> Ending:
    key = (variant or {}).get("ending") or default_ending(theme)
    return ENDINGS.get(str(key), ENDINGS[DEFAULT_ENDING])


def default_opening(theme=None) -> str:
    allowed = tuple(getattr(theme, "openings", ()) or ())
    return allowed[0] if allowed else DEFAULT_OPENING


def default_ending(theme=None) -> str:
    allowed = tuple(getattr(theme, "endings", ()) or ())
    return allowed[0] if allowed else DEFAULT_ENDING


def allowed_openings(theme) -> tuple[str, ...]:
    return tuple(getattr(theme, "openings", ()) or ()) or (DEFAULT_OPENING,)


def allowed_endings(theme) -> tuple[str, ...]:
    return tuple(getattr(theme, "endings", ()) or ()) or (DEFAULT_ENDING,)


def rule10(variant: Mapping[str, Any] | None, theme=None) -> str:
    """脚本提示词第 10 条:标题 + 开头要求 + 结尾要求。默认变体与旧版逐字相同。"""
    o, e = opening(variant, theme), ending(variant, theme)
    return f"10. **{o.title},{e.title}**:\n{o.rule}\n{e.rule}"


def parse(raw: Any) -> dict[str, Any]:
    """项目行里的 variant 列 → dict。空 / 坏数据 = {}(默认变体)。"""
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _rng(project_id: str) -> random.Random:
    digest = hashlib.sha256(f"variety:{project_id}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _choose(rng: random.Random, allowed: tuple[str, ...], used: Iterable[str]) -> str:
    """在允许集合里挑一个,优先挑最近没用过的;全用过就挑最久没用的那些里的一个。"""
    recent = list(used)
    fresh = [x for x in allowed if x not in recent]
    if fresh:
        return rng.choice(fresh)
    # 全用过:离现在最远的那个(recent 越靠前越新)
    return max(allowed, key=lambda x: recent.index(x))


def pick(theme, project_id: str, recent: Iterable[Mapping[str, Any]] = (),
         opening: str | None = None, ending: str | None = None) -> dict[str, Any]:
    """给新项目挑变体。`recent` = 同品类最近的项目变体(新的在前)。

    用户在建项目时指定了开头 / 结尾就用指定的(须在片型允许的集合里)。"""
    rng = _rng(project_id)
    recent = [parse(v) for v in recent][:RECENT_WINDOW]
    openings, endings = allowed_openings(theme), allowed_endings(theme)
    if opening and opening not in openings:
        raise ValueError(f"这个片型的开头可选:{'、'.join(OPENINGS[o].label for o in openings)}")
    if ending and ending not in endings:
        raise ValueError(f"这个片型的结尾可选:{'、'.join(ENDINGS[e].label for e in endings)}")
    chosen_opening = opening or _choose(rng, openings, (v.get("opening") for v in recent))
    chosen_ending = ending or _choose(rng, endings, (v.get("ending") for v in recent))
    return {
        "opening": chosen_opening,
        "ending": chosen_ending,
        "order_seed": rng.randrange(1, 1 << 30),
        "seed": rng.randrange(1, 1 << 30),
    }


def options(theme) -> dict[str, list[dict[str, str]]]:
    """建项目页的下拉选项。"""
    return {
        "openings": [{"id": o, "label": OPENINGS[o].label} for o in allowed_openings(theme)],
        "endings": [{"id": e, "label": ENDINGS[e].label} for e in allowed_endings(theme)],
    }


def describe(variant: Mapping[str, Any] | None, theme=None) -> str:
    """项目页上的一句话:「开头:场景冷开 · 结尾:决策卡」。"""
    return f"开头:{opening(variant, theme).label} · 结尾:{ending(variant, theme).label}"
