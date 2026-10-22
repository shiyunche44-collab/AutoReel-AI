"""Provider 响应的形状约定。

`cli.call()` 归一化 Connector 的信封后,返回的形状有**两种**:payload
字典,或(单键且值是非空 list 时)那个 list 本身。

这是"形状嗅探"换来的方便,代价是调用方**必须两种都兼容**。这里把约定
写成测试锁住,免得将来有人改了归一化逻辑,或者新增调用方只按一种形状写,
结果在很深的地方才报 TypeError。
"""
from __future__ import annotations

import pytest

from app.providers import audio, cli


def test_error_code_raises():
    with pytest.raises(cli.ConnectorError):
        cli._unwrap("t", {"code": 500, "message": "炸了"})


def test_ok_code_is_accepted():
    for code in (0, 200, "0"):
        assert cli._unwrap("t", {"code": code, "message": "ok", "x": 1}) == {"x": 1}


def test_single_key_list_is_unwrapped():
    """单键 + 非空 list → 直接给 list(调用方少写一层取值)。"""
    got = cli._unwrap("t", {"code": 0, "message": "ok", "items": [{"a": 1}]})
    assert got == [{"a": 1}]


def test_single_key_dict_is_not_unwrapped():
    """单键但值不是 list → 保持字典。"""
    got = cli._unwrap("t", {"code": 0, "message": "ok", "item": {"a": 1}})
    assert got == {"item": {"a": 1}}


def test_empty_list_is_not_unwrapped():
    """单键但值是空 list → 保持字典(空 list 会被当成"没有内容")。"""
    got = cli._unwrap("t", {"code": 0, "message": "ok", "items": []})
    assert got == {"items": []}


def test_multi_key_payload_stays_a_dict():
    got = cli._unwrap("t", {"code": 0, "message": "ok", "a": [1], "b": [2]})
    assert got == {"a": [1], "b": [2]}


def test_empty_payload_returns_the_whole_response():
    got = cli._unwrap("t", {"code": 0, "message": "ok"})
    assert got == {"code": 0, "message": "ok"}


def test_no_envelope_returns_as_is():
    got = cli._unwrap("t", [{"a": 1}])
    assert got == [{"a": 1}]


# --------------------------------------------------------------------------
# 已知的调用方:两种形状都要兼容
# --------------------------------------------------------------------------
def test_extract_success_items_handles_both_shapes():
    """TTS/BGM 的取件逻辑:list 形状与 dict 形状都要能取到。"""
    items = [{"node_id": "n1"}]
    assert audio._extract_success_items(items) == items
    assert audio._extract_success_items({"success_items": items}) == items
    assert audio._extract_success_items({"other": items}) == []


def test_list_voices_handles_both_shapes(monkeypatch):
    """音色列表:list 形状与 dict 形状都要能取到。"""
    voices = [{"voice_id": "v1"}]
    for shape in (voices, {"available_voices": voices}):
        monkeypatch.setattr(cli, "call", lambda *a, **k: shape)
        got = audio.list_voices()
        assert got == voices, f"形状 {type(shape).__name__} 取不到音色"
