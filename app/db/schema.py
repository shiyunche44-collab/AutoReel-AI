"""建表、迁移与初始化。

`_INITIALISED` 放在包 `app.db` 上(测试通过 `monkeypatch.setattr(db, "_INITIALISED", False)`
重置),所以这里经 `sys.modules` 读写,而不是用本模块的全局。
"""
from __future__ import annotations

import sqlite3
import sys
import threading

from .. import config
from .core import db_path

_SCHEMA_LOCK = threading.Lock()

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


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    """返回某张表已存在的列名集合。"""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补上后加的列。

    `SCHEMA` 全是 `CREATE TABLE IF NOT EXISTS`,它**不会**给已存在的表补列 ——
    本项目此前也没有任何迁移机制。新装机器上建表是完整的,老机器上的
    AutoReel-AI.db 则停留在建表那一刻。因此每加一列都必须在这里显式补。
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
    pkg = sys.modules[__package__]
    with _SCHEMA_LOCK:
        if pkg._INITIALISED:
            return
        config.ensure_dirs()
        conn = sqlite3.connect(db_path(), timeout=30)
        try:
            conn.executescript(SCHEMA)
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()
        pkg._INITIALISED = True
