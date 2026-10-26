"""运营官:每 2 小时拉一次团队作品的新评论,分类、拟回复,等你在 /team 批量审核后才发。

**分类**:问题 / 夸 / 杠 / 广告 / 其他。先用关键词规则分(没有 LLM 也能用),有 LLM 时
一批评论一次调用,LLM 的分类和回复覆盖规则结果;LLM 的回复过不了 `packaging.check_reply`
(超字数、禁用词、事实层里找不到的数字)就退回模板。

**回复以你的名义发出,一律经你确认**:新评论的回复只是草稿,你点了「批准」才进发送队列;
运营官下一轮(或你点「发送已批的」)才开浏览器逐条发,遇到第一条失败就停,不连着试。
「夸」类模板感谢有个自动档(`/team` 页开关,默认关),开了才会不经你批直接发。
不做刷量、互赞、批量关注这类养号操作。

**问题回灌选题池**:观众问的就是观众想看的。问题类评论交给主编(`audience_text`),
附在选题提示词里;你也可以在审核区把某条标成「不进选题池」。

**回复写法**(`reply_style`):补充信息 / 反问 / 感谢,每条评论按 id 定一种(重拟还是同一种),
有结论时按结论的权重挑、留 30% 探索。发出去 24 小时后量这条作品又多了几条评论、原评论者
有没有再来留言(只存一级评论,楼中楼看不到,所以「再回复」= 同一个人又留了一条),
每周学哪种写法引得来互动(`learn_styles`),结论和别的打法一样能在 /team 停用。

**断更提醒**:抖音连续 `IDLE_DAYS` 天没发、接下来一天也没排上,记一条团队事件提醒你补位
(一天最多提醒一次)。

**不重试轰炸**:登录失效记一次失败,之后不再开浏览器,直到你重新扫码(和分析师同一个判断)。
"""
from __future__ import annotations

import datetime as dt
import logging
import random
import re
import time
from typing import Any, Callable

from .. import db
from . import Skip, TeamError

log = logging.getLogger(__name__)

KIND_LABEL = {"question": "问题", "praise": "夸", "argue": "杠", "ad": "广告", "other": "其他"}
#: 只看发布后这么多天内的作品(老作品的评论少,不值得每 2 小时开一次浏览器)
WATCH_DAYS = 30
#: 一轮最多发这么多条回复
SEND_LIMIT = 10
#: 连续这么多天没发作品就提醒
IDLE_DAYS = 3
#: 问题类评论给主编看多少天内的
QUESTION_DAYS = 14
#: 同一台车被问到这么多条 / 这些问题合计这么多赞,就算「观众问的」选题
ASK_MIN_COUNT = 2
ASK_MIN_LIKES = 5

_COUNTS_KEY = "engage_counts"
_LOGIN_KEY = "engage_login_bad"
_IDLE_KEY = "engage_idle_warned"
AUTO_THANKS_KEY = "engage_auto_thanks"

_AD_RE = re.compile(r"微信|威信|薇信|[vV][xX]|加[vV]|\+[vV]|[qQ]{2}|扣扣|私信我|私我|代理|兼职|招募"
                    r"|日结|低价出|https?://|www\.|\d{7,}")
_QUESTION_RE = re.compile(r"[?？]|吗|么|呢|多少|怎么|咋|如何|哪个|哪款|哪些|能不能|可不可以|是不是|有没有"
                          r"|值不值|值得|还是|什么时候|啥时候|求|请问|建议|推荐|对比|区别|出一期|讲讲|说说")
_ARGUE_RE = re.compile(r"垃圾|智商税|韭菜|水军|恰饭|广子|收钱|胡说|瞎说|乱说|外行|不懂|黑子|洗地|误导"
                       r"|错了|不对|离谱|吹|扯|无脑|偏心|双标|别听")
