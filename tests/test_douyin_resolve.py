"""抖音「发布后回查作品 id」的测试。

网页版发布那条路,抖音不把作品 id 返回给你 —— 只在作品管理页里显示。
所以发完要自己回去查一遍,查到了才能把发布记录跟远端作品对上。

**红线:这里绝不开浏览器。** 作品列表接口整个换成假的。
"""
from __future__ import annotations

from tests.library_patch import patch_in_package

from app.publish.base import PublishError
from app.publish.platforms import douyin


def _work(wid: str, desc: str, create_time: int) -> dict:
    return {"id": wid, "create_time": create_time, "aweme": {"aweme_id": wid, "desc": desc}}


def test_resolve_finds_the_just_published_work(monkeypatch):
    works = [_work("111", "10-15万新能源轿车:装不了桩,纯电还是插混? 其他", 1700000000),
             _work("222", "10-15万新能源轿车:装不了桩,纯电还是插混? 简介", 1700000100)]
    patch_in_package(monkeypatch, douyin, "fetch_creator_works", lambda **kw: works)
    got = douyin.resolve_published_work("10-15万新能源轿车:装不了桩,纯电还是插混?",
                                         since=1700000000)
    assert got["aweme_id"] == "222"
    assert got["url"] == "https://www.douyin.com/video/222"


def test_resolve_ignores_works_from_before_this_publish(monkeypatch):
    """同标题的上一条不能认成刚发的 —— 那会把发布记录指到旧作品上。"""
    works = [_work("111", "10-15万新能源轿车:装不了桩,纯电还是插混?", 1700000000)]
    patch_in_package(monkeypatch, douyin, "fetch_creator_works", lambda **kw: works)
    got = douyin.resolve_published_work("10-15万新能源轿车:装不了桩,纯电还是插混?",
                                         since=1700005000)
    assert got == {}


def test_resolve_returns_empty_when_not_yet_listed(monkeypatch):
    """作品入库有延迟,查不到不算失败 —— 发布本身是成功的。"""
    patch_in_package(monkeypatch, douyin, "fetch_creator_works", lambda **kw: [])
    got = douyin.resolve_published_work("标题", since=1700000000, rounds=2, gap=0)
    assert got == {}


def test_resolve_never_raises_when_the_list_fails(monkeypatch):
    def boom(**kw):
        raise PublishError("登录已失效", retryable=False, credential=True)

    patch_in_package(monkeypatch, douyin, "fetch_creator_works", boom)
    assert douyin.resolve_published_work("标题", rounds=1) == {}


def test_resolve_without_title_does_nothing(monkeypatch):
    called = []
    patch_in_package(monkeypatch, douyin, "fetch_creator_works", lambda **kw: called.append(1) or [])
    assert douyin.resolve_published_work("") == {}
    assert called == []
