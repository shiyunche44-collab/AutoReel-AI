"""B 站适配器:创作中心 Web 投稿 + 公开数据接口。

**为什么走网页投稿接口而不是开放平台**:B站的开放平台投稿 API 要资质审核,
而创作中心的 Web 投稿接口用一组 Cookie 就能跑(SESSDATA + bili_jct),
当天可用、零资质。代价是接口没有正式文档、平台改版要跟进 —— 所以下面每个
步骤都做了多路兜底,并且**失败时把平台的原始返回带出来**,方便对着抓包修。

投稿链路(五步):

1. `GET  /x/vupre/web/upload/preupload`  预上传,拿 upos 地址 / auth / 分片大小
2. `POST {upos}?uploads&output=json`     申请上传会话,拿 upload_id
3. `PUT  {upos}?partNumber=N&uploadId=…` 分片上传(每片 10MB)
4. `POST {upos}?output=json&…`           合片,拿 aid / cid
5. `POST /x/vu/web/cover/up` + `POST /x/vu/web/add/v3`  传封面 + 投递稿件

数据接口是**公开**的,不需要登录:

- `GET /x/web-interface/archive/stat?bvid=` → 播放/点赞/收藏/投币/转发/弹幕
- `GET /x/relation/stat?vmid=`            → 粉丝数
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from ..base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, \
    PublishRequest, PublishResult
from ..http import UPLOAD_TIMEOUT, find_key, new_client, request_json
from ..registry import LANDSCAPE

#: 创作中心域名
MEMBER = "https://member.bilibili.com"
#: 公开 API 域名
API = "https://api.bilibili.com"

#: 默认分区(日常)。账号 meta 里可以覆盖。
DEFAULT_TID = 21
#: 单片大小上限(预上传返回的 chunk_size 一般就是 10MB)
MAX_CHUNK = 10 * 1024 * 1024


def _bvid_of(url: str) -> str:
    """从视频页地址里取 bvid。"""
    text = str(url or "")
    if "BV" in text:
        idx = text.index("BV")
        return text[idx:idx + 12]
    return text


class BilibiliAdapter:
    """B 站(创作中心 Web 投稿)。"""

    key = "bilibili"
    label = "B 站"
    preferred_aspect = LANDSCAPE
    auth_kind = "cookie"

    # -- 凭据 ------------------------------------------------------------
    def _cookie(self, account: Account) -> str:
        cookie = account.cred("cookie")
        if not cookie:
            raise PublishError("没有配置 B 站 Cookie", retryable=False, credential=True)
        return cookie

    def _csrf(self, account: Account) -> str:
        """CSRF token 就是 Cookie 里的 bili_jct。"""
        from ..http import parse_cookie

        token = parse_cookie(self._cookie(account)).get("bili_jct", "")
        if not token:
            raise PublishError("Cookie 里没有 bili_jct(CSRF token)", retryable=False,
                               credential=True)
        return token

    # -- 自检 ------------------------------------------------------------
    def check_account(self, account: Account) -> AccountStatus:
        try:
            cookie = self._cookie(account)
        except PublishError as exc:
            return AccountStatus(False, exc.message, hint=exc.message)
        try:
            with new_client(cookie) as client:
                data = request_json(client, "GET", f"{API}/x/web-interface/nav", timeout=30)
        except PublishError as exc:
            return AccountStatus(False, exc.message,
                                 hint="Cookie 无效或网络不通,重新复制 SESSDATA 与 bili_jct")
        if data.get("code") != 0:
            return AccountStatus(False, f"未登录(code={data.get('code')})",
                                 hint="Cookie 已失效,重新从浏览器复制")
        info = data.get("data") or {}
        if not info.get("isLogin"):
            return AccountStatus(False, "Cookie 未登录",
                                 hint="重新从浏览器复制 SESSDATA 与 bili_jct")
        mid = str(info.get("mid") or "")
        name = str((info.get("uname") or "").strip() or "B 站用户")
        detail = {"mid": mid, "uname": name,
                  "level": ((info.get("level_info") or {}).get("current_level"))}
        return AccountStatus(True, f"已登录:{name}", detail=detail)

    # -- 发布 ------------------------------------------------------------
    def publish(self, req: PublishRequest, account: Account) -> PublishResult:
        cookie = self._cookie(account)
        csrf = self._csrf(account)
        video = Path(req.video_path)
        if not video.is_file():
            raise PublishError(f"成片不存在:{video.name}", retryable=False)

        with new_client(cookie, timeout=UPLOAD_TIMEOUT) as client:
            # 有上次的会话就接着传,没有才重新预上传
            session = self._restore(req.upload_id)
            if session is None:
                session = self._preupload(client, video)
                if callable(getattr(req, "on_session", None)):
                    req.on_session(json.dumps(session, ensure_ascii=False))
            self._prepare(client, session)
            self._chunks(client, session, video)
            complete = self._complete(client, session, video)
            cover_url = self._upload_cover(client, csrf, req.cover_path)
            return self._submit(client, csrf, req, session, complete, cover_url)

    def _restore(self, raw: str) -> dict[str, Any] | None:
        """从落库的会话里恢复。恢复不了就重新预上传(会话可能已过期)。"""
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        need = ("endpoint", "upos_uri", "auth", "chunk_size")
        if any(not data.get(k) for k in need):
            return None
        data["chunk_size"] = int(data.get("chunk_size") or MAX_CHUNK)
        return data

    def _preupload(self, client, video: Path) -> dict[str, Any]:
        """预上传:拿上传地址、auth、分片大小。"""
        params = {
            "name": video.name, "size": str(video.stat().st_size), "r": "upos",
            "profile": "ugcupos/bup", "ssl": "0", "version": "2.10.4.0",
            "build": "20221109", "probe_version": "20221109",
        }
        errors: list[str] = []
        for path in ("/x/vupre/web/upload/preupload", "/preupload"):
            try:
                data = request_json(client, "GET", f"{MEMBER}{path}", params=params, timeout=60)
            except PublishError as exc:
                errors.append(f"{path}:{exc.message}")
                continue
            payload = data.get("data") if isinstance(data.get("data"), dict) else data
            if not isinstance(payload, dict) or not payload.get("upos_uri"):
                errors.append(f"{path}:返回里没有 upos_uri")
                continue
            endpoint = str(payload.get("endpoint") or "")
            if endpoint.startswith("//"):
                endpoint = "https:" + endpoint
            elif not endpoint.startswith("http"):
                endpoint = "https://" + endpoint.lstrip("/")
            return {
                "endpoint": endpoint.rstrip("/"),
                "upos_uri": str(payload["upos_uri"]),
                "auth": str(payload.get("auth") or ""),
                "chunk_size": int(payload.get("chunk_size") or MAX_CHUNK),
                "biz_id": str(payload.get("biz_id") or ""),
                "filename": str(payload["upos_uri"]).rsplit("/", 1)[-1],
            }
        raise PublishError("B 站预上传失败:" + " / ".join(errors[:2]), retryable=True)

    def _prepare(self, client, session: dict[str, Any]) -> None:
        """申请上传会话,拿 upload_id。"""
        url = f"{session['endpoint']}/{session['upos_uri']}"
        try:
            data = request_json(client, "POST", url, params={"uploads": "", "output": "json"},
                                headers={"X-Upos-Auth": session["auth"]}, timeout=60)
        except PublishError:
            # 有的版本不需要这一步(预上传已带 upload_id),忽略即可
            return
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        upload_id = find_key(payload, "upload_id")
        if upload_id:
            session["upload_id"] = str(upload_id)
        session["prepared"] = True

    def _chunks(self, client, session: dict[str, Any], video: Path) -> None:
        """分片上传。失败的片最多重传 3 次,间隔 3 秒。"""
        url = f"{session['endpoint']}/{session['upos_uri']}"
        total = video.stat().st_size
        chunk_size = max(1, min(session["chunk_size"], MAX_CHUNK))
        total_chunks = max(1, -(-total // chunk_size))
        upload_id = session.get("upload_id") or ""
        part = 0
        with video.open("rb") as fh:
            while True:
                blob = fh.read(chunk_size)
                if not blob:
                    break
                part += 1
                params = {"partNumber": str(part), "chunk": str((part - 1) * chunk_size),
                          "chunks": str(total_chunks), "size": str(len(blob)),
                          "start": str((part - 1) * chunk_size),
                          "end": str(part * chunk_size - 1), "total": str(total)}
                if upload_id:
                    params["uploadId"] = upload_id
                self._put_chunk(client, url, session, params, blob, part)
        session["parts"] = part

    def _put_chunk(self, client, url: str, session: dict[str, Any], params: dict[str, str],
                   blob: bytes, part: int) -> None:
        headers = {"X-Upos-Auth": session["auth"], "Content-Type": "application/octet-stream"}
        last = ""
        for attempt in range(3):
            try:
                resp = client.put(url, params=params, content=blob, headers=headers,
                                  timeout=UPLOAD_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - 网络异常统一重试
                last = f"{type(exc).__name__}:{exc}"
            else:
                if resp.status_code == 200:
                    return
                last = f"HTTP {resp.status_code}:{resp.text[:120]}"
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            import time

            time.sleep(3)
        raise PublishError(f"第 {part} 片上传失败:{last}", retryable=True)

    def _complete(self, client, session: dict[str, Any], video: Path) -> dict[str, Any]:
        """合片,拿 aid / cid / filename。"""
        url = f"{session['endpoint']}/{session['upos_uri']}"
        params = {
            "output": "json", "name": video.name, "profile": "ugcupos/bup",
            "uploadId": session.get("upload_id") or "", "biz_id": session.get("biz_id") or "",
            "chunk_size": str(session["chunk_size"]), "size": str(video.stat().st_size),
        }
        body = {
            "r": "upos", "profile": "ugcupos/bup", "ssl": 0, "name": video.name,
            "upload_id": session.get("upload_id") or "", "biz_id": session.get("biz_id") or "",
            "chunk_size": session["chunk_size"], "size": video.stat().st_size,
        }
        data = request_json(client, "POST", url, params=params, json=body,
                            headers={"X-Upos-Auth": session["auth"]}, timeout=300)
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        aid = find_key(payload, "aid")
        cid = find_key(payload, "cid")
        filename = find_key(payload, "filename") or session.get("filename")
        if not aid or not cid:
            raise PublishError(f"合片返回里没有 aid/cid:{json.dumps(payload, ensure_ascii=False)[:200]}",
                               retryable=True)
        return {"aid": int(aid), "cid": int(cid), "filename": str(filename),
                "bvid": str(find_key(payload, "bvid") or "")}

    def _upload_cover(self, client, csrf: str, cover: Path | None) -> str:
        """传封面,拿 CDN 地址。没有封面就返回空串(平台会自动取帧)。"""
        if not cover or not Path(cover).is_file():
            return ""
        raw = Path(cover).read_bytes()
        payload = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        data = request_json(client, "POST", f"{MEMBER}/x/vu/web/cover/up",
                            data={"csrf": csrf, "cover": payload}, timeout=120)
        if data.get("code") not in (0, None):
            raise PublishError(f"封面上传失败:code={data.get('code')} {data.get('message','')}",
                               retryable=False)
        url = find_key(data.get("data") or data, "url")
        if not url:
            raise PublishError("封面上传返回里没有 url", retryable=True)
        return str(url)

    def _submit(self, client, csrf: str, req: PublishRequest, session: dict[str, Any],
                complete: dict[str, Any], cover_url: str) -> PublishResult:
        """投递稿件。"""
        title = req.title or "解说视频"
        desc = req.description or ""
        tags = ",".join(req.tags[:10])
        tid = int(req.extra.get("tid") or DEFAULT_TID)
        body = {
            "videos": [{"filename": complete["filename"], "title": title,
                        "desc": desc, "cid": complete["cid"]}],
            "title": title,
            "tid": tid,
            "tag": tags,
            "desc": desc,
            "desc_format_id": 9999,
            "copyright": 1,
            "csrf": csrf,
            "recreate": -1,
            "dynamic": desc[:200],
            "no_reprint": 1,
            "no_disturbance": 0,
            "up_selection_reply": False,
            "up_close_reply": False,
            "up_close_danmu": False,
            "web_os": 3,
        }
        if cover_url:
            body["cover"] = cover_url
        data = request_json(client, "POST", f"{MEMBER}/x/vu/web/add/v3", json=body, timeout=120)
        code = data.get("code")
        if code not in (0, None):
            message = str(data.get("message") or data.get("msg") or "")
            if code in (-101,):
                raise PublishError("登录态失效,重新复制 Cookie", retryable=False, credential=True)
            if code == -111:
                raise PublishError("CSRF 校验失败,重新复制 bili_jct", retryable=False,
                                   credential=True)
            raise PublishError(f"投稿失败:code={code} {message}", retryable=False)
        payload = data.get("data") or {}
        bvid = str(payload.get("bvid") or "")
        aid = payload.get("aid")
        if not bvid:
            raise PublishError(f"投稿返回里没有 bvid:{json.dumps(payload, ensure_ascii=False)[:200]}",
                               retryable=True)
        return PublishResult(
            ok=True, state="published", remote_id=bvid,
            remote_url=f"https://www.bilibili.com/video/{bvid}",
            message=f"已投稿 {bvid}", raw={"aid": aid, "bvid": bvid, "tid": tid},
        )

    # -- 数据 ------------------------------------------------------------
    def fetch_metrics(self, ref: PlatformRef, account: Account) -> MetricSnapshot:
        bvid = ref.remote_id or _bvid_of(ref.remote_url)
        if not bvid:
            raise PublishError("没有 bvid,无法抓数据", retryable=False)
        with new_client(account.cred("cookie")) as client:
            data = request_json(client, "GET", f"{API}/x/web-interface/archive/stat",
                                params={"bvid": bvid}, timeout=30)
            if data.get("code") not in (0, None):
                raise PublishError(f"作品数据返回 code={data.get('code')}", retryable=True)
            stat = data.get("data") or {}
            followers = self._followers(client, account)
        return MetricSnapshot(
            views=_int(stat.get("view")),
            likes=_int(stat.get("like")),
            comments=_int(stat.get("reply")),
            favorites=_int(stat.get("favorite")),
            shares=_int(stat.get("share")),
            coins=_int(stat.get("coin")),
            danmaku=_int(stat.get("danmaku")),
            followers=followers,
            raw={"stat": stat},
        )

    def _followers(self, client, account: Account) -> int | None:
        mid = account.setting("mid") or self._mid_of(client, account)
        if not mid:
            return None
        try:
            data = request_json(client, "GET", f"{API}/x/relation/stat",
                                params={"vmid": mid}, timeout=30)
        except PublishError:
            return None
        if data.get("code") not in (0, None):
            return None
        return _int((data.get("data") or {}).get("follower"))

    def _mid_of(self, client, account: Account) -> str:
        """取 UP 主的 mid(粉丝数要用)。拿不到就算了,不影响其它指标。"""
        try:
            data = request_json(client, "GET", f"{API}/x/web-interface/nav", timeout=30)
        except PublishError:
            return ""
        if data.get("code") != 0:
            return ""
        return str((data.get("data") or {}).get("mid") or "")


def _int(value: Any) -> int | None:
    """转 int。取不到就 None —— 不能拿 0 顶替「取不到」。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
