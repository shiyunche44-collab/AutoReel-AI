"""抓取兜底:直连被拒时改用浏览器。不开真浏览器,全部 monkeypatch。"""
from __future__ import annotations

import httpx
import pytest

from app import config
from app.providers import browser_fetch, car_specs, webassets


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "BROWSER_FETCH", True)
    monkeypatch.setattr(config, "BROWSER_FETCH_HOSTS", [])
    monkeypatch.setattr(browser_fetch, "_MIN_INTERVAL", 0)
    browser_fetch._fails.clear()
    browser_fetch._last_hit.clear()


def _status(code: int, body: bytes = b""):
    def _get(url, **kw):
        req = httpx.Request("GET", url, params=kw.get("params"))
        return httpx.Response(code, content=body, request=req)
    return _get


# -- looks_blocked -----------------------------------------------------------
@pytest.mark.parametrize("html", [
    '<div id="nc_1_wrapper"></div>',
    "<html><title>安全验证</title></html>",
    "<title>Just a moment...</title>",
    '<script src="https://static.geetest.com/gt.js"></script>',
    "<p>访问过于频繁,请稍后再试</p>",
])
def test_looks_blocked_hits(html):
    assert browser_fetch.looks_blocked(html)


@pytest.mark.parametrize("html", [
    "",
    "<title>小米YU7 参数配置</title><a>登录</a><input placeholder='验证码'>",
])
def test_looks_blocked_normal_page(html):
    assert browser_fetch.looks_blocked(html) == ""


def test_host_allowed_suffix():
    assert browser_fetch.host_allowed("car.autohome.com.cn", ["autohome.com.cn"])
    assert browser_fetch.host_allowed("autohome.com.cn", ["autohome.com.cn"])
    assert not browser_fetch.host_allowed("fakeautohome.com.cn", ["autohome.com.cn"])
    assert browser_fetch.host_allowed("anything.com", [])


def test_should_fallback(monkeypatch):
    assert browser_fetch.should_fallback("https://a.com/x", 403)
    assert not browser_fetch.should_fallback("https://a.com/x", 404)
    monkeypatch.setattr(config, "BROWSER_FETCH_HOSTS", ["b.com"])
    assert not browser_fetch.should_fallback("https://a.com/x", 403)
    monkeypatch.setattr(config, "BROWSER_FETCH", False)
    assert not browser_fetch.should_fallback("https://b.com/x", 403)


# -- _http_bytes 接入 ----------------------------------------------------------
def test_http_bytes_falls_back_on_403(monkeypatch):
    monkeypatch.setattr(httpx, "get", _status(403))
    seen = {}

    def _fake(url, *, referer=None, as_text=False, timeout=30):
        seen["url"], seen["referer"] = url, referer
        return "<html>ok</html>" if as_text else b"ok"

    monkeypatch.setattr(browser_fetch, "fetch", _fake)
    assert webassets._http_text("https://a.com/p", referer="https://a.com/") == "<html>ok</html>"
    assert seen == {"url": "https://a.com/p", "referer": "https://a.com/"}


def test_http_bytes_no_fallback_on_404(monkeypatch):
    monkeypatch.setattr(httpx, "get", _status(404))
    monkeypatch.setattr(browser_fetch, "fetch",
                        lambda *a, **k: pytest.fail("404 不该走浏览器"))
    with pytest.raises(webassets.WebAssetError, match="HTTP 404"):
        webassets._http_bytes("https://a.com/p")


def test_http_bytes_disabled(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_FETCH", False)
    monkeypatch.setattr(httpx, "get", _status(403))
    monkeypatch.setattr(browser_fetch, "fetch",
                        lambda *a, **k: pytest.fail("关掉后不该走浏览器"))
    with pytest.raises(webassets.WebAssetError, match="HTTP 403"):
        webassets._http_bytes("https://a.com/p")


def test_blocked_records_and_stays_blocked(monkeypatch):
    monkeypatch.setattr(httpx, "get", _status(412))
    calls = []

    def _browser_get(url, *, referer, timeout):
        calls.append(url)
        raise browser_fetch.BrowserBlocked("页面含「geetest」")

    monkeypatch.setattr(browser_fetch, "_browser_get", _browser_get)
    with pytest.raises(webassets.WebAssetError, match="浏览器兜底"):
        webassets._http_bytes("https://a.com/p")
    items = browser_fetch.blocked_list()
    assert [b["host"] for b in items] == ["a.com"]
    # 人工处理前不再开浏览器重试
    with pytest.raises(webassets.WebAssetError, match="等人工过验证"):
        webassets._http_bytes("https://a.com/q")
    assert calls == ["https://a.com/p"]
    browser_fetch.clear_blocked("a.com")
    assert browser_fetch.blocked_list() == []


def test_fetch_caches_and_gives_up_after_fails(monkeypatch):
    bodies = iter([b"first"])
    monkeypatch.setattr(browser_fetch, "_browser_get",
                        lambda url, **k: next(bodies))
    assert browser_fetch.fetch("https://a.com/p") == b"first"
    assert browser_fetch.fetch("https://a.com/p") == b"first"  # 缓存命中,不再开浏览器

    def _boom(url, **k):
        raise browser_fetch.BrowserFetchError("timeout")

    monkeypatch.setattr(browser_fetch, "_browser_get", _boom)
    for _ in range(browser_fetch._MAX_FAILS):
        with pytest.raises(browser_fetch.BrowserFetchError, match="timeout"):
            browser_fetch.fetch("https://b.com/p")
    with pytest.raises(browser_fetch.BrowserFetchError, match="不再试"):
        browser_fetch.fetch("https://b.com/p")


# -- car_specs 接入 ------------------------------------------------------------
def test_car_specs_falls_back(monkeypatch):
    monkeypatch.setattr(httpx, "get", _status(403))
    seen = {}

    def _fake(url, *, referer=None, as_text=False, timeout=30):
        seen["url"] = url
        return '{"returncode":0,"result":{"x":1}}'.encode("gb18030")

    monkeypatch.setattr(browser_fetch, "fetch", _fake)
    assert car_specs._get_json("https://www.autohome.com.cn/api", {"a": "1"}) == {"x": 1}
    assert seen["url"] == "https://www.autohome.com.cn/api?a=1"
