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


#: 已在文档里核对过的豆包 2.0 音色(`speaker`)。没有列表接口,设置页的音色下拉读这张表
VOICES = [
    {"voice_id": "zh_male_dayi_uranus_bigtts", "voice_name": "大壹(默认)"},
    {"voice_id": "zh_male_liufei_uranus_bigtts", "voice_name": "刘飞(备选)"},
    {"voice_id": "zh_female_mizai_uranus_bigtts", "voice_name": "黑猫侦探社咪仔(备选)"},
    {"voice_id": "zh_male_m191_uranus_bigtts", "voice_name": "云舟(原默认)"},
    {"voice_id": "zh_female_gaolengyujie_uranus_bigtts", "voice_name": "高冷御姐"},
    {"voice_id": "zh_female_vv_uranus_bigtts", "voice_name": "Vivi"},
]


def resolve_voice(voice_id: str) -> str:
    """旧版 profile / 旋钮里留着的音色名不是豆包音色(不以 _bigtts 结尾),换成默认。"""
    return voice_id if voice_id.endswith("_bigtts") else config.ARK_TTS_VOICE


def speech_rate(speed: float) -> int:
    """倍速 → 豆包 speech_rate(-50..100,0 为原速)。"""
    return max(-50, min(100, round((speed - 1.0) * 100)))


def synthesize(text: str, *, voice_id: str, speed: float = 1.0, timeout: float = 120,
               retries: int = 2, emotion: str = "", emotion_scale: int = 4) -> bytes:
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
    if emotion:
        # 只有多情感音色认;不认的音色接口照常出声,不会因此失败
        audio_params["emotion"] = emotion
        audio_params["emotion_scale"] = emotion_scale
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


def synthesize_take(ssml: str, *, voice_id: str, speed: float = 1.0, timeout: float = 180,
                    retries: int = 2, emotion: str = "", emotion_scale: int = 4
                    ) -> tuple[bytes, list[tuple[str, float, float]]]:
    """整段 SSML 合成一条,连逐字时间戳一起返回:(音频, [(字, 起, 止)])。时间戳算上了 break。"""
    if not config.ARK_API_KEY:
        raise ConnectorError(TOOL, "未配置 ARK_API_KEY(方舟 Agent Plan 专属 API Key)")
    headers = {
        "X-Api-Key": config.ARK_API_KEY,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    audio_params: dict = {"format": "mp3", "sample_rate": 24000, "enable_subtitle": True}
    rate = speech_rate(speed)
    if rate:
        audio_params["speech_rate"] = rate
    if emotion:
        audio_params["emotion"] = emotion
        audio_params["emotion_scale"] = emotion_scale
    body = {"user": {"uid": "aimovice"},
            "req_params": {"ssml": ssml, "speaker": resolve_voice(voice_id),
                           "audio_params": audio_params}}
    last: ConnectorError | None = None
    for attempt in range(retries + 1):
        sentences: list[dict] = []
        try:
            audio, problem, status = _stream(headers, body, timeout, sentences)
        except httpx.HTTPError as exc:
            audio, problem, status = b"", f"网络错误:{type(exc).__name__}: {exc}", 0
        if audio and not problem:
            return audio, _take_words(sentences)
        last = ConnectorError(TOOL, problem or "没有返回音频", code=status or None)
        if status and status not in _RETRY_HTTP:
            raise last
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last


def _take_words(sentences: list[dict]) -> list[tuple[str, float, float]]:
    """逐句消息里的 words 接起来。整段汇总的那条消息没有 words,自然被跳过。"""
    out: list[tuple[str, float, float]] = []
    for sentence in sentences:
        for w in sentence.get("words") or []:
            start, end = w.get("startTime"), w.get("endTime")
            if w.get("word") and start is not None and end is not None:
                if not out or float(start) >= out[-1][1]:
                    out.append((str(w["word"]), float(start), float(end)))
    return out


def _stream(headers: dict, body: dict, timeout: float,
            sentences: list[dict] | None = None) -> tuple[bytes, str, int]:
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
            if sentences is not None and isinstance(msg.get("sentence"), dict):
                sentences.append(msg["sentence"])
            code = msg.get("code")
            if code not in (None, 0, _DONE):
                problem = f"{code}:{msg.get('message')}"
    return bytes(audio), problem, 200


def synthesize_to_file(text: str, *, voice_id: str, speed: float, dest: Path,
                       timeout: float = 120, emotion: str = "") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(synthesize(text, voice_id=voice_id, speed=speed, timeout=timeout,
                                emotion=emotion))
    return dest
