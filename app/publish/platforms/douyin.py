"""抖音适配器:开放平台优先,创作者平台网页版兜底。

**两条路**:

1. **开放平台(正规路)**:OAuth2 换 token → 分片上传 → `create_video` 建片。
   需要企业开发者资质与 `video.create` 能力审核。返回 `item_id` 就能查数据。
2. **创作者平台网页版(兜底)**:Playwright 驱动已登录的浏览器走发布页。
   没有资质时的唯一路,但页面改版就得跟进。

**100MB 限制**:开放平台单文件 ≤100MB。我们的片子 1080p 五分钟能有 200MB+,
所以超限时先用 ffmpeg 转一版 720p 再传 —— 平台自己推荐的也是 720p 起。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..http import UPLOAD_TIMEOUT, new_client, request_json
from ..registry import PORTRAIT

OPEN = "https://open.douyin.com"
#: 创作者平台发布页(网页版兜底)
CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
#: 平台文档给的单文件上限
MAX_UPLOAD = 100 * 1024 * 1024
#: 单片建议 20MB
PART_SIZE = 20 * 1024 * 1024


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _config(key: str) -> str:
    from ... import config

    return str(getattr(config, key, "") or "")


class DouyinAdapter:
    """抖音(开放平台优先,网页版兜底)。"""

    key = "douyin"
    label = "抖音"
    preferred_aspect = PORTRAIT
    auth_kind = "oauth"

    # -- 凭据 ------------------------------------------------------------
    def _access_token(self, account: Account) -> str:
        token = account.cred("access_token")
        if not token:
            raise PublishError("没有配置抖音 access_token", retryable=False, credential=True)
        return token

    def _open_id(self, account: Account) -> str:
        open_id = account.cred("open_id")
        if not open_id:
            raise PublishError("没有配置抖音 open_id", retryable=False, credential=True)
        return open_id

    def _official_ready(self, account: Account) -> bool:
        return bool(account.cred("access_token") and account.cred("open_id"))

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        if not self._official_ready(account):
            return AccountStatus(
                False, "没配开放平台凭据,只能走创作者平台网页版",
                hint="填 access_token 与 open_id(开放平台),或先在分发台点「登录」扫码创作者平台。")
        token = self._access_token(account)
        open_id = self._open_id(account)
        with new_client() as client:
            try:
                data = request_json(client, "GET", f"{OPEN}/oauth/userinfo/",
                                    params={"open_id": open_id},
                                    headers={"access-token": token}, timeout=30)
            except PublishError as exc:
                return AccountStatus(False, exc.message, hint="token 可能已过期,重新授权一次")
        extra = data.get("data") or {}
        if data.get("error_code") not in (0, None):
            return AccountStatus(False, f"平台返回 error_code={data.get('error_code')}",
                                 hint="重新授权一次")
        return AccountStatus(True, f"已授权:{extra.get('nickname') or open_id}",
                             detail={"open_id": open_id, "nickname": extra.get("nickname")})

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        if self._official_ready(account):
            return self._publish_official(req, account)
        return self._publish_browser(req, account)

    def _publish_official(self, req: PublishRequest, account: Account) -> PublishResult:
        token = self._access_token(account)
        open_id = self._open_id(account)
        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        upload_path = self._fit_size(video)
        try:
            video_id = self._upload(token, open_id, upload_path)
            return self._create(token, open_id, video_id, req)
        finally:
            if upload_path != video:
                try:
                    upload_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _fit_size(self, video: Path) -> Path:
        """超过 100MB 就转一版 720p。平台自己推荐的也是 720p 起。"""
        if video.stat().st_size <= MAX_UPLOAD:
            return video
        from ... import config

        if not config.FFMPEG_PATH:
            raise PublishError(
                f"文件 {video.stat().st_size // 1024 // 1024}MB 超过抖音 100MB 上限,"
                "又没找到 ffmpeg 可转码", retryable=False)
        out = video.parent / (video.stem + "_720p.mp4")
        if out.is_file() and out.stat().st_size <= MAX_UPLOAD:
            return out
        proc = subprocess.run(
            [str(config.FFMPEG_PATH), "-y", "-v", "error", "-i", str(video),
             "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "medium", "-crf", "26",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)],
            capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0 or not out.is_file():
            raise PublishError(f"转 720p 失败:{(proc.stderr or '')[-200:]}", retryable=True)
        return out

    def _upload(self, token: str, open_id: str, video: Path) -> str:
        """上传,拿 video_id。"""
        url = f"{OPEN}/api/douyin/v1/video/upload_video/?open_id={open_id}"
        with new_client(timeout=UPLOAD_TIMEOUT) as client:
            with video.open("rb") as fh:
                resp = client.post(url, headers={"access-token": token},
                                   files={"video": (video.name, fh, "video/mp4")},
                                   timeout=UPLOAD_TIMEOUT)
        if resp.status_code in (401, 403):
            raise PublishError(f"抖音拒绝上传(HTTP {resp.status_code}):{resp.text[:200]}",
                               retryable=False, credential=True)
        if resp.status_code >= 400:
            raise PublishError(f"抖音返回 {resp.status_code}:{resp.text[:200]}",
                               retryable=resp.status_code >= 500)
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise PublishError(f"抖音返回的不是 JSON:{resp.text[:200]}", retryable=True) from exc
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            code = extra.get("error_code")
            if code in (28001003, 28001008):
                raise PublishError("抖音 token 失效,重新授权", retryable=False, credential=True)
            raise PublishError(f"抖音上传失败:error_code={code} {extra.get('description','')}",
                               retryable=code == 2100004)
        video_id = (data.get("data") or {}).get("video_id") or ""
        if not video_id:
            raise PublishError(f"抖音上传返回里没有 video_id:{resp.text[:200]}", retryable=True)
        return str(video_id)

    def _create(self, token: str, open_id: str, video_id: str,
                req: PublishRequest) -> PublishResult:
        """建片(挂文案与封面)。"""
        text = req.title or ""
        if req.tags:
            topics = " ".join(f"#{t}" for t in req.tags[:5])
            text = f"{text} {topics}".strip()
        body: dict[str, Any] = {
            "video_id": video_id,
            "text": text[:1000],
            "download_type": 0,
            "private_status": 0,
        }
        # 封面:用第 1 秒那一帧(平台自己从视频里取)
        body["cover_tsp"] = 1.0
        url = f"{OPEN}/api/douyin/v1/video/create_video/?open_id={open_id}"
        with new_client() as client:
            data = request_json(client, "POST", url, json=body,
                                headers={"access-token": token, "content-type": "application/json"},
                                timeout=120)
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            code = extra.get("error_code")
            if code == 2114007:
                raise PublishError("今天的发布数超过平台上限(75 条)", retryable=False)
            raise PublishError(f"建片失败:error_code={code} {extra.get('description','')}",
                               retryable=code == 2100004)
        payload = data.get("data") or {}
        item_id = str(payload.get("item_id") or "")
        vid = str(payload.get("video_id") or "")
        if not item_id:
            raise PublishError(f"建片返回里没有 item_id:{json.dumps(payload, ensure_ascii=False)[:200]}",
                               retryable=True)
        return PublishResult(
            ok=True, state="submitted", remote_id=item_id,
            remote_url=f"https://www.douyin.com/video/{vid}" if vid else "",
            message="已提交,平台审核中", raw={"item_id": item_id, "video_id": vid},
        )

    # -- 网页版兜底 ------------------------------------------------------
    def _publish_browser(self, req: PublishRequest, account: Account) -> PublishResult:
        from . import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        try:
            with browser.BrowserSession("douyin") as session:
                session.goto(CREATOR_UPLOAD, wait_ms=3000)
                if not session.exists("input[type=file]"):
                    raise PublishError("创作者平台发布页没找到上传入口(页面改版了?)",
                                       retryable=False)
                session.upload_file("input[type=file]", video)
                session.page.wait_for_timeout(8000)
                for selector in ("input[name=title]", "input[placeholder*='标题']",
                                 "input[class*='title']"):
                    if session.exists(selector, timeout=1500):
                        session.fill(selector, (req.title or "")[:55])
                        break
                for selector in ("button:has-text('发布')", "button:has-text('提交')",
                                 "text=发布"):
                    if session.exists(selector, timeout=1500):
                        session.click(selector)
                        break
                session.page.wait_for_timeout(6000)
                session.save_state()
                body = session.page_text()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"网页版发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if "发布成功" not in body and "投稿成功" not in body:
            raise PublishError("网页版发布后没找到成功提示,可能是页面改版或需要验证码",
                               retryable=True)
        return PublishResult(ok=True, state="submitted", remote_id="",
                             remote_url=CREATOR_UPLOAD, message="网页版已提交(未拿到作品 id)")

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        if not self._official_ready(account):
            raise PublishError(
                "没有开放平台凭据,抓不了作品数据(需要 video.data 能力)",
                retryable=False, credential=True)
        token = self._access_token(account)
        open_id = self._open_id(account)
        item_id = ref.remote_id
        if not item_id:
            raise PublishError("没有 item_id,无法抓数据", retryable=False)
        url = f"{OPEN}/api/douyin/v1/video/video_data/?open_id={open_id}"
        with new_client() as client:
            data = request_json(client, "POST", url, json={"item_ids": [item_id]},
                                headers={"access-token": token, "content-type": "application/json"},
                                timeout=60)
        extra = data.get("extra") or {}
        if extra.get("error_code") not in (0, None):
            raise PublishError(f"数据接口返回 error_code={extra.get('error_code')}", retryable=True)
        from ...metrics.collector import parse_douyin_statistics

        return MetricSnapshot(**parse_douyin_statistics(data), raw={"data": data.get("data")})
