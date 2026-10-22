"""发布域:把成片发到各平台,并把平台数据抓回来。

分层:

- `base` —— 契约(PlatformAdapter / PublishRequest / PublishResult / MetricSnapshot)
- `registry` —— 平台规则与 adapter 解析
- `accounts` —— 账号与凭据
- `metadata` —— 文案生成(纯函数)
- `service` —— 编排:建任务、状态机、重试、后台线程
- `platforms/` —— 各平台的具体实现
- `http` —— 带 Cookie 的 HTTP 工具与错误分级
"""
from .base import (
    Account,
    AccountStatus,
    MetricSnapshot,
    PlatformRef,
    PublishError,
    PublishRequest,
    PublishResult,
)
from .registry import PLATFORMS, PlatformRule, adapter_for, platforms_info, rule_for

__all__ = [
    "Account", "AccountStatus", "MetricSnapshot", "PlatformRef", "PublishError",
    "PublishRequest", "PublishResult", "PLATFORMS", "PlatformRule", "adapter_for",
    "platforms_info", "rule_for",
]
