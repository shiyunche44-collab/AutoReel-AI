"""ffmpeg / ffprobe 路径解析(`config._resolve_binary`)。

回归:`FFMPEG_BIN` 写成 ffmpeg 可执行文件的全路径时,ffprobe 也被解析成同一个 ffmpeg,
所有用 ffprobe 量时长的地方都拿到 0,自检却报"ffprobe 可用"。
"""
from __future__ import annotations

from app import config


def _bin(dirpath, *names):
    dirpath.mkdir(parents=True, exist_ok=True)
    for n in names:
        (dirpath / n).write_bytes(b"")
    return dirpath


def test_full_path_to_ffmpeg_finds_sibling_ffprobe(tmp_path, monkeypatch):
    d = _bin(tmp_path / "bin", "ffmpeg.exe", "ffprobe.exe")
    monkeypatch.setenv("FFMPEG_BIN", str(d / "ffmpeg.exe"))
    assert config._resolve_binary("FFMPEG_BIN", None, "ffmpeg") == d / "ffmpeg.exe"
    assert config._resolve_binary("FFMPEG_BIN", None, "ffprobe") == d / "ffprobe.exe"


def test_directory_and_names_without_extension(tmp_path, monkeypatch):
    d = _bin(tmp_path / "bin", "ffmpeg", "ffprobe")
    monkeypatch.setenv("FFMPEG_BIN", str(d))
    assert config._resolve_binary("FFMPEG_BIN", None, "ffprobe") == d / "ffprobe"


def test_missing_ffprobe_is_none_not_ffmpeg(tmp_path, monkeypatch):
    d = _bin(tmp_path / "bin", "ffmpeg.exe")
    monkeypatch.setenv("FFMPEG_BIN", str(d / "ffmpeg.exe"))
    assert config._resolve_binary("FFMPEG_BIN", None, "ffprobe") is None


def test_default_dir_is_used_when_env_unset(tmp_path, monkeypatch):
    d = _bin(tmp_path / "bin", "ffprobe.exe")
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    assert config._resolve_binary("FFMPEG_BIN", str(d), "ffprobe") == d / "ffprobe.exe"


def test_status_does_not_count_ffmpeg_as_ffprobe(tmp_path, monkeypatch):
    d = _bin(tmp_path / "bin", "ffmpeg.exe")
    monkeypatch.setattr(config, "FFMPEG_PATH", d / "ffmpeg.exe")
    monkeypatch.setattr(config, "FFPROBE_PATH", d / "ffmpeg.exe")   # 旧解析的结果
    ok, msg = config.ffmpeg_status()
    assert ok and "ffprobe 缺失" in msg
