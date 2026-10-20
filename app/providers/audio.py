"""音频 Provider:TTS 配音、BGM、语音识别质检。

解说视频的旁白必须逐字准确(车型名、"702 公里"、"3.8 秒"),因此单独成轨、
在合成阶段混音。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cli

TTS_TOOL = "connector__matrix__batch_text_to_audio"
MUSIC_TOOL = "connector__matrix__batch_text_to_music"
LISTEN_TOOL = "connector__matrix__listen_audio"

#: 批量 TTS 单次调用上限
TTS_BATCH_LIMIT = 10
#: 批量音乐单次调用上限
MUSIC_BATCH_LIMIT = 5

#: 默认中文解说音色。可在设置页切换。
DEFAULT_VOICE_ID = "male-qn-jingying"   # 精英青年音色
FEMALE_VOICE_ID = "female-yujie"        # 御姐音色


@dataclass
class AudioAsset:
    """一条已生成并下载的音频。"""

    text: str
    local_path: Path
    duration: float = 0.0
    node_id: str = ""
    #: 在 synthesize_batch 输入里的位置。失败的条目不进结果,调用方要靠它对回原句
    index: int = -1


def list_voices() -> list[dict[str, str]]:
    try:
        result = cli.call("connector__matrix__get_voice_list", {}, timeout=60)
    except cli.ConnectorError:
        return []
    if isinstance(result, dict):
        voices = result.get("available_voices")
        if isinstance(voices, list):
            return [v for v in voices if isinstance(v, dict)]
    if isinstance(result, list):
        return [v for v in result if isinstance(v, dict)]
    return []


def _extract_success_items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        items = result.get("success_items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)]
    if isinstance(result, list):
        return [i for i in result if isinstance(i, dict)]
    return []


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------
def synthesize_batch(
    texts: list[str],
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    speed: float = 1.0,
    emotion: str | None = None,
    volume: float | None = None,
    output_dir: str | Path | None = None,
    timeout: int = 600,
    file_prefix: str = "tts",
) -> list[AudioAsset]:
    """批量合成语音。自动按 10 条一批切分,逐批下载到本地。

    返回的列表与输入 texts **顺序一致**;失败的条目不会出现在结果里 ——
    中间有一条失败,后面的就整体错位,调用方必须按 `AudioAsset.index` 对回原句。
    `file_prefix` 让补跑的那一批不覆盖第一批的文件。
    """
    if not texts:
        return []
    out_dir = Path(output_dir) if output_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    assets: list[AudioAsset] = []
    for start in range(0, len(texts), TTS_BATCH_LIMIT):
        batch = texts[start : start + TTS_BATCH_LIMIT]
        requests: list[dict[str, Any]] = []
        for offset, text in enumerate(batch):
            item: dict[str, Any] = {"text": text, "voice_id": voice_id}
            if speed != 1.0:
                item["speed"] = speed
            if emotion:
                item["emotion"] = emotion
            if volume is not None:
                item["volume"] = volume
            if out_dir:
                item["output_file"] = f"{file_prefix}_{start + offset:03d}.mp3"
            requests.append(item)

        result = cli.call(TTS_TOOL, {"requests": requests}, timeout=timeout)
        items = _extract_success_items(result)
        for offset, item in enumerate(items[: len(batch)]):
            node_id = item.get("node_id")
            if not node_id:
                continue
            file_name = item.get("file_name") or ""
            try:
                url = cli.get_asset_url(str(node_id))
            except cli.ConnectorError:
                continue
            if out_dir:
                ext = Path(file_name).suffix or ".mp3"
                dest = out_dir / f"{file_prefix}_{start + offset:03d}{ext}"
                try:
                    _download(url, dest)
                except Exception:
                    continue
            else:
                dest = Path(file_name or f"{file_prefix}_{start + offset:03d}.mp3")
            assets.append(
                AudioAsset(text=batch[offset], local_path=dest, node_id=str(node_id),
                           index=start + offset)
            )
    return assets


# --------------------------------------------------------------------------
# BGM
# --------------------------------------------------------------------------
def generate_music(
    prompt: str,
    *,
    lyrics: str | None = None,
    output_dir: str | Path | None = None,
    output_file: str = "bgm.mp3",
    timeout: int = 600,
) -> AudioAsset | None:
    """生成背景音乐。失败返回 None —— BGM 是锦上添花,不应阻断流水线。"""
    request: dict[str, Any] = {"prompt": prompt}
    if lyrics:
        request["lyrics"] = lyrics
    if output_dir:
        request["output_file"] = output_file
    try:
        result = cli.call(MUSIC_TOOL, {"requests": [request]}, timeout=timeout)
    except cli.ConnectorError:
        return None

    items = _extract_success_items(result)
    if not items:
        return None
    node_id = items[0].get("node_id")
    if not node_id:
        return None
    try:
        url = cli.get_asset_url(str(node_id))
    except cli.ConnectorError:
        return None
    if not output_dir:
        return AudioAsset(text=prompt, local_path=Path(output_file), node_id=str(node_id))
    dest = Path(output_dir) / output_file
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _download(url, dest)
    except Exception:
        return None
    return AudioAsset(text=prompt, local_path=dest, node_id=str(node_id))


# --------------------------------------------------------------------------
# 质检
# --------------------------------------------------------------------------
def transcribe(audio_path: str | Path, *, prompt: str = "请逐字转写这段音频内容。",
               timeout: int = 180) -> str:
    """语音识别。用于抽检 TTS 是否把数字/型号读错。"""
    local = Path(audio_path)
    try:
        url = local.as_uri() if local.is_absolute() else str(local)
        if not local.exists():
            url = cli.upload_temp_url(local)
    except Exception:
        url = str(audio_path)
    try:
        result = cli.call(LISTEN_TOOL, {"url": url, "prompt": prompt}, timeout=timeout)
    except cli.ConnectorError:
        return ""
    return _flatten_text(result)


def _flatten_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(_flatten_text(item) for item in result)
    if isinstance(result, dict):
        for key in ("text", "content", "result", "answer", "message", "transcript"):
            if key in result:
                return _flatten_text(result[key])
        return "\n".join(
            f"{k}: {v}" for k, v in result.items() if isinstance(v, (str, int, float))
        )
    return str(result)


def _download(url: str, dest: Path) -> None:
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                fh.write(chunk)
    if dest.stat().st_size == 0:
        raise RuntimeError(f"下载到空文件: {dest}")
