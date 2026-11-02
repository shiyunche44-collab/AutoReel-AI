"""Provider 响应的形状约定:`cli.call()` 可能返回 payload 字典,也可能直接返回 list,
调用方必须两种都兼容。这里把约定写成测试锁住。
"""
from __future__ import annotations

import pytest

from app.providers import audio, cli


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
