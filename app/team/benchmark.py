"""对标分析员:每天看一遍你在 /team 维护的对标账号主页,记下每条作品的数据和爆款倍数。

**爆款倍数 = 这条点赞 / 这个号近期点赞中位数。** 号与号体量差很多,绝对数没法比;
和它自己比才看得出哪条「出圈」了。置顶作品不进中位数(常年挂着,数虚高),
作品少于 `MIN_POSTS` 条的号不算倍数。点赞比播放靠谱:主页接口的播放数常是 0。

**用在三处。** 主编排选题时拿「对标爆款」当证据,车型命中的加热度;复盘官的周报里
把对标爆款的标题开头和我们自己最好 / 最差的标题并排放;复盘官还把对标作品按标题写法、
时长、车型、开场画面、剪辑节奏算成**先验**(`stats.learn_benchmark`),自己样本不够的
取值先照对标号的规律轻推一把。

**拆爆款结构(`tag_posts`):** 每轮看完主页后,挑几条新抓到的爆款和几条普通作品当对照,
ffmpeg 只读前 3 秒(每秒 2 帧)和封面,本地比相邻帧算切换快慢,再让视觉模型标开场画面类型、
有没有判断句、封面类型。播放地址带签名几小时就过期,所以只拆刚抓到的;失败也记一行,不反复试。

**反爬:** 只读公开主页、一次只开一个无头浏览器、号与号之间停几秒、每号只翻一屏;
任何一个号失败就记下原因、本轮停手,不重试。登录失效等你重新扫码后才再开浏览器。
"""
from __future__ import annotations

import logging
import re
import statistics
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .. import db
from . import Skip, TeamError

log = logging.getLogger(__name__)

#: 作品少于这么多条不算爆款倍数(中位数没意义)
MIN_POSTS = 5
#: 倍数到这个算爆款
BURST_RATIO = 3.0
#: 同一个号多久重抓一次
REFRESH_HOURS = 20
#: 一轮最多看几个号
MAX_PER_RUN = 20
#: 号与号之间停几秒
GAP_SECONDS = 8
#: 主编 / 复盘官看多少天内的对标爆款
HIT_DAYS = 14
#: 账号上限(计划里说 10–20 个)
MAX_ACCOUNTS = 30

_LOGIN_KEY = "bench_login_bad"
#: 拆结构:每轮最多拆几条爆款(倍数 ≥ TAG_MIN_BURST)+ 几条普通作品当对照
TAG_HITS, TAG_CONTROLS = 5, 3
TAG_MIN_BURST = 2.0
CONTROL_MAX_BURST = 1.2
#: 播放地址带签名:抓到后这么久内才去拆
URL_FRESH_HOURS = 12
#: 先验 / 结构对照看多久的对标作品
PRIOR_DAYS = 60
#: 相邻两帧 dHash 差这么多位(共 64 位)算一次切换
CUT_BITS = 20
OPENING_LABEL = {"car": "实拍车", "person": "人出镜", "text": "大字", "data": "数据"}
COVER_LABEL = {"text": "大字封面", "car": "车图封面", "person": "人像封面"}
#: 开场画面类型 → 我们的 `hook_visual` 旋钮(人出镜我们做不了,不映射)
OPENING_TO_HOOK = {"text": "hook", "data": "highlight", "car": "broll"}
HOOK_TO_OPENING = {v: k for k, v in OPENING_TO_HOOK.items()}
TAG_PROMPT = """前几张图是一条抖音汽车视频前 3 秒的画面,最后一张是它的封面。只输出 JSON:
{"opening": "car|person|text|data", "claim": true, "cover": "text|car|person"}
opening:开场画面主要是什么 —— car 实拍车,person 人出镜讲,text 屏幕大字,data 数据图表 / 参数卡。
claim:开场画面或字幕里有没有一句明确的判断(如「别买」「它赢了」「最值的一款」),有填 true。
cover:封面主体 —— text 大字为主,car 车图为主,person 人像为主。"""
USER_URL = "https://www.douyin.com/user/"


