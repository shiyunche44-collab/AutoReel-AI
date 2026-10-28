"""活跃官:主动去**别人**的作品下评论 / 点赞 / 分享 / 关注,把账号在平台侧用起来。

**为什么要有这个角色**:运营官(`engagement`)只管"自己作品下的评论"——那是**回**。
账号在抖音上真正"活着"的另一半是**对向互动**:去对标号的作品下留一条说到点上的
评论、点个赞、分享一下、关注一下。这条链路原来没有。

**和"养号"的区别**:抖音《社区自律公约》32 条把"用自动化手段、频率明显异于常人地
评论/点赞/关注/抓数据"列为违规,官方**没给频率阈值**。所以这里的做法是:
量级压在真人区间(每天 ≤10 条评论 / ≤30 个赞 / ≤8 次分享 / ≤3 个关注),
时间点按当天种子随机分散在 09:00–22:30、两条评论之间至少隔 45 分钟,
同一批里的动作之间停几秒,同一个对标号一天最多互动 3 次,只开一个浏览器不并发。
**全自动,但不频**。官方没有"养号"这种说法,官方也没有"流量池晋级"这种说法 ——
那些都是第三方经验词,别拿它们当依据。

**不做什么**:不刷量、不互赞、不批量关注、不关注后取关、不在别人评论区留联系方式
或引导关注(公约 33 不当营销)。评论**一律不带数字** —— 对方作品的数据我们没核实过,
复述了就是公约 21 的不实信息。

**动作在哪做**:`app/publish/platforms/douyin_outreach.py`(Playwright 驱动已登录的
浏览器)。抖音网页版的**作品详情页没有评论输入框**,能评论的是推荐流弹层
(`?modal_id=`),细节都在那个模块的注释里。

**留痕**:每次动作都写 `outbound_actions` 表(目标、文案、结果、失败原因),
/team 页面上看得见。同一目标同一动作一辈子只做一次(唯一索引兜着)。

**全自动没有人工闸,所以护栏必须硬**:
- 额度到就停(可在 /team 调);
- 任一动作失败 → **当日剩余动作全部停**,不重试、不换作品接着刷;
- 撞上验证码 / 登录失效 → 当日停,等人处理;
- 账号被合规审核官判定疑似限流 → 跟着停;
- `active_enabled=false` → 立刻停手(页面上一键)。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import random
import re
import statistics
import time
from typing import Any, Callable

from .. import config, db
from . import Skip, TeamError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
#: 动作名
COMMENT, LIKE, SHARE, FOLLOW = "comment", "like", "share", "follow"
#: 状态机
PLANNED, DOING, DONE, FAILED, SKIPPED = "planned", "doing", "done", "failed", "skipped"

#: 运行时设置键(存在 team_settings 里,页面能改)
ENABLED_KEY = "active_enabled"
PAUSED_DAY_KEY = "active_paused_day"
LOGIN_BAD_KEY = "active_login_bad"
PLAN_KEY = "active_plan_{day}"
#: 当天"跳过"的计划槽位(没有可用目标 / 没有合格文案)。不记下来的话,
#: 角色会一轮一轮地重挑同一条永远做不成的计划项。
SKIPPED_KEY = "active_skipped_{day}"

#: 额度默认值(config 里也有一份,这里给出的是回退值)
DEFAULT_QUOTAS = {COMMENT: 10, LIKE: 30, SHARE: 8, FOLLOW: 3}

#: 评论长度区间
MIN_CHARS = 8
MAX_CHARS = 25

#: 候选池:点赞数在这个区间外的作品不碰(太小没意义,太大的号看不见我们)
MIN_LIKES = 100
MAX_LIKES = 200000
#: 候选池看多少天内的作品
CANDIDATE_DAYS = 7

#: 引流 / 联系方式 / 账号引导(公约 33)
_FORBIDDEN_AD = ("微信", "威信", "薇信", "加v", "加V", "扣扣", "扣1", "私信", "抖音号",
                 "链接", "公众号", "小程序", "扫码", "评论区见", "置顶", "关注我", "加个",
                 "留个", "+V", "vx", "VX")
#: 提我们自己(在别人作品下提自己 = 营销)
_FORBIDDEN_SELF = ("我们", "我号", "本号", "上期", "这期", "上条", "主页", "我的视频",
                   "我们的号", "我们号")
#: 假装有亲身体验(我们没开过那台车)
_FORBIDDEN_EXPERIENCE = ("我试驾", "我提车", "我开过", "我坐过", "我买过", "我去了",
                          "我朋友", "我上次", "亲测")
#: 引战 / 贬损
_FORBIDDEN_ARGUE = ("垃圾", "智商税", "韭菜", "水军", "恰饭", "广子", "收钱", "胡说",
                    "瞎说", "外行", "不懂车", "黑子", "洗地", "瞎扯", "无脑", "吊打",
                    "碾压", "秒杀", "最强", "第一")
#: 中文数量词 —— 数字的另一种写法。「不写数字」这条规则要连它一起管住,
#: 否则会出现「续航超千公里」这种看着没数字、其实在复述数据的句子。
_FORBIDDEN_NUMBER_CN = ("千", "百", "万", "亿", "几", "半")
#: 分词时忽略的词
_STOP_TOKENS = {"这个", "那个", "什么", "怎么", "为什么", "视频", "作品", "一下", "真的",
                "就是", "还是", "可以", "自己", "我们", "他们", "今天", "现在"}

_TEMPLATES = (
    "{kw}这块讲得挺实在",
    "{kw}的角度少见,想看下实测",
    "{kw}的取舍说得比参数表清楚",
    "{kw}这点值得多说两句",
)

SYSTEM = """你是汽车类抖音账号的运营,要在**别人**的作品下面留一条评论。

