"""外部接口冷却登记(`providers/cooldown.py`)和接上它的两处:B站空间接口、抖音搜索发现。"""
from __future__ import annotations

import time

import pytest

from app import db
from app.providers import cooldown, platform_video


@pytest.fixture(autouse=True)
def _db():
    db.init_db()


def test_trip_check_clear():
    cooldown.check("x")                                   # 没登记:放行
    until = cooldown.trip("x", 60, "HTTP 412")
    assert until > time.time() + 50
    with pytest.raises(cooldown.Cooling, match="HTTP 412"):
        cooldown.check("x")
    assert 50 < cooldown.remaining("x") <= 60
    assert [r["key"] for r in cooldown.active()] == ["x"]
    cooldown.clear("x")
    cooldown.check("x")
    assert cooldown.active() == []


def test_repeated_blocks_double_the_cooldown(monkeypatch):
    cooldown.trip("x", 60, "第一次")
    # 冷却刚过就又被拦:时长翻倍
    with db.connect() as conn:
        conn.execute("UPDATE cooldowns SET until = ? WHERE key = 'x'", (time.time() - 1,))
    assert cooldown.trip("x", 60, "第二次") - time.time() > 100
    # 隔了很久才再被拦:从头计
    with db.connect() as conn:
        conn.execute("UPDATE cooldowns SET until = ? WHERE key = 'x'", (time.time() - 3600,))
    assert cooldown.trip("x", 60, "很久以后") - time.time() < 61


def test_cooldown_is_capped(monkeypatch):
    monkeypatch.setattr(cooldown, "MAX_SECONDS", 100.0)
    with db.connect() as conn:
        conn.execute("INSERT INTO cooldowns VALUES ('x', ?, '', 0, 10)", (time.time() - 1,))
    assert cooldown.trip("x", 60) - time.time() <= 100


def test_registry_failure_never_blocks_calls(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cooldown, "_row", boom)
    cooldown.check("x")                                    # 读不了按可以调算
    assert cooldown.remaining("x") == 0.0


# --------------------------------------------------------------------------
# B 站空间接口
# --------------------------------------------------------------------------
def test_bilibili_risk_control_trips_and_skips_next_call(monkeypatch):
    calls: list[int] = []

    def blocked(*a, **k):
        calls.append(1)
        raise platform_video.PlatformError("B 站空间接口 HTTP 412")

    monkeypatch.setattr(platform_video, "_bilibili_uploads_once", blocked)
    monkeypatch.setattr(platform_video.time, "sleep", lambda s: None)
    with pytest.raises(platform_video.PlatformError, match="412"):
        platform_video.bilibili_uploads(1, "YU7")
    assert len(calls) == platform_video.BILI_SPACE_RETRIES
    # 冷却期内:不再发请求,直接报冷却
    with pytest.raises(platform_video.PlatformError, match="冷却中"):
        platform_video.bilibili_uploads(2, "SU7")
    assert len(calls) == platform_video.BILI_SPACE_RETRIES


def test_bilibili_other_errors_do_not_trip(monkeypatch):
    def broken(*a, **k):
        raise platform_video.PlatformError("B 站空间接口不可用:timeout")

    monkeypatch.setattr(platform_video, "_bilibili_uploads_once", broken)
    monkeypatch.setattr(platform_video.time, "sleep", lambda s: None)
    with pytest.raises(platform_video.PlatformError):
        platform_video.bilibili_uploads(1, "YU7")
    assert cooldown.active() == []


def test_bilibili_success_clears_old_cooldown(monkeypatch):
    cooldown.trip(platform_video.BILI_SPACE_COOLDOWN_KEY, 60, "旧的")
    with db.connect() as conn:
        conn.execute("UPDATE cooldowns SET until = ?", (time.time() - 1,))
    monkeypatch.setattr(platform_video, "_bilibili_uploads_once", lambda *a, **k: [])
    assert platform_video.bilibili_uploads(1, "YU7") == []
    assert cooldown._row(platform_video.BILI_SPACE_COOLDOWN_KEY) is None


# --------------------------------------------------------------------------
# 抖音搜索发现对标号
# --------------------------------------------------------------------------
def test_discover_cools_down_after_captcha_but_not_after_login(monkeypatch):
    from app.publish.base import PublishError
    from app.team import benchmark_discover as disc

    monkeypatch.setattr(disc, "keywords", lambda c, limit=5: ["SUV 推荐"])
    hits: list[str] = []

    def captcha(kw):
        hits.append(kw)
        raise PublishError("搜索页跳了验证码中间页")

    out = disc.discover("auto_long", search=captcha, sleep=lambda s: None)
    assert "验证码" in out["error"] and hits == ["SUV 推荐"]
    out = disc.discover("auto_long", search=captcha, sleep=lambda s: None)
    assert "冷却中" in out["error"] and hits == ["SUV 推荐"]   # 第二轮没去撞

    cooldown.clear(disc.SEARCH_COOLDOWN_KEY)

    def login(kw):
        raise PublishError("登录失效", retryable=False, credential=True)

    disc.discover("auto_long", search=login, sleep=lambda s: None)
    assert cooldown.active() == []                            # 登录失效另有提醒,不冷却
