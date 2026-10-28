"""入池:下载、按剪辑点切段、抽帧、台标检测、官图入池。"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Sequence
from ...providers import clips as clips_mod
from ...providers import images, official, platform_video
from .. import logo_detect, media_check

from .model import Item, Library, MIN_TAG_SECONDS, SCENE_THRESHOLD, Source, WHOLE_MAX_SECONDS, WINDOWS_PER_LONG_SOURCE, WINDOW_SECONDS, platform_of, profile_of, short_id


# --------------------------------------------------------------------------
# 攒 + 切:下载与场景检测
# --------------------------------------------------------------------------
def download_windows(duration: float) -> list[tuple[float, float]]:
    """要下载的 (起点, 时长)。短源整条下;长源在 8%–92% 区间均匀取几个窗
    (片头片尾多是 logo、口播、求三连)。"""
    if 0 < duration <= WHOLE_MAX_SECONDS:
        return [(0.0, duration)]
    if duration <= 0:
        return [(0.0, WHOLE_MAX_SECONDS)]
    lo, hi = duration * 0.08, duration * 0.92 - WINDOW_SECONDS
    n = WINDOWS_PER_LONG_SOURCE
    step = (hi - lo) / (n - 1) if n > 1 and hi > lo else 0.0
    return [(round(lo + step * k, 2), WINDOW_SECONDS) for k in range(n if step else 1)]


_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def scene_cuts(path: str | Path) -> list[float]:
    """原片自己的剪辑点(秒)。缩到 320 宽再算,一条三分钟 1080p 几秒钟就跑完。"""
    proc = subprocess.run(
        [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-v", "info", "-i", str(path),
         "-an", "-vf", f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
    )
    return sorted({round(float(t), 3) for t in _PTS_RE.findall(proc.stderr or "")})


def split_segments(cuts: Sequence[float], duration: float) -> list[tuple[float, float]]:
    """剪辑点 → 段。每段两头各让 0.1 秒,避开转场那一两帧。"""
    edges = [0.0, *[c for c in cuts if 0 < c < duration], duration]
    out: list[tuple[float, float]] = []
    for a, b in zip(edges, edges[1:]):
        a2, b2 = a + 0.1, b - 0.1
        if b2 - a2 > 0.2:
            out.append((round(a2, 3), round(b2, 3)))
    return out


def extract_frame(video: str | Path, at: float, dest: Path) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [clips_mod._ffmpeg(), "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1",
         "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return dest if proc.returncode == 0 and dest.is_file() else None


def ingest_source(lib: Library, src: Source, errors: list[str]) -> int:
    """下载一个源、按剪辑点切段、抽帧。返回新增的段数。失败把源标成 failed。"""
    raw_dir = lib.dir / "raw"
    frame_dir = lib.dir / "frames"
    try:
        if not src.duration or not src.title:
            info = platform_video.probe(src.url)
            src.duration = src.duration or float(info.get("duration") or 0.0)
            src.title = src.title or str(info.get("title") or "")
            src.uploader = src.uploader or str(info.get("uploader") or info.get("channel") or "")
        added = 0
        for k, (start, length) in enumerate(download_windows(src.duration)):
            try:
                raw = platform_video.download_segment(
                    src.url, raw_dir / f"{src.id}_{k}.mp4", start=start, seconds=length,
                    timeout_hint=600)
            except platform_video.PlatformError as exc:
                errors.append(f"素材池下载失败({src.url} 窗 {k}):{str(exc)[:160]}")
                continue
            dur = clips_mod.probe_duration(raw)
            if dur <= 0:
                continue
            # 一个窗文件只读一次,切出来的段共用
            info = media_check.probe_video(raw)
            segs = split_segments(scene_cuts(raw), dur)
            for n, (a, b) in enumerate(segs):
                item_id = f"{src.id}_{k}_{n:03d}"
                frame, skipped, sharp = "", "", -1.0
                if b - a >= MIN_TAG_SECONDS:
                    got = extract_frame(raw, (a + b) / 2, frame_dir / f"{item_id}.jpg")
                    frame = str(got) if got else ""
                    # 送视觉模型之前先本地看三帧:黑场、纯色淡场直接跳过,不花调用
                    stats = media_check.frame_stats(raw, a, b)
                    skipped = media_check.frame_verdict(stats)
                    sharp = media_check.median_sharpness(stats)
                lib.items.append(Item(
                    id=item_id, kind="video", path=str(raw), frame=frame, source=src.id,
                    url=src.url, platform=src.platform or platform_of(src.url),
                    official=src.official,
                    origin=f"library:{src.platform or platform_of(src.url)}",
                    license=f"{src.platform}:{src.uploader or '未知上传者'}",
                    start=a, end=b, skipped=skipped, sharp=sharp, **info,
                ))
                added += 1
        if not added:
            raise platform_video.PlatformError("没有下载到任何可用的片段")
        src.status, src.error = "done", ""
        return added
    except Exception as exc:  # noqa: BLE001 - 一个源失败只是少一个源
        src.status, src.error = "failed", str(exc)[:200]
        errors.append(f"素材池源失败({src.url}):{str(exc)[:160]}")
        return 0


def check_logos(lib: Library, src: Source) -> list[list[float]] | None:
    """给一个已下载的源做静态角标检测,结果写回源和源里的每一段。

    - 检测到角标、能裁掉:段的 `logo_crop` = 要收的比例;
    - 裁不掉(台标太大 / 离边太远):第三方的段标成跳过(logo_uncroppable),
      不再送视觉模型;官方的段照常用(多是品牌自己的免责小字,不裁);
    - 帧太少下不了结论:不写结果,`_crop_edge` 照旧按来源一刀切。
    """
    segs = [i for i in lib.items if i.source == src.id and i.kind == "video"]
    paths = sorted({i.path for i in segs})
    boxes = logo_detect.find_logos(logo_detect.sample_frames(paths))
    src.logo_checked = True
    for i in segs:  # 重测时先清掉上一次的结论
        i.logo_crop = -1.0
        if i.skipped == "logo_uncroppable":
            i.skipped = ""
    if boxes is None:
        src.logo_boxes = []
        return None
    src.logo_boxes = boxes
    crop = logo_detect.crop_for(boxes) if boxes else 0.0
    for i in segs:
        i.logo_crop = crop if crop is not None else 0.0
        if crop is None and not src.official:
            i.skipped = i.skipped or "logo_uncroppable"
    return boxes


def ingest_gallery(lib: Library, errors: list[str], *,
                   spec: dict[str, tuple[str, ...]] | None = None) -> int:
    """profile 声明的静图来源(汽车之家官图、品牌官网 + 图片检索……)→ 池子里的图。
    返回新增张数。"""
    have = {i.url for i in lib.items if i.kind == "still"}
    added = 0
    for name in profile_of(lib).galleries:
        if name in lib.galleries_done:
            continue
        gallery = official.GALLERIES.get(name)
        if gallery is None:
            errors.append(f"未知静图来源:{name}")
            continue
        try:
            refs = gallery.fetch(lib.subject, spec or {}, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"素材池静图抓取失败({name}):{str(exc)[:120]}")
            continue
        for ref in refs:
            if ref.url in have:
                continue
            item = _add_still(lib, ref.url, errors, platform=name, referer=gallery.referer,
                              official=name != "product_images" or ref.origin.startswith(
                                  "official"),
                              license=ref.license, view=_GALLERY_VIEW.get(ref.angle, ""))
            if item is None:
                continue
            have.add(ref.url)
            if gallery.min_long_side and max(item.width, item.height) < gallery.min_long_side:
                # 页面上的小图标、缩略图:下载了才知道尺寸,不够大的不留
                lib.items.remove(item)
                for f in {item.path, item.frame}:
                    Path(f).unlink(missing_ok=True)
                continue
            added += 1
        lib.galleries_done.append(name)
    lib.gallery_at = time.time()
    return added


def _add_still(lib: Library, url: str, errors: list[str], *, platform: str, referer: str = "",
               official: bool = False, license: str = "", view: str = "") -> Item | None:
    """下载一张图进池子(未打标签)。失败记一条错误,返回 None。"""
    item_id = "img_" + short_id(url)
    dest = lib.dir / "stills" / f"{item_id}.jpg"
    headers = {"User-Agent": clips_mod.BROWSER_UA}
    if referer:
        headers["Referer"] = referer
    try:
        if url.startswith(("http://", "https://")):
            images.download(url, dest, headers=headers)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(url).read_bytes())
    except Exception as exc:  # noqa: BLE001
        errors.append(f"静图下载失败({url[:60]}):{str(exc)[:100]}")
        return None
    frame = _thumb(dest, lib.dir / "frames" / f"{item_id}.jpg")
    width, height = media_check.image_size(dest)
    item = Item(
        id=item_id, kind="still", path=str(dest), frame=str(frame or dest), url=url,
        platform=platform, official=official, origin=f"library:{platform}",
        license=license, view=view, width=width, height=height,
    )
    lib.items.append(item)
    return item


#: 官图分类自带的视角,作为打标签前的初值(打标签会覆盖成更细的)
_GALLERY_VIEW = {"exterior_45": "exterior_45", "interior": "interior", "detail": "detail"}


def _thumb(src: Path, dest: Path) -> Path | None:
    """官图原图动辄几 MB,打标签只要一张 1280 宽的。"""
    try:
        from PIL import Image

        dest.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((1280, 1280))
            im.save(dest, "JPEG", quality=85)
        return dest
    except Exception:  # noqa: BLE001
        return None
