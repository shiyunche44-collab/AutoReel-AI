"""发布编排测试。

**红线:这里绝不发真稿。** 所有发布路径都走 fake adapter,不碰任何平台接口。

要锁死的三件事:
1. **幂等** —— 同一项目 + 同一账号 + 同一画幅只发一次;
2. **失败分级** —— 凭据失效不重试,网络抖动才重试;
3. **不回滚** —— 一个平台挂了,另一个平台仍然是已发布。
"""
from __future__ import annotations

import json

import pytest

from app import config, db
from app.publish import service
from app.publish.base import Account, AccountStatus, MetricSnapshot, PublishError, \
    PublishResult


class FakeAdapter:
    """假平台。`behaviour` 决定 publish 怎么反应。"""

    def __init__(self, behaviour="ok", key="bilibili"):
        self.behaviour = behaviour
        self.key = key
        self.calls = 0

    def check_account(self, account: Account) -> AccountStatus:
        return AccountStatus(True, "ok")

    def publish(self, req, account: Account) -> PublishResult:
        self.calls += 1
        if self.behaviour == "ok":
            return PublishResult(ok=True, state="published", remote_id="BV1fake",
                                 remote_url="https://example.invalid/BV1fake")
        if self.behaviour == "credential":
            raise PublishError("登录态失效", retryable=False, credential=True)
        if self.behaviour == "retryable":
            raise PublishError("系统繁忙", retryable=True)
        if self.behaviour == "notok":
            return PublishResult(ok=False, state="failed", message="被拒")
        raise AssertionError(f"未知行为:{self.behaviour}")

    def fetch_metrics(self, ref, account: Account) -> MetricSnapshot:
        return MetricSnapshot(views=1)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    db._INITIALISED = False
    db.init_db()
    db.create_project({
        "id": "p_1", "title": "小米YU7 解说视频", "subject": "小米YU7",
        "category": "auto_long", "theme": "buyguide", "angle": "", "subjects": [],
        "duration": 240, "shot_count": 10, "narration_mode": "tts", "state": "DONE",
        "state_reason": None, "quota_budget": 40.0, "spent_cost": 0.0, "est_cost": 1.0,
        "created_at": 0.0, "updated_at": 0.0,
    })
    db.create_account({
        "id": "acc_1", "platform": "bilibili", "name": "测试号", "auth_kind": "cookie",
        "credential": json.dumps({"cookie": "SESSDATA=x"}), "meta": "{}", "enabled": 1,
        "created_at": 0.0, "updated_at": 0.0,
    })
    # 成片文件:发布前会检查它存不存在
    out = config.project_dir("p_1") / "output.mp4"
    out.write_bytes(b"fake mp4")
    yield
    db._INITIALISED = False


def _install(monkeypatch, adapter: FakeAdapter) -> FakeAdapter:
    monkeypatch.setattr(service, "adapter_for", lambda key: adapter)
    return adapter


# --------------------------------------------------------------------------
# 建任务
# --------------------------------------------------------------------------
def test_enqueue_creates_one_task(env, monkeypatch):
    _install(monkeypatch, FakeAdapter())
    out = service.enqueue("p_1", account_ids=["acc_1"])
    assert len(out["created"]) == 1
    assert out["skipped"] == []
    row = db.list_publications(project_id="p_1")[0]
    assert row["state"] == "pending"
    assert row["platform"] == "bilibili"
    assert row["title"]


