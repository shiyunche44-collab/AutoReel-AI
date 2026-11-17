"""LLM Provider 协议。

全流程只有两个能力需求:
1. `structured()` —— 拿回**结构化 JSON**。脚本、事实、分镜都是结构化产物,
   用 JSON 模式比解析自由文本可靠得多,也便于 Pydantic 校验。
2. `available()` —— 探测是否可用,让 Web 层在缺 Key 时给出明确提示而非运行到一半才炸。
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """LLM 调用失败。"""


class LLMNotConfigured(LLMError):
    """缺少 API Key —— 属于配置问题,不是运行期错误,应尽早暴露。"""


class LLMOutputInvalid(LLMError):
    """模型没有按要求返回可解析的 JSON。"""


class LLMTruncated(LLMError):
    """输出撞到长度上限被截断(finish_reason=length)。原样重试只会再截一次,不重试。"""


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def structured(
        self,
        system: str,
        user: str,
        *,
        required_keys: list[str] | None = None,
        temperature: float = 0.6,
        max_retries: int = 2,
    ) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# 解析辅助
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """从模型输出中稳健地提取 JSON 对象。

    模型即使被要求"只输出 JSON",仍常带上 Markdown 围栏或前后解释文字,
    这里逐级降级:直接解析 → 去围栏 → 截取首个平衡的花括号块。
    """
    if not text:
        raise LLMOutputInvalid("模型返回为空")
    raw = text.strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _FENCE_RE.search(raw)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start : i + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        else:
            # 开头的花括号到文末都没配平 = 输出半截断了。再往里找只会捞到某个内层对象
            # (曾把脚本的第一个段落当成整份返回,报成"缺少必需字段",查了半天)。
            # 前面有解释文字时不算:那可能只是正文里一个落单的花括号
            if raw[:start].strip() in ("", "```", "```json"):
                raise LLMOutputInvalid(
                    f"输出的 JSON 不完整(疑似被截断),结尾:{raw[-120:]}")
        start = raw.find("{", start + 1)

    raise LLMOutputInvalid(f"无法从输出中提取 JSON: {raw[:300]}")


def require_keys(data: dict[str, Any], keys: list[str] | None) -> None:
    if not keys:
        return
    missing = [k for k in keys if k not in data]
    if missing:
        raise LLMOutputInvalid(f"返回缺少必需字段: {', '.join(missing)}")
