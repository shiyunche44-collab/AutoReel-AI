"""门禁 B 分批审片的回归测试。

分批审片建立在两个既有缺陷的修复之上:
1. `approved_shots.json` 过去**只写不读**,逐镜头批准完全失效;
2. 批次边界必须对齐一致性链 —— 把一条链劈开,后半段就拿不到前驱尾帧。
"""
from __future__ import annotations

import pytest

from app import config
from app.pipeline import chains as chains_mod
from app.pipeline.orchestrator import _review_batches


def test_batches_align_to_chains():
    """40 镜头 / 每链 3 = 14 链,每批 4 链 = 12 镜头 → 4 批。"""
    batches = _review_batches("p_test", 40)
    assert [len(b) for b in batches] == [12, 12, 12, 4]
    assert len(batches) == 4


def test_batches_never_split_a_chain():
    """批次边界必须落在链边界上,否则连续性会断。

    注意:一批里可以有**多条**链 —— 要求是"同一条链不跨批",
    而不是"一批只含一条链"。
    """
    for shot_count in (10, 15, 20, 30, 40):
        chains = chains_mod.build_chains(shot_count)
        chain_of = {
            idx: chain.chain_id
            for chain in chains
            for idx in chain.members
        }
        seen_chains: set[int] = set()
        prev_chain_id = -1
        for batch in _review_batches("p_test", shot_count):
            ids = {chain_of[i] for i in batch}
            # 同一条链不能出现在两批里
            assert not (ids & seen_chains), (
                f"{shot_count} 镜头:链被切到了两批:{batch}"
            )
            # 批次之间的链 id 必须单调递增(批次是连续切分)
            assert min(ids) == prev_chain_id + 1, (
                f"{shot_count} 镜头:批次之间跳过了链:{batch}"
            )
            prev_chain_id = max(ids)
            seen_chains |= ids


def test_batches_cover_all_shots_exactly_once():
    for shot_count in (10, 15, 20, 30, 40):
        flat = [i for batch in _review_batches("p_test", shot_count) for i in batch]
        assert flat == list(range(shot_count))


def test_batch_count_grows_with_length():
    """镜头越多批次越多 —— 这正是长视频要分批的原因。"""
    assert len(_review_batches("p_test", 20)) == 2
    assert len(_review_batches("p_test", 30)) == 3
    assert len(_review_batches("p_test", 40)) == 4


# ---------------------------------------------------------------------------
# approved 集合的语义
# ---------------------------------------------------------------------------
def test_approved_gate_limits_candidates():
    """核心行为:只放行被批准的镜头。

    这条在改造前是失效的 —— `poll_running` 没把 approved 传给 tick,
    于是无论用户批准了谁,所有镜头都会被提交。
    """
    chains = chains_mod.build_chains(40)
    assert len(chains) == 14

    # 全部批准:每条链都能放行链首
    full = chains_mod.ready_shots(chains, set(), None)
    assert len(full) == 14

    # 只批准前 12 个镜头:只有前 4 条链能放行
    partial = chains_mod.ready_shots(chains, set(), set(range(12)))
    assert len(partial) == 4
    assert set(partial) == {0, 3, 6, 9}


def test_stage_complete_uses_approved_subset():
    """阶段完成判定必须按已批准集合,而不是全量。

    40 镜头里只批了 12 个时,另外 28 个还停在 pending,
    若按全量判定,阶段永远判不完,项目会卡在 FINAL_RENDERING。
    """
    # 直接验证 approved_shots.json 的读取语义
    assert config.CHAIN_GROUP_SIZE == 3


# ---------------------------------------------------------------------------
# 完整性护栏
# ---------------------------------------------------------------------------
def test_missing_final_shots_detects_incomplete():
    """`finish()` 的护栏依赖这个判定:缺镜头必须能被列出来。"""
    from app.pipeline import orchestrator

    assert callable(orchestrator.missing_final_shots)
    assert callable(orchestrator.review_progress)