_PRAISE_RE = re.compile(r"好|棒|赞|专业|干货|讲得|清楚|清晰|支持|喜欢|学到|有用|牛|厉害|客观|良心|实在"
                        r"|谢谢|感谢|收藏|关注了|期待|👍|❤|\[(?:赞|鼓掌|爱心|比心|玫瑰|强|666)\]")

#: 没有 LLM 或 LLM 的回复不合格时用。广告不回
TEMPLATES = {
    "praise": "谢谢支持!下一期继续把参数和取舍讲清楚。",
    "question": "好问题,记下了,后面专门出一期讲这个。",
    "argue": "数据都是官方口径,片里标了出处,有出入欢迎具体指出来。",
    "other": "谢谢关注,想看哪台车评论区告诉我。",
}

#: 回复写法(旋钮 `reply_style`)
REPLY_STYLES = {"info": "补充信息", "ask": "反问", "thanks": "感谢"}
STYLE_RULE = {
    "info": "多给一点片里没说透的信息(只能用事实里的数字)",
    "ask": "回应完反问观众一句,引他接着聊(比如他更在意哪一点、想看哪台车)",
    "thanks": "先真诚道谢,再简短回应",
}
#: 各写法的模板。info 就是原来那套
STYLE_TEMPLATES = {
    "info": TEMPLATES,
    "ask": {
        "praise": "谢谢支持!你最想看哪台车的参数拆解?",
        "question": "好问题,记下了。你更在意哪一点?后面重点讲。",
        "argue": "数据是官方口径,片里标了出处。你看到的是哪个数?发出来对一下。",
        "other": "谢谢关注,你最近在看哪台车?",
    },
    "thanks": {
        "praise": "谢谢你!看到这样的评论特别有动力。",
        "question": "谢谢提问!记下了,后面专门出一期讲这个。",
        "argue": "谢谢你较真,数据是官方口径,有出入欢迎具体指出来。",
        "other": "谢谢你来看!",
    },
}
#: 按结论挑写法之外留多少均匀探索(和 `variety.EXPLORE` 一样)
STYLE_EXPLORE = 0.3
#: 回复后看多久的评论增量 / 原评论者多久内再来算「又回了」
OUTCOME_HOURS = 24
AGAIN_DAYS = 7
#: 每种写法至少这么多条回复才下结论;收缩强度
STYLE_MIN_N = 5
STYLE_PRIOR = 5
STYLE_RANGE = (0.7, 1.3)
#: 回复写法结论的维度 / 指标
STYLE_DIMENSION = "reply_style"
STYLE_METRICS = {"reply_more": "回复后 24 小时评论增量", "reply_again": "原评论者再留言率"}
#: 启动时由 `team.playbook.install` 挂上:() → {写法: 权重}。None = 均匀挑
style_weights: Callable[[], dict[str, float]] | None = None

SYSTEM = """你是汽车类抖音账号的运营,替作者给观众评论分类并拟回复。回复会以作者名义发出,作者会逐条审核。
分类(kind):question=提问 / 想看什么;praise=夸奖感谢;argue=抬杠、质疑、骂;ad=广告引流;other=其他。
回复规则:
- 口吻是作者本人,真诚简短,不超过 {max_chars} 个字,不用「亲」「宝子」这类客服腔;
- 只能用下面「事实」里出现过的数字,没有就别写数字;不用「最」「第一」「绝对」这类绝对化用语;
- 问题类:事实里有答案就直接答,答不了就说记下了、后面讲;
- 杠类:不争吵,摆出处或承认可以讨论;
- 广告类:reply 留空;
- 每条评论后面标了「写法」,按写法拟:{style_rules}。
只输出 JSON:{{"items": [{{"id": "评论 id", "kind": "...", "reply": "..."}}]}}"""


# --------------------------------------------------------------------------
# 纯函数:分类、模板、拟回复
# --------------------------------------------------------------------------
def classify(text: str) -> str:
    """关键词规则分类。广告优先(引流里也常带问号),杠先于夸(「吹得好」是杠)。"""
    text = str(text or "")
    if _AD_RE.search(text):
        return "ad"
    if _ARGUE_RE.search(text):
        return "argue"
    if _QUESTION_RE.search(text):
        return "question"
    if _PRAISE_RE.search(text):
        return "praise"
    return "other"


