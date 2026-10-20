from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_audio_cache(tmp_path, monkeypatch):
    """TTS 句子缓存和 BGM 曲库是跨项目的(data/_cache)。测试里假的音频
    绝不能写进真缓存,否则下次真出片会读到一段测试正弦音。"""
    from app.pipeline import narration

    monkeypatch.setattr(narration, "cache_root", lambda: tmp_path / "_audio_cache")
    # 自备 BGM 目录指到空的临时目录,且不联网自动收曲子
    from app import config

    monkeypatch.setattr(config, "BGM_DIR", tmp_path / "_bgm")
    monkeypatch.setattr(config, "BGM_AUTO_COLLECT", False)
