"""DouyinAdapter:开放平台优先,网页版兜底。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from ...base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, PublishRequest, PublishResult
from ...http import UPLOAD_TIMEOUT, new_client, request_json
from ...registry import PORTRAIT

from .consts import CREATOR_UPLOAD, MAX_UPLOAD, OPEN, PUBLISH_WAIT_MS, TITLE_MAX, UPLOAD_ENTRY_TIMEOUT_MS, UPLOAD_WAIT_MS
from .cover import dismiss_cover_prompt, set_covers
from .publish_page import DECLARE_AI, _declare_enabled, click_publish, publish_done, set_declaration, set_description, wait_publish_ready
from .works import resolve_published_work


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
        from .... import config

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
        message = "已提交,平台审核中"
        if _declare_enabled(req):
            # 开放平台的建片接口没有「自主声明」字段(声明是创作者中心侧的),
            # 走这条路只能靠平台自动识别 → 要挂标识得去创作者中心手动勾。
            message += ";开放平台接口无自主声明字段,AI 标识需去创作者中心手动勾"
        return PublishResult(
            ok=True, state="submitted", remote_id=item_id,
            remote_url=f"https://www.douyin.com/video/{vid}" if vid else "",
            message=message, raw={"item_id": item_id, "video_id": vid},
        )

    # -- 网页版兜底 ------------------------------------------------------
    def _publish_browser(self, req: PublishRequest, account: Account) -> PublishResult:
        from ... import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        cover_note = ""
        desc_note = ""
        started_at = time.time()
        try:
            with browser.BrowserSession("douyin") as session:
                # 打开发布页可能撞上瞬时断连(ERR_CONNECTION_CLOSED),重试几次:
                # 一次打不开就判「页面改版」是冤枉平台了(实跑踩过)。
                opened = False
                last_err = ""
                for _ in range(3):
                    try:
                        session.goto(CREATOR_UPLOAD, wait_ms=4000)
                        opened = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_err = str(exc)[:120]
                        time.sleep(3)
                if not opened:
                    raise PublishError(f"打不开创作者平台发布页:{last_err}", retryable=True)
                if not session.exists("input[type=file]", timeout=UPLOAD_ENTRY_TIMEOUT_MS):
                    if "扫码登录" in session.page_text():
                        raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                           retryable=False, credential=True)
                    raise PublishError("创作者平台发布页没找到上传入口(页面改版了?)",
                                       retryable=True)
                # 上传控件要等页面把「加载中」跑完才出现(约 18 秒),给足等待
                session.upload_file("input[type=file]", video, timeout=90000)
                if not session.wait_text("重新上传", timeout=UPLOAD_WAIT_MS):
                    raise PublishError("视频上传没在时限内完成", retryable=True)
                # 「重新上传」出现 = 文件传完了,但平台还在转码/生成封面,这时「发布」
                # 按钮是灰的 —— 点它等于没点。等它可点再往下走。
                wait_publish_ready(session, timeout_ms=180 * 1000)
                for selector in ("input[name=title]", "input[placeholder*='标题']",
                                 "input[class*='title']"):
                    if session.exists(selector, timeout=1500):
                        session.fill(selector, (req.title or "")[:TITLE_MAX])
                        break
                # 顺序要紧:**先设封面,再填简介**。填简介会弹出「#添加话题 / @好友」
                # 的浮层,它就盖在封面区上,封面位上的「选择封面」点了没反应(实跑踩过:
                # 等弹窗等超时,封面一直是平台自动截帧)。
                cover_note = set_covers(session, req.cover_path)
                # 自主声明要赶在填简介**之前**:填简介会弹「#添加话题」浮层,那浮层会
                # 盖住下面「自主声明」这一行的下拉,勾不上。
                declare_note = ""
                if _declare_enabled(req):
                    declare_note = set_declaration(session, DECLARE_AI)
                topics = " ".join(f"#{str(t).strip()}" for t in (req.tags or [])[:5] if str(t).strip())
                desc_note = set_description(session, req.description, hashtags=topics)
                # 点发布前:封面引导框可能又冒出来(它盖住整页),先关掉;
                # 再确认「发布」按钮已经是可点的(处理中的时候它是灰的)
                dismiss_cover_prompt(session)
                wait_publish_ready(session, timeout_ms=90 * 1000)
                # 按文字精确匹配:左栏有个「作品发布」按钮,模糊匹配会先命中它,点了等于没发
                reason = click_publish(session)
                if reason:
                    raise PublishError(reason, retryable=False)
                body = ""
                ok = False
                for _ in range(PUBLISH_WAIT_MS // 3000):
                    session.page.wait_for_timeout(3000)
                    body = session.page_text()
                    if publish_done(session):
                        ok = True
                        break
                    if "接收短信验证码" in body:  # 平台要本人验证,只能人来;重试只会反复发短信
                        raise PublishError("抖音要求短信验证(本人操作):在分发台点「登录」,"
                                           "在打开的窗口里手动发一次稿并输入验证码",
                                           retryable=False, credential=True)
                session.save_state()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"网页版发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if not ok:
            # 页面上没看到成功提示,不代表平台没收下 —— 回查作品管理页确认真伪。
            # 确认已经发出去了就按成功算,**不要重试**:重试会在账号里多留一条一样的作品
            # (实跑踩过:一条片子在抖音上留下 4 条)。
            work = resolve_published_work(req.title or "", since=started_at, rounds=2, gap=4)
            if work.get("aweme_id"):
                return PublishResult(
                    ok=True, state="submitted", remote_id=work["aweme_id"],
                    remote_url=work.get("url") or "",
                    message=f"网页版已发布(页面没给成功提示,回查确认),作品 id {work['aweme_id']}",
                    raw=work)
            raise PublishError("网页版发布后没找到成功提示,可能是页面改版或需要验证码",
                               retryable=True)
        message = "网页版已提交"
        # 网页版这条路拿不到作品 id(抖音只在作品管理页里显示它)—— 回查一次补上,
        # 否则项目里的发布记录永远是一条"没有链接"的任务,后面抓数据/对账都无从下手。
        work = resolve_published_work(req.title or "", since=started_at)
        if work.get("aweme_id"):
            message = f"网页版已发布,作品 id {work['aweme_id']}"
        for note in (desc_note, cover_note, declare_note):
            if note:
                message += f";{note}"
        if work.get("aweme_id"):
            return PublishResult(ok=True, state="submitted", remote_id=work["aweme_id"],
                                 remote_url=work.get("url") or "", message=message,
                                 raw=work)
        return PublishResult(ok=True, state="submitted", remote_id="",
                             remote_url=CREATOR_UPLOAD, message=message)

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        if not self._official_ready(account):
            raise PublishError(
                "没有开放平台凭据,抓不了作品数据(需要 video.data 能力);"
                "网页版发的作品由 AI 团队分析师从创作者中心抓",
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
        from ....metrics.collector import parse_douyin_statistics

        return MetricSnapshot(**parse_douyin_statistics(data), raw={"data": data.get("data")})
