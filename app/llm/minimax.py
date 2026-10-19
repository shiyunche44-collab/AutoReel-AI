"""MiniMax M 系列 Provider(可选备选)。

⚠️ 实施状态说明:**本 Provider 未经过真实验证**。
用户持有 M Plan Explore 订阅并有订阅 Key(sk-cp-...),但:
  - 该 Key 未提供给本会话,因此无法验证端点、鉴权头与模型名是否正确;
  - 用户控制台已挂出"M2.5、M2.7 系列模型下线通知",默认模型必须用 M3 系列。

因此这里的模型名、端点与鉴权方式都是**基于 MiniMax 公开文档的合理实现**,
首次启用时请用 `python -c "from app.llm.minimax import MiniMaxProvider; ..."` 做一次连通性验证。
未配置 Key 时 `available()` 返回 False,平台会自动回退到 DeepSeek,不会中断流水线。
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .base import LLMError, LLMNotConfigured, extract_json, require_keys

#: M2.5 / M2.7 已挂出下线通知,默认只认 M3 系列。
DEFAULT_MODEL = "MiniMax-M3"
SUPPORTED_MODEL_PREFIXES = ("MiniMax-M3", "minimax-M3")

DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"


class MiniMaxProvider:
    name = "minimax"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 120) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("MINIMAX_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("MINIMAX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.getenv("MINIMAX_MODEL") or DEFAULT_MODEL
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key) and self.model.startswith(SUPPORTED_MODEL_PREFIXES)

    def _ensure(self) -> None:
        if not self.api_key:
            raise LLMNotConfigured(
                "未配置 MINIMAX_API_KEY(订阅 Key)。未配置时平台自动使用 DeepSeek。"
            )
        if not self.model.startswith(SUPPORTED_MODEL_PREFIXES):
            raise LLMError(
                f"模型 {self.model} 不在支持列表。M2.5/M2.7 已下线通知,请使用 M3 系列。"
            )

    def chat(self, system: str, user: str, *, temperature: float = 0.6,
             json_mode: bool = True) -> str:
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
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300] if exc.response is not None else ""
            raise LLMError(f"MiniMax HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"MiniMax 网络错误: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"MiniMax 返回结构异常: {str(data)[:300]}") from exc

    def structured(
        self,
        system: str,
        user: str,
        *,
        required_keys: list[str] | None = None,
        temperature: float = 0.6,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        last: Exception | None = None
        current_user = user
        for attempt in range(max_retries + 1):
            try:
                data = extract_json(self.chat(system, current_user, temperature=temperature))
                require_keys(data, required_keys)
                return data
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < max_retries:
                    current_user = (
                        f"{user}\n\n[上一次调用失败:{exc}]"
                        "请严格只输出一个 JSON 对象,不要解释文字或代码围栏。"
                    )
                    time.sleep(1.0 + attempt)
        assert last is not None
        raise LLMError(f"MiniMax 结构化输出重试 {max_retries} 次仍失败: {last}") from last


def resolve_default() -> MiniMaxProvider | None:
    """仅当可用时返回实例,供平台做多 Provider 降级。"""
    provider = MiniMaxProvider()
    return provider if provider.available() else None
