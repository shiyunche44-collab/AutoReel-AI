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

**断更提醒**:抖音连续 `IDLE_DAYS` 天没发、接下来一天也没排上,记一条团队事件提醒你补位
(一天最多提醒一次)。

**不重试轰炸**:登录失效记一次失败,之后不再开浏览器,直到你重新扫码(和分析师同一个判断)。
"""
from __future__ import annotations

import datetime as dt
import logging
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

SYSTEM = """你是汽车类抖音账号的运营,替作者给观众评论分类并拟回复。回复会以作者名义发出,作者会逐条审核。
分类(kind):question=提问 / 想看什么;praise=夸奖感谢;argue=抬杠、质疑、骂;ad=广告引流;other=其他。
回复规则:
- 口吻是作者本人,真诚简短,不超过 {max_chars} 个字,不用「亲」「宝子」这类客服腔;
- 只能用下面「事实」里出现过的数字,没有就别写数字;不用「最」「第一」「绝对」这类绝对化用语;
- 问题类:事实里有答案就直接答,答不了就说记下了、后面讲;
- 杠类:不争吵,摆出处或承认可以讨论;
- 广告类:reply 留空。
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


def template_draft(kind: str) -> dict[str, str]:
    """模板回复。广告类不回(状态直接 skipped)。"""
    if kind == "ad":
        return {"reply_draft": "", "reply_source": "template", "reply_state": "skipped"}
    return {"reply_draft": TEMPLATES.get(kind, TEMPLATES["other"]), "reply_source": "template",
            "reply_state": "draft"}


def parse_comment(raw: dict[str, Any], pub: dict[str, Any]) -> dict[str, Any] | None:
    """评论接口的一条 → 入库的行。作者自己的、楼中楼都不要。"""
    if raw.get("is_author") or int(raw.get("level") or 1) != 1:
        return None
    cid = str(raw.get("comment_id") or "")
    if not cid:
        return None
    text = str(raw.get("text") or "").strip()
    kind = classify(text)
    return {"id": cid, "publication_id": pub["id"], "account_id": pub.get("account_id"),
            "remote_id": pub.get("remote_id"),
            "author": str((raw.get("user_info") or {}).get("screen_name") or ""),
            "text": text, "created_at": float(raw.get("create_time") or 0) or time.time(),
            "likes": int(raw.get("digg_count") or 0), "kind": kind, "kind_source": "rule",
            **template_draft(kind)}


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
            + "\n\n评论(id | 内容):\n" + "\n".join(f"{r['id']} | {r['text'][:200]}" for r in rows))
    try:
        data = llm.structured(SYSTEM.format(max_chars=packaging.REPLY_MAX_CHARS), user,
                              required_keys=["items"], temperature=0.4)
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
        fields: dict[str, str] = {"kind": kind, "kind_source": "llm", **template_draft(kind)}
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
    added = 0
    for wid, raws in (got.get("comments") or {}).items():
        pub = pubs.get(wid)
        if not pub:
            continue
        rows = [r for r in (parse_comment(x, pub) for x in raws) if r]
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
