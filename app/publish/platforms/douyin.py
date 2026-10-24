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
import re
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
#: 作品管理页:页面自己会请求 `WORK_LIST_API`,每条作品带播放、完播率、2 秒跳出、涨粉
CREATOR_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
WORK_LIST_API = "/janus/douyin/creator/pc/work_list"
#: 平台文档给的单文件上限
MAX_UPLOAD = 100 * 1024 * 1024
#: 单片建议 20MB
PART_SIZE = 20 * 1024 * 1024
#: 网页版标题框上限(页面上写着 0/30)
TITLE_MAX = 30
#: 网页版等视频传完(页面出现「重新上传」)的时限
UPLOAD_WAIT_MS = 10 * 60 * 1000
#: 点「发布」后页面先挂「正在发布」(实测 20 秒以上),跳到作品管理才算发完 —— 提前关浏览器等于撤稿
PUBLISH_WAIT_MS = 3 * 60 * 1000
PUBLISH_DONE_TEXTS = ("发布成功", "投稿成功")

#: 网页版封面两个位:(位上的提示字, 弹窗里切到这一步的按钮/页签)
COVER_SLOTS = (("横封面4:3", "设置横封面"), ("竖封面3:4", "设置竖封面"))
#: 封面弹窗里「上传封面」那个隐藏的 file input(按钮本身被上传层盖住,点不到)
COVER_INPUT = ("div:has(> .semi-upload):has-text('上传封面') > .semi-upload "
               "> input.semi-upload-hidden-input")


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cover_files(cover: Path | None) -> list[tuple[str, str, Path]]:
    """网页版两个封面位各传哪张图:[(位上的提示字, 弹窗步骤名, 图)]。

    优先用按 4:3 / 3:4 **重新排版**的那张(`cover_art.build` 出的);没有就退回主封面,
    但只在主封面横竖和这个位一致时 —— 竖封面塞进 4:3 位,平台一裁就只剩中间一条。
    两张都没有的位跳过,平台自己从视频里截帧。"""
    from ...pipeline.narration import cover_3x4_path, cover_4x3_path

    if not cover or not Path(cover).is_file():
        return []
    cover = Path(cover)
    landscape = True
    try:
        from PIL import Image

        with Image.open(cover) as im:
            landscape = im.width >= im.height
    except OSError:
        pass
    out: list[tuple[str, str, Path]] = []
    for (tip, step), exact, same_way in zip(COVER_SLOTS, (cover_4x3_path(cover), cover_3x4_path(cover)),
                                            (landscape, not landscape)):
        if exact.is_file():
            out.append((tip, step, exact))
        elif same_way:
            out.append((tip, step, cover))
    return out


def _cover_srcs(page: Any) -> list[str]:
    """两个封面位上当前那张图的地址。传成功后会换成新地址 —— 拿它判断有没有设上。"""
    return page.eval_on_selector_all(
        "[class*=coverControl]", "els => els.map(e => (e.querySelector('img') || {}).src || '')")


