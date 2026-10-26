"""SQLite 持久化。

分工原则:
- **可编辑的创作内容**(facts / script / storyboard)存 JSON 文件,方便人工查看与修改。
- **运行时状态**(项目元信息、镜头状态、provider task_id、事件流)存 SQLite,
  因为它们是并发写入、需要事务、且断点续跑依赖它们。

WAL 模式 + 每次调用独立连接,适配 FastAPI 的线程池。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

log = logging.getLogger(__name__)

_SCHEMA_LOCK = threading.Lock()
_INITIALISED = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    subject         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'auto_long',
    theme           TEXT NOT NULL DEFAULT '',
    angle           TEXT NOT NULL DEFAULT '',
    -- 项目变体(开头 / 结尾写法、中段顺序种子),JSON。见 pipeline/variety.py
    variant         TEXT NOT NULL DEFAULT '{}',
    thesis          TEXT NOT NULL DEFAULT '',
    subjects        TEXT NOT NULL DEFAULT '[]',
    duration        INTEGER NOT NULL,
    shot_count      INTEGER NOT NULL DEFAULT 0,
    narration_mode  TEXT NOT NULL DEFAULT 'tts',
    -- 画幅:16:9 | 9:16。创建时定,之后不可改(改画幅等于整片重渲)。
    aspect          TEXT NOT NULL DEFAULT '16:9',
    state           TEXT NOT NULL,
    state_reason    TEXT,
    quota_budget    REAL NOT NULL DEFAULT 40,
    spent_cost      REAL NOT NULL DEFAULT 0,
    reserved_cost   REAL NOT NULL DEFAULT 0,
    resume_state    TEXT NOT NULL DEFAULT '',
    est_cost        REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shots (
    project_id       TEXT NOT NULL,
    idx              INTEGER NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'final',
    status           TEXT NOT NULL DEFAULT 'pending',
    model            TEXT DEFAULT '',
    -- ↓↓↓ 历史列:已删除的竖屏生成链路留下的。解说轨的画面是本地渲染的,
    -- 没有"提交给生成服务再轮询"这一步,这几列永远是空/0。
    -- **保留**是为了不重建表(本地数据有风险);Python 侧已不再有它们的语义。
    provider_task_id TEXT,
    submitted_at     REAL,
    last_polled_at   REAL,
    chain_id         INTEGER NOT NULL DEFAULT 0,
    chain_pos        INTEGER NOT NULL DEFAULT 0,
    -- ↑↑↑ 历史列
    draft_path       TEXT,
    final_path       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    consistency      TEXT NOT NULL DEFAULT 'ok',
    est_cost         REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, idx, stage),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shots_task ON shots(provider_task_id);
CREATE INDEX IF NOT EXISTS idx_shots_status ON shots(project_id, stage, status);

CREATE TABLE IF NOT EXISTS job_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    src         TEXT,
    dst         TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    cost        REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_project ON job_events(project_id, id);

-- --------------------------------------------------------------------------
-- 分发:账号 / 发布记录 / 指标快照
--
-- 这三张表是**运行态**:有并发写(调度器 + 用户手动重试)、要按 state 扫描、
-- 要唯一索引做幂等,所以进 SQLite 而不是 JSON 文件(见文件开头的分工原则)。
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS publish_accounts (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,
    name        TEXT NOT NULL,
    auth_kind   TEXT NOT NULL DEFAULT 'cookie',   -- cookie | oauth | browser
    credential  TEXT NOT NULL DEFAULT '{}',        -- JSON: cookie / token / app key
    meta        TEXT NOT NULL DEFAULT '{}',        -- 平台特定: 分区 tid / 话题 / 频道 ID
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_platform ON publish_accounts(platform);

CREATE TABLE IF NOT EXISTS publications (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    platform      TEXT NOT NULL,
    aspect        TEXT NOT NULL DEFAULT '16:9',    -- 16:9 | 9:16
    artifact      TEXT NOT NULL DEFAULT '',        -- output.mp4 | output_vertical.mp4
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '[]',
    variant       TEXT NOT NULL DEFAULT '{}',      -- 发的是哪版包装:{"title_kind","title_source","cover"}
    piece         TEXT NOT NULL DEFAULT '',        -- 空 = 主片;note / clip1 / clip2 = 分发官的衍生品
    state         TEXT NOT NULL DEFAULT 'pending', -- pending|uploading|submitted|published|failed
    remote_id     TEXT NOT NULL DEFAULT '',
    remote_url    TEXT NOT NULL DEFAULT '',
    upload_id     TEXT NOT NULL DEFAULT '',        -- 分片上传会话,断点续跑用
    error         TEXT NOT NULL DEFAULT '',
    attempts      INTEGER NOT NULL DEFAULT 0,
    published_at  REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
-- 幂等基石(同一项目 + 同一账号 + 同一画幅 + 同一件只发一次)在 `_migrate` 里建:
-- 它要用 piece 列,老库得先补列
CREATE INDEX IF NOT EXISTS idx_pub_state ON publications(state);
CREATE INDEX IF NOT EXISTS idx_pub_project ON publications(project_id);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id  TEXT NOT NULL,
    platform        TEXT NOT NULL,
    captured_at     REAL NOT NULL,
    views           INTEGER,
    likes           INTEGER,
    comments        INTEGER,
    favorites       INTEGER,
    shares          INTEGER,
    coins           INTEGER,
    danmaku         INTEGER,
    followers       INTEGER,
    watch_minutes   REAL,      -- 总播放时长(分钟),抖音创作者中心 = 平均播放秒数 × 播放 / 60
    completion_rate REAL,      -- 完播率 0–1
    avg_watch_sec   REAL,      -- 平均播放秒数
    bounce_2s       REAL,      -- 2 秒跳出率 0–1
    followers_gained INTEGER,  -- 这条作品带来的涨粉
    hours_since_publish REAL,  -- 抓的时候发布了多少小时(分析师按 1h/24h/72h/7d 抓)
    raw             TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_uniq
    ON metric_snapshots(publication_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_metric_pub ON metric_snapshots(publication_id);

-- AI 团队(app/team)
CREATE TABLE IF NOT EXISTS trend_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch       TEXT NOT NULL,          -- 同一次抓取的批次号
    category    TEXT NOT NULL,
    captured_at REAL NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    car         TEXT NOT NULL DEFAULT '',
    rank        INTEGER,                -- 同来源内的名次,从 1 起
    metric      TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trend_cat ON trend_snapshots(category, captured_at);
CREATE INDEX IF NOT EXISTS idx_trend_batch ON trend_snapshots(batch);

CREATE TABLE IF NOT EXISTS plan_items (
    id           TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    slot_at      REAL NOT NULL,         -- 目标发布时刻
    kind         TEXT NOT NULL DEFAULT 'single',   -- single / roundup / versus / rapid
    subject      TEXT NOT NULL,
    subjects     TEXT NOT NULL DEFAULT '[]',
    angle        TEXT NOT NULL DEFAULT '',
    theme        TEXT NOT NULL DEFAULT '',
    aspect       TEXT NOT NULL DEFAULT '16:9',
    duration     INTEGER NOT NULL DEFAULT 240,
    hook         TEXT NOT NULL DEFAULT '',
    search_terms TEXT NOT NULL DEFAULT '',
    evidence     TEXT NOT NULL DEFAULT '[]',
    rationale    TEXT NOT NULL DEFAULT '',
    heat         INTEGER NOT NULL DEFAULT 3,
    status       TEXT NOT NULL DEFAULT 'proposed',
    project_id   TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_slot ON plan_items(category, slot_at);
CREATE INDEX IF NOT EXISTS idx_plan_status ON plan_items(status);

CREATE TABLE IF NOT EXISTS team_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    status      TEXT NOT NULL DEFAULT 'running',  -- running / ok / failed / skipped / idle
    summary     TEXT NOT NULL DEFAULT '',
    trigger     TEXT NOT NULL DEFAULT 'schedule'  -- schedule / manual
);
CREATE INDEX IF NOT EXISTS idx_team_role ON team_runs(role, started_at);

CREATE TABLE IF NOT EXISTS team_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 账号健康(合规审核官):疑似限流 / 审核卡住时暂停这个账号的自动发布,人工恢复
CREATE TABLE IF NOT EXISTS account_health (
    account_id TEXT PRIMARY KEY,
    paused     INTEGER NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    paused_at  REAL,
    updated_at REAL NOT NULL
);

-- 打法手册(复盘官每周写):某个特征取某个值时,某个指标相对账号基线的倍数
CREATE TABLE IF NOT EXISTS learnings (
    id         TEXT PRIMARY KEY,           -- dimension:value:metric,同一条结论每周覆盖
    week       TEXT NOT NULL,              -- 2026-W43
    dimension  TEXT NOT NULL,              -- opening / ending / title_kind / theme / subject ...
    value      TEXT NOT NULL,
    metric     TEXT NOT NULL,              -- views / completion_rate
    effect     REAL NOT NULL,              -- 收缩后的倍数,1 = 和基线持平
    n          INTEGER NOT NULL,
    confidence TEXT NOT NULL,              -- medium / high
    evidence   TEXT NOT NULL DEFAULT '[]', -- 支撑的发布记录 id
    status     TEXT NOT NULL DEFAULT 'active', -- active / stale(本周没再算出来) / off(你停用的)
    updated_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'own'     -- own(自己作品) / benchmark(对标号先验)
);

-- 迭代变更日志:打法权重大变、实验开始 / 结束、自动调参……每一次自动改动都留一条
CREATE TABLE IF NOT EXISTS iteration_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,              -- weight / experiment / param / ...
    title      TEXT NOT NULL,              -- 一句人话
    detail     TEXT NOT NULL DEFAULT '{}'  -- 结构化细节(JSON)
);
CREATE INDEX IF NOT EXISTS idx_iteration_log_ts ON iteration_log(ts);

-- 你在两道闸上的每一次表态(批 / 驳回 / 改 / 改标题),主编和写脚本学你的口味用
CREATE TABLE IF NOT EXISTS review_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    item_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,              -- topic(选题闸) / cut(成片闸)
    decision    TEXT NOT NULL,              -- approve / reject / edit
    note        TEXT NOT NULL DEFAULT '',   -- 驳回理由
    title_from  TEXT NOT NULL DEFAULT '',   -- 成片闸改标题:原标题
    title_to    TEXT NOT NULL DEFAULT '',   --              改成
    features    TEXT NOT NULL DEFAULT '{}'  -- 当时的特征 {维度: 取值}(JSON)
);
CREATE INDEX IF NOT EXISTS idx_review_feedback_ts ON review_feedback(ts);

-- 复盘官周报
CREATE TABLE IF NOT EXISTS team_reports (
    week       TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- 运营官抓的评论(只存一级评论,自己的不存)。回复一律你批过才发
CREATE TABLE IF NOT EXISTS comments (
    id             TEXT PRIMARY KEY,       -- 平台评论 id
    publication_id TEXT NOT NULL,
    account_id     TEXT,
    remote_id      TEXT,                   -- 作品 id
    author         TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,          -- 平台上的评论时刻
    likes          INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL DEFAULT 'other', -- question / praise / argue / ad / other
    kind_source    TEXT NOT NULL DEFAULT 'rule',  -- rule / llm / user
    reply_draft    TEXT NOT NULL DEFAULT '',
    reply_source   TEXT NOT NULL DEFAULT 'template', -- template / llm / user
    reply_state    TEXT NOT NULL DEFAULT 'draft',    -- draft / approved / sending / sent / failed / skipped
    reply_error    TEXT NOT NULL DEFAULT '',
    replied_at     REAL,
    topic_state    TEXT NOT NULL DEFAULT '',      -- 问题类:'' 待进选题池 / pooled 已给主编 / dismissed
    reply_style    TEXT NOT NULL DEFAULT '',      -- 回复写法 info / ask / thanks(阶段 6 学哪种引得来互动)
    fetched_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_state ON comments(reply_state, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_pub ON comments(publication_id);

CREATE TABLE IF NOT EXISTS benchmark_accounts (
    id              TEXT PRIMARY KEY,       -- 抖音 sec_uid
    url             TEXT NOT NULL DEFAULT '',
    nickname        TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    median_likes    REAL NOT NULL DEFAULT 0,
    last_fetched_at REAL,
    last_error      TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_posts (
    id          TEXT PRIMARY KEY,           -- aweme_id
    account_id  TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    car         TEXT NOT NULL DEFAULT '',
    duration    REAL NOT NULL DEFAULT 0,    -- 秒
    posted_at   REAL NOT NULL,
    views       INTEGER NOT NULL DEFAULT 0,
    likes       INTEGER NOT NULL DEFAULT 0,
    comments    INTEGER NOT NULL DEFAULT 0,
    shares      INTEGER NOT NULL DEFAULT 0,
    collects    INTEGER NOT NULL DEFAULT 0,
    pinned      INTEGER NOT NULL DEFAULT 0,
    burst       REAL NOT NULL DEFAULT 0,    -- 点赞 / 该账号点赞中位数
    fetched_at  REAL NOT NULL,
    video_url   TEXT NOT NULL DEFAULT '',   -- 播放地址(带签名,几小时就过期,拆结构要趁新)
    cover_url   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bench_posts ON benchmark_posts(account_id, posted_at);

-- 拆爆款结构:前 3 秒几帧 + 封面交给视觉模型打的标签(一条作品一行,失败也记,不反复试)
-- 作品「进度条分析」:播放进度上每个点的跳过率 / 回看率(阶段 5,抖音没有逐秒留存曲线)
CREATE TABLE IF NOT EXISTS progress_curves (
    pub_id     TEXT PRIMARY KEY,
    points     TEXT NOT NULL DEFAULT '[]',  -- [[秒, 跳过率, 回看率], ...](JSON)
    views      INTEGER,
    fetched_at REAL NOT NULL
);

-- 账号每天一张粉丝快照(分析师抓作品列表时顺手记,作品里带作者粉丝数):量活跃官有没有用
CREATE TABLE IF NOT EXISTS account_daily (
    platform    TEXT NOT NULL,
    day         TEXT NOT NULL,              -- 本地日期 YYYY-MM-DD
    followers   INTEGER NOT NULL,
    captured_at REAL NOT NULL,
    PRIMARY KEY (platform, day)
);

CREATE TABLE IF NOT EXISTS benchmark_tags (
    post_id   TEXT PRIMARY KEY,
    tags      TEXT NOT NULL DEFAULT '{}',   -- {opening, claim, cuts, cover}(JSON)
    error     TEXT NOT NULL DEFAULT '',
    tagged_at REAL NOT NULL
);

-- 活跃官:我们去**别人**作品下的互动留痕(评论/点赞/分享/关注)。
-- 每个作品每种动作一辈子只做一次(idx_outbound_once 兜着)。
-- 和 comments(自己作品下的评论)是两回事,别混。
CREATE TABLE IF NOT EXISTS outbound_actions (
    id          TEXT PRIMARY KEY,
    day         TEXT NOT NULL,          -- 2026-10-25
    slot        TEXT NOT NULL DEFAULT '',  -- 当天计划里的第几条(同一批做完要能挑下一批)
    aweme_id    TEXT NOT NULL DEFAULT '',  -- 目标作品(关注动作为空)
    sec_uid     TEXT NOT NULL DEFAULT '',  -- 目标作者
    target      TEXT NOT NULL DEFAULT '',  -- 去重的键:作品 id,关注动作是作者 sec_uid
    author      TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',  -- 目标作品标题(留痕用)
    action      TEXT NOT NULL,          -- comment / like / share / follow
    text        TEXT NOT NULL DEFAULT '',  -- 评论文案,其它动作为空
    state       TEXT NOT NULL DEFAULT 'planned',  -- planned/doing/done/failed/skipped
    reason      TEXT NOT NULL DEFAULT '',  -- 跳过 / 失败原因
    created_at  REAL NOT NULL,
    started_at  REAL,
    done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_outbound_day ON outbound_actions(day, action, state);
-- 同一个目标同一种动作一辈子只做一次:作品 id(关注动作是作者 sec_uid)
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_once ON outbound_actions(target, action);

CREATE TABLE IF NOT EXISTS industry_events (
    id           TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    day          TEXT NOT NULL,             -- YYYY-MM-DD
    kind         TEXT NOT NULL DEFAULT 'other', -- launch / event / show / ota / price / other
    car          TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual', -- news / manual
    url          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'active', -- active / dismissed
    plan_item_id TEXT NOT NULL DEFAULT '',  -- 当天快反出的选题
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_day ON industry_events(category, day);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_key ON industry_events(category, day, title);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个带 WAL 的连接,退出时关闭。

    ⚠️ **这个上下文管理器不提供事务**。连接是 autocommit
    (`isolation_level=None`),所以 `with connect() as conn:` 那个 with 块
    只是"用完关连接",并不会开启事务 —— 多语句操作在崩溃时会只落一半。

    要原子性(比如"改状态 + 记事件"必须同时成功或同时失败)就用
    `transaction()`。
    """
    config.ensure_dirs()
    conn = sqlite3.connect(config.DATA_DIR / "aimovice.db", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务:块里的语句要么全成,要么全不成。

    为什么需要它:`job_events` 是本项目**唯一的排障依据**(状态卡住了,
    靠事件流回放卡在哪一步)。但事件与状态是两条独立语句、两次独立
    连接,进程在中间被杀就会留下"状态变了但没事件"或"有事件但状态
    没变"的裂缝 —— 排障时看到的就是一段自相矛盾的记录。

    `BEGIN IMMEDIATE` 立刻拿写锁,避免两个写事务先读后写升级锁时死锁。
    """
    config.ensure_dirs()
    conn = sqlite3.connect(config.DATA_DIR / "aimovice.db", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚本身失败时别盖掉原异常
                pass
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回某张表已存在的列名集合。"""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补上后加的列。

    `SCHEMA` 全是 `CREATE TABLE IF NOT EXISTS`,它**不会**给已存在的表补列 ——
    本项目此前也没有任何迁移机制。新装机器上建表是完整的,老机器上的
    aimovice.db 则停留在建表那一刻。因此每加一列都必须在这里显式补。
    """
    if "theme" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN theme TEXT NOT NULL DEFAULT ''")
    if "angle" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN angle TEXT NOT NULL DEFAULT ''")
    if "variant" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN variant TEXT NOT NULL DEFAULT '{}'")
    if "thesis" not in _column_names(conn, "projects"):
        # 主论点:全片那句判断,脊椎。生成脚本时定下,可在门禁 A 改。
        conn.execute("ALTER TABLE projects ADD COLUMN thesis TEXT NOT NULL DEFAULT ''")
    if "subjects" not in _column_names(conn, "projects"):
        conn.execute("ALTER TABLE projects ADD COLUMN subjects TEXT NOT NULL DEFAULT '[]'")
    if "reserved_cost" not in _column_names(conn, "projects"):
        # 在途调用的预留额度。真实花费只有调用完才知道,所以先把预估
        # 额度挂上,调用完再按实际结算 —— "预留 → 结算"能真正卡住超支的
        # 前提(单看已花费,一个并发调用就可能把额度冲破)。
        conn.execute("ALTER TABLE projects ADD COLUMN reserved_cost REAL NOT NULL DEFAULT 0")
    if "resume_state" not in _column_names(conn, "projects"):
        # 因配额暂停时,从哪个状态暂停的 —— 提额后要回到那里。
        conn.execute("ALTER TABLE projects ADD COLUMN resume_state TEXT NOT NULL DEFAULT ''")
    if "aspect" not in _column_names(conn, "projects"):
        # 画幅:16:9 | 9:16。老库的项目一律横屏 —— 与它们改造前的行为一致。
        conn.execute("ALTER TABLE projects ADD COLUMN aspect TEXT NOT NULL DEFAULT '16:9'")
    if "variant" not in _column_names(conn, "publications"):
        # 发布时用的标题写法 / 封面版本,数据回流后按它比 A/B。老记录是空对象。
        conn.execute("ALTER TABLE publications ADD COLUMN variant TEXT NOT NULL DEFAULT '{}'")
    if "scheduled_at" not in _column_names(conn, "publications"):
        # 定时发布:到这个时刻才领。空 = 建好就发(原来的行为)。
        conn.execute("ALTER TABLE publications ADD COLUMN scheduled_at REAL")
    if "piece" not in _column_names(conn, "publications"):
        # 一稿多发:主片之外的图文笔记 / 竖版切片。老记录都是主片。
        conn.execute("ALTER TABLE publications ADD COLUMN piece TEXT NOT NULL DEFAULT ''")
    conn.execute("DROP INDEX IF EXISTS idx_pub_unique")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_piece "
                 "ON publications(project_id, account_id, aspect, piece)")
    snap_cols = _column_names(conn, "metric_snapshots")
    for col, ddl in (("avg_watch_sec", "REAL"), ("bounce_2s", "REAL"),
                     ("followers_gained", "INTEGER"), ("hours_since_publish", "REAL")):
        if col not in snap_cols:  # 分析师从抖音创作者中心抓的深度指标
            conn.execute(f"ALTER TABLE metric_snapshots ADD COLUMN {col} {ddl}")
    plan_cols = _column_names(conn, "plan_items")
    for col, ddl in (("hold", "TEXT NOT NULL DEFAULT ''"),        # 自动流程停下等人的原因
                     ("hold_state", "TEXT NOT NULL DEFAULT ''"),  # 停下时项目在哪个状态
                     ("publish_at", "REAL")):                     # 成片闸通过后排定的发布时刻
        if col not in plan_cols:
            conn.execute(f"ALTER TABLE plan_items ADD COLUMN {col} {ddl}")
    # 第一期的「已出片」现在叫「成片待审」
    conn.execute("UPDATE plan_items SET status = 'review' WHERE status = 'ready'")
    if "source" not in _column_names(conn, "learnings"):
        # 对标先验和自己的结论分开写、分开过期
        conn.execute("ALTER TABLE learnings ADD COLUMN source TEXT NOT NULL DEFAULT 'own'")
    if "reply_style" not in _column_names(conn, "comments"):
        conn.execute("ALTER TABLE comments ADD COLUMN reply_style TEXT NOT NULL DEFAULT ''")
    bench_cols = _column_names(conn, "benchmark_posts")
    for col in ("video_url", "cover_url"):
        if col not in bench_cols:
            conn.execute(f"ALTER TABLE benchmark_posts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def init_db() -> None:
    """建表 + 迁移。幂等,可重复调用。"""
    global _INITIALISED
    with _SCHEMA_LOCK:
        if _INITIALISED:
            return
        config.ensure_dirs()
        conn = sqlite3.connect(config.DATA_DIR / "aimovice.db", timeout=30)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        _INITIALISED = True


# --------------------------------------------------------------------------
# JSON 创作内容读写
# --------------------------------------------------------------------------
def _json_path(project_id: str, name: str) -> Path:
    return config.project_dir(project_id) / name


#: `os.replace` 在目标正被别的进程打开时会失败(Windows 的共享冲突)。
#: 读方(read_json / 页面预览)随时可能正开着这个文件,重试几次即可。
_REPLACE_RETRIES = 6


def _atomic_write(path: Path, text: str) -> None:
    """把一段文本原子地写到 path。

    做法:写同目录下的临时文件 → flush → fsync → os.replace 覆盖。
    `os.replace` 在同卷上是原子替换(Windows 亦然),所以读者要么看到
    旧内容,要么看到新内容,**不会看到半截**。

    为什么必须这样:直接 `path.write_text` 写到一半被kill会留下截断的
    文件。README 自己就写了"托管后台任务有 1 小时上限会把服务收走" ——
    写到一半被杀不是假设,是常态。而 `read_json` 对损坏文件是**静默**
    返回默认值的,于是半截的 script.json 会被当成"文件不存在",
    流水线重新生成一份,用户在门禁 A 手改的稿就这么没了。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
        # 理论上到不了这里
        os.replace(tmp, path)
    except BaseException:
        # 失败就把临时文件清掉,别在项目目录里留垃圾
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def write_json(project_id: str, name: str, payload: Any) -> Path:
    path = _json_path(project_id, name)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def _quarantine_corrupt(path: Path, exc: Exception) -> None:
    """损坏的 JSON 另存一份,不当作"文件不存在"。

    静默返回默认值是最坏的处理方式:流水线以为文件没了,重新生成,
    用户手改的稿被覆盖,而且**事后完全查不出发生过什么**。
    改名保留(而不是删除)是为了留证据。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, backup)
    except OSError:
        return
    log.warning("JSON 损坏,已另存为 %s:%s", backup.name, exc)


def read_json(project_id: str, name: str, default: Any = None) -> Any:
    path = _json_path(project_id, name)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _quarantine_corrupt(path, exc)
        return default
    except OSError:
        return default


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
def create_project(row: dict[str, Any], conn: sqlite3.Connection | None = None) -> None:
    """插入项目行。

    `conn` 不为空时用调用方给的连接(通常来自 `transaction()`),
    这样"插项目 + 记事件"可以是一个原子单元。
    """
    init_db()
    row = {"angle": "", "aspect": "16:9", "variant": {}, **row}
    subjects = row.get("subjects") or []
    row["subjects"] = subjects if isinstance(subjects, str) else json.dumps(subjects, ensure_ascii=False)
    if not isinstance(row["variant"], str):
        row["variant"] = json.dumps(row["variant"], ensure_ascii=False)
    sql = (
        """INSERT INTO projects
           (id, title, subject, category, theme, angle, variant, subjects, duration, shot_count, narration_mode,
            aspect, state, state_reason, quota_budget, spent_cost, est_cost, created_at, updated_at)
           VALUES (:id, :title, :subject, :category, :theme, :angle, :variant, :subjects, :duration, :shot_count, :narration_mode,
                   :aspect, :state, :state_reason, :quota_budget, :spent_cost, :est_cost,
                   :created_at, :updated_at)"""
    )
    if conn is not None:
        conn.execute(sql, row)
        return
    with connect() as own:
        own.execute(sql, row)


def get_project(project_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_projects(limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]


def recent_variants(category: str, limit: int = 4) -> list[dict[str, Any]]:
    """同品类最近建的几个项目的变体(新的在前),建项目挑变体时用来避开重复。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT variant FROM projects WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit))
        rows = [r[0] for r in cur.fetchall()]
    out = []
    for raw in rows:
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError):
            value = {}
        out.append(value if isinstance(value, dict) else {})
    return out