规则(一条都不能破):
- 15 字上下,最少 {min_chars} 字,最多 {max_chars} 字;
- **不许出现任何数字**,中文数字也不行(不要写"超千公里""上百万"这种);
- 不许用绝对化用语(最强/第一/绝对/吊打…);
- 不许出现联系方式、链接、二维码、"私信我"、"关注我";
- 不许提我们自己(我们/上期/主页),不许把对方往我们这边引;
- 不许假装自己试驾过、买过那台车;
- 不许贬损、引战、说别人是水军;
- **必须说到对方作品里的一个具体词**(车型或话题),空泛的"好视频"不行;
- **别直接抄标题里的原话**,用自己的话说;
- 语气像普通观众,不是营销号,别像文案;

只输出 JSON:{{"items":[{{"text":"..."}},{{"text":"..."}},{{"text":"..."}}]}}"""


# --------------------------------------------------------------------------
# 设置与额度
# --------------------------------------------------------------------------
def enabled() -> bool:
    """活跃官总开关。"""
    value = db.get_team_setting(ENABLED_KEY, None)
    if value is None:
        return bool(config.TEAM_ACTIVE_ENABLED)
    return bool(value)


def set_enabled(value: bool) -> None:
    db.set_team_setting(ENABLED_KEY, bool(value))


def quota(action: str) -> int:
    """某个动作每天的上限。"""
    key = f"active_quota_{action}s"
    value = db.get_team_setting(key, None)
    if value is None:
        value = getattr(config, f"TEAM_ACTIVE_{action.upper()}S", None)
    if value is None:
        value = DEFAULT_QUOTAS.get(action, 0)
    return max(0, int(value))


def paused_day() -> str:
    """今天是不是已经停手了(失败 / 验证码 / 登录失效之后)。"""
    return str(db.get_team_setting(PAUSED_DAY_KEY, "") or "")


def stop_for_today(reason: str) -> None:
    """今天剩下的动作全部作废,并在页面上留一条告警。"""
    day = dt.date.today().isoformat()
    db.set_team_setting(PAUSED_DAY_KEY, day)
    db.add_event("__team__", "team", reason=f"活跃官今天停手:{reason}")


def account_paused() -> bool:
    """账号被合规审核官判定疑似限流时,活跃官跟着停。"""
    for row in db.list_account_health():
        if row.get("paused"):
            return True
    return False


# --------------------------------------------------------------------------
# 今日计划
# --------------------------------------------------------------------------
def _seed(day: str) -> int:
    """按日期取稳定的随机种子 —— 同一天生成两次,计划必须一模一样。"""
    digest = hashlib.sha256(f"AutoReel-AI-active-{day}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _spread(rng: random.Random, start_min: float, end_min: float, count: int,
            min_gap: float = 0.0) -> list[float]:
    """在 [start_min, end_min](分钟)里取 count 个时刻,相邻至少隔 min_gap 分钟。

    做法:先把 count 个时刻按 min_gap 排好,再在剩下的空档里随机撒开 ——
    这样间隔和窗口都是硬保证,不会像"加抖动"那样把点甩到窗口外。
    """
    if count <= 0:
        return []
    span = max(0.0, end_min - start_min)
    if count == 1:
        return [start_min + span * rng.uniform(0.2, 0.8)]
    gap = max(0.0, min_gap)
    free = max(0.0, span - gap * (count - 1))
    # 空的那些位置随机撒(排好序 → 间隔不塌)
    slack = sorted(rng.uniform(0.0, free) for _ in range(count)) if free else \
        [0.0] * count
    return [start_min + i * gap + slack[i] for i in range(count)]


def build_plan(day: str, quotas: dict[str, int] | None = None) -> dict[str, Any]:
    """生成当天的动作计划(确定性:同一天永远一样)。"""
    quotas = quotas or {a: quota(a) for a in (COMMENT, LIKE, SHARE, FOLLOW)}
    rng = random.Random(_seed(day))
    start_h, end_h = config.TEAM_ACTIVE_WINDOW
    day0 = dt.datetime.fromisoformat(day).replace(hour=0, minute=0, second=0, microsecond=0)
    start_min = start_h * 60
    end_min = end_h * 60

    items: list[dict[str, Any]] = []
    batch_no = 0

    # 评论:每条一个批次(评论要多想多写,别攒在一页里连着发)
    for at in _spread(rng, start_min, end_min, quotas.get(COMMENT, 0),
                      float(config.TEAM_ACTIVE_COMMENT_GAP)):
        batch_no += 1
        items.append({"at": at, "action": COMMENT, "batch": batch_no})

    # 点赞:每 3 个一批,同一批一次浏览器会话做完
    like_times = _spread(rng, start_min, end_min, quotas.get(LIKE, 0))
    like_batch = 0
    for i, at in enumerate(like_times):
        if i % 3 == 0:
            batch_no += 1
            like_batch = batch_no
        items.append({"at": at, "action": LIKE, "batch": like_batch})

    # 分享:挂在某个点赞批次上,顺手做完(省一次开浏览器)
    share_times = _spread(rng, start_min, end_min, quotas.get(SHARE, 0))
    like_batches = sorted({i["batch"] for i in items if i["action"] == LIKE})
    for i, at in enumerate(share_times):
        batch = like_batches[i % len(like_batches)] if like_batches else batch_no + 1
        items.append({"at": at, "action": SHARE, "batch": batch})

    # 关注:单独一批一个(关注要打开作者主页)
    for at in _spread(rng, start_min, end_min, quotas.get(FOLLOW, 0)):
        batch_no += 1
        items.append({"at": at, "action": FOLLOW, "batch": batch_no})

    items.sort(key=lambda x: (x["at"], x["action"]))
    base = day0.timestamp()
    return {"day": day,
            "items": [{"at": base + it["at"] * 60, "action": it["action"], "batch": it["batch"],
                       "slot": f"{i}:{it['action']}"}
                      for i, it in enumerate(items)],
            "quotas": dict(quotas)}


def plan_for(day: str, *, refresh: bool = False) -> dict[str, Any]:
    """取当天的计划(没有就生成并缓存)。"""
    key = PLAN_KEY.format(day=day)
    cached = db.get_team_setting(key, None)
    if cached and not refresh:
        return cached
    plan = build_plan(day)
    db.set_team_setting(key, plan)
    return plan


# --------------------------------------------------------------------------
# 候选池
# --------------------------------------------------------------------------
def _known_cars(category: str | None = None) -> list[str]:
    try:
        from . import benchmark

        return benchmark.known_cars(category or config.TEAM_CATEGORY)
    except Exception:  # noqa: BLE001 - 认不出车型不影响别的
        return []


def _our_work_ids() -> set[str]:
    return {str(p.get("remote_id") or "") for p in db.list_publications(limit=1000)
            if p.get("remote_id")}


def acted_targets(action: str | None = None) -> set[str]:
    """已经互动过的目标。给了动作就只看那个动作的(同一作品可以点赞 + 评论)。"""
    rows = db.list_outbound_actions(actions=(action,) if action else (), limit=5000)
    return {str(r.get("target") or "") for r in rows}


def candidates(now: float | None = None, *, category: str | None = None,
               limit: int = 40, exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """可以去互动的作品:对标号近几天的新作品,排除做过的和我们自己的。

    `exclude` 是要排除的目标集合(默认:任何动作都做过的都排除)。

    同车型(我们正在做的车)排前面 —— 在同赛道上说话,别人更认得出是同行。
    """
    now = now or time.time()
    rows = db.list_benchmark_posts(since=now - CANDIDATE_DAYS * 86400, limit=300)
    ours = _our_work_ids()
    acted = acted_targets() if exclude is None else exclude
    cars = _known_cars(category)
    out = []
    for r in rows:
        if r.get("pinned"):
            continue
        if not r.get("id") or r["id"] in ours or r["id"] in acted:
            continue
        likes = int(r.get("likes") or 0)
        if likes < MIN_LIKES or likes > MAX_LIKES:
            continue
        hit = ""
        for car in cars:
            if car and car.lower() in str(r.get("title") or "").lower():
                hit = car
                break
        out.append({**r, "car_hit": hit})
    out.sort(key=lambda r: (0 if r.get("car_hit") else 1, -(r.get("posted_at") or 0)))
    return out[:limit]


def accounts_to_follow(day: str) -> list[dict[str, Any]]:
    """今天还能关注的对标号(已经关注过的不再关注;一天最多 TEAM_ACTIVE_PER_ACCOUNT 个)。"""
    accounts = [a for a in db.list_benchmark_accounts(enabled_only=True)]
    done = acted_targets(FOLLOW)
    return [a for a in accounts if a["id"] not in done]


# --------------------------------------------------------------------------
# 评论文案
# --------------------------------------------------------------------------
def _tokens(title: str) -> set[str]:
    """从对方标题里取"实义词":字母型号 + 中文二元组。带数量词的一律不要。"""
    text = re.sub(r"#\S+|@\S+", " ", str(title or ""))
    out: set[str] = set()
    for m in re.findall(r"[A-Za-z]{2,}", text):
        out.add(m.upper())
    for run in re.findall(r"[一-鿿]+", text):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    out = {t for t in out if t not in _STOP_TOKENS}
    return {t for t in out if not any(w in t for w in _FORBIDDEN_NUMBER_CN)}


def _relevant(text: str, title: str) -> bool:
    """评论里必须沾到对方作品的一个实义词,否则就是"好视频"式灌水。"""
    tokens = _tokens(title)
    if not tokens:
        return False
    upper = str(text or "").upper()
    return any(tok in upper for tok in tokens)


def check_text(text: str, title: str, terms: tuple[str, ...] = ()) -> list[str]:
    """评论文案的硬校验。返回问题列表,空列表 = 通过。"""
    from ..pipeline import packaging

    text = str(text or "").strip()
    problems: list[str] = []
    if not text:
        return ["空评论"]
    length = packaging.spoken_len(text)
    if length < MIN_CHARS:
        problems.append(f"太短({length} 字,至少 {MIN_CHARS})")
    if length > MAX_CHARS:
        problems.append(f"太长({length} 字,最多 {MAX_CHARS})")
    if packaging.figures_in(text):
        problems.append("有数字(对方的数据我们没核实过)")
    for word in _FORBIDDEN_NUMBER_CN:
        if word in text:
            problems.append(f"有中文数量词({word})(等于在复述对方的数据)")
            break
    hits = packaging.forbidden_hits(text, terms)
    if hits:
        problems.append("命中禁用词:" + "、".join(hits))
    for word in _FORBIDDEN_AD:
        if word in text:
            problems.append(f"像引流({word})")
            break
    for word in _FORBIDDEN_SELF:
        if word in text:
            problems.append(f"提我们自己({word})")
            break
    for word in _FORBIDDEN_EXPERIENCE:
        if word in text:
            problems.append(f"假装有亲身体验({word})")
            break
    for word in _FORBIDDEN_ARGUE:
        if word in text:
            problems.append(f"引战 / 贬损({word})")
            break
    if not _relevant(text, title):
        problems.append("跟对方作品没关系")
    return problems


def _keyword_of(title: str, cars: list[str]) -> str:
    """兜底模板要用的那个词:优先车型名,其次字母型号,再次中文二元组。"""
    for car in cars:
        if car and car.lower() in str(title or "").lower():
            return car
    tokens = sorted(_tokens(title), key=lambda t: (-len(t), t))
    return tokens[0] if tokens else ""


def fallback_texts(title: str, cars: list[str] | None = None) -> list[str]:
    """LLM 不可用时的兜底文案(同样过全部硬规则)。"""
    kw = _keyword_of(title, cars or [])
    if not kw:
        return []
    return [tpl.format(kw=kw) for tpl in _TEMPLATES]


def llm_texts(title: str, llm: Any, cars: list[str] | None = None) -> list[str]:
    """让 LLM 出 3 条候选(**不校验**,校验交给调用方)。"""
    if llm is None:
        return []
    kw = _keyword_of(title, cars or [])
    user = f"对方作品标题:{title}\n"
    if kw:
        user += f"必须用到的词(至少一个):{kw}\n"
    user += "给 3 条候选。"
    try:
        data = llm.structured(SYSTEM.format(min_chars=MIN_CHARS, max_chars=MAX_CHARS), user,
                              required_keys=["items"], temperature=0.6)
    except Exception as exc:  # noqa: BLE001 - LLM 不行就用模板
        log.warning("活跃官拟评论失败,用模板:%s", exc)
        return []
    out: list[str] = []
    for item in (data.get("items") or []) if isinstance(data, dict) else []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def pick_text(title: str, llm: Any = None, cars: list[str] | None = None,
              terms: tuple[str, ...] = ()) -> str:
    """挑一条能用的评论文案。都没有 → 返回空串(这次评论跳过,不硬凑)。"""
    for text in llm_texts(title, llm, cars) + fallback_texts(title, cars):
        if not check_text(text, title, terms):
            return text
    return ""


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def _day(now: float | None = None) -> str:
    return dt.date.fromtimestamp(now or time.time()).isoformat()


def _terms(category: str | None = None) -> tuple[str, ...]:
    """品类禁用词 + 广告法绝对化用语。"""
    from ..categories import load_profile
    from ..pipeline.script_gen import AD_ABSOLUTE_TERMS

    try:
        profile = load_profile(category or config.TEAM_CATEGORY)
        return tuple(dict.fromkeys((*profile.forbidden_terms, *AD_ABSOLUTE_TERMS)))
    except Exception:  # noqa: BLE001
        return tuple(AD_ABSOLUTE_TERMS)


def used_today(day: str, action: str) -> int:
    return db.count_outbound(day, action, states=(DONE,))


def skipped_slots(day: str) -> set[str]:
    """当天已经放弃的计划槽位。"""
    return {str(s) for s in (db.get_team_setting(SKIPPED_KEY.format(day=day), []) or [])}


def mark_skipped(day: str, slot: str) -> None:
    """记下"这条计划项今天做不成了",别一轮一轮地重挑它。"""
    if not slot:
        return
    key = SKIPPED_KEY.format(day=day)
    cur = [str(s) for s in (db.get_team_setting(key, []) or [])]
    if slot in cur:
        return
    cur.append(slot)
    db.set_team_setting(key, cur[-500:])


def _per_account_ok(day: str, account_id: str) -> bool:
    """同一个对标号一天最多互动 TEAM_ACTIVE_PER_ACCOUNT 次。"""
    if not account_id:
        return True
    rows = db.list_outbound_actions(day=day, states=(DONE,), limit=500)
    n = sum(1 for r in rows if str(r.get("sec_uid") or "") == account_id)
    return n < int(config.TEAM_ACTIVE_PER_ACCOUNT)


def _targets_for(day: str, action: str, now: float, category: str | None,
                 used: set[str] | None = None) -> list[dict[str, Any]]:
    """这个动作能用的目标(作品或作者)。

    `used` 是这一批里已经挑走的目标 —— 同一批里的三个赞不能全点同一个视频
    (那另外两个会被唯一索引挡掉,白跑一趟)。
    """
    used = used or set()
    if action == FOLLOW:
        return [{"kind": "account", "sec_uid": a["id"], "title": a.get("nickname") or a["id"],
                 "aweme_id": "", "account_id": a["id"]}
                for a in accounts_to_follow(day) if a["id"] not in used]
    # 只排除"这个动作已经做过"的作品:同一作品可以点赞 + 评论
    pool = candidates(now, category=category, exclude=acted_targets(action))
    out = []
    for post in pool:
        if post["id"] in used:
            continue
        # 对标作品里的 account_id 就是作者的 sec_uid —— 拿它做"同一号一天最多几次"
        account_id = str(post.get("account_id") or "")
        if not _per_account_ok(day, account_id):
            continue
        out.append({"kind": "post", "aweme_id": post["id"], "sec_uid": account_id,
                    "title": post.get("title") or "", "account_id": account_id,
                    "car": post.get("car") or "", "likes": post.get("likes") or 0})
    return out


def _record(day: str, action: str, target: dict[str, Any], text: str = "",
            slot: str = "") -> str:
    """建一条 planned 记录(唯一索引会挡住重复的)。返回动作 id,重复时返回空串。

    `slot` 是当天计划里第几条 —— 有了它,「这批做完了」才认得出来,
    否则角色会一直挑同一个批次,当天的计划只跑得完第一批。
    """
    from ..models import new_id

    target_id = target.get("aweme_id") or target.get("sec_uid") or ""
    n = db.add_outbound_actions([{
        "id": new_id("oa_"), "day": day, "slot": slot, "aweme_id": target.get("aweme_id") or "",
        "sec_uid": target.get("sec_uid") or "", "target": target_id,
        "author": target.get("author") or "", "title": str(target.get("title") or "")[:120],
        "action": action, "text": text, "state": PLANNED,
    }])
    if not n:
        return ""
    rows = db.list_outbound_actions(target=target_id, actions=(action,), limit=1)
    return rows[0]["id"] if rows else ""


def execute_batch(items: list[dict[str, Any]], day: str, now: float,
                  llm: Any = None, category: str | None = None) -> dict[str, Any]:
    """执行一个批次里的动作。返回 {"done", "failed", "skipped", "notes"}。"""
    from ..publish.base import PublishError
    from ..publish.platforms.douyin_outreach import VerificationNeeded
    from ..publish.platforms import douyin_outreach

    out = {"done": 0, "failed": 0, "skipped": 0, "notes": []}
    cars = _known_cars(category)
    terms = _terms(category)

    # 1) 先把这一批的动作都定下来(目标 + 文案),落 planned
    planned: list[dict[str, Any]] = []
    used: dict[str, set[str]] = {}
    for item in items:
        action = str(item.get("action") or "")
        slot = str(item.get("slot") or "")
        if used_today(day, action) >= quota(action):
            out["skipped"] += 1
            out["notes"].append(f"{action} 额度用完了")
            mark_skipped(day, slot)
            continue
        targets = _targets_for(day, action, now, category, used.get(action))
        if not targets:
            out["skipped"] += 1
            out["notes"].append(f"{action} 没有可用目标")
            mark_skipped(day, slot)
            continue
        target = targets[0]
        used.setdefault(action, set()).add(target.get("aweme_id") or target.get("sec_uid") or "")
        text = ""
        if action == COMMENT:
            text = pick_text(str(target.get("title") or ""), llm, cars, terms)
            if not text:
                out["skipped"] += 1
                out["notes"].append("没有合格的评论文案,这次跳过")
                mark_skipped(day, slot)
                continue
        action_id = _record(day, action, target, text, slot)
        if not action_id:
            out["skipped"] += 1
            mark_skipped(day, slot)
            continue
        planned.append({"id": action_id, "action": action, "aweme_id": target.get("aweme_id") or "",
                        "sec_uid": target.get("sec_uid") or "", "text": text,
                        "slot": str(item.get("slot") or "")})

    if not planned:
        return out

    # 2) 逐条抢锁 + 执行
    def on_done(job: dict[str, Any], error: str | None) -> None:
        if error:
            db.cas_outbound_state(job["id"], (DOING,), FAILED, reason=error[:200],
                                  done_at=time.time())
            out["failed"] += 1
            out["notes"].append(error[:60])
        else:
            db.cas_outbound_state(job["id"], (DOING,), DONE, done_at=time.time())
            out["done"] += 1

    jobs = []
    for row in planned:
        if db.cas_outbound_state(row["id"], (PLANNED,), DOING, started_at=time.time()):
            jobs.append(row)
    if not jobs:
        return out
    try:
        douyin_outreach.run_actions(jobs, on_done)
    except VerificationNeeded:
        # 二次验证 / 验证码:当天停手,等人处理。不当普通失败吞掉 ——
        # 停手的原因要写清楚。
        for job in jobs:
            if (db.get_outbound_action(job["id"]) or {}).get("state") == DOING:
                db.cas_outbound_state(job["id"], (DOING,), FAILED, reason="撞上二次验证",
                                      done_at=time.time())
                out["failed"] += 1
        raise
    except PublishError as exc:
        for job in jobs:
            if (db.get_outbound_action(job["id"]) or {}).get("state") == DOING:
                db.cas_outbound_state(job["id"], (DOING,), FAILED, reason=exc.message[:200],
                                      done_at=time.time())
                out["failed"] += 1
                out["notes"].append(exc.message[:60])
        if exc.credential:
            raise  # 登录失效要往上抛,让 run() 记登录态
    if out["failed"]:
        stop_for_today("有个动作失败,今天剩下的不做了")
    return out


def run(now: float | None = None) -> str:
    """角色入口:到点就做一个批次。"""
    from ..publish.base import PublishError
    from ..publish.platforms.douyin_outreach import VerificationNeeded
    from ..pipeline import orchestrator

    now = now or time.time()
    day = _day(now)
    if not enabled():
        raise Skip("活跃官已关")
    if paused_day() == day:
        raise Skip("今天已经停手了")
    if account_paused():
        raise Skip("账号被暂停了(疑似限流),活跃官跟着停")
    if not db.list_benchmark_accounts(enabled_only=True):
        raise Skip("还没有对标账号:在 /team 页加几个")
    # 卡在 doing 的(上次进程崩了)退回 planned,好重排
    db.reset_stuck_outbound(config.TEAM_ACTIVE_STUCK_MINUTES * 60, now)

    plan = plan_for(day)
    due = [it for it in plan["items"] if float(it.get("at") or 0) <= now]
    if not due:
        raise Skip("没有到点的互动")
    # 计划里已经做过的槽位不算 —— 不然会一直挑同一个批次,当天的计划只跑得完第一批
    done_slots = {str(r.get("slot") or "") for r in
                  db.list_outbound_actions(day=day, limit=1000) if r.get("slot")}
    done_slots |= skipped_slots(day)
    pending = [it for it in due if str(it.get("slot") or "") not in done_slots]
    if not pending:
        raise Skip("到点的都做过了")
    batch = int(pending[0]["batch"])
    items = [it for it in pending if int(it["batch"]) == batch]
    try:
        out = execute_batch(items, day, now, llm=orchestrator._llm())
    except VerificationNeeded as exc:
        stop_for_today(str(exc))
        raise TeamError(str(exc)) from exc
    except PublishError as exc:
        if exc.credential:
            db.set_team_setting(LOGIN_BAD_KEY, now)
            stop_for_today("登录失效了")
        raise TeamError(exc.message) from exc
    if not out["done"] and not out["failed"]:
        raise Skip("这一批没什么可做的")
    msg = (f"互动 {out['done']} 次"
           + (f",{out['failed']} 次失败" if out["failed"] else "")
           + (f",{out['skipped']} 次跳过" if out["skipped"] else ""))
    if out["notes"]:
        msg += ";" + ";".join(out["notes"][:3])
    return msg


# --------------------------------------------------------------------------
# 面板数据
# --------------------------------------------------------------------------
def stats(now: float | None = None, days: int = 1) -> dict[str, Any]:
    """今日额度 / 已用 / 留痕(页面用)。"""
    now = now or time.time()
    day = _day(now)
    rows = db.list_outbound_actions(day=day, limit=500)
    counts: dict[str, int] = {}
    for action in (COMMENT, LIKE, SHARE, FOLLOW):
        counts[action] = sum(1 for r in rows if r["action"] == action and r["state"] == DONE)
    plan = plan_for(day)
    effect_state = db.get_team_setting(EFFECT_KEY, {}) or {}
    return {
        "day": day,
        # 每周量一次的效果:最近一周的结论 + 连续几周看不出效果
        "effect": {"last": (effect_state.get("history") or [None])[-1],
                   "streak": int(effect_state.get("streak") or 0), "limit": EFFECT_STREAK},
        "enabled": enabled(),
        "paused": paused_day() == day,
        "quotas": {a: quota(a) for a in (COMMENT, LIKE, SHARE, FOLLOW)},
        "counts": counts,
        "failed": sum(1 for r in rows if r["state"] == FAILED),
        "pending": sum(1 for r in rows if r["state"] in (PLANNED, DOING)),
        "planned": len(plan.get("items") or []),
        "items": [dict(r, action_label=_label(r["action"])) for r in rows[:30]],
    }


def _label(action: str) -> str:
    return {COMMENT: "评论", LIKE: "点赞", SHARE: "分享", FOLLOW: "关注"}.get(action, action)


# --------------------------------------------------------------------------
# 效果:互动多的日子,涨粉有没有多(复盘官每周量一次)
# --------------------------------------------------------------------------
#: 看最近几天
EFFECT_DAYS = 28
#: 互动多 / 少两组各至少这么多天才比
EFFECT_MIN_DAYS = 4
#: 互动多的日子平均涨粉比少的日子多这么多(且至少多 1 个)才算有用
EFFECT_MIN_GAIN = 0.2
#: 连续这么多周「看不出效果」就把额度减半
EFFECT_STREAK = 4
EFFECT_KEY = "active_effect"


def effect(now: float | None = None) -> dict[str, Any]:
    """近 `EFFECT_DAYS` 天每天做完的互动数 vs 当天到第二天的涨粉(第二天的粉丝快照 − 当天的)。

    按互动数的中位数分两组:多于中位数的是「互动多」,其余是「互动少」。
    返回 {verdict: effective / none / unknown, high, low: {days, actions, gain}, days, why}。
    天天都做满额度(两组拉不开)、快照不够,都是 unknown —— 比不出来不等于没用。"""
    now = now or time.time()
    today = dt.date.fromtimestamp(now)
    start = today - dt.timedelta(days=EFFECT_DAYS)
    followers = db.list_account_daily("douyin", start.isoformat())
    done = db.list_outbound_actions(states=(DONE,), since=now - (EFFECT_DAYS + 2) * 86400, limit=10000)
    acts: dict[str, int] = {}
    for r in done:
        acts[r["day"]] = acts.get(r["day"], 0) + 1
    days = []
    for i in range(EFFECT_DAYS):
        d = start + dt.timedelta(days=i)
        nxt = (d + dt.timedelta(days=1)).isoformat()
        if d.isoformat() in followers and nxt in followers:
            days.append({"day": d.isoformat(), "actions": acts.get(d.isoformat(), 0),
                         "gain": followers[nxt] - followers[d.isoformat()]})
    out: dict[str, Any] = {"verdict": "unknown", "days": len(days), "high": None, "low": None}
    if len(days) < 2 * EFFECT_MIN_DAYS:
        return {**out, "why": f"粉丝快照只有 {len(days)} 天能算涨粉,至少要 {2 * EFFECT_MIN_DAYS} 天"}
    mid = statistics.median(d["actions"] for d in days)
    high = [d for d in days if d["actions"] > mid]
    low = [d for d in days if d["actions"] <= mid]

    def group(xs: list[dict[str, Any]]) -> dict[str, Any]:
        return {"days": len(xs), "actions": round(statistics.fmean(d["actions"] for d in xs), 1),
                "gain": round(statistics.fmean(d["gain"] for d in xs), 2)}

    if len(high) < EFFECT_MIN_DAYS or len(low) < EFFECT_MIN_DAYS:
        return {**out, "why": "每天的互动量差不多,分不出多和少的日子"}
    hi, lo = group(high), group(low)
    better = hi["gain"] - lo["gain"]
    verdict = "effective" if better >= max(1.0, EFFECT_MIN_GAIN * abs(lo["gain"])) else "none"
    return {**out, "verdict": verdict, "high": hi, "low": lo,
            "why": (f"互动多的 {hi['days']} 天(平均 {hi['actions']:g} 次)平均涨粉 {hi['gain']:g},"
                    f"互动少的 {lo['days']} 天(平均 {lo['actions']:g} 次)平均涨粉 {lo['gain']:g}")}


def weekly_effect(now: float | None = None, week: str = "") -> dict[str, Any]:
    """复盘官每周调一次:量效果,记进历史;连续 `EFFECT_STREAK` 周「看不出效果」(none)就把
    各动作额度减半(至少留 1)、写变更日志,计数清零重来。有效果维持原额度,**不会自动加**;
    unknown(比不出来)不算进连续周数,也不清零。同一周重复跑只算一次。"""
    now = now or time.time()
    got = effect(now)
    state = db.get_team_setting(EFFECT_KEY, {}) or {}
    history = list(state.get("history") or [])
    streak = int(state.get("streak") or 0)
    if not history or history[-1].get("week") != week:
        if got["verdict"] == "none":
            streak += 1
        elif got["verdict"] == "effective":
            streak = 0
        history.append({"week": week, "verdict": got["verdict"], "why": got.get("why", "")})
    halved: dict[str, list[int]] = {}
    if streak >= EFFECT_STREAK:
        for action in DEFAULT_QUOTAS:
            old = quota(action)
            new = max(1, old // 2) if old else 0
            if new != old:
                db.set_team_setting(f"active_quota_{action}s", new)
                halved[action] = [old, new]
        streak = 0
        if halved:
            db.add_iteration_log(
                "param", f"活跃官连续 {EFFECT_STREAK} 周看不出涨粉效果,额度减半:"
                + "、".join(f"{_label(a)} {o}→{n}" for a, (o, n) in halved.items()),
                {"halved": halved, "why": got.get("why", "")}, ts=now)
    db.set_team_setting(EFFECT_KEY, {"history": history[-12:], "streak": streak})
    return {**got, "streak": streak, "halved": halved, "streak_limit": EFFECT_STREAK}


def effect_lines(e: dict[str, Any] | None) -> list[str]:
    """周报的一行。"""
    if not e:
        return []
    verdict = {"effective": "有效果(维持额度)", "none": "看不出效果",
               "unknown": "比不出来"}.get(e["verdict"], e["verdict"])
    line = f"活跃官效果:{verdict};{e.get('why', '')}"
    if e["verdict"] == "none":
        line += f";已连续 {e.get('streak', 0)}/{e.get('streak_limit', EFFECT_STREAK)} 周"
    if e.get("halved"):
        line += ";额度已减半:" + "、".join(f"{_label(a)} {o}→{n}" for a, (o, n) in e["halved"].items())
    return [line]