def template_draft(kind: str, style: str = "info") -> dict[str, str]:
    """模板回复。广告类不回(状态直接 skipped)。"""
    if kind == "ad":
        return {"reply_draft": "", "reply_source": "template", "reply_state": "skipped"}
    table = STYLE_TEMPLATES.get(style) or TEMPLATES
    return {"reply_draft": table.get(kind, table["other"]), "reply_source": "template",
            "reply_state": "draft"}


def _style_weights() -> dict[str, float]:
    if style_weights is None:
        return {}
    try:
        return dict(style_weights() or {})
    except Exception:  # noqa: BLE001 - 结论坏了照样均匀挑
        log.warning("读回复写法结论失败,这次均匀挑", exc_info=True)
        return {}


def pick_style(comment_id: str, weights: dict[str, float] | None = None) -> str:
    """这条评论用哪种写法。按评论 id 定(重抓 / 重拟还是同一种);有权重时 70% 按权重挑。"""
    rng = random.Random(f"reply_style:{comment_id}")
    styles = list(REPLY_STYLES)
    if weights and rng.random() >= STYLE_EXPLORE:
        return rng.choices(styles, [max(float(weights.get(s, 1.0)), 0.01) for s in styles])[0]
    return rng.choice(styles)


def parse_comment(raw: dict[str, Any], pub: dict[str, Any],
                  weights: dict[str, float] | None = None) -> dict[str, Any] | None:
    """评论接口的一条 → 入库的行。作者自己的、楼中楼都不要。`weights` = 回复写法的权重。"""
    if raw.get("is_author") or int(raw.get("level") or 1) != 1:
        return None
    cid = str(raw.get("comment_id") or "")
    if not cid:
        return None
    text = str(raw.get("text") or "").strip()
    kind = classify(text)
    style = pick_style(cid, weights)
    return {"id": cid, "publication_id": pub["id"], "account_id": pub.get("account_id"),
            "remote_id": pub.get("remote_id"),
            "author": str((raw.get("user_info") or {}).get("screen_name") or ""),
            "text": text, "created_at": float(raw.get("create_time") or 0) or time.time(),
            "likes": int(raw.get("digg_count") or 0), "kind": kind, "kind_source": "rule",
            "reply_style": style, **template_draft(kind, style)}


def _reply_context(project_id: str) -> tuple[set[str], tuple[str, ...], list[str]]:
    """(允许出现的数字, 禁用词, 事实行)。项目没了 / 读不到就空着 —— 那就不许写数字。"""
    from ..categories import load_profile
    from ..pipeline import packaging

    project = db.get_project(project_id) or {}
    try:
        inp = packaging.build_inputs(project_id, load_profile(project["category"]))
    except Exception:  # noqa: BLE001 - 拟回复不该因为项目产物缺了就挂
        return set(), tuple(packaging.AD_ABSOLUTE_TERMS), []
    return inp["allowed"], tuple(inp["terms"]), inp["fact_lines"]


