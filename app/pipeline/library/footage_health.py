"""素材清晰度体检:cookies 生没生效、平台肯给多高的清晰度、池子里低清段占多少。

B 站不登录最高只给 480p,池子里的视频段八成是这么来的。体检拿池子里一条官方号投稿
`probe` 一下(只解析不下载),看格式表里最高的视频流:

    python -m app.pipeline.library cookies-check

结果缓存在 `data/library/_cookies_check.json`,`/api/health` 的 `footage` 和首页横幅读它。
cookies.txt 由用户自己导出、放进 `YTDLP_COOKIES` 指的文件夹;这里只看配没配(文件在不在),
不读 cookie 内容。重拉高清(`upgrade.py`)每探一条源也顺手更新这里的结论。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from ... import config
from ...providers import platform_video
from .model import LIB_ROOT_NAME, Library

log = logging.getLogger(__name__)

#: 体检的平台
PLATFORMS = ("bilibili", "youtube")
#: B 站不登录能拿到的最高短边:探到不超过它 = cookies 没生效
GUEST_MAX_HEIGHT = 480
#: 体检结果文件(在 data/library/ 下)
CHECK_FILE = "_cookies_check.json"
#: 低清占比多久重算一次(秒)。全部池子读一遍约 1 秒,不能每次 /api/health 都读
RATIO_MAX_AGE = 600.0


def cooldown_key(platform: str) -> str:
    """平台视频解析 / 下载的冷却登记名(`providers/cooldown.py`)。"""
    return f"{platform}.video"


def _check_path() -> Path:
    return config.DATA_DIR / LIB_ROOT_NAME / CHECK_FILE


def load_check() -> dict[str, Any]:
    try:
        raw = json.loads(_check_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_check(data: dict[str, Any]) -> None:
    path = _check_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _effective(platforms: dict[str, Any]) -> bool | None:
    """cookies 生没生效:以 B 站为准(素材池的大头);B 站没探成功就看 YouTube。
    都没探成功 → None(不知道)。"""
    bili = platforms.get("bilibili") or {}
    if int(bili.get("height") or 0) > 0:
        return int(bili["height"]) > GUEST_MAX_HEIGHT
    yt = platforms.get("youtube") or {}
    if int(yt.get("height") or 0) > 0:
        return int(yt["height"]) > GUEST_MAX_HEIGHT
    return None


_check_lock = threading.Lock()


def record_probe(platform: str, url: str, height: int, error: str = "") -> None:
    """记一次平台探测的结论(体检、重拉高清都调)。"""
    with _check_lock:
        data = load_check()
        plats = dict(data.get("platforms") or {})
        plats[platform] = {"url": url, "height": int(height), "error": str(error)[:200],
                           "at": time.time()}
        data["platforms"] = plats
        data["checked_at"] = time.time()
        data["configured"] = bool(platform_video.cookies_state()["configured"])
        data["effective"] = _effective(plats)
        _save_check(data)


def _pools() -> list[Library]:
    root = config.DATA_DIR / LIB_ROOT_NAME
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "library.json").is_file():
            out.append(Library.load(d.name))
    return out


def probe_url(platform: str) -> str:
    """体检用哪条视频:池子里该平台的官方号投稿(建完的),最近更新的池子优先。"""
    for lib in sorted(_pools(), key=lambda lb: -lb.updated_at):
        for s in lib.sources:
            if s.official and s.status == "done" and s.platform == platform and s.url:
                return s.url
    return ""


def check(urls: dict[str, str] | None = None) -> dict[str, Any]:
    """探一遍各平台(每个平台一条视频,只解析不下载),写缓存并返回结论。

    冷却中的平台不探(如实记成冷却中);撞了限流登记冷却。"""
    from ...providers import cooldown

    state = platform_video.cookies_state()
    for platform in PLATFORMS:
        url = (urls or {}).get(platform) or probe_url(platform)
        if not url:
            record_probe(platform, "", 0, "池子里没有这个平台的官方号投稿,没法探")
            continue
        try:
            cooldown.check(cooldown_key(platform))
        except cooldown.Cooling as exc:
            record_probe(platform, url, 0, str(exc))
            continue
        try:
            info = platform_video.probe(url)
        except platform_video.PlatformError as exc:
            if platform_video.is_rate_limited(exc):
                cooldown.trip(cooldown_key(platform), 1800.0, str(exc)[:200])
            record_probe(platform, url, 0, str(exc))
            continue
        record_probe(platform, url, platform_video.best_short_side(info))
    data = load_check()
    data["configured"] = bool(state["configured"])
    data["config_note"] = state["note"]
    _save_check(data)
    return data


# --------------------------------------------------------------------------
# 池子里的低清占比(缓存,后台刷新)
# --------------------------------------------------------------------------
_ratio_cache: tuple[float, dict[str, Any]] | None = None
_ratio_lock = threading.Lock()
_ratio_refreshing = False


def low_res_share() -> dict[str, Any]:
    """所有池子的视频段里(读到尺寸的、没被重拉换下来的)低清段占多少。"""
    videos = low = 0
    for lib in _pools():
        for i in lib.items:
            if i.kind != "video" or i.skipped == "replaced" or not (i.width and i.height):
                continue
            videos += 1
            low += i.low_res
    return {"video_segments": videos, "low_res_segments": low,
            "low_res_ratio": round(low / videos, 3) if videos else None}


def _refresh_ratio() -> None:
    global _ratio_cache, _ratio_refreshing
    try:
        _ratio_cache = (time.time(), low_res_share())
    except Exception:  # noqa: BLE001 - 体检读不了池子不该拖垮健康检查
        log.warning("低清占比统计失败", exc_info=True)
    finally:
        _ratio_refreshing = False


def low_res_share_cached(max_age: float = RATIO_MAX_AGE) -> dict[str, Any]:
    """`low_res_share` 的缓存值。过期了后台重算、本次先给旧值;还没算过 → 空(不等)。"""
    global _ratio_refreshing
    cached = _ratio_cache
    if cached is not None and time.time() - cached[0] <= max_age:
        return cached[1]
    with _ratio_lock:
        if not _ratio_refreshing:
            _ratio_refreshing = True
            threading.Thread(target=_refresh_ratio, name="footage-ratio", daemon=True).start()
    return cached[1] if cached else {}


def status() -> dict[str, Any]:
    """给 /api/health 和首页横幅:cookies 配没配 / 生没生效、各平台最高清晰度、低清占比。

    `low_res_only` = 体检探到 B 站只给 480p(没登录态):首页挂横幅。没体检过不挂(不知道)。"""
    data = load_check()
    state = platform_video.cookies_state()
    plats = data.get("platforms") or {}
    best = {p: int((plats.get(p) or {}).get("height") or 0) for p in PLATFORMS}
    errors = {p: str((plats.get(p) or {}).get("error") or "") for p in PLATFORMS
              if (plats.get(p) or {}).get("error")}
    share = low_res_share_cached()
    bili = best.get("bilibili", 0)
    low_only = 0 < bili <= GUEST_MAX_HEIGHT
    note = ""
    if low_only:
        ratio = share.get("low_res_ratio")
        pct = f",素材池里 {round(ratio * 100)}% 的视频段是低清" if ratio is not None else ""
        why = "cookies 未生效(可能过期)" if state["configured"] else "cookies 未配置"
        note = (f"B 站最高只拿得到 {bili}p({why}){pct}。导出 B 站 / YouTube 的 cookies.txt "
                "放进 YTDLP_COOKIES 指的文件夹,重启服务后运行 "
                "python -m app.pipeline.library cookies-check")
    return {
        "cookies": {"configured": bool(state["configured"]), "note": state["note"],
                    "effective": data.get("effective"),
                    "checked_at": float(data.get("checked_at") or 0.0)},
        "best_height": best,
        "errors": errors,
        # 还没算过(刚启动)时是 None,后台算好之后下一次就有
        "video_segments": share.get("video_segments"),
        "low_res_segments": share.get("low_res_segments"),
        "low_res_ratio": share.get("low_res_ratio"),
        "low_res_only": low_only,
        "note": note,
    }
