"""火山方舟 Agent Plan 上的豆包 Seed(OpenAI 兼容接口)。

和 DeepSeekProvider 同一套 chat / structured,只换 Key、地址和模型。Seed 是推理模型:
默认会先想一大段(写一篇脚本要几分钟),`ARK_SCRIPT_THINKING=disabled` 关掉思考,
快一个量级。目前只给脚本生成用(`config.SCRIPT_LLM=ark`)。
"""
from __future__ import annotations

from .. import config
from .base import LLMNotConfigured
from .deepseek import DeepSeekProvider


class ArkProvider(DeepSeekProvider):
    name = "ark"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, timeout: int = 900,
                 thinking: str | None = None) -> None:
        super().__init__(
            api_key=api_key if api_key is not None else config.ARK_API_KEY,
            base_url=base_url or config.ARK_BASE_URL,
            model=model or config.ARK_SCRIPT_MODEL,
            timeout=timeout,
        )
        thinking = (thinking if thinking is not None else config.ARK_SCRIPT_THINKING).strip().lower()
        # enabled / disabled / 空(= 模型默认)
        self.extra_payload = {"thinking": {"type": thinking}} if thinking in ("enabled", "disabled") else {}

    def _ensure(self) -> None:
        if not self.api_key:
            raise LLMNotConfigured("未配置 ARK_API_KEY(方舟 Agent Plan 专属 API Key)。")