def llm_drafts(rows: list[dict[str, Any]], title: str, ctx: tuple[set[str], tuple[str, ...], list[str]],
               llm: Any) -> dict[str, dict[str, str]]:
    """同一条作品下的一批评论交给 LLM 分类 + 拟回复。返回 {评论 id: 要改的字段},不合格的不在里面。"""
    from ..pipeline import packaging

    if llm is None or not rows:
        return {}
    allowed, terms, facts = ctx
    user = (f"作品标题:{title}\n\n事实:\n" + ("\n".join(facts[:40]) or "(无)")
            + "\n\n评论(id | 写法 | 内容):\n"
            + "\n".join(f"{r['id']} | {REPLY_STYLES.get(r.get('reply_style') or 'info', '补充信息')}"
                        f" | {r['text'][:200]}" for r in rows))
    rules = ";".join(f"{REPLY_STYLES[k]}={v}" for k, v in STYLE_RULE.items())
    styles = {r["id"]: r.get("reply_style") or "info" for r in rows}
    try:
        data = llm.structured(SYSTEM.format(max_chars=packaging.REPLY_MAX_CHARS, style_rules=rules),
                              user, required_keys=["items"], temperature=0.4)
    except Exception as exc:  # noqa: BLE001 - 模板兜底
        log.warning("评论拟回复失败,用模板:%s", exc)
        return {}
    ids = {r["id"] for r in rows}
    out: dict[str, dict[str, str]] = {}
    for item in data.get("items") or [] if isinstance(data, dict) else []:
        if not isinstance(item, dict) or str(item.get("id")) not in ids:
            continue
        kind = str(item.get("kind") or "")
        if kind not in KIND_LABEL:
            continue
        fields: dict[str, str] = {"kind": kind, "kind_source": "llm",
                                  **template_draft(kind, styles[str(item["id"])])}
        reply = str(item.get("reply") or "").strip()
        if kind != "ad" and reply and not packaging.check_reply(reply, allowed, terms):
            fields.update(reply_draft=reply, reply_source="llm")
        out[str(item["id"])] = fields
    return out


# --------------------------------------------------------------------------
# 抓评论
# --------------------------------------------------------------------------
def watched(now: float) -> list[dict[str, Any]]:
    """要看评论的发布记录:抖音、已对上作品、发布 `WATCH_DAYS` 天内。"""
    return [p for p in db.list_publications(limit=500)
            if p["platform"] == "douyin" and p.get("remote_id")
            and p["state"] in ("submitted", "published")
            and now - (p.get("published_at") or 0) <= WATCH_DAYS * 86400]


def _login_blocked() -> bool:
    """上次登录失效后还没重新扫码:不再开浏览器。"""
    from ..publish import browser

    bad = db.get_team_setting(_LOGIN_KEY, None)
    if not bad:
        return False
    path = browser.state_path("douyin")
    return not (path.is_file() and path.stat().st_mtime > float(bad))


def _browser_failed(exc: Any, now: float, what: str) -> TeamError:
    if exc.credential:
        db.set_team_setting(_LOGIN_KEY, now)
        db.add_event("__team__", "team", reason=f"运营官{what}:{exc.message}")
    return TeamError(exc.message)


def collect(now: float | None = None, fetch: Callable[..., dict[str, Any]] | None = None,
            llm: Any = None) -> dict[str, int]:
    """拉一轮新评论并拟好回复。`fetch` 测试用(默认开浏览器)。"""
    from ..publish.base import PublishError
    from ..publish.platforms import douyin

    now = time.time() if now is None else now
    pubs = {p["remote_id"]: p for p in watched(now)}
    if not pubs:
        raise Skip(f"近 {WATCH_DAYS} 天没有对上作品的抖音发布")
    seen = db.get_team_setting(_COUNTS_KEY, {}) or {}
    known = {wid: int(seen.get(wid, 0)) for wid in pubs}
    try:
        got = (fetch or douyin.fetch_creator_comments)(known)
    except PublishError as exc:
        raise _browser_failed(exc, now, "抓不了评论") from exc
    db.set_team_setting(_LOGIN_KEY, None)
    auto = bool(db.get_team_setting(AUTO_THANKS_KEY, False))
    weights = _style_weights()
    added = 0
    for wid, raws in (got.get("comments") or {}).items():
        pub = pubs.get(wid)
        if not pub:
            continue
        rows = [r for r in (parse_comment(x, pub, weights) for x in raws) if r]
        fresh = [r for r in rows if not db.get_comment(r["id"])]
        if fresh:
            ctx = _reply_context(pub["project_id"])
            better = llm_drafts(fresh, pub.get("title") or "", ctx, llm)
            for r in fresh:
                r.update(better.get(r["id"], {}))
                if auto and r["kind"] == "praise" and r["reply_source"] == "template":
                    r["reply_state"] = "approved"  # 你开了感谢自动档
        added += db.add_comments(rows)
    db.set_team_setting(_COUNTS_KEY, {**seen, **(got.get("counts") or {})})
    return {"works": len(pubs), "checked": len(got.get("comments") or {}), "added": added}