class BenchError(ValueError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"[\s\-_·()（）]+", "", str(text or "")).lower()


# --------------------------------------------------------------------------
# 纯函数
# --------------------------------------------------------------------------
def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def known_cars(category: str) -> list[str]:
    """认车型用的候选名:最新一批热点里的车 + 团队排过的车。长名在前(先认「小米YU7」再认「小米」)。"""
    from . import scout

    names: set[str] = set()
    cur = scout.latest(category)
    if cur:
        names.update(r["car"] for r in db.trend_rows(cur["batch"]) if r.get("car"))
    for item in db.list_plan_items(category, limit=300):
        names.add(item["subject"])
        names.update(item.get("subjects") or [])
    names.discard("")
    return sorted(names, key=lambda n: -len(_norm(n)))


def car_in(title: str, cars: list[str]) -> str:
    """标题里提到的车。全名命中优先;再看车名里的型号段(YU7 / Model Y 这类 ≥3 个字符的字母数字)。"""
    t = _norm(title)
    for name in cars:
        if _norm(name) and _norm(name) in t:
            return name
    for name in cars:
        for code in re.findall(r"[a-z0-9]{3,}", _norm(name)):
            if not code.isdigit() and re.search(rf"(?<![a-z0-9]){code}(?![a-z0-9])", t):
                return name
    return ""


def parse_post(a: dict[str, Any], account_id: str, cars: list[str]) -> dict[str, Any] | None:
    aid = str(a.get("aweme_id") or "")
    if not aid:
        return None
    s = a.get("statistics") or {}
    title = str(a.get("desc") or "").strip()
    video = a.get("video") or {}
    dur = _int(a.get("duration") or video.get("duration"))
    return {
        "id": aid, "account_id": account_id, "title": title[:200], "car": car_in(title, cars),
        "duration": round(dur / 1000, 1) if dur > 1000 else float(dur),
        "posted_at": float(_int(a.get("create_time"))),
        "views": _int(s.get("play_count")), "likes": _int(s.get("digg_count")),
        "comments": _int(s.get("comment_count")), "shares": _int(s.get("share_count")),
        "collects": _int(s.get("collect_count")), "pinned": 1 if a.get("is_top") else 0,
        "video_url": _first_url(video.get("play_addr")), "cover_url": _first_url(video.get("cover")),
    }


def _first_url(addr: Any) -> str:
    urls = (addr or {}).get("url_list") if isinstance(addr, dict) else None
    return str(urls[0]) if urls else ""