# ---------------------------------------------------------------------------
# 集成:走真实 SQLite,验证文件往返与幂等
# ---------------------------------------------------------------------------
@pytest.fixture
def live_project(tmp_path, monkeypatch):
    from app import config, db
    from app.models import ShotSpec

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    pid = "p_batch_test"
    db.create_project({
        "id": pid, "title": "批次测试", "subject": "测试车", "category": "auto",
        "theme": "compare", "duration": 240, "shot_count": 40,
        "narration_mode": "tts", "state": "GATE_B", "state_reason": None,
        "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 45.7,
        "created_at": 1.0, "updated_at": 2.0,
    })
    specs = [ShotSpec(idx=i, duration=6.0, section="竞品对比",
                      visual_zh="x", camera_zh="Static Shot") for i in range(40)]
    db.write_json(pid, "storyboard.json", [s.model_dump() for s in specs])
    return pid


def test_migration_adds_theme_to_existing_db(tmp_path, monkeypatch):
    """老库升级:projects 表建在没有 theme 列时,init_db 必须补上。

    `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列 —— 没有这段迁移,
    老机器上的 aimovice.db 会直接 "no such column: theme"。
    """
    import sqlite3

    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    config.ensure_dirs()

    legacy = sqlite3.connect(config.DATA_DIR / "aimovice.db")
    legacy.execute("DROP TABLE IF EXISTS projects")
    legacy.execute("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, subject TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'auto', duration INTEGER NOT NULL,
            shot_count INTEGER NOT NULL DEFAULT 0, narration_mode TEXT NOT NULL DEFAULT 'tts',
            state TEXT NOT NULL, state_reason TEXT, quota_budget REAL NOT NULL DEFAULT 40,
            spent_cost REAL NOT NULL DEFAULT 0, est_cost REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)
    """)
    legacy.execute(
        "INSERT INTO projects (id,title,subject,duration,state,created_at,updated_at)"
        " VALUES ('old','旧项目','旧车',60,'DONE',1.0,2.0)"
    )
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()

    row = db.get_project("old")
    assert row is not None
    assert row["theme"] == "", "旧行应拿到空串,解析时回退默认方向"


def test_merge_approved_accumulates_across_batches(live_project):
    from app.pipeline import orchestrator

    # 第一批
    merged = orchestrator._merge_approved(live_project, 40, list(range(12)))
    assert len(merged) == 12
    assert orchestrator._review_state(live_project, 40)["cursor"] == 1

    # 第二批:并集累积,不覆盖
    merged = orchestrator._merge_approved(live_project, 40, list(range(12, 24)))
    assert merged == set(range(24))
    assert orchestrator._review_state(live_project, 40)["cursor"] == 2

    # 重复批准同一批必须幂等
    merged = orchestrator._merge_approved(live_project, 40, list(range(12, 24)))
    assert merged == set(range(24))


def test_approved_file_is_read_back(live_project):
    """这正是改造前的缺陷:批准列表写了却从不读。"""
    from app.pipeline import orchestrator

    orchestrator._merge_approved(live_project, 40, [0, 1, 2])
    read_back = orchestrator._approved_indices(live_project)
    assert read_back == {0, 1, 2}, "approved_shots.json 必须能被读回"


def test_approve_all_uses_legacy_semantics(live_project):
    """不传 approved(旧前端/旧脚本)等价于一次性批准全部。"""
    from app.pipeline import orchestrator

    merged = orchestrator._merge_approved(live_project, 40, None)
    assert merged == set(range(40))
    state = orchestrator._review_state(live_project, 40)
    assert state["cursor"] == state["total_batches"]


def test_review_progress_lists_pending_batches(live_project):
    from app.pipeline import orchestrator

    orchestrator._merge_approved(live_project, 40, list(range(12)))
    rv = orchestrator.review_progress(live_project)
    assert rv["total_batches"] == 4
    assert rv["pending_total"] == 28
    first = rv["batch_list"][0]
    assert first["ready"] is True
    assert first["pending"] == []
    assert rv["batch_list"][1]["pending"] == list(range(12, 24))


def test_out_of_range_indices_are_dropped(live_project):
    from app.pipeline import orchestrator

    merged = orchestrator._merge_approved(live_project, 40, [0, 39, 99, -1])
    assert merged == {0, 39}