# --------------------------------------------------------------------------
# 审核 + 发送
# --------------------------------------------------------------------------
class EngageError(ValueError):
    """审核操作不成立(状态不对、回复不合格)。"""


def edit(comment_id: str, *, reply: str | None = None, kind: str | None = None) -> dict[str, Any]:
    """你改回复 / 改分类。改了回复就是你写的(仍要批准才发)。"""
    from ..pipeline import packaging

    c = db.get_comment(comment_id)
    if not c:
        raise EngageError("没有这条评论")
    if c["reply_state"] in ("sending", "sent"):
        raise EngageError("这条已经发出去了")
    fields: dict[str, Any] = {}
    if kind is not None:
        if kind not in KIND_LABEL:
            raise EngageError("分类不对")
        fields.update(kind=kind, kind_source="user")
    if reply is not None:
        reply = reply.strip()
        allowed, terms, _ = _reply_context(_project_of(c))
        problems = [p for p in packaging.check_reply(reply, allowed, terms)
                    if not p.startswith("数字")]  # 你写的数字你自己负责,只拦超长和禁用词
        if problems:
            raise EngageError(";".join(problems))
        fields.update(reply_draft=reply, reply_source="user")
        if c["reply_state"] in ("skipped", "failed"):
            fields["reply_state"] = "draft"
    db.update_comment(comment_id, **fields)
    return db.get_comment(comment_id) or {}


def _project_of(c: dict[str, Any]) -> str:
    pub = db.get_publication(c["publication_id"]) or {}
    return pub.get("project_id") or ""


def decide(ids: list[str], action: str) -> int:
    """批量:approve 进发送队列 / skip 不回 / reset 退回草稿。返回改了几条。"""
    moves = {"approve": (("draft", "failed"), "approved"),
             "skip": (("draft", "approved", "failed"), "skipped"),
             "reset": (("approved", "skipped", "failed"), "draft")}
    if action not in moves:
        raise EngageError("action 只能是 approve / skip / reset")
    expected, new = moves[action]
    n = 0
    for cid in ids:
        c = db.get_comment(cid)
        if not c:
            continue
        if action == "approve" and not c["reply_draft"].strip():
            continue
        if db.cas_comment_state(cid, expected, new, reply_error=""):
            n += 1
    return n


def set_topic(comment_id: str, pooled: bool) -> None:
    """问题类评论进 / 不进选题池。"""
    if not db.get_comment(comment_id):
        raise EngageError("没有这条评论")
    db.update_comment(comment_id, topic_state="" if pooled else "dismissed")


def send_approved(now: float | None = None,
                  send: Callable[[list[dict[str, Any]], Callable], None] | None = None) -> dict[str, int]:
    """把你批过的回复发出去。先把状态抢成 sending,发完逐条落 sent / failed。"""
    from ..publish.base import PublishError
    from ..publish.platforms import douyin

    now = time.time() if now is None else now
    queue = sorted(db.list_comments(states=("approved",), limit=SEND_LIMIT),
                   key=lambda c: (c["remote_id"] or "", c["created_at"], c["id"]))
    jobs = [{"id": c["id"], "work_id": c["remote_id"], "author": c["author"], "text": c["text"],
             "reply": c["reply_draft"]}
            for c in queue if db.cas_comment_state(c["id"], ("approved",), "sending")]
    if not jobs:
        return {"sent": 0, "failed": 0}
    out = {"sent": 0, "failed": 0}
    done: set[str] = set()

    def on_done(job: dict[str, Any], error: str | None) -> None:
        done.add(job["id"])
        if error:
            db.cas_comment_state(job["id"], ("sending",), "failed", reply_error=error)
            out["failed"] += 1
        else:
            db.cas_comment_state(job["id"], ("sending",), "sent", replied_at=time.time())
            out["sent"] += 1

    try:
        (send or douyin.send_comment_replies)(jobs, on_done)
    except PublishError as exc:
        raise _browser_failed(exc, now, "发不了回复") from exc
    finally:
        # 中途停下(第一条失败就停 / 浏览器挂了)没轮到的退回已批准,下轮再发
        for job in jobs:
            if job["id"] not in done:
                db.cas_comment_state(job["id"], ("sending",), "approved")
    return out


