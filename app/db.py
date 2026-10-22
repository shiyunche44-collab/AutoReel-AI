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
-- 幂等基石:同一项目 + 同一账号 + 同一画幅只发一次
CREATE UNIQUE INDEX IF NOT EXISTS idx_pub_unique
    ON publications(project_id, account_id, aspect);
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
    watch_minutes   REAL,      -- 预留: 深度指标(本期不填)
    completion_rate REAL,      -- 预留: 完播率(本期不填)
    raw             TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_metric_uniq
    ON metric_snapshots(publication_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_metric_pub ON metric_snapshots(publication_id);
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
        "tags": "[]", "state": "pending", "remote_id": "", "remote_url": "",
        "upload_id": "", "error": "", "attempts": 0, "published_at": None,
        **row,
    }
    row.setdefault("created_at", now)
    row["updated_at"] = now
    with connect() as conn:
        conn.execute(
            """INSERT INTO publications
               (id, project_id, account_id, platform, aspect, artifact, title, description,
                tags, state, remote_id, remote_url, upload_id, error, attempts,
                published_at, created_at, updated_at)
               VALUES (:id, :project_id, :account_id, :platform, :aspect, :artifact, :title,
                       :description, :tags, :state, :remote_id, :remote_url, :upload_id, :error,
                       :attempts, :published_at, :created_at, :updated_at)""",
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


def find_publication(project_id: str, account_id: str, aspect: str) -> dict[str, Any] | None:
    """幂等查询:同一项目 + 同一账号 + 同一画幅只应有一条记录。"""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM publications WHERE project_id = ? AND account_id = ? AND aspect = ?",
            (project_id, account_id, aspect),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def claim_publications(limit: int = 3) -> list[dict[str, Any]]:
    """取出待推进的发布任务。

    **同一账号一次只给一条** —— 单账号高频上传会触发平台风控,这是硬要求。
    """
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM publications
               WHERE state IN ('pending', 'uploading')
               ORDER BY created_at
               LIMIT ?""",
            (limit * 4,),
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
                shares, coins, danmaku, followers, watch_minutes, completion_rate, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                publication_id, platform, captured_at,
                metrics.get("views"), metrics.get("likes"), metrics.get("comments"),
                metrics.get("favorites"), metrics.get("shares"), metrics.get("coins"),
                metrics.get("danmaku"), metrics.get("followers"),
                metrics.get("watch_minutes"), metrics.get("completion_rate"),
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