def set_covers(session: Any, cover: Path | None) -> str:
    """在发布页的封面弹窗里把横、竖封面传上去。成功返回空串;没设上返回一句说明。

    设不上**不拦发布**:平台会自己截帧当封面,片子照样能发;说明写进发布结果给人看。"""
    files = cover_files(cover)
    if not files:
        return "没有封面文件,用平台自动截帧"
    page = session.page
    dialog = None
    try:
        dialog = page.locator("[role=dialog]")
        before = _cover_srcs(page)
        page.locator("[class*=coverControl]", has_text=files[0][0]) \
            .locator("[class*=cover-]").first.click()
        dialog.first.wait_for(timeout=10000)
        for i, (_tip, step, path) in enumerate(files):
            if i:
                button = dialog.get_by_role("button", name=step)
                (button if button.count() else dialog.get_by_text(step, exact=True)).first.click()
                page.wait_for_timeout(1500)
            dialog.locator(COVER_INPUT).first.set_input_files(str(path))
            page.wait_for_timeout(4000)
        dialog.get_by_role("button", name="完成").first.click()
        dialog.first.wait_for(state="hidden", timeout=15000)
        after = _cover_srcs(page)
    except Exception as exc:  # noqa: BLE001 - 封面设不上不该拦住发布
        try:
            if dialog is not None and dialog.count():
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
        return f"封面没设上({type(exc).__name__}),用平台自动截帧"
    slots = [tip for tip, _step in COVER_SLOTS]
    unchanged = [tip for tip, _s, _p in files
                 if len(before) == len(after) == len(slots)
                 and before[slots.index(tip)] == after[slots.index(tip)]]
    if unchanged:
        return f"封面可能没换上:{'、'.join(unchanged)}"
    return ""


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
        from .. import browser

        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)
        cover_note = ""
        try:
            with browser.BrowserSession("douyin") as session:
                session.goto(CREATOR_UPLOAD, wait_ms=3000)
                if not session.exists("input[type=file]"):
                    if "扫码登录" in session.page_text():
                        raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                           retryable=False, credential=True)
                    raise PublishError("创作者平台发布页没找到上传入口(页面改版了?)",
                                       retryable=False)
                session.upload_file("input[type=file]", video)
                if not session.wait_text("重新上传", timeout=UPLOAD_WAIT_MS):
                    raise PublishError("视频上传没在时限内完成", retryable=True)
                for selector in ("input[name=title]", "input[placeholder*='标题']",
                                 "input[class*='title']"):
                    if session.exists(selector, timeout=1500):
                        session.fill(selector, (req.title or "")[:TITLE_MAX])
                        break
                cover_note = set_covers(session, req.cover_path)
                # 按文字精确匹配:左栏有个「作品发布」按钮,模糊匹配会先命中它,点了等于没发
                for selector in ("button:text-is('发布')", "button:text-is('提交')"):
                    if session.exists(selector, timeout=1500):
                        session.click(selector)
                        break
                else:
                    raise PublishError("发布页没找到「发布」按钮(页面改版了?)", retryable=False)
                body = ""
                for _ in range(PUBLISH_WAIT_MS // 3000):
                    session.page.wait_for_timeout(3000)
                    body = session.page_text()
                    if any(t in body for t in PUBLISH_DONE_TEXTS):
                        break
                    if "接收短信验证码" in body:  # 平台要本人验证,只能人来;重试只会反复发短信
                        raise PublishError("抖音要求短信验证(本人操作):在分发台点「登录」,"
                                           "在打开的窗口里手动发一次稿并输入验证码",
                                           retryable=False, credential=True)
                    if "content/manage" in (session.page.url or ""):  # 发完会跳到作品管理
                        body += "\n发布成功"
                        break
                session.save_state()
        except browser.BrowserUnavailable as exc:
            raise PublishError(str(exc), retryable=False) from exc
        except PublishError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PublishError(f"网页版发布失败:{type(exc).__name__}: {exc}", retryable=True) from exc
        if not any(t in body for t in PUBLISH_DONE_TEXTS):
            raise PublishError("网页版发布后没找到成功提示,可能是页面改版或需要验证码",
                               retryable=True)
        message = "网页版已提交(未拿到作品 id)"
        if cover_note:
            message += f";{cover_note}"
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
        from ...metrics.collector import parse_douyin_statistics

        return MetricSnapshot(**parse_douyin_statistics(data), raw={"data": data.get("data")})


def fetch_creator_works(*, since: float | None = None, max_scrolls: int = 4) -> list[dict[str, Any]]:
    """无头打开创作者中心「作品管理」,截页面自己请求的作品列表接口,只读、不点按钮。

    返回 `work_list` 的 `items[]`(带 `metrics`),每条再挂上同一次返回里
    `aweme_list` 对应那条(`work["aweme"]`:标题、审核状态、作者粉丝数)。
    第一页 12 条;给了 `since`(时间戳)且最老一条还比它新,就往下滚着加载,最多
    `max_scrolls` 次。登录失效抛 `PublishError(credential=True)`,不重试。
    """
    from .. import browser

    pages: list[dict[str, Any]] = []

    def on_response(resp: Any) -> None:
        if WORK_LIST_API not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and body.get("items") is not None:
            pages.append(body)

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            session.page.on("response", on_response)
            session.goto(CREATOR_MANAGE, wait_ms=6000)
            if "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            for _ in range(max_scrolls):
                if not pages or not pages[-1].get("has_more") or since is None:
                    break
                oldest = min((_int(i.get("create_time")) or 0) for i in pages[-1]["items"])                     if pages[-1]["items"] else 0
                if oldest <= since:
                    break
                got = len(pages)
                session.page.mouse.wheel(0, 6000)
                session.page.wait_for_timeout(3000)
                if len(pages) == got:
                    break
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    if not pages:
        raise PublishError("作品管理页没请求作品列表接口(页面改版了?)", retryable=False)
    works: dict[str, dict[str, Any]] = {}
    for body in pages:
        awemes = {str(a.get("aweme_id")): a for a in body.get("aweme_list") or []}
        for item in body.get("items") or []:
            wid = str(item.get("id") or "")
            if wid:
                works[wid] = {**item, "id": wid, "aweme": awemes.get(wid) or {}}
    return list(works.values())


# --------------------------------------------------------------------------
# 评论(运营官):创作者中心「评论管理」页
# --------------------------------------------------------------------------
#: 评论管理页:默认打开最新一条作品;「选择作品」弹窗里点哪条才会请求哪条的评论
CREATOR_COMMENTS = "https://creator.douyin.com/creator-micro/interactive/comment"
#: 页面自己请求的作品列表(带每条作品的评论数)和单条作品的评论列表
COMMENT_ITEMS_API = "/aweme/v1/creator/item/list/"
COMMENT_LIST_API = "/aweme/v1/creator/comment/list/"
#: 每条作品最多往下翻这么多页评论(一页 10 条)
COMMENT_PAGES = 3
#: 评论里的表情在接口里是「[赞]」,页面上是图片
_EMOJI_RE = re.compile(r"\[[^\[\]]{1,6}\]")

#: 评论行:同时含作者名、评论文字和「回复」的最小那个块,把它里面的「回复」标出来
_MARK_REPLY_JS = """([author, text]) => {
  document.querySelectorAll('[data-aim-reply]').forEach((e) => e.removeAttribute('data-aim-reply'));
  const hits = [];
  for (const el of document.querySelectorAll('div')) {
    const t = el.innerText || '';
    if (t.includes(author) && (!text || t.includes(text)) && t.includes('回复')) hits.push(el);
  }
  const rows = hits.filter((el) => !hits.some((o) => o !== el && el.contains(o)));
  if (!rows.length) return 0;
  const btn = [...rows[0].querySelectorAll('*')].reverse()
    .find((e) => (e.innerText || '').trim() === '回复');
  if (!btn) return 0;
  btn.setAttribute('data-aim-reply', '1');
  return rows.length;
}"""
#: 回复框旁边那个「发送」(框里有字才可点)
_MARK_SEND_JS = """() => {
  document.querySelectorAll('[data-aim-send]').forEach((e) => e.removeAttribute('data-aim-send'));
  const box = document.querySelector('[data-aim-box]');
  for (let el = box; el; el = el.parentElement) {
    const btn = [...el.querySelectorAll('button')].find((b) => (b.innerText || '').trim() === '发送');
    if (btn) { btn.setAttribute('data-aim-send', '1'); return !btn.disabled; }
  }
  return false;
}"""


def comment_text_key(text: str) -> str:
    """页面上能看到的评论文字(去掉表情,取前 20 字,够认出是哪条)。"""
    return _EMOJI_RE.sub("", str(text or "")).strip()[:20]


class _CommentPage:
    """开着的评论管理页:截作品列表和评论列表接口,按标题切作品。只读,除非调 `reply`。"""

    def __init__(self, session: Any) -> None:
        self.s = session
        self.items: dict[str, dict[str, Any]] = {}
        self.lists: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        session.page.on("response", self._on_response)

    def _on_response(self, resp: Any) -> None:
        url = resp.url
        if COMMENT_ITEMS_API not in url and COMMENT_LIST_API not in url \
                and not (resp.request.method == "POST" and "comment" in url):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        if COMMENT_ITEMS_API in url:
            for it in body.get("item_info_list") or []:
                wid = str(it.get("item_id_plain") or "")
                if wid:
                    self.items[wid] = it
        elif COMMENT_LIST_API in url:
            self.lists.append(body)
        else:
            self.posts.append(body)

    def open(self) -> None:
        self.s.goto(CREATOR_COMMENTS, wait_ms=6000)
        if "扫码登录" in self.s.page_text():
            raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                               retryable=False, credential=True)
        if not self.items:
            raise PublishError("评论管理页没请求作品列表接口(页面改版了?)", retryable=False)

    def select(self, work_id: str) -> list[dict[str, Any]]:
        """切到这条作品,返回它评论列表接口的第一页(没请求到返回空)。"""
        page = self.s.page
        title = str((self.items.get(work_id) or {}).get("title") or "").strip()
        if not title:
            return []
        got = len(self.lists)
        page.get_by_text("选择作品", exact=True).first.click()
        page.wait_for_timeout(2500)
        target = page.get_by_text(title[:12], exact=False)
        if not target.count():
            page.keyboard.press("Escape")
            return []
        target.last.click()
        for _ in range(16):
            if len(self.lists) > got:
                break
            page.wait_for_timeout(500)
        return self.lists[got:]

    def comments(self, work_id: str) -> list[dict[str, Any]]:
        bodies = self.select(work_id)
        for _ in range(COMMENT_PAGES - 1):
            if not bodies or not bodies[-1].get("has_more"):
                break
            got = len(self.lists)
            self.s.page.mouse.wheel(0, 4000)
            self.s.page.wait_for_timeout(2500)
            if len(self.lists) == got:
                break
            bodies += self.lists[got:]
        out: dict[str, dict[str, Any]] = {}
        for body in bodies:
            for c in body.get("comment_info_list") or []:
                if c.get("comment_id"):
                    out[str(c["comment_id"])] = c
        return list(out.values())

    def reply(self, author: str, text: str, reply: str) -> None:
        """在当前作品下找到这条评论,点「回复」、填字、点「发送」,等平台回执。"""
        page = self.s.page
        key = comment_text_key(text)
        found = 0
        for _ in range(COMMENT_PAGES):
            found = page.evaluate(_MARK_REPLY_JS, [author, key])
            if found:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2000)
        if not found:
            raise PublishError("评论列表里没找到这条评论(可能被删了)", retryable=False)
        if found > 1 and not key:
            raise PublishError("同一个人有好几条只有表情的评论,分不清回哪条,去抖音手动回",
                               retryable=False)
        page.locator("[data-aim-reply]").first.click()
        page.wait_for_timeout(1200)
        box = page.locator('[contenteditable="true"][placeholder^="回复"]')
        if not box.count():
            raise PublishError("点了「回复」没出现回复框(页面改版了?)", retryable=False)
        box = box.last
        box.evaluate("(e) => e.setAttribute('data-aim-box', '1')")
        box.click()
        page.keyboard.type(reply, delay=40)
        ready = False
        for _ in range(10):
            ready = page.evaluate(_MARK_SEND_JS)
            if ready:
                break
            page.wait_for_timeout(300)
        if not ready:
            raise PublishError("回复框填了字,「发送」还是灰的", retryable=False)
        got = len(self.posts)
        page.locator("[data-aim-send]").first.click()
        for _ in range(16):
            if len(self.posts) > got:
                break
            page.wait_for_timeout(500)
        answers = [b for b in self.posts[got:] if "status_code" in b]
        if answers:
            code = answers[-1].get("status_code")
            if code not in (0, "0"):
                raise PublishError(f"抖音没收这条回复:{answers[-1].get('status_msg') or code}",
                                   retryable=False)
            return
        if reply[:10] not in self.s.page_text():
            raise PublishError("点了发送,没等到平台回执,页面上也没看到这条回复;去抖音核对一下"
                               "再决定要不要重发", retryable=False)