def recent_themes(category: str, limit: int = 2) -> list[str]:
    """同品类最近建的几个项目的片型(新的在前)。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT theme FROM projects WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit))
        return [str(r[0] or "") for r in cur.fetchall()]


def project_subjects(row: dict[str, Any] | None) -> list[dict[str, str]]:
    """项目行里的横评车单 -> [{name, group}]。单车项目 / 旧库 / 坏数据都返回 []。"""
    raw = (row or {}).get("subjects") or "[]"
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for item in items or []:
        if isinstance(item, dict) and str(item.get("name") or "").strip():
            out.append({"name": str(item["name"]).strip(), "group": str(item.get("group") or "").strip()})
    return out


#: `projects` 表里允许 `update_project` 改的列。
#:
#: 之前 SET 子句是用 f-string 直接拼 kwargs 的键。当前所有调用点的键都
#: 是内部字面量,**没有注入面**;但这是一张等着被人顺手塞进用户输入
#: 的表 —— 白名单把这条路堵死,代价只是一份常量。
_PROJECT_COLUMNS = frozenset({
    "title", "subject", "category", "theme", "angle", "variant", "thesis", "subjects",
    "duration", "shot_count", "narration_mode", "state", "state_reason",
    "quota_budget", "spent_cost", "reserved_cost", "resume_state", "est_cost",
    "created_at", "updated_at",
})


def update_project(project_id: str, *, conn: sqlite3.Connection | None = None,
                   **fields: Any) -> None:
    """改项目行。`conn` 不为空时用调用方给的连接(见 `transaction()`)。"""
    if not fields:
        return
    init_db()
    unknown = set(fields) - _PROJECT_COLUMNS
    if unknown:
        raise ValueError(f"未知列:{'、'.join(sorted(unknown))}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    sql = f"UPDATE projects SET {assignments} WHERE id = :id"
    args = {"id": project_id, **fields}
    if conn is not None:
        conn.execute(sql, args)
        return
    with connect() as own:
        own.execute(sql, args)


def cas_state(project_id: str, expected: str, new: str, *,
              conn: sqlite3.Connection | None = None,
              reason: str | None = None) -> bool:
    """状态机的 CAS 写:只有当前状态还是 `expected` 时才改成 `new`。

    返回 True = 改成了;False = 状态已经被别人推进了,调用方应当放弃
    本轮并重读状态。

    为什么需要:原来的 `_set_state` 是"读出来 → 判断转移合法 → 无条件
    UPDATE",中间有个 TOCTOU 窗口。两个执行流(后台调度器 + 用户手动
    推进)都读到 DRAFT_RENDERING 时,会各自开渲同一批拍,写同一个文件。
    把"判断"和"写"合成一条 SQL 就没有这个窗口了。
    """
    init_db()
    now = time.time()
    if reason is None:
        sql = ("UPDATE projects SET state = :new, updated_at = :now "
               "WHERE id = :id AND state = :expected")
    else:
        sql = ("UPDATE projects SET state = :new, state_reason = :reason, updated_at = :now "
               "WHERE id = :id AND state = :expected")
    args = {"id": project_id, "expected": expected, "new": new,
            "reason": reason, "now": now}
    if conn is not None:
        cur = conn.execute(sql, args)
        return cur.rowcount > 0
    with connect() as own:
        cur = own.execute(sql, args)
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# 配额账本:预留 → 结算
# --------------------------------------------------------------------------
def reserve_spend(project_id: str, amount: float) -> bool:
    """预留额度。只有"已花费 + 在途预留 + 本次预留"不超过上限时才成功。

    真实花费只有调用完才知道,所以调用**之前**先把预估额度挂上:

    - 不挂就直接看已花费 —— 一次调用就能把额度冲破;
    - 挂了才知道"这个项目已经花到哪、还能不能再花",多个在途调用也
      不会各自都觉得"我还够"。

    返回 False 表示超限(调用方据此中止,不该真的去调)。
    """
    init_db()
    now = time.time()
    sql = (
        "UPDATE projects SET reserved_cost = reserved_cost + :amount, updated_at = :now "
        "WHERE id = :id AND (spent_cost + reserved_cost + :amount) <= quota_budget"
    )
    with connect() as conn:
        cur = conn.execute(sql, {"id": project_id, "amount": max(0.0, float(amount)),
                                  "now": now})
        return cur.rowcount > 0


def commit_spend(project_id: str, reserved: float, actual: float) -> None:
    """结算:释放预留,加上实际花费。"""
    init_db()
    now = time.time()
    sql = (
        "UPDATE projects SET spent_cost = spent_cost + :actual, "
        "reserved_cost = MAX(0, reserved_cost - :reserved), updated_at = :now "
        "WHERE id = :id"
    )
    with connect() as conn:
        conn.execute(sql, {"id": project_id, "reserved": max(0.0, float(reserved)),
                           "actual": max(0.0, float(actual)), "now": now})


def release_spend(project_id: str, reserved: float) -> None:
    """调用失败:释放预留,不记账。"""
    init_db()
    now = time.time()
    sql = ("UPDATE projects SET reserved_cost = MAX(0, reserved_cost - :reserved), "
           "updated_at = :now WHERE id = :id")
    with connect() as conn:
        conn.execute(sql, {"id": project_id, "reserved": max(0.0, float(reserved)),
                           "now": now})


# --------------------------------------------------------------------------
# 镜头
# --------------------------------------------------------------------------
def upsert_shots(project_id: str, shots: list[dict[str, Any]]) -> None:
    init_db()
    now = time.time()
    payload = []
    for s in shots:
        item = dict(s)
        item.setdefault("project_id", project_id)
        item.setdefault("stage", "final")
        item.setdefault("status", "pending")
        item.setdefault("attempts", 0)
        item.setdefault("consistency", "ok")
        item.setdefault("chain_id", 0)
        item.setdefault("chain_pos", 0)
        item.setdefault("est_cost", 0.0)
        item["updated_at"] = now
        payload.append(item)
    with connect() as conn:
        conn.executemany(
            """INSERT INTO shots
               (project_id, idx, stage, status, model, provider_task_id, submitted_at,
                last_polled_at, draft_path, final_path, attempts, error, consistency,
                chain_id, chain_pos, est_cost)
               VALUES (:project_id, :idx, :stage, :status, :model, :provider_task_id,
                       :submitted_at, :last_polled_at, :draft_path, :final_path,
                       :attempts, :error, :consistency, :chain_id, :chain_pos, :est_cost)
               ON CONFLICT(project_id, idx, stage) DO UPDATE SET
                   status = excluded.status,
                   model = excluded.model,
                   provider_task_id = COALESCE(excluded.provider_task_id, shots.provider_task_id),
                   submitted_at = COALESCE(excluded.submitted_at, shots.submitted_at),
                   last_polled_at = excluded.last_polled_at,
                   draft_path = COALESCE(excluded.draft_path, shots.draft_path),
                   final_path = COALESCE(excluded.final_path, shots.final_path),
                   attempts = excluded.attempts,
                   error = excluded.error,
                   consistency = excluded.consistency,
                   chain_id = excluded.chain_id,
                   chain_pos = excluded.chain_pos,
                   est_cost = excluded.est_cost""",
            payload,
        )


def get_shots(project_id: str, stage: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if stage:
            cur = conn.execute(
                "SELECT * FROM shots WHERE project_id = ? AND stage = ? ORDER BY idx",
                (project_id, stage),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM shots WHERE project_id = ? ORDER BY stage, idx",
                (project_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def update_shot(project_id: str, idx: int, stage: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE shots SET {assignments} WHERE project_id = :pid AND idx = :idx AND stage = :stage",
            {"pid": project_id, "idx": idx, "stage": stage, **fields},
        )


def delete_shots(project_id: str, stage: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            "DELETE FROM shots WHERE project_id = ? AND stage = ?", (project_id, stage)
        )


def find_shot_by_task_id(task_id: str) -> dict[str, Any] | None:
    """按 provider task_id 反查镜头。

    ⚠️ **历史遗留**:解说轨的画面是本地渲染的,没有"提交给生成服务再轮询"
    这一步,`provider_task_id` 永远是空的。全项目无调用。
    `shots` 表的列保留(删列要重建表,对本地数据有风险),但这个函数已经
    没有意义了 —— 留着只会让人以为还有轮询器。
    """
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM shots WHERE provider_task_id = ? LIMIT 1", (task_id,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_pending_shots(project_id: str, stage: str, limit: int) -> list[dict[str, Any]]:
    """取出处于 pending 且未达重试上限的镜头,供调度器提交。

    ⚠️ **历史遗留**:同 `find_shot_by_task_id` —— 解说轨没有"提交"这一步。
    渲染的幂等现在由**项目锁**保证(同一项目同一时刻只有一个写入者),
    不再靠"provider_task_id 非空"这种标记。
    """
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """SELECT * FROM shots
               WHERE project_id = ? AND stage = ? AND status = 'pending'
                 AND provider_task_id IS NULL
               ORDER BY chain_id, chain_pos, idx
               LIMIT ?""",
            (project_id, stage, limit),
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
def add_event(project_id: str, dst: str, src: str | None = None,
              reason: str = "", cost: float = 0.0,
              conn: sqlite3.Connection | None = None) -> None:
    """记一条事件。

    `conn` 不为空时用调用方给的连接(通常来自 `transaction()`),
    让"改状态 + 记事件"成为一个原子单元 —— 事件流是本项目唯一的
    排障依据,状态与事件不一致的裂缝比没有事件更误导人。
    """
    init_db()
    sql = (
        "INSERT INTO job_events (project_id, created_at, src, dst, reason, cost)"
        " VALUES (?, ?, ?, ?, ?, ?)"
    )
    args = (project_id, time.time(), src, dst, reason, cost)
    if conn is not None:
        conn.execute(sql, args)
        return
    with connect() as own:
        own.execute(sql, args)


def get_events(project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM job_events WHERE project_id = ? ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# 分发账号
# --------------------------------------------------------------------------
def create_account(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {"enabled": 1, "credential": "{}", "meta": "{}", **row}
    row.setdefault("created_at", now)
    row["updated_at"] = now
    with connect() as conn:
        conn.execute(
            """INSERT INTO publish_accounts
               (id, platform, name, auth_kind, credential, meta, enabled, created_at, updated_at)
               VALUES (:id, :platform, :name, :auth_kind, :credential, :meta, :enabled,
                       :created_at, :updated_at)""",
            row,
        )


def get_account(account_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM publish_accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_accounts(platform: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if platform:
            cur = conn.execute(
                "SELECT * FROM publish_accounts WHERE platform = ? ORDER BY created_at",
                (platform,),
            )
        else:
            cur = conn.execute("SELECT * FROM publish_accounts ORDER BY platform, created_at")
        return [dict(r) for r in cur.fetchall()]


def update_account(account_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE publish_accounts SET {assignments} WHERE id = :id",
            {"id": account_id, **fields},
        )


def delete_account(account_id: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM publish_accounts WHERE id = ?", (account_id,))


# --------------------------------------------------------------------------
# 发布记录
# --------------------------------------------------------------------------
def create_publication(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {
        "aspect": "16:9", "artifact": "", "title": "", "description": "",
        "tags": "[]", "variant": "{}", "piece": "", "state": "pending", "remote_id": "", "remote_url": "",
        "upload_id": "", "error": "", "attempts": 0, "published_at": None, "scheduled_at": None,
        **row,
    }
    row.setdefault("created_at", now)
    row["updated_at"] = now
    with connect() as conn:
        conn.execute(
            """INSERT INTO publications
               (id, project_id, account_id, platform, aspect, artifact, title, description,
                tags, variant, piece, state, remote_id, remote_url, upload_id, error, attempts,
                published_at, scheduled_at, created_at, updated_at)
               VALUES (:id, :project_id, :account_id, :platform, :aspect, :artifact, :title,
                       :description, :tags, :variant, :piece, :state, :remote_id, :remote_url, :upload_id, :error,
                       :attempts, :published_at, :scheduled_at, :created_at, :updated_at)""",
            row,
        )


def get_publication(pub_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM publications WHERE id = ?", (pub_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_publications(*, state: str | None = None, project_id: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with connect() as conn:
        cur = conn.execute(
            f"SELECT * FROM publications {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [dict(r) for r in cur.fetchall()]


def update_publication(pub_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE publications SET {assignments} WHERE id = :id",
            {"id": pub_id, **fields},
        )


def find_publication(project_id: str, account_id: str, aspect: str,
                     piece: str = "") -> dict[str, Any] | None:
    """幂等查询:同一项目 + 同一账号 + 同一画幅 + 同一件只应有一条记录。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM publications WHERE project_id = ? AND account_id = ? AND aspect = ?"
            " AND piece = ?",
            (project_id, account_id, aspect, piece),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_publications(limit: int = 3, now: float | None = None) -> list[dict[str, Any]]:
    """取出待推进的发布任务。定时发布的没到点不领,被合规审核官暂停的账号不领。

    **同一账号一次只给一条** —— 单账号高频上传会触发平台风控,这是硬要求。
    """
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM publications
               WHERE state IN ('pending', 'uploading')
                 AND (scheduled_at IS NULL OR scheduled_at <= ?)
                 AND account_id NOT IN (SELECT account_id FROM account_health WHERE paused = 1)
               ORDER BY created_at
               LIMIT ?""",
            (time.time() if now is None else now, limit * 4),
        ).fetchall()
    out: list[dict[str, Any]] = []
    busy: set[str] = set()
    for row in rows:
        account_id = row["account_id"]
        if account_id in busy:
            continue
        busy.add(account_id)
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def publications_for_metrics(limit: int = 200) -> list[dict[str, Any]]:
    """需要抓数据的作品:已提交(待审)与已发布。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """SELECT * FROM publications
               WHERE state IN ('submitted', 'published') AND remote_id != ''
               ORDER BY published_at DESC LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------
# 指标快照
# --------------------------------------------------------------------------
def add_snapshot(publication_id: str, platform: str, captured_at: float,
                 metrics: dict[str, Any], raw: dict[str, Any] | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO metric_snapshots
               (publication_id, platform, captured_at, views, likes, comments, favorites,
                shares, coins, danmaku, followers, watch_minutes, completion_rate,
                avg_watch_sec, bounce_2s, followers_gained, hours_since_publish, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                publication_id, platform, captured_at,
                metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
                metrics.get("favorites"), metrics.get("shares"), metrics.get("coins"),
                metrics.get("danmaku"), metrics.get("followers"),
                metrics.get("watch_minutes"), metrics.get("completion_rate"),
                metrics.get("avg_watch_sec"), metrics.get("bounce_2s"),
                metrics.get("followers_gained"), metrics.get("hours_since_publish"),
                json.dumps(raw or {}, ensure_ascii=False),
            ),
        )


def list_snapshots(publication_id: str, limit: int = 200) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM metric_snapshots WHERE publication_id = ? ORDER BY captured_at LIMIT ?",
            (publication_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def latest_snapshot(publication_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM metric_snapshots WHERE publication_id = ? ORDER BY captured_at DESC LIMIT 1",
            (publication_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# AI 团队:热点快照 / 选题单 / 角色运行记录 / 团队设置
# --------------------------------------------------------------------------
_TREND_COLS = ("source", "kind", "title", "car", "rank", "metric", "url", "date")


def add_trend_batch(category: str, batch: str, captured_at: float,
                    rows: list[dict[str, Any]]) -> int:
    """一次抓取的热点整批落库。返回条数。"""
    init_db()
    with transaction() as conn:
        for row in rows:
            conn.execute(
                "INSERT INTO trend_snapshots (batch, category, captured_at, source, kind, title,"
                " car, rank, metric, url, date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (batch, category, captured_at,
                 *[row.get(c) if c == "rank" else str(row.get(c) or "") for c in _TREND_COLS]),
            )
    return len(rows)


def trend_batches(category: str, limit: int = 20) -> list[dict[str, Any]]:
    """最近几批的 (batch, captured_at, 条数),新的在前。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT batch, MAX(captured_at) AS captured_at, COUNT(*) AS n FROM trend_snapshots"
            " WHERE category = ? GROUP BY batch ORDER BY captured_at DESC LIMIT ?",
            (category, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def trend_rows(batch: str) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM trend_snapshots WHERE batch = ? ORDER BY id", (batch,))
        return [dict(r) for r in cur.fetchall()]


_PLAN_JSON = ("subjects", "evidence")


def _plan_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in _PLAN_JSON:
        try:
            out[key] = json.loads(out.get(key) or "[]")
        except (TypeError, ValueError):
            out[key] = []
    return out


def create_plan_item(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {"kind": "single", "subjects": [], "angle": "", "theme": "", "aspect": "16:9",
           "duration": 240, "hook": "", "search_terms": "", "evidence": [], "rationale": "",
           "heat": 3, "status": "proposed", "project_id": "", "note": "", **row}
    row.setdefault("created_at", now)
    row["updated_at"] = now
    for key in _PLAN_JSON:
        if not isinstance(row[key], str):
            row[key] = json.dumps(row[key], ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """INSERT INTO plan_items
               (id, category, slot_at, kind, subject, subjects, angle, theme, aspect, duration,
                hook, search_terms, evidence, rationale, heat, status, project_id, note,
                created_at, updated_at)
               VALUES (:id, :category, :slot_at, :kind, :subject, :subjects, :angle, :theme,
                       :aspect, :duration, :hook, :search_terms, :evidence, :rationale, :heat,
                       :status, :project_id, :note, :created_at, :updated_at)""",
            row,
        )


def get_plan_item(item_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        return _plan_row(conn.execute("SELECT * FROM plan_items WHERE id = ?", (item_id,)).fetchone())


def list_plan_items(category: str | None = None, *, statuses: tuple[str, ...] = (),
                    since: float | None = None, until: float | None = None,
                    limit: int = 200) -> list[dict[str, Any]]:
    """按目标发布时刻排序的选题。"""
    init_db()
    where, args = [], []
    if category:
        where.append("category = ?")
        args.append(category)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if since is not None:
        where.append("slot_at >= ?")
        args.append(since)
    if until is not None:
        where.append("slot_at < ?")
        args.append(until)
    sql = "SELECT * FROM plan_items"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY slot_at, created_at LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [_plan_row(r) for r in conn.execute(sql, args).fetchall()]


def update_plan_item(item_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    for key in _PLAN_JSON:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE plan_items SET {cols} WHERE id = ?", (*fields.values(), item_id))


def cas_plan_status(item_id: str, expected: tuple[str, ...], new: str, **fields: Any) -> bool:
    """状态是 expected 之一才改成 new。两个执行流抢同一条选题时只有一个成功。"""
    init_db()
    fields = {"status": new, "updated_at": time.time(), **fields}
    cols = ", ".join(f"{k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(f"UPDATE plan_items SET {cols} WHERE id = ? AND status IN ({marks})",
                           (*fields.values(), item_id, *expected))
        return cur.rowcount == 1


def start_team_run(role: str, trigger: str = "schedule", stale_after: float = 3600) -> int | None:
    """登记一次角色运行。同角色已有一次在跑(且没超时)就返回 None。

    用数据库而不是进程内锁:两个服务连同一个库时(以前出过事),
    同一角色也只会跑一份。
    """
    init_db()
    now = time.time()
    with transaction() as conn:
        busy = conn.execute(
            "SELECT 1 FROM team_runs WHERE role = ? AND status = 'running' AND started_at > ?",
            (role, now - stale_after)).fetchone()
        if busy:
            return None
        cur = conn.execute("INSERT INTO team_runs (role, started_at, status, trigger)"
                           " VALUES (?, ?, 'running', ?)", (role, now, trigger))
        return int(cur.lastrowid)


def finish_team_run(run_id: int, status: str, summary: str = "") -> None:
    init_db()
    with connect() as conn:
        conn.execute("UPDATE team_runs SET finished_at = ?, status = ?, summary = ? WHERE id = ?",
                     (time.time(), status, summary[:500], run_id))


def list_team_runs(role: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if role:
            cur = conn.execute("SELECT * FROM team_runs WHERE role = ? ORDER BY id DESC LIMIT ?",
                               (role, limit))
        else:
            cur = conn.execute("SELECT * FROM team_runs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]


def last_team_run(role: str, *, scheduled_only: bool = False) -> dict[str, Any] | None:
    init_db()
    sql = "SELECT * FROM team_runs WHERE role = ?"
    if scheduled_only:
        sql += " AND trigger = 'schedule'"
    with connect() as conn:
        row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", (role,)).fetchone()
    return dict(row) if row else None


def get_account_health(account_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM account_health WHERE account_id = ?",
                           (account_id,)).fetchone()
    return dict(row) if row else None


def list_account_health() -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM account_health ORDER BY updated_at DESC").fetchall()]


def pause_account(account_id: str, reason: str) -> None:
    """暂停一个账号的自动发布:发布线程不再领它的任务(已排的留着,恢复后照发)。"""
    init_db()
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO account_health (account_id, paused, reason, paused_at, updated_at)"
            " VALUES (?, 1, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET paused = 1,"
            " reason = excluded.reason, paused_at = excluded.paused_at, updated_at = excluded.updated_at",
            (account_id, reason[:300], now, now))


def resume_account(account_id: str) -> bool:
    init_db()
    with connect() as conn:
        cur = conn.execute("UPDATE account_health SET paused = 0, updated_at = ? WHERE account_id = ?"
                           " AND paused = 1", (time.time(), account_id))
        return cur.rowcount > 0


def replace_learnings(week: str, rows: list[dict[str, Any]], source: str = "own") -> None:
    """写本周的结论。上周有、本周没算出来的标 stale(不再生效);你停用过的保持停用。

    `source` 分开过期:写对标先验不会把自己作品的结论标 stale,反过来也一样。"""
    init_db()
    now = time.time()
    with transaction() as conn:
        off = {r[0] for r in conn.execute("SELECT id FROM learnings WHERE status = 'off'")}
        conn.execute("UPDATE learnings SET status = 'stale', updated_at = ?"
                     " WHERE status = 'active' AND source = ?", (now, source))
        for r in rows:
            conn.execute(
                "INSERT INTO learnings (id, week, dimension, value, metric, effect, n, confidence,"
                " evidence, status, updated_at, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET week = excluded.week, effect = excluded.effect,"
                " n = excluded.n, confidence = excluded.confidence, evidence = excluded.evidence,"
                " status = excluded.status, updated_at = excluded.updated_at",
                (r["id"], week, r["dimension"], r["value"], r["metric"], r["effect"], r["n"],
                 r["confidence"], json.dumps(r.get("evidence") or [], ensure_ascii=False),
                 "off" if r["id"] in off else "active", now, source))


def list_learnings(statuses: tuple[str, ...] = ("active",)) -> list[dict[str, Any]]:
    init_db()
    marks = ",".join("?" * len(statuses))
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM learnings WHERE status IN ({marks})"
                            " ORDER BY dimension, effect DESC", statuses).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.get("evidence") or "[]")
        out.append(d)
    return out


def set_learning_status(learning_id: str, status: str) -> bool:
    """你在 /team 停用 / 恢复一条结论。"""
    init_db()
    with connect() as conn:
        cur = conn.execute("UPDATE learnings SET status = ?, updated_at = ? WHERE id = ?",
                           (status, time.time(), learning_id))
        return cur.rowcount > 0


def add_iteration_log(kind: str, title: str, detail: dict[str, Any] | None = None,
                      ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO iteration_log (ts, kind, title, detail) VALUES (?, ?, ?, ?)",
                     (ts or time.time(), kind, title,
                      json.dumps(detail or {}, ensure_ascii=False)))


def list_iteration_log(limit: int = 50, since: float | None = None) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM iteration_log WHERE ts >= ? ORDER BY ts DESC, id DESC"
                            " LIMIT ?", (since or 0, limit)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


def add_review_feedback(row: dict[str, Any]) -> None:
    init_db()
    row = {"ts": time.time(), "note": "", "title_from": "", "title_to": "", **row}
    row["features"] = json.dumps(row.get("features") or {}, ensure_ascii=False)
    with connect() as conn:
        conn.execute("INSERT INTO review_feedback (ts, item_id, stage, decision, note, title_from,"
                     " title_to, features) VALUES (:ts, :item_id, :stage, :decision, :note,"
                     " :title_from, :title_to, :features)", row)


def list_review_feedback(since: float | None = None, stage: str | None = None,
                         limit: int = 1000) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    sql = "SELECT * FROM review_feedback WHERE ts >= ?"
    args: list[Any] = [since or 0]
    if stage:
        sql += " AND stage = ?"
        args.append(stage)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    return [{**dict(r), "features": json.loads(r["features"] or "{}")} for r in rows]


def save_report(week: str, data: dict[str, Any]) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO team_reports (week, data, created_at) VALUES (?, ?, ?)"
                     " ON CONFLICT(week) DO UPDATE SET data = excluded.data,"
                     " created_at = excluded.created_at",
                     (week, json.dumps(data, ensure_ascii=False), time.time()))


def latest_report() -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM team_reports ORDER BY week DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {"week": row["week"], "created_at": row["created_at"], **json.loads(row["data"])}


#: 评论里允许改的字段(`update_comment`)
_COMMENT_FIELDS = {"kind", "kind_source", "reply_draft", "reply_source", "reply_state",
                   "reply_error", "replied_at", "topic_state", "reply_style"}


def add_comments(rows: list[dict[str, Any]]) -> int:
    """新评论入库,已有的只更新点赞数(分类、草稿、审核状态都不动)。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    with transaction() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO comments (id, publication_id, account_id, remote_id, author,"
                " text, created_at, likes, kind, kind_source, reply_draft, reply_source,"
                " reply_state, reply_style, fetched_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["publication_id"], r.get("account_id"), r.get("remote_id"),
                 r.get("author") or "", r.get("text") or "", r["created_at"], r.get("likes") or 0,
                 r.get("kind") or "other", r.get("kind_source") or "rule",
                 r.get("reply_draft") or "", r.get("reply_source") or "template",
                 r.get("reply_state") or "draft", r.get("reply_style") or "", now, now))
            if cur.rowcount:
                n += 1
            else:
                conn.execute("UPDATE comments SET likes = ? WHERE id = ?",
                             (r.get("likes") or 0, r["id"]))
    return n


def get_comment(comment_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    return dict(row) if row else None


def list_comments(*, states: tuple[str, ...] = (), kinds: tuple[str, ...] = (),
                  publication_id: str | None = None, since: float | None = None,
                  topic_states: tuple[str, ...] | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
    """新评论在前。"""
    init_db()
    where, args = [], []
    for col, vals in (("reply_state", states), ("kind", kinds), ("topic_state", topic_states)):
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            args.extend(vals)
    if publication_id:
        where.append("publication_id = ?")
        args.append(publication_id)
    if since is not None:
        where.append("created_at >= ?")
        args.append(since)
    sql = "SELECT * FROM comments" + (" WHERE " + " AND ".join(where) if where else "")
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def update_comment(comment_id: str, **fields: Any) -> None:
    bad = set(fields) - _COMMENT_FIELDS
    if bad:
        raise ValueError(f"不能改的评论字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE comments SET {sets}, updated_at = ? WHERE id = ?",
                     (*fields.values(), time.time(), comment_id))


def cas_comment_state(comment_id: str, expected: tuple[str, ...], new: str, **fields: Any) -> bool:
    """回复状态从 expected 之一改成 new;已经被别人改走就返回 False(批量审核和发送并发时不重发)。"""
    bad = set(fields) - _COMMENT_FIELDS
    if bad:
        raise ValueError(f"不能改的评论字段:{sorted(bad)}")
    init_db()
    sets = "".join(f", {k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE comments SET reply_state = ?{sets}, updated_at = ?"
            f" WHERE id = ? AND reply_state IN ({marks})",
            (new, *fields.values(), time.time(), comment_id, *expected))
        return cur.rowcount > 0


def get_team_setting(key: str, default: Any = None) -> Any:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT value FROM team_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def set_team_setting(key: str, value: Any) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT INTO team_settings (key, value) VALUES (?, ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def prune_idle_runs(role: str) -> None:
    """轮询型角色(制片每 5 分钟看一眼)大多数时候没事可做:只留最近一条空转记录。"""
    init_db()
    with connect() as conn:
        conn.execute(
            "DELETE FROM team_runs WHERE role = ? AND status = 'idle' AND id < "
            "(SELECT MAX(id) FROM team_runs WHERE role = ? AND status = 'idle')",
            (role, role))


# --------------------------------------------------------------------------
# 对标账号 / 对标作品 / 行业日历
# --------------------------------------------------------------------------
_BENCH_FIELDS = {"url", "nickname", "note", "enabled", "median_likes", "last_fetched_at",
                 "last_error"}


def add_benchmark_account(sec_uid: str, url: str = "", note: str = "") -> bool:
    """加一个对标账号。已经有了返回 False。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO benchmark_accounts (id, url, note, created_at) VALUES (?,?,?,?)",
            (sec_uid, url, note, time.time()))
    return bool(cur.rowcount)


def list_benchmark_accounts(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    init_db()
    sql = "SELECT * FROM benchmark_accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY created_at").fetchall()]


