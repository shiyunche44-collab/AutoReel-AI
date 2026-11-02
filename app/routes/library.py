"""素材池页面与登录入口(从 main.py 拆出)。"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .. import config, maintenance
from ..pipeline import library
from ..providers import official
from ..publish import browser as publish_browser
from ..templating import templates

log = logging.getLogger(__name__)

router = APIRouter()


def _pool_dirs() -> list[Path]:
    root = config.DATA_DIR / library.LIB_ROOT_NAME
    if not root.is_dir():
        return []
    return [d for d in root.iterdir() if (d / "library.json").is_file()]


def _load_pool(slug: str) -> library.Library:
    """按目录名取池子。目录名是 slug,真正的选题名记在 library.json 里。"""
    path = config.DATA_DIR / library.LIB_ROOT_NAME / slug / "library.json"
    if "/" in slug or "\\" in slug or slug.startswith(".") or not path.is_file():
        raise HTTPException(status_code=404, detail="素材池不存在")
    try:
        subject = json.loads(path.read_text(encoding="utf-8")).get("subject") or slug
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=404, detail="素材池已损坏")
    lib = library.Library.load(subject)
    if lib.dir.name != slug:
        raise HTTPException(status_code=404, detail="素材池不存在")
    return lib


def _pool_cover(lib: library.Library) -> str:
    item = next((i for i in lib.items if i.usable and i.frame), None) \
        or next((i for i in lib.items if i.frame), None)
    return item.id if item else ""


@router.get("/library", response_class=HTMLResponse)
def library_index(request: Request):
    pools = []
    for d in _pool_dirs():
        try:
            lib = _load_pool(d.name)
        except HTTPException:
            continue
        st = lib.stats()
        pools.append({"slug": d.name, "subject": lib.subject, "category": lib.category,
                      "updated_at": lib.updated_at, "cover": _pool_cover(lib),
                      "building": library.is_building(lib.subject), "stats": st,
                      "gaps": library.footage_gaps(st)})
    pools.sort(key=lambda p: -p["updated_at"])
    return templates.TemplateResponse(request, "library.html", {
        "request": request, "pools": pools,
        "dcd_logged_in": official.dongchedi_logged_in(),
        "douyin_logged_in": official.douyin_logged_in()})


@router.post("/api/sources/login/dongchedi")
def source_login_dongchedi():
    """打开浏览器登录懂车帝(图库要登录)。登录成功自动存好关窗;后台线程跑。"""
    def _run() -> None:
        try:
            publish_browser.login(official.DCD_LOGIN_KEY, "https://www.dongchedi.com/",
                                  timeout=900, done_cookie=official.DCD_LOGIN_COOKIE)
        except Exception:  # noqa: BLE001 - 登录失败由用户看浏览器
            log.warning("登录失败由用户看浏览器", exc_info=True)

    import threading

    threading.Thread(target=_run, name="login-dongchedi", daemon=True).start()
    return {"ok": True, "message": "浏览器已打开,登录懂车帝后窗口会自动关闭"}


@router.post("/api/sources/login/douyin")
def source_login_douyin():
    """打开浏览器登录抖音网页版(拉官方号投稿要登录)。登录成功自动存好关窗;后台线程跑。
    碰到验证码由人在窗口里自己过,程序不碰。"""
    def _run() -> None:
        try:
            publish_browser.login(official.DOUYIN_LOGIN_KEY, "https://www.douyin.com/",
                                  timeout=900, done_cookie=official.DOUYIN_LOGIN_COOKIE)
        except Exception:  # noqa: BLE001 - 登录失败由用户看浏览器
            log.warning("登录失败由用户看浏览器", exc_info=True)

    import threading

    threading.Thread(target=_run, name="login-douyin", daemon=True).start()
    return {"ok": True, "message": "浏览器已打开,登录抖音后窗口会自动关闭"}


@router.get("/library/{slug}", response_class=HTMLResponse)
def library_pool(request: Request, slug: str):
    lib = _load_pool(slug)
    items = [i for i in lib.items if i.frame]
    # 可用的排前面,同类里视频按时长
    items.sort(key=lambda i: (not i.usable, i.kind != "video", -i.length))
    return templates.TemplateResponse(
        request,
        "library_pool.html",
        {"request": request, "slug": slug, "lib": lib, "items": items,
         "building": library.is_building(lib.subject), "stats": lib.stats()},
    )


@router.get("/library/{slug}/frame/{item_id}")
def library_frame(slug: str, item_id: str):
    lib = _load_pool(slug)
    item = next((i for i in lib.items if i.id == item_id), None)
    if not item or not item.frame:
        raise HTTPException(status_code=404, detail="没有这一帧")
    path = Path(item.frame).resolve()
    # 帧路径是绝对路径存的,只放行池子目录里面的文件
    if not path.is_file() or lib.dir.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="没有这一帧")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@router.delete("/api/library/{slug}")
def delete_library_pool(slug: str, force: bool = False):
    """驱逐一个素材池(删掉它的目录)。

    素材池是跨项目共享的:还有项目在用这个池时会拒绝,除非 `force=true`。
    重建一个池要重新下载 + 视觉打标签,很贵 —— 所以别把别的项目正用着的
    池子删了。
    """
    lib = _load_pool(slug)
    out = maintenance.evict_pool(lib.subject, force=force)
    if not out.get("ok"):
        raise HTTPException(409, str(out.get("reason") or "驱逐失败"))
    return out
