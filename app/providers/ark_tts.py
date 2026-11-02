"""火山方舟 Agent Plan 的豆包语音合成 2.0(seed-tts-2.0)。

走 openspeech 的 `/api/v3/plan/tts/unidirectional`(套餐专用路径,不是按量的 `/api/v3/tts/...`),
鉴权就是控制台里的专属 API Key。接口是 HTTP chunked:一行一个 JSON,`data` 是 base64 音频片段,
`code=20000000` 表示结束。
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from pathlib import Path

import httpx

from .. import config
from .cli import ConnectorError

log = logging.getLogger(__name__)

TOOL = "ark_tts"
URL = "https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional"
RESOURCE_ID = "seed-tts-2.0"
_DONE = 20000000
_RETRY_HTTP = (429, 500, 502, 503, 504)


def resolve_voice(voice_id: str) -> str:
    """profile / 旋钮里的音色是 MiniMax 的名字(male-qn-jingying);不是豆包音色就换成默认。"""
    return voice_id if voice_id.endswith("_bigtts") else config.ARK_TTS_VOICE


def speech_rate(speed: float) -> int:
    """倍速 → 豆包 speech_rate(-50..100,0 为原速)。"""
    return max(-50, min(100, round((speed - 1.0) * 100)))


def synthesize(text: str, *, voice_id: str, speed: float = 1.0, timeout: float = 120,
               retries: int = 2) -> bytes:
    if not config.ARK_API_KEY:
        raise ConnectorError(TOOL, "未配置 ARK_API_KEY(方舟 Agent Plan 专属 API Key)")
    headers = {
        "X-Api-Key": config.ARK_API_KEY,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    audio_params: dict = {"format": "mp3", "sample_rate": 24000}
    rate = speech_rate(speed)
    if rate:
        audio_params["speech_rate"] = rate
    body = {"user": {"uid": "aimovice"},
            "req_params": {"text": text, "speaker": resolve_voice(voice_id),
                           "audio_params": audio_params}}
    last: ConnectorError | None = None
    for attempt in range(retries + 1):
        try:
            audio, problem, status = _stream(headers, body, timeout)
        except httpx.HTTPError as exc:
            audio, problem, status = b"", f"网络错误:{type(exc).__name__}: {exc}", 0
        if audio and not problem:
            return audio
        last = ConnectorError(TOOL, problem or "没有返回音频", code=status or None)
        if status and status not in _RETRY_HTTP:
            raise last
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last


def _stream(headers: dict, body: dict, timeout: float) -> tuple[bytes, str, int]:
    audio = bytearray()
    problem = ""
    with httpx.stream("POST", URL, headers=headers, json=body, timeout=timeout) as resp:
        if resp.status_code != 200:
            raw = resp.read().decode("utf-8", "replace")[:300]
            return b"", f"HTTP {resp.status_code}:{raw}", resp.status_code
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("data"):
                audio.extend(base64.b64decode(msg["data"]))
            code = msg.get("code")
            if code not in (None, 0, _DONE):
                problem = f"{code}:{msg.get('message')}"
    return bytes(audio), problem, 200


def synthesize_to_file(text: str, *, voice_id: str, speed: float, dest: Path,
                       timeout: float = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(synthesize(text, voice_id=voice_id, speed=speed, timeout=timeout))
    return dest
