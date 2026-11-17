"""模型调用账本(`app/llm/ledger.py`):每次调用记一行,带调用方、项目 / 角色、token、耗时。"""
from __future__ import annotations

import httpx
import pytest

from app import db
from app.llm import deepseek as ds
from app.llm import ledger
from app.llm.base import LLMError, LLMTruncated


class _Client:
    replies: list = []

    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        r = _Client.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        status, body = r
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def client(monkeypatch):
    db.init_db()
    _Client.replies = []
    monkeypatch.setattr(ds.httpx, "Client", _Client)
    return _Client


def _ok(content="{}", finish="stop", **usage):
    return (200, {"choices": [{"finish_reason": finish, "message": {"content": content}}],
                  "usage": usage})


def _provider():
    return ds.DeepSeekProvider(api_key="k", base_url="http://x", model="m-1")


def write_script_for_test(p):
    """模拟业务代码里的调用方:账本的 purpose 应当认出这一帧。"""
    return p.chat("系统提示词", "用户输入")


def test_success_is_recorded_with_caller_tags_and_tokens(client):
    client.replies = [_ok(prompt_tokens=120, completion_tokens=30,
                          completion_tokens_details={"reasoning_tokens": 10})]
    with ledger.tag(project_id="p_1"), ledger.tag(role="editor"):
        write_script_for_test(_provider())
    [row] = db.recent_llm_calls()
    assert row["ok"] == 1 and row["kind"] == "text" and row["model"] == "m-1"
    assert row["purpose"].endswith("test_llm_ledger.write_script_for_test")
    assert (row["project_id"], row["role"]) == ("p_1", "editor")
    assert (row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"]) == (120, 30, 10)
    assert row["system_hash"] == ledger.fingerprint("系统提示词") and row["finish_reason"] == "stop"
    assert row["prompt_chars"] == len("系统提示词") + len("用户输入")


def test_tags_do_not_leak_out_of_the_block(client):
    client.replies = [_ok()]
    with ledger.tag(project_id="p_1"):
        pass
    _provider().chat("s", "u")
    assert db.recent_llm_calls()[0]["project_id"] == ""


def test_failures_are_recorded(client):
    client.replies = [(500, {"error": "boom"}), httpx.ConnectError("断了"),
                      _ok("半截", finish="length", completion_tokens=4096)]
    p = _provider()
    with pytest.raises(LLMError):
        p.chat("s", "u")
    with pytest.raises(LLMError):
        p.chat("s", "u")
    with pytest.raises(LLMTruncated):
        p.chat("s", "u")
    rows = db.recent_llm_calls()
    assert [r["ok"] for r in rows] == [0, 0, 0]
    assert rows[0]["finish_reason"] == "length" and rows[0]["completion_tokens"] == 4096
    assert "网络错误" in rows[1]["error"] and "HTTP 500" in rows[2]["error"]
    assert db.recent_llm_calls(errors_only=True) == rows


def test_ledger_failure_never_breaks_the_call(client, monkeypatch):
    def boom(row):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, "add_llm_call", boom)
    client.replies = [_ok('{"a": 1}')]
    assert _provider().chat("s", "u") == '{"a": 1}'


def test_usage_summary_endpoint(client):
    from fastapi.testclient import TestClient

    from app.main import app

    client.replies = [_ok(prompt_tokens=10, completion_tokens=5)] * 3
    with ledger.tag(project_id="p_1"):
        for _ in range(3):
            _provider().chat("s", "u")
    c = TestClient(app)
    rows = c.get("/api/llm/usage", params={"by": "project_id"}).json()["rows"]
    assert rows == [dict(rows[0], key="p_1", calls=3, errors=0, prompt_tokens=30,
                         completion_tokens=15)]
    assert c.get("/api/llm/usage", params={"by": "乱写"}).status_code == 400
    assert len(c.get("/api/llm/calls", params={"project_id": "p_1"}).json()) == 3


def test_vision_calls_are_recorded(monkeypatch):
    from app import config
    from app.providers import direct

    db.init_db()
    monkeypatch.setattr(config, "ARK_API_KEY", "k")
    monkeypatch.setattr(direct.httpx, "post", lambda *a, **k: httpx.Response(
        200, json={"choices": [{"message": {"content": "一台白车"}}],
                   "usage": {"prompt_tokens": 900, "completion_tokens": 12}},
        request=httpx.Request("POST", "http://x")))
    assert direct._ark_vlm("data:image/png;base64,xx", "看看这是什么车", timeout=5) == "一台白车"
    [row] = db.recent_llm_calls()
    assert row["kind"] == "vision" and row["ok"] == 1 and row["prompt_tokens"] == 900
    assert "test_llm_ledger.test_vision_calls_are_recorded" in row["purpose"]


def test_prune_keeps_recent_calls(client):
    client.replies = [_ok()]
    _provider().chat("s", "u")
    with db.connect() as conn:
        conn.execute("INSERT INTO llm_calls (at, kind, provider, model, latency_ms, ok)"
                     " VALUES (1.0, 'text', 'x', 'm', 1, 1)")
    assert db.prune_llm_calls(90) == 1
    assert len(db.recent_llm_calls()) == 1
