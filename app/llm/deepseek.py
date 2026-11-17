"""DeepSeek Provider(OpenAI 兼容接口)。

默认 LLM。理由:脚本生成与提示词编译对质量要求高、对延迟不敏感,
DeepSeek 的中文文案能力与 JSON 稳定性足够,且成本相对视频生成可忽略。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .. import config
from . import ledger
from .base import LLMError, LLMNotConfigured, LLMTruncated, extract_json, require_keys


class DeepSeekProvider:
    name = "deepseek"
    #: 并进每次请求体的额外字段(子类用,如方舟的 thinking 开关)
    extra_payload: dict[str, Any] = {}
    #: 正文长度上限(token);0 = 不传,用接口默认。方舟默认只给 4096,长稿会被截断
    max_tokens: int = 0

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 120) -> None:
        self.api_key = api_key if api_key is not None else config.deepseek_api_key()
        self.base_url = (base_url or config.deepseek_base_url()).rstrip("/")
        self.model = model or config.deepseek_model()
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def _ensure(self) -> None:
        if not self.api_key:
            key = "ARK_API_KEY" if config.text_llm() == "ark" else "DEEPSEEK_API_KEY"
            raise LLMNotConfigured(
                f"未配置 {key}(文字通道 TEXT_LLM={config.text_llm()})。请在 .env 填入 Key。"
            )

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.6,
        json_mode: bool = True,
    ) -> str:
        """原始对话补全,返回文本。"""
        self._ensure()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        payload.update(self.extra_payload)

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        data: dict[str, Any] = {}

        def _log(ok: bool, error: str = "", finish: str = "") -> None:
            ledger.record(kind="text", provider=self.name, model=self.model, started=started,
                          ok=ok, usage=data.get("usage") if isinstance(data, dict) else None,
                          system=system, prompt_chars=len(system) + len(user),
                          finish_reason=finish, error=error)

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300] if exc.response is not None else ""
            _log(False, f"HTTP {exc.response.status_code}: {detail}")
            raise LLMError(f"DeepSeek HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            _log(False, f"网络错误: {exc}")
            raise LLMError(f"DeepSeek 网络错误: {exc}") from exc

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            _log(False, f"返回结构异常: {str(data)[:200]}")
            raise LLMError(f"DeepSeek 返回结构异常: {str(data)[:300]}") from exc
        finish = str(choice.get("finish_reason") or "")
        if finish == "length":
            usage = data.get("usage") or {}
            thought = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            _log(False, "输出被截断", finish)
            raise LLMTruncated(
                f"{self.model} 的输出撞到长度上限被截断(正文 {len(content)} 字,"
                f"输出 {usage.get('completion_tokens', '?')} token"
                + (f",其中思考 {thought}" if thought else "")
                + f";max_tokens={self.max_tokens or '接口默认'})。调大 max_tokens 或缩短要写的内容。")
        _log(True, finish=finish)
        return content

    def structured(
        self,
        system: str,
        user: str,
        *,
        required_keys: list[str] | None = None,
        temperature: float = 0.6,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """要求模型返回 JSON 对象,失败时带着错误信息重试。"""
        last: Exception | None = None
        current_user = user
        for attempt in range(max_retries + 1):
            try:
                text = self.chat(system, current_user, temperature=temperature)
                data = extract_json(text)
                require_keys(data, required_keys)
                return data
            except LLMTruncated:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一转为重试
                last = exc
                if attempt < max_retries:
                    current_user = (
                        f"{user}\n\n"
                        f"[上一次调用失败:{exc}]请严格只输出一个 JSON 对象,不要任何解释文字或代码围栏。"
                    )
                    time.sleep(1.0 + attempt)
        assert last is not None
        raise LLMError(f"DeepSeek 结构化输出重试 {max_retries} 次仍失败: {last}") from last