def with_burst(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    """给每条作品算爆款倍数,返回 (作品, 点赞中位数)。作品太少的号倍数全记 0。"""
    base = [r["likes"] for r in rows if not r["pinned"]]
    if len(base) < MIN_POSTS:
        return [{**r, "burst": 0.0} for r in rows], 0.0
    median = max(1.0, float(statistics.median(base)))
    return [{**r, "burst": round(r["likes"] / median, 2)} for r in rows], median


def opening(title: str, n: int = 14) -> str:
    """标题开头:去掉话题标签和 @,取前 n 个字。周报里比开头写法用。"""
    t = re.sub(r"#\S+|@\S+", "", title or "").strip()
    return t[:n]


# --------------------------------------------------------------------------
# 账号维护
# --------------------------------------------------------------------------
def add_account(target: str, note: str = "", *,
                fetch: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """加对标账号。主页链接直接取 sec_uid;短链要开一次浏览器解析(顺便把作品抓了)。"""
    from ..publish.platforms import douyin

    target = (target or "").strip()
    if not target:
        raise BenchError("贴一个抖音主页链接或分享链接")
    if len(db.list_benchmark_accounts()) >= MAX_ACCOUNTS:
        raise BenchError(f"对标账号最多 {MAX_ACCOUNTS} 个,先删掉不看的")
    sec = douyin.sec_uid_of(target)
    if not sec:
        m = re.search(r"https?://\S+", target)
        if not m:
            raise BenchError("看不懂这个链接:贴抖音主页链接(douyin.com/user/…)或分享短链")
        got = _fetch_one(m.group(0), fetch)
        sec = got["sec_uid"]
        if not db.add_benchmark_account(sec, url=m.group(0), note=note[:60]):
            raise BenchError("这个号已经在对标列表里了")
        _store(sec, got)
        return db.get_benchmark_account(sec)
    if not db.add_benchmark_account(sec, url=USER_URL + sec, note=note[:60]):
        raise BenchError("这个号已经在对标列表里了")
    return db.get_benchmark_account(sec)



def set_enabled(sec_uid: str, enabled: bool) -> None:
    if not db.get_benchmark_account(sec_uid):
        raise BenchError("没有这个对标账号")
    db.update_benchmark_account(sec_uid, enabled=1 if enabled else 0)


def remove_account(sec_uid: str) -> None:
    db.delete_benchmark_account(sec_uid)


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------
def _login_blocked() -> bool:
    from ..publish import browser

    bad = db.get_team_setting(_LOGIN_KEY, None)
    if not bad:
        return False
    path = browser.state_path("douyin")
    return not (path.is_file() and path.stat().st_mtime > float(bad))


def _fetch_one(target: str, fetch: Callable[[str], dict[str, Any]] | None) -> dict[str, Any]:
    from ..publish.base import PublishError
    from ..publish.platforms import douyin

    try:
        return (fetch or douyin.fetch_profile_posts)(target)
    except PublishError as exc:
        if exc.credential:
            db.set_team_setting(_LOGIN_KEY, time.time())
        raise BenchError(str(exc)) from exc


def _store(sec: str, got: dict[str, Any], category: str | None = None) -> dict[str, Any]:
    from .. import config

    cars = known_cars(category or config.TEAM_CATEGORY)
    rows = [r for r in (parse_post(a, sec, cars) for a in got.get("awemes") or []) if r]
    rows, median = with_burst(rows)
    added = db.upsert_benchmark_posts(rows)
    fields: dict[str, Any] = {"median_likes": median, "last_fetched_at": time.time(), "last_error": ""}
    if got.get("nickname"):
        fields["nickname"] = got["nickname"][:40]
    db.update_benchmark_account(sec, **fields)
    return {"added": added, "hits": sum(1 for r in rows if r["burst"] >= BURST_RATIO)}


def due_accounts(now: float | None = None) -> list[dict[str, Any]]:
    """该抓的号:没抓过的先,再按上次抓的时间从早到晚。"""
    now = now or time.time()
    rows = [a for a in db.list_benchmark_accounts(enabled_only=True)
            if not a["last_fetched_at"] or now - a["last_fetched_at"] >= REFRESH_HOURS * 3600]
    return sorted(rows, key=lambda a: a["last_fetched_at"] or 0)[:MAX_PER_RUN]


def collect(now: float | None = None, *, fetch: Callable[[str], dict[str, Any]] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """抓一轮。任何一个号失败就记下、停手(不连着试别的号,免得风控升级)。"""
    accounts = due_accounts(now)
    out = {"accounts": 0, "added": 0, "hits": 0, "error": ""}
    for i, acc in enumerate(accounts):
        if i:
            sleep(GAP_SECONDS)
        try:
            got = _fetch_one(acc["id"], fetch)
        except BenchError as exc:
            # 失败的号也记上抓取时间,排到队尾,别让一个死号每次都堵在最前面
            db.update_benchmark_account(acc["id"], last_error=str(exc)[:200],
                                        last_fetched_at=time.time())
            out["error"] = f"{acc['nickname'] or acc['id'][:12]}:{exc}"
            break
        r = _store(acc["id"], got)
        out["accounts"] += 1
        out["added"] += r["added"]
        out["hits"] += r["hits"]
    return out


# --------------------------------------------------------------------------
# 给主编 / 复盘官 / 页面
# --------------------------------------------------------------------------
def hits(now: float | None = None, days: int = HIT_DAYS, limit: int = 20) -> list[dict[str, Any]]:
    """近期对标爆款,倍数高的在前,带账号昵称。"""
    now = now or time.time()
    names = {a["id"]: a["nickname"] or a["note"] or a["id"][:12]
             for a in db.list_benchmark_accounts()}
    rows = db.list_benchmark_posts(since=now - days * 86400, min_burst=BURST_RATIO, limit=200)
    rows = [{**r, "account": names.get(r["account_id"], "")} for r in rows
            if r["account_id"] in names]
    return sorted(rows, key=lambda r: -r["burst"])[:limit]


def hot_cars(now: float | None = None) -> set[str]:
    """对标爆款里出现的车(归一化),主编给命中的选题加热度。"""
    return {_norm(r["car"]) for r in hits(now) if r["car"]}


def prompt_text(now: float | None = None, limit: int = 8) -> str:
    rows = hits(now, limit=limit)
    if not rows:
        return ""
    lines = [f"- 《{r['title'][:40]}》({r['account']},点赞 {r['likes']},是该号平常的 {r['burst']:g} 倍"
             + (f",车型 {r['car']}" if r["car"] else "") + ")" for r in rows]
    return (f"对标账号近 {HIT_DAYS} 天的爆款(可借鉴车型和角度,不要照抄标题):\n"
            + "\n".join(lines))


def evidence_for(car: str, now: float | None = None) -> list[str]:
    key = _norm(car)
    return [f"对标爆款:《{r['title'][:40]}》{r['account']} 点赞 {r['likes']}(×{r['burst']:g})"
            for r in hits(now) if r["car"] and _norm(r["car"]) == key][:3]


def title_compare(our_top: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """周报用:对标爆款的标题开头 vs 我们最好的几条标题开头。"""
    rows = hits(now, limit=6)
    return {
        "theirs": [{"opening": opening(r["title"]), "title": r["title"][:60], "account": r["account"],
                    "burst": r["burst"], "duration": r["duration"]} for r in rows],
        "ours": [{"opening": opening(w.get("title") or ""), "title": (w.get("title") or "")[:60],
                  "lift": w.get("lift")} for w in our_top],
        "their_avg_duration": round(statistics.fmean([r["duration"] for r in rows]), 1) if rows else None,
    }


# --------------------------------------------------------------------------
# 拆爆款结构
# --------------------------------------------------------------------------
def _grab(video_url: str, cover_url: str, workdir: Path) -> tuple[list[Path], Path | None]:
    """ffmpeg 只读前 3 秒,每秒 2 帧(最多 6 张),再取封面。"""
    from .. import config

    ffmpeg = str(config.FFMPEG_PATH or "ffmpeg")

    def run(src: str, *args: str, timeout: int) -> None:
        # 抖音 CDN 不带 Referer 会 403;本地文件不认 -headers
        head = (["-headers", "Referer: https://www.douyin.com/\r\nUser-Agent: Mozilla/5.0\r\n"]
                if src.startswith("http") else [])
        proc = subprocess.run([ffmpeg, "-y", "-loglevel", "error", *head, *args],
                              capture_output=True, timeout=timeout)
        if proc.returncode:
            err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise TeamError(f"ffmpeg 失败:{err[-1] if err else proc.returncode}")

    run(video_url, "-t", "3", "-i", video_url, "-vf", "fps=2,scale=480:-2", "-frames:v", "6",
        str(workdir / "f_%d.jpg"), timeout=90)
    frames = sorted(workdir.glob("f_*.jpg"), key=lambda p: int(p.stem[2:]))
    cover = None
    if cover_url:
        out = workdir / "cover.jpg"
        try:
            run(cover_url, "-i", cover_url, "-frames:v", "1", str(out), timeout=60)
            cover = out if out.is_file() else None
        except (TeamError, subprocess.SubprocessError, OSError) as exc:
            log.info("对标封面没取到:%s", exc)
    return frames, cover


def _dhash(path: Path) -> list[bool]:
    from PIL import Image

    with Image.open(path) as im:
        px = list(im.convert("L").resize((9, 8)).tobytes())
    return [px[r * 9 + c] > px[r * 9 + c + 1] for r in range(8) for c in range(8)]


def cut_speed(frames: list[Path]) -> str:
    """前 3 秒相邻帧有几次大变化:≥3 次 fast,1–2 次 mid,没有 slow(对上 `pace` 旋钮)。"""
    if len(frames) < 2:
        return ""
    hs = [_dhash(f) for f in frames]
    cuts = sum(1 for a, b in zip(hs, hs[1:]) if sum(x != y for x, y in zip(a, b)) > CUT_BITS)
    return "fast" if cuts >= 3 else "mid" if cuts else "slow"


def _ask(paths: list[Path]) -> dict[str, Any]:
    from ..llm.base import extract_json
    from ..providers import cli, images

    text = images.describe([cli.upload_temp_url(p) for p in paths], TAG_PROMPT)
    if not text:
        raise TeamError("视觉模型没回话")
    return extract_json(text)


def clean_tags(got: dict[str, Any]) -> dict[str, Any]:
    opening = str(got.get("opening") or "").strip().lower()
    cover = str(got.get("cover") or "").strip().lower()
    claim = got.get("claim")
    return {"opening": opening if opening in OPENING_LABEL else "",
            "cover": cover if cover in COVER_LABEL else "",
            "claim": claim is True or str(claim).lower() in ("true", "1", "yes", "有")}


def tag_candidates(now: float | None = None) -> list[dict[str, Any]]:
    """这轮拆哪些:刚抓到、有播放地址、还没拆过的爆款(倍数高的先)+ 几条普通作品。"""
    now = now or time.time()
    done = db.list_benchmark_tags()
    fresh = [p for p in db.list_benchmark_posts(since=now - HIT_DAYS * 86400, limit=400)
             if p["video_url"] and not p["pinned"] and p["id"] not in done
             and p["fetched_at"] >= now - URL_FRESH_HOURS * 3600]
    top = sorted((p for p in fresh if p["burst"] >= TAG_MIN_BURST), key=lambda p: -p["burst"])
    controls = [p for p in fresh if 0 < p["burst"] < CONTROL_MAX_BURST]
    return top[:TAG_HITS] + controls[:TAG_CONTROLS]


def tag_posts(now: float | None = None, *,
              grab: Callable[[str, str, Path], tuple[list[Path], Path | None]] | None = None,
              ask: Callable[[list[Path]], dict[str, Any]] | None = None) -> int:
    """拆这轮的候选,返回拆成了几条。单条失败记下原因接着拆下一条。"""
    now = now or time.time()
    n = 0
    for p in tag_candidates(now):
        with tempfile.TemporaryDirectory(prefix="bench_") as tmp:
            try:
                frames, cover = (grab or _grab)(p["video_url"], p["cover_url"], Path(tmp))
                if not frames:
                    raise TeamError("没取到画面")
                tags = clean_tags((ask or _ask)([*frames[::2][:3], *([cover] if cover else [])]))
                tags["cuts"] = cut_speed(frames)
                db.save_benchmark_tag(p["id"], tags, ts=now)
                n += 1
            except Exception as exc:  # noqa: BLE001 - 一条拆不了不影响别的
                log.info("对标作品 %s 拆结构失败:%s", p["id"], exc)
                db.save_benchmark_tag(p["id"], None, error=str(exc) or type(exc).__name__, ts=now)
    return n


def post_features(post: dict[str, Any], tags: dict[str, Any] | None = None) -> dict[str, str]:
    """对标作品 → 和我们自己的特征同名同值的维度(`stats.BENCH_DIMENSIONS`)。"""
    from ..pipeline import packaging
    from .features import duration_bucket

    tags = tags or {}
    return {"title_kind": packaging.guess_kind(post["title"]),
            "duration": duration_bucket(post["duration"]) if post["duration"] else "",
            "subject": post["car"],
            "hook_visual": OPENING_TO_HOOK.get(tags.get("opening") or "", ""),
            "pace": str(tags.get("cuts") or "")}


def prior_rows(now: float | None = None, days: int = PRIOR_DAYS) -> list[dict[str, Any]]:
    """给 `stats.learn_benchmark`:近期算得出倍数的非置顶作品,带特征。"""
    now = now or time.time()
    tags = db.list_benchmark_tags()
    return [{"id": p["id"], "title": p["title"], "burst": p["burst"],
             "features": post_features(p, (tags.get(p["id"]) or {}).get("tags"))}
            for p in db.list_benchmark_posts(since=now - days * 86400, limit=2000)
            if p["burst"] > 0 and not p["pinned"]]


#: 周报的结构对照:(维度, 名字)。`opening` 我们这边用 hook_visual 反查
STRUCT_DIMENSIONS = (("opening", "开场画面"), ("pace", "剪辑节奏"), ("title_kind", "标题写法"),
                     ("duration", "时长"), ("cover", "封面"))


def _shares(values: list[str], label: Callable[[str], str], top: int = 3) -> list[dict[str, Any]]:
    values = [v for v in values if v]
    counts = Counter(values).most_common(top)
    return [{"value": v, "label": label(v), "share": round(c / len(values), 2)} for v, c in counts]


def structure_compare(own_rows: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """周报用:拆过结构的对标爆款 vs 我们近期的作品,各维度占比最高的几种。"""
    from .features import value_label

    now = now or time.time()
    tags = db.list_benchmark_tags()
    theirs = []
    for p in db.list_benchmark_posts(since=now - PRIOR_DAYS * 86400, min_burst=TAG_MIN_BURST,
                                     limit=400):
        t = (tags.get(p["id"]) or {}).get("tags") or {}
        if t:
            theirs.append({**post_features(p, t), "opening": t.get("opening") or "",
                           "cover": t.get("cover") or "", "claim": bool(t.get("claim"))})
    if not theirs:
        return {"n_theirs": 0, "n_ours": len(own_rows), "dims": [], "claim_share": None}
    ours = [{**r["features"], "opening": HOOK_TO_OPENING.get(r["features"].get("hook_visual") or "", "")}
            for r in own_rows]

    def label_of(dim: str) -> Callable[[str], str]:
        if dim == "opening":
            return lambda v: OPENING_LABEL.get(v, v)
        if dim == "cover":
            return lambda v: COVER_LABEL.get(v, v)
        return lambda v: value_label(dim, v)

    dims = []
    for dim, name in STRUCT_DIMENSIONS:
        their = _shares([x.get(dim, "") for x in theirs], label_of(dim))
        if not their:
            continue
        dims.append({"dimension": dim, "label": name, "theirs": their,
                     "ours": [] if dim == "cover" else _shares([x.get(dim, "") for x in ours], label_of(dim))})
    return {"n_theirs": len(theirs), "n_ours": len(ours), "dims": dims,
            "claim_share": round(sum(x["claim"] for x in theirs) / len(theirs), 2)}


def accounts_view() -> list[dict[str, Any]]:
    out = []
    for a in db.list_benchmark_accounts():
        posts = db.list_benchmark_posts(account_id=a["id"], limit=60)
        out.append({**a, "posts": len(posts),
                    "hits": sum(1 for p in posts if p["burst"] >= BURST_RATIO)})
    return out


def run() -> str:
    if not db.list_benchmark_accounts(enabled_only=True):
        raise Skip("还没有对标账号:在 /team 页加几个")
    if _login_blocked():
        raise Skip("抖音登录失效,等你去分发台重新扫码")
    if not due_accounts():
        raise Skip(f"对标账号 {REFRESH_HOURS} 小时内都看过了")
    out = collect()
    if out["error"] and not out["accounts"]:
        raise TeamError(f"对标主页没抓到:{out['error']}")
    msg = f"看了 {out['accounts']} 个对标号,新作品 {out['added']} 条,爆款 {out['hits']} 条"
    tagged = tag_posts()
    if tagged:
        msg += f",拆了 {tagged} 条的开场结构"
    return msg + (f";停在 {out['error']}" if out["error"] else "")