def get_benchmark_account(sec_uid: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM benchmark_accounts WHERE id = ?", (sec_uid,)).fetchone()
    return dict(row) if row else None


def save_benchmark_tag(post_id: str, tags: dict[str, Any] | None, error: str = "",
                       ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO benchmark_tags (post_id, tags, error, tagged_at)"
                     " VALUES (?, ?, ?, ?)",
                     (post_id, json.dumps(tags or {}, ensure_ascii=False), error[:200],
                      ts or time.time()))


def list_benchmark_tags(since: float | None = None) -> dict[str, dict[str, Any]]:
    """{post_id: {tags, error, tagged_at}},`since` 按打标签的时间。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM benchmark_tags WHERE tagged_at >= ?",
                            (since or 0,)).fetchall()
    return {r["post_id"]: {**dict(r), "tags": json.loads(r["tags"] or "{}")} for r in rows}


def save_progress_curve(pub_id: str, points: list[list[float]], views: int | None = None,
                        ts: float | None = None) -> None:
    init_db()
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO progress_curves (pub_id, points, views, fetched_at)"
                     " VALUES (?, ?, ?, ?)",
                     (pub_id, json.dumps(points), views, ts or time.time()))


def save_account_daily(platform: str, day: str, followers: int, ts: float | None = None) -> bool:
    """记当天的粉丝数。一天只记第一张(当天早些的数更接近「前一天结束时」)。返回是不是新记的。"""
    init_db()
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO account_daily (platform, day, followers, captured_at)"
                           " VALUES (?, ?, ?, ?)", (platform, day, int(followers), ts or time.time()))
    return bool(cur.rowcount)


def list_account_daily(platform: str = "douyin", since_day: str = "") -> dict[str, int]:
    """{日期: 粉丝数},按日期升序。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT day, followers FROM account_daily WHERE platform = ? AND day >= ?"
                            " ORDER BY day", (platform, since_day)).fetchall()
    return {r["day"]: int(r["followers"]) for r in rows}


