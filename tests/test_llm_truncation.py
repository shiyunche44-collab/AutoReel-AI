"""输出被长度上限截断时:报清楚、不重试、不把内层对象当成整份返回。

2026-11-17:8 分钟横评(60 段)的脚本 JSON 被方舟默认的 4096 token 截断,
`extract_json` 捞到第一个段落,报成"缺少必需字段: segments, title, thesis"。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.llm import deepseek as ds
from app.llm.ark import ArkProvider
from app.llm.base import LLMError, LLMOutputInvalid, LLMTruncated, extract_json

TRUNCATED = ('{\n  "title": "t",\n  "segments": [\n    {"section": "a", "text": "x", '
             '"fact_refs": ["f_1"]},\n    {"section": "b", "te')


class _Client:
    """顶替 httpx.Client:记下请求体,按队列回放响应。"""

    sent: list[dict] = []
    replies: list[dict] = []

    def __init__(self, **_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        _Client.sent.append(json)
        return httpx.Response(200, json=_Client.replies.pop(0),
                              request=httpx.Request("POST", url))


@pytest.fixture
def client(monkeypatch):
    _Client.sent, _Client.replies = [], []
    monkeypatch.setattr(ds.httpx, "Client", _Client)
    monkeypatch.setattr(ds.time, "sleep", lambda _s: None)
    return _Client


def _reply(content: str, finish: str = "stop", **usage) -> dict:
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}],
            "usage": usage}


def test_truncated_json_is_not_mistaken_for_inner_object():
    with pytest.raises(LLMOutputInvalid, match="不完整"):
        extract_json(TRUNCATED)
    with pytest.raises(LLMOutputInvalid, match="不完整"):
        extract_json("```json\n" + TRUNCATED)


def test_extract_json_still_finds_object_after_prose():
    assert extract_json('说明:用 { 开头。结果如下 {"a": 1}') == {"a": 1}
    assert extract_json('好的:\n{"a": {"b": 2}}\n以上') == {"a": {"b": 2}}


def test_length_finish_raises_truncated_without_retry(client):
    client.replies = [_reply(TRUNCATED, "length", completion_tokens=25077,
                             completion_tokens_details={"reasoning_tokens": 20981})]
    llm = ds.DeepSeekProvider(api_key="k", base_url="http://x", model="m")
    with pytest.raises(LLMTruncated, match="截断") as err:
        llm.structured("s", "u", required_keys=["segments"])
    assert len(client.sent) == 1          # 原样重试只会再截一次
    assert "20981" in str(err.value)
    assert isinstance(err.value, LLMError)


def test_other_failures_still_retry(client):
    client.replies = [_reply("不是 JSON"), _reply(json.dumps({"segments": []}))]
    llm = ds.DeepSeekProvider(api_key="k", base_url="http://x", model="m")
    assert llm.structured("s", "u", required_keys=["segments"]) == {"segments": []}
    assert len(client.sent) == 2


def test_max_tokens_sent_only_when_set(client, monkeypatch):
    client.replies = [_reply("{}"), _reply("{}"), _reply("{}")]
    ds.DeepSeekProvider(api_key="k", base_url="http://x", model="m").chat("s", "u")
    assert "max_tokens" not in client.sent[0]

    monkeypatch.setattr(ds.config, "ARK_SCRIPT_MAX_TOKENS", 16000)
    ArkProvider(api_key="k", base_url="http://x", model="m", thinking="enabled").chat("s", "u")
    assert client.sent[1]["max_tokens"] == 16000
    assert client.sent[1]["thinking"] == {"type": "enabled"}

    monkeypatch.setattr(ds.config, "ARK_SCRIPT_MAX_TOKENS", 0)
    ArkProvider(api_key="k", base_url="http://x", model="m").chat("s", "u")
    assert "max_tokens" not in client.sent[2]