def fetch_creator_comments(known: dict[str, int]) -> dict[str, Any]:
    """无头打开评论管理页,只读。`known` 是 {作品 id: 上次看到的评论数}。

    作品列表接口带每条作品的评论数,只切到评论数比上次多的那几条去拉评论(作品不在
    `known` 里的不看 —— 只管团队发的)。返回 {"counts": {作品 id: 评论数},
    "comments": {作品 id: [评论接口原始条目]}}。登录失效抛 `PublishError(credential=True)`。"""
    from .. import browser

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            cp = _CommentPage(session)
            cp.open()
            counts = {wid: int(it.get("comment_count") or 0) for wid, it in cp.items.items()}
            got: dict[str, list[dict[str, Any]]] = {}
            for wid, seen in known.items():
                if counts.get(wid, 0) > seen:
                    got[wid] = cp.comments(wid)
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    return {"counts": {w: counts[w] for w in known if w in counts}, "comments": got}


def send_comment_replies(jobs: list[dict[str, Any]], on_done: Any) -> None:
    """按你批过的回复逐条发。`jobs` 每条:{id, work_id, author, text, reply}。

    每发完一条调 `on_done(job, error_or_None)`;遇到第一条失败就停(不连着试,免得触发风控)。
    条与条之间停几秒,像人在回。"""
    from .. import browser

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            cp = _CommentPage(session)
            cp.open()
            current = None
            for i, job in enumerate(jobs):
                try:
                    if job["work_id"] != current:
                        if job["work_id"] not in cp.items or not cp.select(job["work_id"]):
                            raise PublishError("评论管理页里切不到这条作品", retryable=False)
                        current = job["work_id"]
                    cp.reply(job["author"], job["text"], job["reply"])
                except PublishError as exc:
                    on_done(job, exc.message)
                    return
                on_done(job, None)
                if i + 1 < len(jobs):
                    session.page.wait_for_timeout(4000 + 1000 * (i % 3))
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc


# --------------------------------------------------------------------------
# 对标账号主页(公开页,只读)
# --------------------------------------------------------------------------
USER_PAGE = "https://www.douyin.com/user/"
PROFILE_POST_API = "/aweme/v1/web/aweme/post/"
_SEC_UID = re.compile(r"/user/(MS4wLjAB[\w-]+)")


def sec_uid_of(text: str) -> str:
    """从主页链接里取 sec_uid;没有(比如 v.douyin.com 短链)返回空串。"""
    m = _SEC_UID.search(text or "")
    if m:
        return m.group(1)
    text = (text or "").strip()
    return text if text.startswith("MS4wLjAB") and " " not in text else ""


def fetch_profile_posts(target: str, *, max_scrolls: int = 1) -> dict[str, Any]:
    """无头打开对标账号主页,截页面自己请求的作品列表接口。只读,不点任何东西。

    `target` 是 sec_uid、主页链接或分享短链(短链让浏览器跳一次,从落地页 URL 取 sec_uid)。
    返回 {"sec_uid", "nickname", "awemes": [作品接口原始条目]}。一个号只翻 `max_scrolls` 次
    (一页 18 条左右,算中位数够了)。登录失效抛 `PublishError(credential=True)`,
    拿不到数据抛普通 `PublishError` —— 都不重试,由调用方记下来等人看。"""
    from .. import browser

    sec = sec_uid_of(target)
    pages: list[dict[str, Any]] = []

    def on_response(resp: Any) -> None:
        if PROFILE_POST_API not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict) and body.get("aweme_list") is not None:
            pages.append(body)

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=60) as session:
            session.page.on("response", on_response)
            if sec:
                session.goto(USER_PAGE + sec, wait_ms=8000)
            else:
                url = target.strip()
                if not url.startswith("http"):
                    raise PublishError("看不懂这个账号:贴主页链接或分享链接", retryable=False)
                session.goto(url, wait_ms=8000)
                sec = sec_uid_of(session.page.url)
                if not sec:
                    raise PublishError("链接没跳到抖音用户主页", retryable=False)
            if not pages and "扫码登录" in session.page_text():
                raise PublishError("抖音登录已失效:去分发台点「登录」扫码一次",
                                   retryable=False, credential=True)
            for _ in range(max_scrolls):
                if not pages or not pages[-1].get("has_more"):
                    break
                got = len(pages)
                session.page.mouse.wheel(0, 5000)
                session.page.wait_for_timeout(3000)
                if len(pages) == got:
                    break
    except browser.BrowserUnavailable as exc:
        raise PublishError(str(exc), retryable=False) from exc
    if not pages:
        raise PublishError("主页没请求作品列表接口(被风控或页面改版了?)", retryable=False)
    awemes: dict[str, dict[str, Any]] = {}
    for body in pages:
        for a in body.get("aweme_list") or []:
            if a.get("aweme_id"):
                awemes[str(a["aweme_id"])] = a
    first = next(iter(awemes.values()), {})
    return {"sec_uid": sec, "nickname": str((first.get("author") or {}).get("nickname") or ""),
            "awemes": list(awemes.values())}