# --------------------------------------------------------------------------
# 回灌、断更、指标
# --------------------------------------------------------------------------
def audience_text(now: float | None = None, limit: int = 8, mark: bool = False) -> str:
    """给主编的「观众在问」:近 `QUESTION_DAYS` 天的问题类评论,赞多的在前。"""
    now = time.time() if now is None else now
    rows = db.list_comments(kinds=("question",), topic_states=("", "pooled"),
                            since=now - QUESTION_DAYS * 86400, limit=100)
    rows = sorted(rows, key=lambda r: (-r["likes"], -r["created_at"]))[:limit]
    if not rows:
        return ""
    titles: dict[str, str] = {}
    lines = []
    for r in rows:
        if r["publication_id"] not in titles:
            titles[r["publication_id"]] = (db.get_publication(r["publication_id"]) or {}).get("title") or ""
        lines.append(f"- 《{titles[r['publication_id']][:20]}》下:{r['text'][:60]}")
        if mark and not r["topic_state"]:
            db.update_comment(r["id"], topic_state="pooled")
    return "观众在评论里问(观众想看的,能排就排):\n" + "\n".join(lines)


def reply_outcomes(now: float | None = None, days: int = 60) -> list[dict[str, Any]]:
    """近 `days` 天发出去、已满 `OUTCOME_HOURS` 小时的回复,各自的效果:
    {id, style, more: 回复后 24 小时这条作品新增几条评论, again: 原评论者 7 天内有没有再留言}。

    只算按写法拟的(模板 / AI);你自己写的不算哪种写法。"""
    now = time.time() if now is None else now
    rows = db.list_comments(since=now - (days + AGAIN_DAYS) * 86400, limit=5000)
    by_pub: dict[str, list[float]] = {}
    by_author: dict[str, list[float]] = {}
    for r in rows:
        by_pub.setdefault(r["publication_id"], []).append(float(r["created_at"]))
        if r["author"]:
            by_author.setdefault(r["author"], []).append(float(r["created_at"]))
    out = []
    for r in rows:
        at = r.get("replied_at")
        if (r["reply_state"] != "sent" or not at or r.get("reply_style") not in REPLY_STYLES
                or r["reply_source"] not in ("template", "llm")
                or now - at < OUTCOME_HOURS * 3600 or now - at > days * 86400):
            continue
        more = sum(1 for t in by_pub.get(r["publication_id"], ())
                   if at < t <= at + OUTCOME_HOURS * 3600)
        again = bool(r["author"]) and any(at < t <= at + AGAIN_DAYS * 86400
                                          for t in by_author.get(r["author"], ()))
        out.append({"id": r["id"], "style": r["reply_style"], "kind": r["kind"],
                    "more": more, "again": again, "text": r["text"][:40]})
    return out


def _shrunk(vals: list[float], base: float) -> float:
    return (sum(vals) + STYLE_PRIOR * base) / (len(vals) + STYLE_PRIOR) / base