def list_progress_curves(since: float | None = None) -> dict[str, dict[str, Any]]:
    """{pub_id: {points, views, fetched_at}},`since` 按抓取时间。"""
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM progress_curves WHERE fetched_at >= ?",
                            (since or 0,)).fetchall()
    return {r["pub_id"]: {**dict(r), "points": json.loads(r["points"] or "[]")} for r in rows}


def update_benchmark_account(sec_uid: str, **fields: Any) -> None:
    bad = set(fields) - _BENCH_FIELDS
    if bad:
        raise ValueError(f"不能改的对标账号字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE benchmark_accounts SET {sets} WHERE id = ?",
                     (*fields.values(), sec_uid))


def delete_benchmark_account(sec_uid: str) -> None:
    """删账号连带它的作品(对标数据是抓来的,随时能再抓,不是用户资产)。"""
    init_db()
    with transaction() as conn:
        conn.execute("DELETE FROM benchmark_posts WHERE account_id = ?", (sec_uid,))
        conn.execute("DELETE FROM benchmark_accounts WHERE id = ?", (sec_uid,))


def upsert_benchmark_posts(rows: list[dict[str, Any]]) -> int:
    """作品入库,已有的刷新数据。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    cols = ("id", "account_id", "title", "car", "duration", "posted_at", "views", "likes",
            "comments", "shares", "collects", "pinned", "burst", "video_url", "cover_url")
    text = ("id", "account_id", "title", "car", "video_url", "cover_url")
    with transaction() as conn:
        for r in rows:
            exists = conn.execute("SELECT 1 FROM benchmark_posts WHERE id = ?", (r["id"],)).fetchone()
            vals = [r.get(c, "" if c in text else 0) for c in cols]
            conn.execute(
                f"INSERT OR REPLACE INTO benchmark_posts ({', '.join(cols)}, fetched_at)"
                f" VALUES ({','.join('?' * len(cols))}, ?)", (*vals, now))
            n += 0 if exists else 1
    return n


def list_benchmark_posts(*, account_id: str | None = None, since: float | None = None,
                         min_burst: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """新发的在前。"""
    init_db()
    where, args = [], []
    if account_id:
        where.append("account_id = ?")
        args.append(account_id)
    if since is not None:
        where.append("posted_at >= ?")
        args.append(since)
    if min_burst is not None:
        where.append("burst >= ?")
        args.append(min_burst)
    sql = "SELECT * FROM benchmark_posts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY posted_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------
# 活跃官:对外互动留痕(评论 / 点赞 / 分享 / 关注)
# --------------------------------------------------------------------------
#: 允许改的字段(`update_outbound_action`)
_OUTBOUND_FIELDS = {"text", "state", "reason", "started_at", "done_at", "title",
                     "author", "sec_uid"}


def add_outbound_actions(rows: list[dict[str, Any]]) -> int:
    """新动作入库。同一目标同一动作已经有了就忽略(唯一索引兜着)。返回新增条数。"""
    init_db()
    now = time.time()
    n = 0
    with transaction() as conn:
        for r in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO outbound_actions (id, day, slot, aweme_id, sec_uid, target,"
                " author, title, action, text, state, reason, created_at, started_at, done_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["day"], r.get("slot") or "", r.get("aweme_id") or "",
                 r.get("sec_uid") or "",
                 r.get("target") or r.get("aweme_id") or r.get("sec_uid") or "",
                 r.get("author") or "", r.get("title") or "", r["action"],
                 r.get("text") or "", r.get("state") or "planned", r.get("reason") or "",
                 r.get("created_at") or now, r.get("started_at"), r.get("done_at")))
            if cur.rowcount:
                n += 1
    return n


def get_outbound_action(action_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM outbound_actions WHERE id = ?",
                           (action_id,)).fetchone()
    return dict(row) if row else None


def list_outbound_actions(*, day: str | None = None, actions: tuple[str, ...] = (),
                          states: tuple[str, ...] = (), target: str | None = None,
                          since: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """新的在前。"""
    init_db()
    where, args = [], []
    if day:
        where.append("day = ?")
        args.append(day)
    if actions:
        where.append(f"action IN ({','.join('?' * len(actions))})")
        args.extend(actions)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    if target:
        where.append("target = ?")
        args.append(target)
    if since is not None:
        where.append("created_at >= ?")
        args.append(since)
    sql = "SELECT * FROM outbound_actions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_outbound_action(action_id: str, **fields: Any) -> None:
    bad = set(fields) - _OUTBOUND_FIELDS
    if bad:
        raise ValueError(f"不能改的互动字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE outbound_actions SET {sets} WHERE id = ?",
                     (*fields.values(), action_id))


def cas_outbound_state(action_id: str, expected: tuple[str, ...], new: str,
                       **fields: Any) -> bool:
    """状态从 expected 之一改成 new;已经被别人改走就返回 False(抢不到就说明有人在做)。"""
    bad = set(fields) - _OUTBOUND_FIELDS
    if bad:
        raise ValueError(f"不能改的互动字段:{sorted(bad)}")
    init_db()
    sets = "".join(f", {k} = ?" for k in fields)
    marks = ",".join("?" * len(expected))
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE outbound_actions SET state = ?{sets} WHERE id = ? AND state IN ({marks})",
            (new, *fields.values(), action_id, *expected))
        return cur.rowcount > 0


def count_outbound(day: str, action: str | None = None,
                   states: tuple[str, ...] = ("done",)) -> int:
    """某天某动作做了多少条(默认只数成功的)。"""
    init_db()
    sql = "SELECT COUNT(*) FROM outbound_actions WHERE day = ?"
    args: list[Any] = [day]
    if action:
        sql += " AND action = ?"
        args.append(action)
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        args.extend(states)
    with connect() as conn:
        return int(conn.execute(sql, args).fetchone()[0])


def reset_stuck_outbound(older_than: float, now: float | None = None) -> int:
    """卡在 doing 超过 `older_than` 秒的(进程崩了)退回 planned,好重排一次。返回改了几条。"""
    init_db()
    now = now or time.time()
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE outbound_actions SET state = 'planned', started_at = NULL"
            " WHERE state = 'doing' AND COALESCE(started_at, created_at) <= ?",
            (now - older_than,))
        return cur.rowcount


def add_industry_event(row: dict[str, Any]) -> bool:
    """同品类同一天同标题的只记一次。返回是否新增。"""
    init_db()
    row = {"kind": "other", "car": "", "source": "manual", "url": "", "status": "active",
           "plan_item_id": "", **row}
    row.setdefault("created_at", time.time())
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO industry_events (id, category, day, kind, car, title, source,"
            " url, status, plan_item_id, created_at) VALUES (:id, :category, :day, :kind, :car,"
            " :title, :source, :url, :status, :plan_item_id, :created_at)", row)
    return bool(cur.rowcount)


def get_industry_event(event_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute("SELECT * FROM industry_events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def list_industry_events(category: str, *, day_from: str = "", day_to: str = "",
                         statuses: tuple[str, ...] = ("active",),
                         limit: int = 200) -> list[dict[str, Any]]:
    """按日期排。day_from / day_to 是闭区间的 YYYY-MM-DD。"""
    init_db()
    where, args = ["category = ?"], [category]
    if day_from:
        where.append("day >= ?")
        args.append(day_from)
    if day_to:
        where.append("day <= ?")
        args.append(day_to)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    sql = "SELECT * FROM industry_events WHERE " + " AND ".join(where) + \
        " ORDER BY day, created_at LIMIT ?"
    args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def update_industry_event(event_id: str, **fields: Any) -> None:
    bad = set(fields) - {"status", "plan_item_id", "day", "kind", "car", "title"}
    if bad:
        raise ValueError(f"不能改的日历字段:{sorted(bad)}")
    if not fields:
        return
    init_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE industry_events SET {sets} WHERE id = ?", (*fields.values(), event_id))
