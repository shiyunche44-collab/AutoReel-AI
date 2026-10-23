from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_audio_cache(tmp_path, monkeypatch):
    """TTS 句子缓存和 BGM 曲库是跨项目的(data/_cache)。测试里假的音频
    绝不能写进真缓存,否则下次真出片会读到一段测试正弦音。"""
    from app.pipeline import narration

    monkeypatch.setattr(narration, "TTS_BACKOFF_SECONDS", 0.0)
    from app.providers import audio as _audio

    monkeypatch.setattr(_audio, "TTS_RATE_LIMIT_BACKOFF", 0.0)
    monkeypatch.setattr(_audio, "TTS_MIN_BATCH_INTERVAL", 0.0)
    monkeypatch.setattr(narration, "cache_root", lambda: tmp_path / "_audio_cache")
    # 自备 BGM 目录指到空的临时目录,且不联网自动收曲子
    from app import config

    monkeypatch.setattr(config, "BGM_DIR", tmp_path / "_bgm")
    monkeypatch.setattr(config, "BGM_AUTO_COLLECT", False)
    # 语音质检默认关(假音频是等长正弦波,语速必然"异常");专门的测试自己打开。
    # 转写永远关,免得测试去加载模型
    monkeypatch.setattr(config, "VOICE_QA", False)
    monkeypatch.setattr(config, "VOICE_QA_ASR", False)
    # 统一风格封面要联网生图;默认关,专门的测试自己打开
    monkeypatch.setattr(config, "COVER_ART", False)