def learn_styles(outcomes: list[dict[str, Any]], week: str) -> list[dict[str, Any]]:
    """每种写法的两个倍数(相对所有回复的平均,往平均收缩),写成 learnings 的行。

    评论增量和再留言率都是 0 的时候没得比,不下结论。"""
    rows = []
    for metric, get in (("reply_more", lambda o: float(o["more"])),
                        ("reply_again", lambda o: 1.0 if o["again"] else 0.0)):
        vals = [get(o) for o in outcomes]
        base = sum(vals) / len(vals) if vals else 0.0
        if base <= 0:
            continue
        for style in REPLY_STYLES:
            mine = [get(o) for o in outcomes if o["style"] == style]
            if len(mine) < STYLE_MIN_N:
                continue
            effect = round(min(STYLE_RANGE[1], max(STYLE_RANGE[0], _shrunk(mine, base))), 3)
            if abs(effect - 1) < 0.05:
                continue
            sample = [o for o in outcomes if o["style"] == style]
            sample.sort(key=lambda o: -get(o) if effect > 1 else get(o))
            rows.append({"id": f"reply:{STYLE_DIMENSION}:{style}:{metric}", "dimension": STYLE_DIMENSION,
                         "value": style, "metric": metric, "effect": effect, "n": len(mine),
                         "confidence": "high" if len(mine) >= 3 * STYLE_MIN_N else "medium",
                         "evidence": [{"comment_id": o["id"], "title": o["text"], "more": o["more"],
                                       "again": o["again"]} for o in sample[:3]]})
    return rows


def style_report(outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """周报用:每种写法发了几条、平均带来几条新评论、原评论者再留言的比例。"""
    out = []
    for style, name in REPLY_STYLES.items():
        mine = [o for o in outcomes if o["style"] == style]
        if not mine:
            continue
        out.append({"style": style, "label": name, "n": len(mine),
                    "more": round(sum(o["more"] for o in mine) / len(mine), 2),
                    "again": round(sum(1 for o in mine if o["again"]) / len(mine), 3)})
    return out


def _subject_of(pub_id: str, cache: dict[str, str]) -> str:
    """评论所在作品讲的车(多车的取第一台)。没写车名的问题默认问的就是它。"""
    if pub_id not in cache:
        pub = db.get_publication(pub_id) or {}
        project = db.get_project(pub.get("project_id") or "") or {}
        subjects = project.get("subjects") or []
        cache[pub_id] = str(subjects[0] if subjects else project.get("subject") or "")
    return cache[pub_id]


def audience_topics(now: float | None = None, cars: list[str] | None = None) -> list[dict[str, Any]]:
    """把近 `QUESTION_DAYS` 天的问题类评论按车型聚成题:评论里点了车名就归那台车,
    没点就归评论所在作品讲的车。被问 ≥`ASK_MIN_COUNT` 条或合计 ≥`ASK_MIN_LIKES` 赞的才算。

    返回 [{car, n, likes, samples: [评论原文]}],问得多的在前。`cars` 测试用(默认认热点 + 排过的车)。"""
    from .. import config
    from .benchmark import car_in, known_cars

    now = time.time() if now is None else now
    rows = db.list_comments(kinds=("question",), topic_states=("", "pooled"),
                            since=now - QUESTION_DAYS * 86400, limit=300)
    if not rows:
        return []
    if cars is None:
        try:
            cars = known_cars(config.TEAM_CATEGORY)
        except Exception:  # noqa: BLE001 - 认不出车名就只按作品归
            cars = []
    groups: dict[str, dict[str, Any]] = {}
    subjects: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: (-r["likes"], -r["created_at"])):
        car = car_in(r["text"], cars) or _subject_of(r["publication_id"], subjects)
        if not car:
            continue
        g = groups.setdefault(car, {"car": car, "n": 0, "likes": 0, "samples": []})
        g["n"] += 1
        g["likes"] += int(r["likes"] or 0)
        if len(g["samples"]) < 3:
            g["samples"].append(r["text"][:40])
    hot = [g for g in groups.values() if g["n"] >= ASK_MIN_COUNT or g["likes"] >= ASK_MIN_LIKES]
    return sorted(hot, key=lambda g: (-g["n"], -g["likes"]))


