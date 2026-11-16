"""发布包装:3 个标题 + 置顶评论 + 备用回复的校验与兜底(纯函数,不碰网络/数据库)。

LLM 只负责出主意;**能不能用由这里的代码说了算** —— 标题里的数字必须能在事实层找到,
命中广告法用语 / 搜索词式标题的直接丢弃,凑不够 3 条就走确定性模板。
这样即使 LLM 挂了或胡编,也永远有一套合规的包装可选。

三个标题固定三种写法(`KINDS`),不是同义改写:只有写法不同,发出去之后才能拿数据比较。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field

from . import grounding
from .script_gen import _BENIGN_FIRST_RE, AD_ABSOLUTE_TERMS

#: 标题字数上限。最短的平台上限是小红书的 20 字;标题要带车型名,不再留 2 字余量
TITLE_MAX_CHARS = 30
#: 置顶评论总长上限;选项各自的上限
COMMENT_MAX_CHARS = 40
OPTION_MAX_CHARS = 8
#: 备用回复上限
REPLY_MAX_CHARS = 60
REPLY_COUNT = 3

#: 三种写法:立场/反问、数字+结论、人群/场景
KIND_STAKE, KIND_NUMBER, KIND_PERSON = "stake", "number", "person"
KINDS = (KIND_STAKE, KIND_NUMBER, KIND_PERSON)
KIND_LABEL = {KIND_STAKE: "立场", KIND_NUMBER: "数字", KIND_PERSON: "人群"}

#: 空泛的文章式标题词。「XX 值不值得买 / 怎么选」是观众真会搜的说法,带上车型名反而吃搜索流量,
#: 所以不再拦(2026-11 数据:标题不带车型名的几条几乎只有推荐流量)
TITLE_AVOID = ("全面解析", "深度对比", "深度解析", "一文看懂", "全方位")

#: 通用的备用回复。不含任何数字和判断,作为 LLM 回复全被丢弃时的兜底
GENERIC_REPLIES = (
    "数据都是官方口径,片里标了出处,有出入欢迎在评论区指出来。",
    "每个人的用车场景不一样,这条只讲参数和取舍,最后怎么选看你自己。",
    "说得对的地方记下了,下一期会把大家问得最多的点展开讲。",
)

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
#: 型号代号(YU7、X5、Model3)里的数字是名字不是参数
_MODEL_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,5}\s?\d{1,2}(?![\d.])[A-Za-z]*")
_QUESTION_RE = re.compile(r"[?？]")


class TitleOption(BaseModel):
    text: str
    kind: str
    note: str = ""


class PinnedComment(BaseModel):
    text: str = ""
    options: list[str] = Field(default_factory=list)


class Packaging(BaseModel):
    """一条片的发布包装。落盘为 `packaging.json`。"""

    titles: list[TitleOption] = Field(default_factory=list)
    comment: PinnedComment = Field(default_factory=PinnedComment)
    replies: list[str] = Field(default_factory=list)
    #: 用户的选择:title 是 titles 的下标;cover 是 "a" / "b"
    picked: dict[str, Any] = Field(default_factory=lambda: {"title": 0, "cover": "a"})
    #: "llm":LLM 产出且有合格项;"fallback":全部来自模板
    source: str = "llm"
    made_at: float = 0.0


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def spoken_len(text: str) -> int:
    """字数:不算空白。"""
    return len(re.sub(r"\s+", "", str(text or "")))


def forbidden_hits(text: str, terms: Iterable[str] = ()) -> list[str]:
    """命中的禁用词(广告法绝对化用语 + 调用方给的品类禁用词)。「第一眼」这类不算。"""
    plain = _BENIGN_FIRST_RE.sub("", text or "")
    pool = dict.fromkeys((*terms, *AD_ABSOLUTE_TERMS))
    return [t for t in pool if t and t in plain]


#: 口语里带数字但不是事实的惯用语(「评论区投一票」「一起聊聊」),不该要求事实支撑
_IDIOM_RE = re.compile(r"[投来]?一票|一起|一下|一直|一样|一般|一定|一边|一旦")


def figures_in(text: str) -> set[str]:
    """需要事实支撑的数字:剥掉年款和型号代号,小数点去掉(与 `grounding.numbers_in` 同口径)。"""
    plain = _IDIOM_RE.sub("", text or "")
    stripped = _MODEL_CODE_RE.sub("", _YEAR_RE.sub("", grounding.arabicize(plain)))
    return grounding.numbers_in(stripped)


#: 标题里点名人群的说法(对标号的标题没有 kind,靠这个猜「人群」写法)
_PERSON_RE = re.compile(r"家庭|宝妈|奶爸|通勤|年轻人|新手|上班族|打工人|二胎|一家|女生|男生|老人"
                        r"|适合谁|适合.{0,4}的人|谁该买|谁别买")


def guess_kind(title: str) -> str:
    """别人的标题归到我们的三种写法:带事实数字 → 数字;点名人群 → 人群;其余算立场。"""
    t = re.sub(r"#\S+|@\S+", "", title or "")
    if figures_in(t):
        return KIND_NUMBER
    if _PERSON_RE.search(t):
        return KIND_PERSON
    return KIND_STAKE


def allowed_figures(fact_claims: Iterable[str] = (), script_texts: Iterable[str] = (),
                    extra: Iterable[str] = ()) -> set[str]:
    """标题 / 评论里允许出现的数字:事实层 + 脚本 + 额外(如封面入门价)。"""
    out: set[str] = set()
    for text in (*fact_claims, *script_texts, *extra):
        out |= grounding.numbers_in(text or "")
    return out


_CJK = "一-鿿"


def subject_names(subject: str) -> set[str]:
    """车型的几种叫法(都小写、去空白):全名、各个词及相邻两词(宋Ultra)、型号段(i6 / YU7 / N8)、
    车系尾巴(海鸥 / 海豹07 / 3系)。"""
    s = re.sub(r"\s+", "", subject or "").lower()
    if not s:
        return set()
    names = {s}
    words = [w.lower() for w in (subject or "").split()]
    names |= {w for w in words if len(w) >= 2}
    names |= {a + b for a, b in zip(words, words[1:])}
    names |= {c for c in re.findall(r"[a-z]*\d+[a-z0-9]*|[a-z]{2,}\d*", s)
              if len(c) >= 2 and not c.isdigit()}
    tail = re.search(rf"[{_CJK}]{{2}}[a-z0-9]*$|[a-z0-9]+[{_CJK}]$", s)
    if tail and tail.group(0) != s:
        names.add(tail.group(0))
    return names


def mentions_subject(text: str, subject: str) -> bool:
    t = re.sub(r"\s+", "", text or "").lower()
    names = subject_names(subject)
    return not names or any(n in t for n in names)


def _lead_name(text: str, subject: str) -> str:
    """标题开头用的那种叫法(小写、去空白,取最长的);不是以车型名开头返回空串。"""
    t = re.sub(r"\s+", "", text or "").lower()
    return max((n for n in subject_names(subject) if t.startswith(n)), key=len, default="")


def leads_with_subject(text: str, subject: str) -> bool:
    """标题以车型名(某种叫法)开头。搜索流量靠车型名,越靠前越容易被搜到。"""
    return not subject_names(subject) or bool(_lead_name(text, subject))


def lead_full_name(text: str, subject: str) -> str:
    """开头是简称(「荣放」「H10」)就换成完整车名(「丰田RAV4荣放」);换完超长或不是简称开头就原样返回。"""
    text = str(text or "").strip()
    full = (subject or "").strip()
    lead = _lead_name(text, full)
    if not lead or lead == re.sub(r"\s+", "", full).lower():
        return text
    # 把开头那段简称(按去空白后的字数)在原文里定位出来
    taken, cut = 0, 0
    while cut < len(text) and taken < len(lead):
        if not text[cut].isspace():
            taken += 1
        cut += 1
    rest = text[cut:]
    out = full + rest
    return out if spoken_len(out) <= TITLE_MAX_CHARS else text


def misattributed(text: str, owners: Mapping[str, set[str]] | None) -> list[str]:
    """横评标题点了某几台车、却用了只属于另一台车的数字(「元UP 冬季实测约350公里」,350 是海豚的)。

    `owners`:车型名 → 这台车的事实 / 旁白里的数字;键 "" 是不属于哪台车的共用数字。
    标题没点名任何一台车时不查(按价位段 / 人群写的标题)。"""
    if not owners:
        return []
    named = [s for s in owners if s and mentions_subject(text, s)]
    if not named:
        return []
    ok = set(owners.get("", set()))
    for s in named:
        ok |= owners[s]
    out: list[str] = []
    for fig in sorted(figures_in(text) - ok):
        others = [s for s in owners if s and s not in named and fig in owners[s]]
        if others:
            out.append(f"{fig}(是{'、'.join(others)}的)")
    return out


#: 疑似车型名的词:字母+数字(YU7 / H10)或 汉字+数字(钛7;取最后一个汉字,够用即可)。
_UNIT_AFTER = "公万元年月日期号个人座辆台米秒分时天度%千克升排项条次倍成岁档款代"
_MODEL_TOKEN = re.compile(rf"[A-Za-z]+\d+[A-Za-z0-9]*|[{_CJK}]\d+[A-Za-z]?(?![\d.{_UNIT_AFTER}])")
#: 这些字后面跟数字是量词 / 序数(第2期、近3年),不是车型名
_NOT_MODEL_LEAD = set("第近前后约超共每仅只含满上下去今这那")


def unknown_models(text: str, known: str) -> list[str]:
    """标题里出现、但车型名 / 脚本 / 事实里都没有的疑似车型名(单车片标题凭空拉进一个对手车型)。"""
    pool = re.sub(r"\s+", "", known or "").lower()
    out: list[str] = []
    for m in _MODEL_TOKEN.finditer(re.sub(r"\s+", "", text or "")):
        tok = m.group()
        if tok[0] in _NOT_MODEL_LEAD and not tok[0].isascii() or tok.lower() in pool:
            continue
        out.append(tok)
    return out


def check_title(text: str, allowed: set[str], terms: Iterable[str] = (),
                avoid: Sequence[str] = TITLE_AVOID, name: str = "", known: str = "",
                owners: Mapping[str, set[str]] | None = None,
                spoken: set[str] | None = None) -> list[str]:
    """标题的问题清单,空 = 合格。

    `name` = 单车片的车型名:给了就要求标题以它的某种叫法开头(搜索流量靠车型名);横评不传。
    `owners` = 横评每台车自己的数字(见 `misattributed`);单车片不传。
    `spoken` = 旁白里说过的数字(加入门价):给了就要求标题的数字片里真说过 ——
    事实层里有、片里没讲的数字放进标题,观众点进来找不到(2026-11 元UP「冬季约350公里」)。"""
    text = str(text or "").strip()
    if not text:
        return ["空标题"]
    problems: list[str] = []
    n = spoken_len(text)
    if n > TITLE_MAX_CHARS:
        problems.append(f"{n} 字,超过 {TITLE_MAX_CHARS} 字")
    if n < 4:
        problems.append("太短")
    hits = forbidden_hits(text, terms)
    if hits:
        problems.append("命中禁用词:" + "、".join(hits))
    stale = [w for w in avoid if w in text]
    if stale:
        problems.append("搜索词式标题:" + "、".join(stale))
    loose = sorted(figures_in(text) - allowed)
    if loose:
        problems.append("数字在事实层里找不到:" + "、".join(loose))
    unsaid = sorted(figures_in(text) - set(spoken)) if spoken is not None else []
    unsaid = [f for f in unsaid if f not in loose]
    if unsaid:
        problems.append("数字片里没说:" + "、".join(unsaid))
    if name and not mentions_subject(text, name):
        problems.append(f"没带车型名「{name}」")
    elif name and not leads_with_subject(text, name):
        problems.append(f"没以车型名「{name}」开头")
    wrong = misattributed(text, owners)
    if wrong:
        problems.append("数字安错了车:" + "、".join(wrong))
    if name and known:
        stray = unknown_models(text, known)
        if stray:
            problems.append("脚本和事实里没有的车型:" + "、".join(stray))
    return problems


def _option_ok(opt: str) -> bool:
    return 0 < spoken_len(opt) <= OPTION_MAX_CHARS


def check_comment(text: str, options: Sequence[str], allowed: set[str],
                  terms: Iterable[str] = ()) -> list[str]:
    """置顶评论的问题清单:二选一结构、长度、禁用词、数字溯源。"""
    text = str(text or "").strip()
    problems: list[str] = []
    opts = [str(o or "").strip() for o in options]
    if len(opts) != 2 or not all(_option_ok(o) for o in opts):
        problems.append(f"需要两个 ≤{OPTION_MAX_CHARS} 字的选项")
    elif opts[0] == opts[1]:
        problems.append("两个选项相同")
    elif not all(o in text for o in opts):
        problems.append("评论里没有写出两个选项")
    if not text:
        problems.append("空评论")
        return problems
    if spoken_len(text) > COMMENT_MAX_CHARS:
        problems.append(f"{spoken_len(text)} 字,超过 {COMMENT_MAX_CHARS} 字")
    if not _QUESTION_RE.search(text):
        problems.append("没有提问")
    hits = forbidden_hits(text, terms)
    if hits:
        problems.append("命中禁用词:" + "、".join(hits))
    loose = sorted(figures_in(text) - allowed)
    if loose:
        problems.append("数字在事实层里找不到:" + "、".join(loose))
    return problems


def check_reply(text: str, allowed: set[str], terms: Iterable[str] = ()) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return ["空回复"]
    problems: list[str] = []
    if spoken_len(text) > REPLY_MAX_CHARS:
        problems.append(f"{spoken_len(text)} 字,超过 {REPLY_MAX_CHARS} 字")
    hits = forbidden_hits(text, terms)
    if hits:
        problems.append("命中禁用词:" + "、".join(hits))
    loose = sorted(figures_in(text) - allowed)
    if loose:
        problems.append("数字在事实层里找不到:" + "、".join(loose))
    return problems


# --------------------------------------------------------------------------
# 兜底模板
# --------------------------------------------------------------------------
def _first_valid(candidates: Iterable[str], allowed: set[str], terms: Iterable[str],
                 avoid: Sequence[str] = TITLE_AVOID, name: str = "") -> str:
    for cand in candidates:
        if not check_title(cand, allowed, terms, avoid, name=name):
            return cand
    return ""


def fallback_title(kind: str, subject: str, *, thesis: str = "", price: str = "",
                   allowed: set[str] | None = None, terms: Iterable[str] = (),
                   avoid: Sequence[str] = TITLE_AVOID, name: str = "") -> str:
    """一种写法的模板标题。多个模板按优先级试,第一个合格的胜出;全不合格就裁主体名。"""
    subject = re.sub(r"\s+", "", subject or "") or "这台车"
    # 兜底用的数字(价格)自己就来自事实层,放进允许集合
    pool = set(allowed or ())
    pool |= figures_in(price)
    if kind == KIND_STAKE:
        cands = [thesis, *([f"{subject}:{thesis}"] if thesis else []),
                 f"{subject},到底该不该买?", f"{subject}买不买?", "这台车到底买不买?"]
    elif kind == KIND_NUMBER:
        cands = ([f"{subject}{price},这价位怎么选", f"{subject}{price},值吗?", f"{price},怎么选"]
                 if price else []) + [f"{subject}这几个数字要看懂", f"{subject}的关键数字"]
    else:
        cands = [f"{subject},想买先看你是哪类人", f"{subject}适合谁?", "这台车适合谁?"]
    found = _first_valid(cands, pool, terms, avoid, name=name)
    if found:
        return found
    return subject[:TITLE_MAX_CHARS]


def fallback_comment(subject: str, options: Sequence[str] = (),
                     terms: Iterable[str] = ()) -> PinnedComment:
    """二选一置顶评论。没给选项就用「现在买 / 再等等」这对通用取舍。"""
    opts = [str(o).strip() for o in options if str(o).strip()]
    if len(opts) != 2 or opts[0] == opts[1] or not all(_option_ok(o) for o in opts):
        opts = ["现在买", "再等等"]
    text = f"你会选{opts[0]}还是{opts[1]}?评论区投一票"
    if check_comment(text, opts, set(), terms):  # 极端情况下(禁用词撞选项)退回通用
        opts = ["现在买", "再等等"]
        text = f"你会选{opts[0]}还是{opts[1]}?评论区投一票"
    return PinnedComment(text=text, options=opts)


# --------------------------------------------------------------------------
# 把 LLM 的原始输出收拢成 Packaging
# --------------------------------------------------------------------------
def _as_dict(item: Any) -> Mapping[str, Any]:
    return item if isinstance(item, Mapping) else {}


def build_titles(raw: Any, subject: str, *, allowed: set[str], terms: Iterable[str] = (),
                 thesis: str = "", price: str = "",
                 avoid: Sequence[str] = TITLE_AVOID,
                 order: Sequence[str] = KINDS, name: str = "",
                 known: str = "", owners: Mapping[str, set[str]] | None = None,
                 spoken: set[str] | None = None) -> tuple[list[TitleOption], bool]:
    """每种写法取 LLM 给的第一条合格标题,缺的用模板补。返回 (恰好 3 条, 是否用了 LLM)。

    `order` 是三种写法的排列顺序,第一条就是默认发布用的标题(见 `title_order`)。
    单车片开头是简称的,换成完整车名(放得下的话)。"""
    terms = tuple(terms)
    chosen: dict[str, TitleOption] = {}
    for item in raw if isinstance(raw, list) else []:
        d = _as_dict(item)
        kind = str(d.get("kind") or "").strip()
        text = str(d.get("text") or "").strip()
        if kind not in KINDS or kind in chosen:
            continue
        if name:
            text = lead_full_name(text, name)
        if check_title(text, allowed, terms, avoid, name=name, known=known, owners=owners,
                       spoken=spoken):
            continue
        chosen[kind] = TitleOption(text=text, kind=kind, note=str(d.get("note") or "").strip()[:40])
    used_llm = bool(chosen)
    out: list[TitleOption] = []
    seen: set[str] = set()
    for kind in order:
        opt = chosen.get(kind)
        if opt is None or opt.text in seen:
            text = fallback_title(kind, subject, thesis=thesis, price=price, allowed=allowed,
                                  terms=terms, avoid=avoid, name=name)
            opt = TitleOption(text=text, kind=kind, note="模板")
        seen.add(opt.text)
        out.append(opt)
    return out, used_llm


def build_comment(raw: Any, subject: str, *, allowed: set[str],
                  terms: Iterable[str] = ()) -> tuple[PinnedComment, bool]:
    d = _as_dict(raw)
    text = str(d.get("text") or "").strip()
    options = [str(o).strip() for o in (d.get("options") or [])] if isinstance(d.get("options"), list) else []
    if text and not check_comment(text, options, allowed, terms):
        return PinnedComment(text=text, options=options), True
    return fallback_comment(subject, (), terms), False


def build_replies(raw: Any, *, allowed: set[str], terms: Iterable[str] = ()) -> list[str]:
    """合格的 LLM 回复优先,不足 3 条用通用回复补齐。"""
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        text = str(item or "").strip()
        if text and text not in out and not check_reply(text, allowed, terms):
            out.append(text)
        if len(out) >= REPLY_COUNT:
            break
    for text in GENERIC_REPLIES:
        if len(out) >= REPLY_COUNT:
            break
        if text not in out:
            out.append(text)
    return out


def assemble(raw: Any, subject: str, *, allowed: set[str], terms: Iterable[str] = (),
             thesis: str = "", price: str = "", now: float = 0.0,
             avoid: Sequence[str] = TITLE_AVOID, order: Sequence[str] = KINDS,
             name: str = "", known: str = "",
             owners: Mapping[str, set[str]] | None = None,
             spoken: set[str] | None = None) -> Packaging:
    """LLM 原始输出 → 合规的 Packaging。`raw` 可以是 None 或残缺的字典,都能出结果。"""
    d = _as_dict(raw)
    terms = tuple(terms)
    titles, t_llm = build_titles(d.get("titles"), subject, allowed=allowed, terms=terms,
                                 thesis=thesis, price=price, avoid=avoid, order=order,
                                 name=name, known=known, owners=owners, spoken=spoken)
    comment, c_llm = build_comment(d.get("comment"), subject, allowed=allowed, terms=terms)
    replies = build_replies(d.get("replies"), allowed=allowed, terms=terms)
    return Packaging(titles=titles, comment=comment, replies=replies,
                     source="llm" if (t_llm or c_llm) else "fallback", made_at=now)


# --------------------------------------------------------------------------
# 生成与读写(接 LLM 和项目目录;上面的校验逻辑不依赖这些)
# --------------------------------------------------------------------------
FILE_NAME = "packaging.json"

SYSTEM = """你是中文{domain}短视频账号的运营,负责给一条已经做好的{subject_label}解说片写发布包装。

