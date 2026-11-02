"""豆包搜索:请求组装、响应解析、直连分流。不发真实请求,不耗额度。"""
from __future__ import annotations

import httpx
import pytest

from app import config
from app.providers import ark_search, cli, direct, facts

REPLY = {
    "ResponseMetadata": {"RequestId": "x"},
    "Result": {"ResultCount": 2, "WebResults": [
        {"Title": "YU7 上市", "Snippet": "短", "Summary": "CLTC 续航 835 公里",
         "Url": "https://a/1", "SiteName": "新浪", "PublishTime": "2026-06-26T10:00:00+08:00",
         "AuthInfoDes": "正常权威", "Content": "不要正文"},
        {"Title": "无摘要", "Snippet": "只有片段", "Url": "https://a/2"},
    ]},
}


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setattr(config, "ARK_SEARCH_API_KEY", "k")
    monkeypatch.setattr(ark_search.time, "sleep", lambda s: None)
    sent, replies = [], []

    def fake(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "json": json, "headers": headers})
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ark_search.httpx, "post", fake)
    sent_replies = type("P", (), {"sent": sent, "replies": replies})
    return sent_replies


def test_parse_prefers_summary_and_drops_content():
    rows = ark_search.parse(REPLY)
    assert rows[0]["snippet"] == "CLTC 续航 835 公里" and rows[0]["authority"] == "正常权威"
    assert rows[1]["snippet"] == "只有片段"
    assert all("不要正文" not in str(r) for r in rows)


def test_parse_error_metadata_raises():
    with pytest.raises(cli.ConnectorError):
        ark_search.parse({"ResponseMetadata": {"Error": {"Code": "100013"}}})


def test_time_range_mapping():
    assert ark_search.time_range("2026-01-01", "2026-11-02") == "2026-01-01..2026-11-02"
    assert ark_search.time_range("2026-01-01", None) is None
    assert ark_search.time_range("bad", "2026-11-02") is None


def test_request_shape(post):
    post.replies.append(Resp(data=REPLY))
    ark_search.search("字" * 150, count=99, time_range="2026-01-01..2026-02-01",
                      auth_level=1, icp_only=True)
    req = post.sent[0]
    assert req["headers"]["Authorization"] == "Bearer k"
    body = req["json"]
    assert len(body["Query"]) == 100 and body["Count"] == 50
    assert body["TimeRange"] == "2026-01-01..2026-02-01"
    assert body["Filter"] == {"NeedUrl": True, "AuthInfoLevel": 1, "IcpHostOnly": True}


def test_retries_then_succeeds(post):
    post.replies += [Resp(429, text="slow"), httpx.ConnectError("boom"), Resp(data=REPLY)]
    assert len(ark_search.search("q")) == 2 and len(post.sent) == 3


def test_non_retryable_status_raises_immediately(post):
    post.replies.append(Resp(401, text="bad key"))
    with pytest.raises(cli.ConnectorError) as ei:
        ark_search.search("q")
    assert ei.value.code == 401 and len(post.sent) == 1


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr(config, "ARK_SEARCH_API_KEY", "")
    with pytest.raises(cli.ConnectorError):
        ark_search.search("q")


def test_search_news_routes_to_ark_with_window_and_label(monkeypatch, tmp_path):
    seen = {}

    def fake(query, **kw):
        seen.update(query=query, **kw)
        return ark_search.parse(REPLY)

    monkeypatch.setattr(ark_search, "search", fake)
    doc = facts.ifind_product_news("小米YU7", project_dir=tmp_path)
    assert doc.ok and "835" in doc.text
    assert doc.tool == direct.ARK_SEARCH_LABEL
    assert seen["time_range"] and ".." in seen["time_range"]
