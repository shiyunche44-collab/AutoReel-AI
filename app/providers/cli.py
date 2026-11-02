"""外部能力调用的唯一接口层。

所有上层模块(images/audio/facts)都经过这里按工具名调用,实际去向是 `direct`
(火山方舟 Agent Plan)。好处是统计、错误归一化只写一遍。工具名沿用旧 Connector 的命名。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .. import config

log = logging.getLogger(__name__)

#: 外部能力调用统计:工具名 -> {calls, errors, seconds}
#:
#: 此前外部调用**完全没有**可观测性 —— 不知道调了哪些工具、调了多少次、
#: 慢在哪、错在哪。`/api/health` 把这份统计暴露出来,排查"是不是额度
#: 烧在某个工具上"时不用再猜。
_CALL_STATS: dict[str, dict[str, float]] = {}
_CALL_STATS_LOCK = threading.Lock()


def call_stats() -> dict[str, dict[str, float]]:
    """外部能力调用统计的快照。"""
    with _CALL_STATS_LOCK:
        return {tool: dict(row) for tool, row in _CALL_STATS.items()}


def _record_call(tool: str, seconds: float, ok: bool) -> None:
    with _CALL_STATS_LOCK:
        row = _CALL_STATS.setdefault(tool, {"calls": 0.0, "errors": 0.0, "seconds": 0.0})
        row["calls"] += 1
        row["seconds"] += seconds
        if not ok:
            row["errors"] += 1


def reset_call_stats() -> None:
    """清空统计(自检脚本用)。"""
    with _CALL_STATS_LOCK:
        _CALL_STATS.clear()


class ConnectorError(RuntimeError):
    """Connector 调用失败。"""

    def __init__(self, tool: str, message: str, *, code: int | None = None) -> None:
        super().__init__(f"[{tool}] {message}")
        self.tool = tool
        self.message = message
        self.code = code


def connector_available() -> bool:
    """方舟 Key 配了没有 —— 图生图等依赖方舟的能力用它判断要不要走。"""
    return bool(config.ARK_API_KEY)


def source_label(tool: str) -> str:
    """一次调用实际用的数据源名,写进事实的 source_tool。与工具名可能不同(搜索实际是豆包搜索)。"""
    from . import direct

    return direct.source_label(tool)


def call(
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
) -> Any:
    """调用一个工具并返回其结果。`args` 必须是单个 JSON 对象。"""
    payload = args if args is not None else {}
    if not isinstance(payload, dict):
        raise ValueError("工具参数必须是单个 JSON 对象")

    from . import direct  # 延迟导入:direct 依赖本模块的异常类

    started = time.monotonic()
    try:
        out = direct.call(tool, payload, timeout=timeout or config.CONNECTOR_TIMEOUT_DEFAULT)
    except Exception:
        _record_call(tool, time.monotonic() - started, ok=False)
        raise
    _record_call(tool, time.monotonic() - started, ok=True)
    return out


# --------------------------------------------------------------------------
# 资源 URL
# --------------------------------------------------------------------------
def get_asset_url(node_id: str) -> str:
    """把生成结果里的 `url:<地址>` 还原成可下载的地址。"""
    from . import direct

    return direct.get_asset_url(node_id)


def upload_temp_url(local_path: str | Path) -> str:
    """本地文件 → 能直接塞进请求里的地址:图片是 data URI,其他是 file:// URI。"""
    from . import direct

    return direct.upload_temp_url(local_path)


def health_check() -> tuple[bool, str]:
    """连通性自检。用于启动横幅与设置页。"""
    from . import direct

    return direct.health_check()