只输出一个 JSON 对象,不要 Markdown 围栏或解释文字:
{{"titles": [{{"kind": "stake|number|person", "text": "...", "note": "..."}}, ...],
  "comment": {{"text": "...", "options": ["A选项", "B选项"]}},
  "replies": ["...", "...", "..."]}}

标题(titles):**正好 3 条,kind 各一种,写法不同,不是同义改写**。
- stake:立场 / 反问。亮出一个取舍或判断,让人想争论(「23 万的 YU7,买屏还是买电?」)。
  优先写成观众会搜的问法:「车名 + 到底该不该买」「车名和对手怎么选」「车名哪个版本值」。
- number:数字 + 结论。用一个事实里的数字撑起一句判断。
- person:人群 / 场景。点名一类人或一个用车场景(「预算 25 万天天跑高速,选哪版」)。
- 每条 {title_max} 字以内,手机上一眼看完;可以用逗号或冒号分成两半。
- 每条都**以车型名开头**(完整车名,如「比亚迪海鸥」「小米YU7」;对比片写「A和B对比」):
  账号的播放九成来自搜索,观众搜的就是车名,车名越靠前、越完整越容易被搜到。
  车名后面接搜索时会带的词:「该不该买」「落地价」「怎么选」「值不值」。
  不以车型名开头的单车片标题会被直接丢弃。横评可以写价位段或人群代替车名;
  横评标题点了哪台车,数字就只能用那台车自己的(别把 A 车的续航安到 B 车头上)。