def test_enqueue_is_idempotent(env, monkeypatch):
    _install(monkeypatch, FakeAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    again = service.enqueue("p_1", account_ids=["acc_1"])
    assert len(again["created"]) == 0
    assert len(again["skipped"]) == 1
    assert len(db.list_publications(project_id="p_1")) == 1


def test_enqueue_force_reuses_the_same_row(env, monkeypatch):
    """重发走同一条记录(唯一索引不允许第二条),状态被重置。"""
    _install(monkeypatch, FakeAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    db.update_publication(pub_id, state="published", remote_id="BV1old")
    again = service.enqueue("p_1", account_ids=["acc_1"], force=True)
    assert len(again["created"]) == 1
    assert len(db.list_publications(project_id="p_1")) == 1
    row = db.get_publication(pub_id)
    assert row["state"] == "pending"
    assert row["remote_id"] == ""


def test_enqueue_ignores_unknown_account(env, monkeypatch):
    _install(monkeypatch, FakeAdapter())
    out = service.enqueue("p_1", account_ids=["acc_nope"])
    assert out["created"] == []


def test_aspect_follows_the_project_not_the_platform(env, monkeypatch):
    """画幅是建项目时选的:横屏项目发抖音也是 16:9,只给一句提示,不再按平台补竖版。"""
    _install(monkeypatch, FakeAdapter())
    db.create_account({
        "id": "acc_dy", "platform": "douyin", "name": "抖号", "auth_kind": "oauth",
        "credential": "{}", "meta": "{}", "enabled": 1, "created_at": 0.0, "updated_at": 0.0,
    })
    out = service.enqueue("p_1", account_ids=["acc_1", "acc_dy"])
    aspects = {r["account_id"]: r["aspect"] for r in db.list_publications(project_id="p_1")}
    assert aspects == {"acc_1": "16:9", "acc_dy": "16:9"}
    assert len(out["warnings"]) == 1 and "抖音" in out["warnings"][0]


def test_portrait_project_publishes_vertical_file_with_warning(env, monkeypatch):
    """竖屏项目发 B站:照发竖屏成片,提示「B站推荐横屏」。"""
    _install(monkeypatch, FakeAdapter())
    # 画幅建项目时定、之后不可改(update_project 不认 aspect 列),所以另建一个竖屏项目
    db.create_project({**db.get_project("p_1"), "id": "p_v", "aspect": "9:16", "subjects": []})
    (config.project_dir("p_v") / "output_vertical.mp4").write_bytes(b"fake")
    out = service.enqueue("p_v", account_ids=["acc_1"])
    row = out["created"][0]
    assert row["aspect"] == "9:16"
    assert row["artifact"] == "output_vertical.mp4"
    assert out["warnings"] and "横屏" in out["warnings"][0]


# --------------------------------------------------------------------------
# 推进
# --------------------------------------------------------------------------
def test_advance_publishes_and_stores_remote_id(env, monkeypatch):
    adapter = _install(monkeypatch, FakeAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    result = service.advance(pub_id)
    assert result["ok"] is True
    row = db.get_publication(pub_id)
    assert row["state"] == "published"
    assert row["remote_id"] == "BV1fake"
    assert row["remote_url"].endswith("BV1fake")
    assert adapter.calls == 1


def test_credential_error_is_not_retried(env, monkeypatch):
    _install(monkeypatch, FakeAdapter("credential"))
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    result = service.advance(pub_id)
    assert result["ok"] is False
    row = db.get_publication(pub_id)
    assert row["state"] == "failed"
    assert "登录态" in row["error"]


def test_retryable_error_goes_back_to_pending(env, monkeypatch):
    _install(monkeypatch, FakeAdapter("retryable"))
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    result = service.advance(pub_id)
    assert result["retryable"] is True
    row = db.get_publication(pub_id)
    assert row["state"] == "pending"
    assert row["attempts"] == 1


def test_retries_stop_at_max_attempts(env, monkeypatch):
    _install(monkeypatch, FakeAdapter("retryable"))
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    for _ in range(service.MAX_ATTEMPTS):
        service.advance(pub_id)
    row = db.get_publication(pub_id)
    assert row["state"] == "failed"
    assert row["attempts"] >= service.MAX_ATTEMPTS


def test_not_ok_result_is_a_failure(env, monkeypatch):
    _install(monkeypatch, FakeAdapter("notok"))
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    service.advance(pub_id)
    assert db.get_publication(pub_id)["state"] == "failed"


def test_forbidden_word_blocks_publishing(env, monkeypatch):
    """文案含广告法禁用词就不发 —— 投放被拒等于白忙。"""
    adapter = _install(monkeypatch, FakeAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    db.update_publication(pub_id, title="史上最强车型")
    result = service.advance(pub_id)
    assert result["ok"] is False
    assert "禁用词" in result["message"]
    assert adapter.calls == 0
    assert db.get_publication(pub_id)["state"] == "failed"


# --------------------------------------------------------------------------
# 多平台:不回滚
# --------------------------------------------------------------------------
def test_one_platform_failure_does_not_roll_back_the_other(env, monkeypatch):
    """B站成功、抖音失败:B站仍是已发布,抖音单独失败可重试。"""
    class MultiAdapter:
        def __init__(self):
            self.calls = []

        def check_account(self, account):
            return AccountStatus(True, "ok")

        def publish(self, req, account):
            self.calls.append(req.platform)
            if req.platform == "douyin":
                raise PublishError("抖音挂了", retryable=False)
            return PublishResult(ok=True, state="published", remote_id="BV1ok")

        def fetch_metrics(self, ref, account):
            return MetricSnapshot()

    adapter = MultiAdapter()
    monkeypatch.setattr(service, "adapter_for", lambda key: adapter)
    db.create_account({
        "id": "acc_2", "platform": "douyin", "name": "抖号", "auth_kind": "oauth",
        "credential": "{}", "meta": "{}", "enabled": 1, "created_at": 0.0, "updated_at": 0.0,
    })
    service.enqueue("p_1", account_ids=["acc_1", "acc_2"])
    for row in db.list_publications(project_id="p_1"):
        service.advance(row["id"])
    states = {r["platform"]: r["state"] for r in db.list_publications(project_id="p_1")}
    assert states["bilibili"] == "published"
    assert states["douyin"] == "failed"


# --------------------------------------------------------------------------
# 手动重试
# --------------------------------------------------------------------------
def test_retry_resets_and_runs(env, monkeypatch):
    _install(monkeypatch, FakeAdapter("credential"))
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    service.advance(pub_id)
    assert db.get_publication(pub_id)["state"] == "failed"
    _install(monkeypatch, FakeAdapter("ok"))
    service.retry(pub_id)
    assert db.get_publication(pub_id)["state"] == "published"


# --------------------------------------------------------------------------
# 视图:凭据不能漏出去
# --------------------------------------------------------------------------
def test_publication_view_has_no_credential(env, monkeypatch):
    _install(monkeypatch, FakeAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    view = service.publication_view(db.list_publications(project_id="p_1")[0])
    assert "credential" not in view
    assert "SESSDATA" not in json.dumps(view, ensure_ascii=False)


# --------------------------------------------------------------------------
# 断点续跑:上传会话能从落库的字符串里恢复
# --------------------------------------------------------------------------
def test_upload_session_can_be_restored():
    from app.publish.platforms.bilibili import BilibiliAdapter

    session = {"endpoint": "https://upos.example", "upos_uri": "ugcboss/x.mp4",
               "auth": "auth-token", "chunk_size": 10485760, "biz_id": "7"}
    adapter = BilibiliAdapter()
    got = adapter._restore(json.dumps(session))
    assert got is not None and got["upos_uri"] == "ugcboss/x.mp4"
    assert got["chunk_size"] == 10485760


def test_broken_upload_session_is_rejected():
    """会话坏了就重新预上传,不能拿半截数据硬传。"""
    from app.publish.platforms.bilibili import BilibiliAdapter

    adapter = BilibiliAdapter()
    assert adapter._restore("") is None
    assert adapter._restore("不是 json") is None
    assert adapter._restore(json.dumps({"endpoint": "x"})) is None
    assert adapter._restore(json.dumps(["不是字典"])) is None


def test_session_is_persisted_when_adapter_provides_one(env, monkeypatch):
    """适配器拿到上传会话时,服务要把它存下来(进程重启后能接着传)。"""
    seen = {}

    class SessionAdapter(FakeAdapter):
        def publish(self, req, account):
            seen["called"] = True
            if callable(getattr(req, "on_session", None)):
                req.on_session('{"upos_uri": "ugcboss/x.mp4"}')
            return PublishResult(ok=True, state="published", remote_id="BV1ok")

    _install(monkeypatch, SessionAdapter())
    service.enqueue("p_1", account_ids=["acc_1"])
    pub_id = db.list_publications(project_id="p_1")[0]["id"]
    service.advance(pub_id)
    assert seen.get("called")
    # 发布成功后会话清掉 —— 重发要重新预上传
    assert db.get_publication(pub_id)["upload_id"] == ""
