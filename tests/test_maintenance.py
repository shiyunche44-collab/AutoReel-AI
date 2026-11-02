"""磁盘与缓存的生命周期。

`data/` 已经 3 GB 出头且只会涨,并行渲染会成倍放大。这一层要在放开
并发**之前**落地。

原则:**只清可重建的,不动不可再生的。**

- `_cache/tts`、`_cache/motion` 是内容寻址的,过期了重新生成即可;
- 共享 BGM 曲库、素材池重建都要花钱,**不自动清**;
- 成片、封面、用户手改的脚本与分镜**永不自动删**。
"""
from __future__ import annotations

import os
import time

import pytest

from app import config, db, maintenance
from app.pipeline import library


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    yield db
    db._INITIALISED = False


def _aged(path, days: float) -> None:
    """把文件的修改时间往前挪,模拟"很久没用过"。"""
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _write(path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --------------------------------------------------------------------------
# 缓存
# --------------------------------------------------------------------------
def test_prune_caches_removes_old_but_keeps_recent(store):
    cache = config.DATA_DIR / "_cache"
    old_tts = cache / "tts" / "old.wav"
    new_tts = cache / "tts" / "new.wav"
    old_motion = cache / "motion" / "old.mp4"
    _write(old_tts)
    _write(new_tts)
    _write(old_motion)
    _aged(old_tts, 90)
    _aged(old_motion, 90)

    out = maintenance.prune_caches(max_age_days=30)
    assert out["removed"] == 2
    assert not old_tts.exists()
    assert not old_motion.exists()
    assert new_tts.exists(), "没到期的缓存不该动"


def test_prune_caches_never_touches_bgm_by_default(store):
    """共享曲库重建要花钱,默认不碰。"""
    song = config.DATA_DIR / "_cache" / "bgm" / "abc" / "song.mp3"
    _write(song)
    _aged(song, 365)

    maintenance.prune_caches(max_age_days=1)
    assert song.exists(), "BGM 曲库默认不该被清"

    maintenance.prune_caches(max_age_days=1, include_bgm=True)
    assert not song.exists(), "显式 include_bgm 才清"


def test_prune_caches_dry_run_deletes_nothing(store):
    old = config.DATA_DIR / "_cache" / "tts" / "old.wav"
    _write(old)
    _aged(old, 90)
    out = maintenance.prune_caches(max_age_days=30, dry_run=True)
    assert out["dry_run"] is True
    assert out["removed"] == 1
    assert old.exists(), "dry-run 不该真的删"


# --------------------------------------------------------------------------
# 临时物
# --------------------------------------------------------------------------
def test_prune_temp_clears_probe_and_tmp(store):
    _write(config.DATA_DIR / "_probe" / "a.jpg")
    _write(config.DATA_DIR / "_tmp" / "b.mp4")
    stray = config.DATA_DIR / "_tmp_shot.mp4"
    _write(stray)

    out = maintenance.prune_temp()
    assert out["removed"] >= 3
    assert not (config.DATA_DIR / "_probe").exists()
    assert not (config.DATA_DIR / "_tmp").exists()
    assert not stray.exists()


def test_prune_temp_keeps_everything_else(store):
    keep = config.DATA_DIR / "projects" / "p1" / "output.mp4"
    _write(keep)
    _write(config.DATA_DIR / "_tmp" / "b.mp4")
    maintenance.prune_temp()
    assert keep.exists()


# --------------------------------------------------------------------------
# 项目中间产物
# --------------------------------------------------------------------------
def test_prune_project_keeps_creative_content(store):
    """成片、封面、用户手改的脚本与分镜**永不自动删**。"""
    root = config.project_dir("p1")
    _write(root / "output.mp4")
    _write(root / "cover.jpg")
    _write(root / "script.json", "{}")
    _write(root / "storyboard.json", "[]")
    _write(root / "drafts" / "000.mp4")
    _write(root / "shots" / "000.mp4")
    _write(root / "audio" / "tts_000.mp3")
    _write(root / "facts_raw" / "raw.json")

    out = maintenance.prune_project("p1")
    assert "drafts" in out["removed"]
    assert (root / "output.mp4").exists()
    assert (root / "cover.jpg").exists()
    assert (root / "script.json").exists()
    assert (root / "storyboard.json").exists()
    assert not (root / "drafts").exists()
    assert not (root / "shots").exists()
    assert not (root / "audio").exists()
    assert not (root / "facts_raw").exists()


# --------------------------------------------------------------------------
# 素材池
# --------------------------------------------------------------------------
def _make_pool(subject: str) -> None:
    slug = library.slug(subject)
    d = config.DATA_DIR / library.LIB_ROOT_NAME / slug
    _write(d / "library.json", "{}")
    _write(d / "clips" / "a.mp4")


def test_evict_pool_refuses_when_a_project_uses_it(store):
    _make_pool("小米YU7")
    store.create_project({
        "id": "p_use", "title": "t", "subject": "小米YU7", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": "DONE", "state_reason": None,
        "quota_budget": 40.0, "spent_cost": 0.0, "est_cost": 0.0,
        "created_at": 1.0, "updated_at": 1.0,
    })
    out = maintenance.evict_pool("小米YU7")
    assert out["ok"] is False
    assert "在用" in out["reason"]
    assert (config.DATA_DIR / library.LIB_ROOT_NAME / library.slug("小米YU7")).exists()


def test_evict_pool_deletes_when_unused(store):
    _make_pool("小米YU7")
    out = maintenance.evict_pool("小米YU7")
    assert out["ok"] is True
    assert not (config.DATA_DIR / library.LIB_ROOT_NAME / library.slug("小米YU7")).exists()


def test_pools_in_use_maps_projects_to_pools(store):
    store.create_project({
        "id": "p_use", "title": "t", "subject": "小米YU7", "category": "auto_long",
        "theme": "buyguide", "angle": "", "subjects": [], "duration": 240,
        "shot_count": 1, "narration_mode": "tts", "state": "DONE", "state_reason": None,
        "quota_budget": 40.0, "spent_cost": 0.0, "est_cost": 0.0,
        "created_at": 1.0, "updated_at": 1.0,
    })
    used = maintenance.pools_in_use()
    assert library.slug("小米YU7") in used
    assert "p_use" in used[library.slug("小米YU7")]


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def test_report_lists_sizes(store):
    _write(config.DATA_DIR / "projects" / "p1" / "output.mp4", "x" * 100)
    info = maintenance.report()
    assert info["total_bytes"] >= 100
    assert "projects" in info["parts"]
    assert info["total"].endswith(("B", "KB", "MB", "GB"))


# --------------------------------------------------------------------------
# 体积上限(LRU)
# --------------------------------------------------------------------------
def test_prune_caches_lru_trims_to_limit_oldest_first(store):
    """motion 缓存的文件都是新的,按龄清不动 —— 超限时按最久没用的淘汰。"""
    d = config.DATA_DIR / "_cache" / "motion"
    for i, days in enumerate((5, 4, 3, 2, 1)):
        f = d / f"{i}.mp4"
        _write(f, "x" * 100)
        _aged(f, days)

    out = maintenance.prune_caches(max_age_days=30, max_bytes={"motion": 250})
    left = sorted(p.name for p in d.iterdir())
    assert left == ["3.mp4", "4.mp4"], "超限就从最旧的删到限额内"
    assert out["lru_removed"] == 3


def test_prune_caches_under_limit_untouched(store):
    f = config.DATA_DIR / "_cache" / "motion" / "a.mp4"
    _write(f, "x" * 100)
    maintenance.prune_caches(max_age_days=30, max_bytes={"motion": 1000})
    assert f.exists()


def test_prune_caches_lru_dry_run_deletes_nothing(store):
    f = config.DATA_DIR / "_cache" / "motion" / "a.mp4"
    _write(f, "x" * 100)
    maintenance.prune_caches(max_age_days=30, dry_run=True, max_bytes={"motion": 1})
    assert f.exists()


# --------------------------------------------------------------------------
# 浏览器 profile
# --------------------------------------------------------------------------
def test_prune_browser_keeps_live_profile_and_newest_backup(store):
    root = config.DATA_DIR / "_browser" / "profiles"
    live_cookies = root / "douyin" / "Default" / "Cookies"
    live_cache = root / "douyin" / "Default" / "Cache" / "f_1"
    old_bak = root / "douyin.bak-1" / "Default" / "Cookies"
    new_bak = root / "douyin.bak-2" / "Default" / "Cookies"
    for p in (live_cookies, live_cache, old_bak, new_bak):
        _write(p)
    _aged(old_bak.parent.parent, 30)
    _aged(old_bak.parent, 30)
    login_json = config.DATA_DIR / "_browser" / "douyin.json"
    _write(login_json)

    out = maintenance.prune_browser(keep_backups=1)
    assert live_cookies.exists(), "活 profile 的登录态绝不能动"
    assert login_json.exists()
    assert not live_cache.exists(), "活 profile 只清可再生的缓存"
    assert new_bak.exists()
    assert not (root / "douyin.bak-1").exists()
    assert "douyin.bak-1" in out["removed"]


def test_prune_browser_dry_run_deletes_nothing(store):
    root = config.DATA_DIR / "_browser" / "profiles"
    bak = root / "douyin.bak-1" / "Default" / "Cookies"
    cache = root / "douyin" / "Default" / "Cache" / "f_1"
    _write(bak)
    _write(cache)
    _write(root / "douyin.bak-2" / "x")
    maintenance.prune_browser(keep_backups=0, dry_run=True)
    assert bak.exists() and cache.exists()


def test_prune_browser_skips_profile_in_use(store):
    root = config.DATA_DIR / "_browser" / "profiles"
    cache = root / "douyin" / "Default" / "Cache" / "f_1"
    _write(cache)
    _write(root / "douyin" / "SingletonLock")
    out = maintenance.prune_browser()
    assert cache.exists(), "浏览器开着时不动它的缓存"
    assert "douyin" in out["skipped"]


# --------------------------------------------------------------------------
# 调试残留
# --------------------------------------------------------------------------
def test_prune_scratch_respects_age_and_scripts(store):
    data = config.DATA_DIR
    old_png = data / "_cache" / "_dy_draft.png"
    new_png = data / "_cache" / "_dy_new.png"
    old_script = data / "_probe_thing.py"
    shot = data / "_after.png"
    for p in (old_png, new_png, old_script, shot):
        _write(p)
    for p in (old_png, old_script, shot):
        _aged(p, 30)

    maintenance.prune_scratch()
    assert not old_png.exists()
    assert not shot.exists()
    assert new_png.exists(), "新的可能还在用"
    assert old_script.exists(), "脚本默认不删"

    maintenance.prune_scratch(include_scripts=True)
    assert not old_script.exists()


# --------------------------------------------------------------------------
# 已完成项目瘦身
# --------------------------------------------------------------------------
def test_prune_finished_projects_only_done_and_old(store):
    from app.state import State

    def _mk(pid, state, age_days):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO projects (id, title, subject, duration, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (pid, "t", "s", 60, state, time.time() - age_days * 86400,
                 time.time() - age_days * 86400))
        root = config.project_dir(pid)
        _write(root / "output.mp4")
        _write(root / "drafts" / "0.mp4", "x" * 50)

    _mk("p_old_done", State.DONE.value, 30)
    _mk("p_new_done", State.DONE.value, 1)
    _mk("p_old_failed", State.FAILED.value, 30)

    out = maintenance.prune_finished_projects(older_than_days=14)
    assert out["projects"] == ["p_old_done"]
    assert (config.project_dir("p_old_done") / "output.mp4").exists()
    assert not (config.PROJECTS_DIR / "p_old_done" / "drafts" / "0.mp4").exists()
    assert (config.PROJECTS_DIR / "p_new_done" / "drafts" / "0.mp4").exists()
    assert (config.PROJECTS_DIR / "p_old_failed" / "drafts" / "0.mp4").exists()