- 标题里出现的每一个数字必须逐字来自下面的事实或入门价,不许四舍五入、不许换算、不许编。
- 标题只能是主论点的另一种说法,不许引入片里没有的新判断。
- 不许用这些词:{forbidden}。不许写成一串关键词堆出来的标题,也不许出现这些词:{avoid}。
- note:一句话说明这条的角度(20 字内)。

置顶评论(comment):由账号自己发在评论区第一条,目的是拉互动。
- 二选一:options 是两个 {opt_max} 字以内的选项,都要原样写进 text;text 是一个明确的提问,{comment_max} 字以内。
- 选项必须是这条片里真实存在的取舍(主论点、版本之间、和对手之间),不是泛泛的「你怎么看」。
- 这条片的结尾写法是「{ending}」,评论的选项要和片尾的提问一致,不要问另一个问题。

备用回复(replies):正好 3 条,预判评论区最可能出现的争论(车主和黑粉、价格、口径),给账号拿来回复用。
- 每条 {reply_max} 字以内,语气平和,承认合理的质疑。
- 只准引用下面事实里的数字;事实里没有的优点、缺点一律不许说。

通用规则:数字只能来自事实;不许编造事实里没有的参数、事件或评价。"""


def default_llm():
    """取可用的 LLM(DeepSeek);没配 Key 返回 None。"""
    from ..llm.deepseek import DeepSeekProvider

    provider = DeepSeekProvider()
    return provider if provider.available() else None


def _fact_value(fact: Any, key: str, default: Any = "") -> Any:
    return fact.get(key, default) if isinstance(fact, Mapping) else getattr(fact, key, default)


def _enabled_facts(facts: Iterable[Any]) -> list[Any]:
    """门禁 A 没被取消勾选、置信度不是 low 的事实(落盘的 enabled 早期是字符串,要兼容)。"""
    return [f for f in facts or []
            if _fact_value(f, "enabled", True) not in (False, "False", "false", 0, "0")
            and _fact_value(f, "confidence", "") != "low"]


def figure_owners(script: Iterable[Any], extra: Iterable[str] = ()) -> dict[str, set[str]]:
    """横评:车型名 → 旁白讲这台车时说的数字;"" → 不属于哪台车的(开场、结论、入门价)。

    只看旁白不看事实:标题要对得上片里怎么说。事实层里元UP 也有「冬季约350」,
    但片里 350 是在讲海豚时说的,标题写「元UP 冬季约350」观众就会对不上。"""
    out: dict[str, set[str]] = {"": set()}
    items = [(s.get("subject"), s.get("text")) for s in script or [] if isinstance(s, Mapping)]
    items += [("", e) for e in extra]
    for subj, text in items:
        key = str(subj or "").strip()
        out.setdefault(key, set()).update(grounding.numbers_in(str(text or "")))
    return out


def build_inputs(project_id: str, profile: Any) -> dict[str, Any]:
    """从项目已有产物取出生成包装需要的全部输入。"""
    from .. import db
    from . import explainer, variety
    from .script_gen import AD_ABSOLUTE_TERMS

    project = db.get_project(project_id) or {}
    facts = _enabled_facts(db.read_json(project_id, "facts.json", []) or [])
    script = db.read_json(project_id, "script.json", []) or []
    texts = [str(s.get("text") or "") for s in script if isinstance(s, Mapping)]
    price, _ = explainer._price_of(facts)
    claims = [str(_fact_value(f, "claim", "")) for f in facts]
    try:
        theme = profile.theme_spec(project.get("theme") or None)
    except Exception:  # noqa: BLE001 - 片型配置缺失不该拦住包装
        theme = None
    try:
        ending = variety.ending(variety.parse(project.get("variant")), theme).label
    except Exception:  # noqa: BLE001
        ending = ""
    terms = tuple(dict.fromkeys((*profile.forbidden_terms, *AD_ABSOLUTE_TERMS)))
    roundup = bool(db.project_subjects(project))
    fact_lines = []
    for f in facts:
        basis = str(_fact_value(f, "basis", "") or "")
        fact_lines.append(f"- {_fact_value(f, 'id', '')} | {_fact_value(f, 'claim', '')}"
                          + (f"({basis})" if basis else ""))
    return {
        "subject": str(project.get("subject") or ""),
        # 单车片标题必须带车型名;横评(subjects 非空)的 subject 是话题,不查
        "name": "" if roundup else str(project.get("subject") or ""),
        "owners": figure_owners(script, [price]) if roundup else None,
        "spoken": allowed_figures((), texts, [price]),
        "thesis": str(project.get("thesis") or "").strip(),
        "price": price,
        "title": str(project.get("title") or ""),
        "ending": ending,
        "terms": terms,
        "avoid": tuple(dict.fromkeys((*TITLE_AVOID, *getattr(profile, "title_avoid", ())))),
        "fact_lines": fact_lines,
        "script_texts": texts,
        "allowed": allowed_figures(claims, texts, [price]),
        "domain": profile.vocab.domain,
        "subject_label": profile.vocab.subject_label,
    }


def _user_prompt(inp: Mapping[str, Any]) -> str:
    head = [f"主体:{inp['subject']}", f"片名:{inp['title']}"]
    if inp["thesis"]:
        head.append(f"主论点:{inp['thesis']}")
    if inp["price"]:
        head.append(f"入门价:{inp['price']}")
    script = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(inp["script_texts"][:40]))
    return ("\n".join(head) + "\n\n事实(id | 内容):\n" + "\n".join(inp["fact_lines"][:60])
            + "\n\n全片旁白:\n" + script)


#: 复盘官的打法手册:`learned("title_kind")` → {写法: 权重}。由 `team.playbook.install` 挂上;
#: 没挂 / 没结论时标题顺序固定是 KINDS
learned: Callable[[str], Mapping[str, float]] | None = None
#: 有手册时也留这么多比例不按效果排(探索)
EXPLORE = 0.3
#: 复盘官下发的配对实验计划:`plan()` → {dimension, value}。这一轮点名要试哪个标题写法,
#: 就把它排第一(凑样本用);没挂 / 不是 title_kind → 照旧按权重排
plan: Callable[..., Mapping[str, Any] | None] | None = None


def _planned_title_kind() -> str | None:
    if plan is None:
        return None
    try:
        from .variety import _ask

        p = _ask(plan, "title_kind") or {}
    except Exception:  # noqa: BLE001 - 计划坏了不能挡住包装
        return None
    if p.get("dimension") != "title_kind":
        return None
    value = p.get("value")
    return str(value) if value in KINDS else None


def title_order(project_id: str) -> tuple[str, ...]:
    """三种标题写法的顺序。有手册时按权重抽出第一条(默认发布的那条),30% 随机;
    种子来自项目 ID,同一个项目重算结果不变。

    配对实验点名了要试哪种写法 → 它排第一(这一轮固定用它,好攒够样本)。"""
    import hashlib
    import random

    planned = _planned_title_kind()
    if planned:
        return (planned, *(k for k in KINDS if k != planned))
    if learned is None:
        return KINDS
    try:
        weights = learned("title_kind") or {}
    except Exception:  # noqa: BLE001 - 手册坏了不能挡住包装
        return KINDS
    if not weights:
        return KINDS
    digest = hashlib.sha256(f"title:{project_id}".encode()).hexdigest()
    rng = random.Random(int(digest[:16], 16))
    if rng.random() < EXPLORE:
        first = rng.choice(KINDS)
    else:
        first = rng.choices(KINDS, [max(float(weights.get(k, 1.0)), 0.01) for k in KINDS])[0]
    rest = sorted((k for k in KINDS if k != first), key=lambda k: -float(weights.get(k, 1.0)))
    return (first, *rest)


def generate(project_id: str, profile: Any, llm: Any = None, *, now: float | None = None) -> Packaging:
    """生成包装并返回(不落盘)。LLM 缺失、出错、输出残缺都能拿到合规结果(模板兜底)。"""
    import logging
    import time

    inp = build_inputs(project_id, profile)
    raw: Any = None
    if llm is not None:
        system = SYSTEM.format(
            domain=inp["domain"], subject_label=inp["subject_label"], title_max=TITLE_MAX_CHARS,
            forbidden="、".join(f"「{t}」" for t in inp["terms"]),
            avoid="、".join(f"「{t}」" for t in inp["avoid"]), opt_max=OPTION_MAX_CHARS,
            comment_max=COMMENT_MAX_CHARS, reply_max=REPLY_MAX_CHARS,
            ending=inp["ending"] or "默认结尾")
        try:
            raw = llm.structured(system, _user_prompt(inp), required_keys=["titles"], temperature=0.7)
        except Exception as exc:  # noqa: BLE001 - 包装只是辅助,出错就走模板
            logging.getLogger(__name__).warning("包装生成失败,改用模板:%s", exc)
    return assemble(raw, inp["subject"], allowed=inp["allowed"], terms=inp["terms"],
                    thesis=inp["thesis"], price=inp["price"], avoid=inp["avoid"],
                    now=time.time() if now is None else now, order=title_order(project_id),
                    name=inp["name"], owners=inp.get("owners"), spoken=inp.get("spoken"),
                    known=" ".join([inp["subject"], *inp["script_texts"], *inp["fact_lines"]]))


def load(project_id: str) -> Packaging | None:
    """读 packaging.json;没有或损坏返回 None。"""
    from .. import db

    data = db.read_json(project_id, FILE_NAME, None)
    if not isinstance(data, dict):
        return None
    try:
        return Packaging.model_validate(data)
    except Exception:  # noqa: BLE001
        return None


def save(project_id: str, packaging: Packaging) -> None:
    from .. import db

    db.write_json(project_id, FILE_NAME, packaging.model_dump())


def ensure(project_id: str, profile: Any, llm: Any = None, *, force: bool = False) -> Packaging:
    """有就读,没有(或 force)就生成并落盘。重新生成会保留用户已做的选择。"""
    old = load(project_id)
    if old is not None and not force:
        return old
    fresh = generate(project_id, profile, llm)
    if old is not None:
        fresh.picked = {**fresh.picked, **{k: v for k, v in old.picked.items() if k == "cover"}}
    save(project_id, fresh)
    return fresh


# --------------------------------------------------------------------------
# 封面 B 的大字
# --------------------------------------------------------------------------
HOOK_MAX_CHARS = 8
_HOOK_NUM = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?:万元|万|公里|分钟|小时|马力|kWh|kW|km|秒|度|升|元|天|%)?")
#: 数量名词:「23.2万台」是销量不是价格,大字单拎出「23.2万」会被读成价格,这类不当大字
_HOOK_COUNT = re.compile(r"万?(?:台|辆|人|次|户|单|条)")
_HOOK_CUT = re.compile(r"[,，。:：;；!！?？、\s]")


def hook_title(titles: Sequence[TitleOption], hook: str) -> str:
    """大字出自哪条标题(封面说明文字要用同一条,否则大字没有上下文);不是出自标题返回空串。"""
    return next((t.text for t in titles if t.kind == KIND_NUMBER and hook and hook in t.text), "")


def cover_hook(titles: Sequence[TitleOption], *, price: str = "", thesis: str = "") -> str:
    """封面 B 的大字:数字型标题里的第一个数字(带单位),其次入门价,再其次主论点的头几个字。
    都取不到返回空串 —— 调用方只出封面 A。数字取自已校验的标题 / 价格事实,不是新编的。"""
    for t in titles:
        if t.kind == KIND_NUMBER:
            for m in _HOOK_NUM.finditer(t.text):
                if _HOOK_COUNT.match(t.text, m.end()) or not figures_in(m.group()):
                    continue
                return m.group()
    m = _HOOK_NUM.search(price or "")
    if m and figures_in(m.group()):
        return m.group()
    head = _HOOK_CUT.split((thesis or "").strip(), maxsplit=1)[0].strip()
    head = head[:HOOK_MAX_CHARS]
    return head if spoken_len(head) >= 3 and not forbidden_hits(head) else ""


# --------------------------------------------------------------------------
# 选择与发布取用
# --------------------------------------------------------------------------
COVER_VARIANTS = ("a", "b")


def cover_file(project_id: str, variant: str = "a", *, ratio: str = "") -> Path | None:
    """封面文件路径(不存在返回 None)。ratio="3x4" 取竖屏的 3:4 版。"""
    from .. import config, db
    from . import aspect
    from .narration import cover_3x4_path

    project = db.get_project(project_id)
    if not project:
        return None
    path = config.project_dir(project_id) / aspect.cover_name(project, "b" if variant == "b" else "a")
    if ratio == "3x4":
        path = cover_3x4_path(path)
    return path if path.is_file() else None


def set_picked(project_id: str, *, title: int | None = None, cover: str | None = None) -> Packaging | None:
    """记下用户选的标题序号 / 封面版本。没有包装返回 None;越界的值抛 ValueError。"""
    pkg = load(project_id)
    if pkg is None:
        return None
    if title is not None:
        if not 0 <= int(title) < len(pkg.titles):
            raise ValueError("标题序号越界")
        pkg.picked["title"] = int(title)
    if cover is not None:
        if cover not in COVER_VARIANTS:
            raise ValueError("封面只能选 a 或 b")
        if cover == "b" and cover_file(project_id, "b") is None:
            raise ValueError("这条片没有封面 B")
        pkg.picked["cover"] = cover
    save(project_id, pkg)
    return pkg


def publish_choice(project_id: str) -> dict[str, str]:
    """发布时取用的选择:{"title": 选中的标题 / "", "kind": 写法, "cover": "a"|"b"}。
    没有包装、选择失效都退回原样(原标题、封面 A)。"""
    pkg = load(project_id)
    if pkg is None or not pkg.titles:
        return {"title": "", "kind": "", "cover": "a"}
    idx = pkg.picked.get("title", 0)
    idx = idx if isinstance(idx, int) and 0 <= idx < len(pkg.titles) else 0
    cover = "b" if pkg.picked.get("cover") == "b" and cover_file(project_id, "b") else "a"
    return {"title": pkg.titles[idx].text, "kind": pkg.titles[idx].kind, "cover": cover}
