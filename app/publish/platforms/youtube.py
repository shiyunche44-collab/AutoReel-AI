"""YouTube 适配器:Data API v3 上传 + 统计接口。

**为什么不用 googleapiclient**:那个包会把一大坨依赖拖进来,而这里用到的只有
两件事 —— 换 access token、发 REST 请求。httpx 手写更省,也和仓库里其它
provider 的风格一致(直接 httpx,不引 SDK)。

流程:

1. `POST oauth2.googleapis.com/token` 用 refresh token 换 access token(离线续期,
   不需要用户重新授权);
2. `POST www.googleapis.com/upload/youtube/v3/videos?part=snippet,status&uploadType=multipart`
   上传(multipart/related:第一段 JSON 元信息,第二段视频字节);
3. `GET youtube/v3/videos?part=statistics` 取作品数据;`channels?part=statistics` 取粉丝数。

注意:一次上传消耗 1600 单位配额,默认配额 10000/天 —— 批量发布时要留意。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..http import new_client, request_json
from ..registry import LANDSCAPE

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"

#: 默认可见性。账号 meta 里可以覆盖:public | unlisted | private
DEFAULT_PRIVACY = "public"
#: 视频分类(汽车/母婴解说归到 Science & Technology 之外的 Education)
DEFAULT_CATEGORY = "27"   # Education


def _client_id(account: Account) -> str:
    return account.cred("client_id") or _config("YOUTUBE_CLIENT_ID")


def _client_secret(account: Account) -> str:
    return account.cred("client_secret") or _config("YOUTUBE_CLIENT_SECRET")


def _config(key: str) -> str:
    from ... import config

    return str(getattr(config, key, "") or "")


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class YoutubeAdapter:
    """YouTube(Data API v3)。"""

    key = "youtube"
    label = "YouTube"
    preferred_aspect = LANDSCAPE
    auth_kind = "oauth"

    # -- 凭据 ------------------------------------------------------------
    def _refresh_token(self, account: Account) -> str:
        return account.cred("refresh_token")

    def _access_token(self, account: Account) -> str:
        """用 refresh token 换 access token。"""
        refresh = self._refresh_token(account)
        if not refresh:
            raise PublishError("没有配置 refresh token", retryable=False, credential=True)
        client_id = _client_id(account)
        client_secret = _client_secret(account)
        if not client_id or not client_secret:
            raise PublishError("没有配置 YouTube client id / secret", retryable=False,
                               credential=True)
        with new_client() as client:
            data = request_json(client, "POST", TOKEN_URL, timeout=60, data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token",
            })
        token = data.get("access_token")
        if not token:
            raise PublishError(f"换 token 失败:{json.dumps(data, ensure_ascii=False)[:200]}",
                               retryable=False, credential=True)
        return str(token)

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        try:
            token = self._access_token(account)
        except PublishError as exc:
            return AccountStatus(False, exc.message,
                                 hint="填 Google OAuth 的 client id / secret 与 refresh token")
        try:
            with new_client() as client:
                data = request_json(client, "GET", f"{API}/channels",
                                    params={"part": "snippet,statistics", "mine": "true"},
                                    headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except PublishError as exc:
            return AccountStatus(False, exc.message, hint="token 无效或网络不通")
        items = data.get("items") or []
        if not items:
            return AccountStatus(False, "这个账号没有 YouTube 频道",
                                 hint="refresh token 对应的账号要先建一个频道")
        channel = items[0]
        snippet = channel.get("snippet") or {}
        stats = channel.get("statistics") or {}
        return AccountStatus(
            True, f"已连接频道:{snippet.get('title') or channel.get('id')}",
            detail={"channel_id": channel.get("id"), "title": snippet.get("title"),
                    "subscribers": stats.get("subscriberCount"),
                    "videos": stats.get("videoCount")},
        )

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        token = self._access_token(account)
        title = req.title or "解说视频"
        description = req.description or ""
        tags = [t for t in req.tags[:15] if t]
        snippet: dict[str, Any] = {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": str(req.extra.get("category_id") or DEFAULT_CATEGORY),
        }
        channel_id = req.extra.get("channel_id") or account.setting("channel_id")
        if channel_id:
            snippet["channelId"] = str(channel_id)
        status = {
            "privacyStatus": str(req.extra.get("privacy_status")
                                 or account.setting("privacy_status") or DEFAULT_PRIVACY),
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        }
        metadata = {"snippet": snippet, "status": status}
        body = self._multipart(json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
                                video.read_bytes(), video.name)
        with new_client(timeout=3600) as client:
            resp = client.post(
                f"{UPLOAD_API}",
                params={"part": "snippet,status", "uploadType": "multipart"},
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "multipart/related; boundary=AutoReelAIyoutube",
                },
                timeout=3600,
            )
        if resp.status_code in (401, 403):
            raise PublishError(f"YouTube 拒绝上传(HTTP {resp.status_code}):{resp.text[:200]}",
                               retryable=False, credential=True)
        if resp.status_code == 429:
            raise PublishError("YouTube 配额用尽或被限流(429)", retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"YouTube 返回 {resp.status_code}:{resp.text[:300]}",
                               retryable=resp.status_code >= 500)
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PublishError(f"YouTube 返回的不是 JSON:{resp.text[:200]}", retryable=True) from exc
        video_id = str(data.get("id") or "")
        if not video_id:
            raise PublishError(f"YouTube 返回里没有 id:{resp.text[:200]}", retryable=True)
        return PublishResult(
            ok=True, state="published", remote_id=video_id,
            remote_url=f"https://www.youtube.com/watch?v={video_id}",
            message=f"已上传 {video_id}",
            raw={"id": video_id, "privacy": status["privacyStatus"]},
        )

    def _multipart(self, metadata: bytes, video: bytes, filename: str) -> bytes:
        """拼 multipart/related:元信息一段,视频一段。"""
        boundary = "AutoReelAIyoutube"
        head = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + metadata + b"\r\n"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8") + video + b"\r\n"
        tail = f"--{boundary}--\r\n".encode("utf-8")
        return head + body + tail

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        video_id = ref.remote_id
        if not video_id:
            raise PublishError("没有 YouTube 视频 id,无法抓数据", retryable=False)
        token = self._access_token(account)
        with new_client() as client:
            data = request_json(client, "GET", f"{API}/videos",
                                params={"part": "statistics,snippet", "id": video_id},
                                headers={"Authorization": f"Bearer {token}"}, timeout=30)
            items = data.get("items") or []
            followers = self._followers(client, token, account)
        if not items:
            raise PublishError("拿不到这个视频的统计(可能已删除)", retryable=True)
        stats = items[0].get("statistics") or {}
        snippet = items[0].get("snippet") or {}
        return MetricSnapshot(
            views=_int(stats.get("viewCount")),
            likes=_int(stats.get("likeCount")),
            comments=_int(stats.get("commentCount")),
            # 收藏 / 转发 / 投币在 YouTube 不公开
            favorites=None, shares=None, coins=None, danmaku=None,
            followers=followers,
            raw={"statistics": stats, "title": snippet.get("title")},
        )

    def _followers(self, client, token: str, account: Account) -> int | None:
        channel_id = account.setting("channel_id")
        try:
            with new_client() as inner:
                data = request_json(inner, "GET", f"{API}/channels",
                                    params={"part": "statistics", "mine": "true"},
                                    headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except PublishError:
            return None
        items = data.get("items") or []
        if not items:
            return None
        if not channel_id:
            channel_id = items[0].get("id")
        stats = items[0].get("statistics") or {}
        return _int(stats.get("subscriberCount"))