def topics_text(topics: list[dict[str, Any]]) -> str:
    """给主编的一行:「观众问得最多的车:小米YU7(3 条)、……」。"""
    if not topics:
        return ""
    return "观众问得最多的车(优先排一期专门回答):" + "、".join(
        f"{t['car']}({t['n']} 条)" for t in topics[:5])


def idle_days(now: float | None = None) -> float | None:
    """抖音多少天没发了(从没发过返回 None)。"""
    now = time.time() if now is None else now
    last = max((p.get("published_at") or 0 for p in db.list_publications(limit=500)
                if p["platform"] == "douyin" and p["state"] in ("submitted", "published")),
               default=0)
    return (now - last) / 86400 if last else None


def check_idle(now: float | None = None) -> str:
    """断更 `IDLE_DAYS` 天、接下来 24 小时也没排上:提醒(一天一次)。返回提醒文字或空。"""
    from .. import config

    now = time.time() if now is None else now
    days = idle_days(now)
    if days is None or days < IDLE_DAYS:
        return ""
    ahead = db.list_plan_items(config.TEAM_CATEGORY, since=now, until=now + 86400,
                               statuses=("approved", "producing", "review", "scheduled"), limit=5)
    if ahead:
        return ""
    today = dt.date.fromtimestamp(now).isoformat()
    if db.get_team_setting(_IDLE_KEY) == today:
        return ""
    db.set_team_setting(_IDLE_KEY, today)
    text = f"抖音已经 {round(days)} 天没发作品,接下来 24 小时也没排上:去批一条选题或让主编再排"
    db.add_event("__team__", "team", reason=f"运营官提醒:{text}")
    return text


def stats(now: float | None = None, days: int = 7) -> dict[str, Any]:
    """近 `days` 天:评论数、按类计数、回复率(该回的里回了多少)、待审数,首小时互动率。"""
    now = time.time() if now is None else now
    rows = db.list_comments(since=now - days * 86400, limit=2000)
    kinds = {k: sum(1 for r in rows if r["kind"] == k) for k in KIND_LABEL}
    need = [r for r in rows if r["kind"] != "ad"]
    sent = sum(1 for r in need if r["reply_state"] == "sent")
    rates = []
    for p in db.list_publications(limit=500):
        if p["platform"] != "douyin" or now - (p.get("published_at") or 0) > days * 86400:
            continue
        first = next((s for s in db.list_snapshots(p["id"], limit=50)
                      if (s.get("hours_since_publish") or 99) <= 2), None)
        if first and first.get("views"):
            acts = sum(int(first.get(k) or 0) for k in ("likes", "comments", "shares", "favorites"))
            rates.append(acts / first["views"])
    return {
        "days": days, "total": len(rows), "kinds": kinds,
        "reply_rate": round(sent / len(need), 3) if need else None,
        "pending": len(db.list_comments(states=("draft",), limit=500)),
        "first_hour": round(sum(rates) / len(rates), 4) if rates else None,
        "first_hour_n": len(rates),
        "idle_days": idle_days(now),
    }


def run() -> str:
    from ..pipeline import orchestrator

    now = time.time()
    parts = []
    blocked = _login_blocked()
    if not blocked and db.list_comments(states=("approved",), limit=1):
        out = send_approved(now)
        parts.append(f"发出 {out['sent']} 条回复" + (f",{out['failed']} 条失败" if out["failed"] else ""))
    idle = check_idle(now)
    if idle:
        parts.append("断更提醒")
    if blocked:
        raise Skip("抖音登录失效,等你去分发台重新扫码" + (";" + idle if idle else ""))
    try:
        got = collect(now, llm=orchestrator._llm())
        if got["added"]:
            parts.append(f"{got['checked']} 条作品有新评论,新增 {got['added']} 条评论")
    except Skip:
        if not parts:
            raise
    if not parts:
        raise Skip("没有新评论")
    return ",".join(parts)
