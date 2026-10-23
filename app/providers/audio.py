"""音频 Provider:TTS 配音、BGM、语音识别质检。

解说视频的旁白必须逐字准确(车型名、"702 公里"、"3.8 秒"),因此单独成轨、
在合成阶段混音。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import cli

log = logging.getLogger(__name__)

TTS_TOOL = "connector__matrix__batch_text_to_audio"
MUSIC_TOOL = "connector__matrix__batch_text_to_music"
LISTEN_TOOL = "connector__matrix__listen_audio"

#: 批量 TTS 单次调用上限
TTS_BATCH_LIMIT = 10
#: 单批遇到 RPM 限流(1002)时的重发次数与退避基数(秒,逐次加长)。
#: 以前一批撞限流就让整次调用抛错,已经合成好的句子连同后面的全丢,
#: 上层补跑又立刻撞上同一个限流窗口 —— 143 句的长片要手动确认三四次才放行。
TTS_RATE_LIMIT_RETRIES = 4
TTS_RATE_LIMIT_BACKOFF = 30.0
#: 批与批之间的最小间隔(秒)。每批 10 句 = 10 次请求计入 RPM,实测 143 句的长片
#: 全速发会在第 6 批前后撞限流,且每次补跑只能多补回两成;主动控速到约 50 次/分钟
#: 比「撞了再退」快得多(撞限流后窗口要一分钟以上才恢复)
TTS_MIN_BATCH_INTERVAL = 12.0
_last_batch_at = 0.0


def _offset_from_name(file_name: str, prefix: str, start: int, size: int) -> int | None:
    """`{prefix}_{序号:03d}.ext` → 这一批里的位置;对不上返回 None。"""
    m = re.fullmatch(rf"{re.escape(prefix)}_(\d+)\.\w+", Path(file_name or "").name)
    if not m:
        return None
    off = int(m.group(1)) - start
    return off if 0 <= off < size else None


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "rate limit" in msg or "1002" in msg
#: 短于这个时长(秒)的一句话当作没合成出来:有文件但几乎没声音,那一拍就是哑的
MIN_CLIP_SECONDS = 0.15
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

        result = None
        for attempt in range(TTS_RATE_LIMIT_RETRIES + 1):
            global _last_batch_at
            idle = TTS_MIN_BATCH_INTERVAL - (time.monotonic() - _last_batch_at)
            if idle > 0 and _last_batch_at:
                time.sleep(idle)
            _last_batch_at = time.monotonic()
            try:
                result = cli.call(TTS_TOOL, {"requests": requests}, timeout=timeout)
                break
            except cli.ConnectorError as exc:
                if not _is_rate_limited(exc) or attempt == TTS_RATE_LIMIT_RETRIES:
                    if assets:  # 已经合成好的别丢:返回部分结果,让上层按位置补缺
                        log.warning("TTS 第 %d 批失败,保留已完成的 %d 句:%s",
                                    start // TTS_BATCH_LIMIT + 1, len(assets), exc)
                        return assets
                    raise
                wait = TTS_RATE_LIMIT_BACKOFF * (attempt + 1)
                log.info("TTS 限流,%.0f 秒后重发第 %d 批", wait, start // TTS_BATCH_LIMIT + 1)
                time.sleep(wait)
        items = _extract_success_items(result)
        for pos, item in enumerate(items):
            # 成功项只含成功的:批内哪句失败了,后面的项在列表里整体前移。
            # 按位置对回原句会把后一句的音频挂到前一句上(旁白与画面错位、
            # 某一句念的是别句的词)。回显的 file_name 带着请求时的序号,以它为准;
            # 取不到才退回按位置。
            file_name = item.get("file_name") or ""
            offset = _offset_from_name(file_name, file_prefix, start, len(batch))
            if offset is None:
                offset = pos
            if offset >= len(batch):
                continue
            node_id = item.get("node_id")
            if not node_id:
                continue
            try:
                url = cli.get_asset_url(str(node_id))
            except cli.ConnectorError as exc:
                log.warning("TTS 第 %d 句取不到下载地址:%s", start + offset, exc)
                continue
            if out_dir:
                ext = Path(file_name).suffix or ".mp3"
                dest = out_dir / f"{file_prefix}_{start + offset:03d}{ext}"
                try:
                    _download(url, dest)
                except Exception as exc:  # noqa: BLE001
                    log.warning("TTS 第 %d 句下载失败:%s", start + offset, exc)
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
