"""发布域:平台适配器的契约与共享数据结构。

这一层只定义**形状**,不含任何平台逻辑。上层(编排 / UI / 看板)只认这里的
契约,不认具体平台 —— 加一个平台 = 加一个 adapter,不改上层一行。

三个设计约束:

1. **规格与运行态分离**,和 `models.py` 同一套思路:`PublishRequest` 是"要发什么",
   `PublishResult` 是"平台怎么回的"。
2. **缺失即 None**。指标抓不到就是 None,不用 0 顶替 —— 0 和"没权限"在看板上是
   两回事,0 会把"没权限"画成"零播放"。
3. **失败要分级**。`PublishError.retryable` 决定调度器要不要重试:
   凭据失效重试一万次也没用,网络抖动重试三次就好了。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class PublishError(RuntimeError):
    """发布失败。

    `retryable=False` 表示重试也没用(凭据失效、参数非法、平台拒绝),
    调度器会直接标 failed,不再消耗时间。
    `credential=True` 表示登录态出了问题,UI 会给账号级告警。
    """

    def __init__(self, message: str, *, retryable: bool = True,
                 credential: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.credential = credential


@dataclass(frozen=True)
class Account:
    """一个平台账号 + 它的凭据。

    `credential` 是用户态凭据(cookie / OAuth token),`meta` 是平台特定设置
    (B站分区 tid、抖音默认话题、YouTube 频道 ID …)。
    """

    id: str
    platform: str
    name: str
    auth_kind: str = "cookie"          # cookie | oauth | browser
    credential: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def cred(self, key: str, default: str = "") -> str:
        return str(self.credential.get(key) or default)

    def setting(self, key: str, default: str = "") -> str:
        return str(self.meta.get(key) or default)


@dataclass(frozen=True)
class AccountStatus:
    """连通性自检结果。`ok=False` 时 `hint` 告诉用户该补什么。"""

    ok: bool
    message: str = ""
    hint: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishRequest:
    """一次发布请求。"""

    project_id: str
    account_id: str
    platform: str
    aspect: str = "16:9"               # 16:9 | 9:16
    video_path: Path = Path(".")
    cover_path: Path | None = None
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    #: 平台特定参数(分区 tid / 话题 / 可见范围 / 频道 ID …)
    extra: dict[str, Any] = field(default_factory=dict)
    #: 断点续跑用:上次拿到的上传会话 id
    upload_id: str = ""
    #: 拿到上传会话时的回调(把会话持久化下来,进程重启后能接着传)。
    #: 签名: `on_session(session: str) -> None`
    on_session: Any = None


@dataclass(frozen=True)
class PublishResult:
    """平台返回的结果。

    `state`:
    - `published` —— 已发布,拿得到远端 id
    - `submitted`  —— 平台已收稿、还在审核(抖音通常 1–24 小时)
    - `failed`     —— 失败
    """

    ok: bool
    state: str = "failed"
    remote_id: str = ""
    remote_url: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlatformRef:
    """远端作品的引用。抓数据时凭它回查平台。"""

    remote_id: str
    remote_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricSnapshot:
    """一次指标抓取的结果。

    平台不公开的指标留 None(不填 0)。`raw` 存平台原始返回,便于事后复盘。
    """

    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    favorites: int | None = None
    shares: int | None = None
    coins: int | None = None
    danmaku: int | None = None
    followers: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "views": self.views, "likes": self.likes, "comments": self.comments,
            "favorites": self.favorites, "shares": self.shares, "coins": self.coins,
            "danmaku": self.danmaku, "followers": self.followers,
        }

    def is_empty(self) -> bool:
        """一个指标都没取到 —— 这种快照不该写进表里污染趋势。"""
        return all(v is None for k, v in self.as_dict().items() if k != "followers") \
            and self.followers is None


@runtime_checkable
class PlatformAdapter(Protocol):
    """平台适配器契约。

    每个平台实现这三个方法就够了。官方 API 与 Cookie / 浏览器是**同一个 adapter
    内的两条实现路径**,凭据不足时显式降级并提示,不静默失败。
    """

    #: 平台 key,如 "bilibili"
    key: str
    #: 中文名
    label: str
    #: 该平台首选画幅
    preferred_aspect: str
    #: 凭据类型:cookie | oauth | browser
    auth_kind: str

    def check_account(self, account: Account) -> AccountStatus:
        """连通性自检:登录态是否有效、缺哪个权限。"""

    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        """发布。失败抛 `PublishError`。"""

    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        """抓作品数据与账号粉丝数。"""
