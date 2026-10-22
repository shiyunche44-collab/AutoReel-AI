"""竖版(9:16)成片。

**竖版不是另一套管线,是同一条管线跑第二遍。** 叠加层(`overlays.*` / `cards.*`)、
章节条、大字、数据卡本来就都吃 `size` 参数,合成与封面也一样。缺的只是
"按 1080×1920 再渲一遍"这件事本身。

三处需要特别处理:

1. **背景不硬裁**。`explainer._fit_filter` 对视频片段本来就是「前景完整缩进画幅 +
   同画面模糊铺满」,16:9 源放进 9:16 不会切掉车头车尾 —— 这一层不用改。
   但 `piece.crop_bottom`(烧录字幕段裁底部)那条分支会**放大铺满**,竖版下等于
   把车裁掉,所以竖版强制按 0 处理。
2. **概念动画拍退回数据卡**。`motion` 只支持 1920×1080,其它画幅抛
   `MotionError`;`render_beat` 已经接住了这个异常并画成数据卡 —— 竖版直接
   复用这个既有行为,不新增概念。
3. **字幕字号按短边算**。`timeline.format_ass` 原本按高度算字号,1080×1920 会
   算出 107px 的字,一行放不下 16 个字。改成按短边(min(宽,高))算之后,
   横屏 1920×1080 与竖屏 1080×1920 都是 60px,横屏输出完全不变。
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .. import config, db
from ..models import ShotSpec, SubtitleCue
from . import explainer, library, narration, render as render_mod, timeline

#: 竖版画幅
VERTICAL_SIZE = (1080, 1920)
#: 竖版字幕单行字数。60px 字号 × 15 字 = 900px,画幅内可用宽 952px,刚好不溢出。
VERTICAL_LINE_CHARS = 15
#: 竖版产物名
OUTPUT_NAME = "output_vertical.mp4"
COVER_NAME = "cover_vertical.jpg"


def output_path(project_id: str) -> Path:
    return config.project_dir(project_id) / OUTPUT_NAME


def cover_path(project_id: str) -> Path:
    return config.project_dir(project_id) / COVER_NAME


def exists(project_id: str) -> bool:
    return output_path(project_id).is_file()


def _specs(project_id: str) -> list[ShotSpec]:
    raw = db.read_json(project_id, "storyboard.json", []) or []
    return [ShotSpec(**d) for d in raw]


def build(project_id: str) -> dict[str, Any]:
    """产出竖版:逐拍重渲 + 竖版字幕 + 竖版封面 + 合成。返回产物信息。"""
    from ..categories import load_profile

    project = db.get_project(project_id)
    if not project:
        raise RuntimeError("项目不存在")
    if not (config.project_dir(project_id) / "output.mp4").is_file():
        raise RuntimeError("横屏成片还没生成,先出横屏再出竖屏")

    profile = load_profile(project["category"])
    specs = [s for s in _specs(project_id) if s.duration > 0]
    if not specs:
        raise RuntimeError("没有可渲染的拍")
    subject = str(project.get("subject") or "")
    subjects = db.project_subjects(project)
    names = [s["name"] for s in subjects]
    chapters, where = explainer.chapters_of(specs)
    style = explainer.concept_style_of(project_id)
    out_dir = config.project_dir(project_id) / "shots_v"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 竖版单独计用量,不影响横版的去重账
    used: dict[str, int] = {}
    recent: list[str] = []
    bg_cache: dict[str, list[Path]] = {}
    degraded: list[int] = []
    paths: list[Path] = []
    errors: list[str] = []

    for pos, s in enumerate(specs):
        car = explainer.beat_subject(s, names, subject)
        if car not in bg_cache:
            bg_cache[car] = explainer.card_backgrounds(car)
        bgs = bg_cache[car]
        try:
            with explainer.fit_background():
                path, pieces = explainer.render_beat(
                    s, subject=car, size=VERTICAL_SIZE, out_dir=out_dir,
                    chapters=chapters, chapter_idx=where[pos], used=used,
                    card_bg=bgs[s.idx % len(bgs)] if bgs else None,
                    recent=recent,
                    sting=explainer.sting_for(specs, where, chapters, s.idx),
                    style=style)
        except (render_mod.RenderError, RuntimeError, OSError) as exc:
            errors.append(f"第 {s.idx} 拍:{exc}")
            continue
        if s.layout == "concept":
            degraded.append(s.idx)
        paths.append(path)

    if not paths:
        raise RuntimeError("竖版各拍都没渲出来:" + ("; ".join(errors[:3]) or "未知原因"))

    # ---- 竖版字幕(旁白与 BGM 复用横版,不重新 TTS)----
    nar = db.read_json(project_id, "narration.json", {}) or {}
    ass_path = _vertical_ass(project_id, nar)

    result = render_mod.compose(
        project_id, paths,
        ass_path=ass_path,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        output_name=OUTPUT_NAME,
        size=VERTICAL_SIZE,
        durations=[s.duration for s in specs],
    )
    if not result.ok or not result.output_path:
        raise RuntimeError("竖版合成失败:" + "; ".join(result.warnings or ["未知原因"]))

    total = sum(s.duration for s in specs)
    meta = explainer.cover_meta(project_id, profile, total)
    still = library.cover_still(subject) if subject else None
    cover = render_mod.build_cover(result.output_path, project.get("title") or subject,
                                   cover_path(project_id), subtitle=meta["subtitle"],
                                   size=VERTICAL_SIZE, still=still, kicker=meta["kicker"])

    payload = {
        "output_path": str(result.output_path),
        "cover": str(cover) if cover else None,
        "duration": result.duration,
        "size": list(VERTICAL_SIZE),
        "beats": len(paths),
        "degraded_beats": degraded,
        "errors": errors,
        "warnings": result.warnings or [],
        "last_attempt": time.time(),
    }
    db.write_json(project_id, "render_vertical.json", payload)
    return payload


def _vertical_ass(project_id: str, nar: dict[str, Any]) -> Path | None:
    """按竖版画幅重新生成 ASS 字幕。"""
    cues_raw = nar.get("cues") or []
    if not cues_raw:
        return None
    cues = [SubtitleCue(**c) for c in cues_raw if isinstance(c, dict)]
    if not cues:
        return None
    content = timeline.format_ass(
        cues, width=VERTICAL_SIZE[0], height=VERTICAL_SIZE[1],
        font_name=narration._pick_font_name(), line_chars=VERTICAL_LINE_CHARS)
    path = config.project_dir(project_id) / "audio" / "subs_vertical.ass"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def clean(project_id: str) -> None:
    """删掉竖版产物(重新生成前用)。"""
    for path in (output_path(project_id), cover_path(project_id)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(config.project_dir(project_id) / "shots_v", ignore_errors=True)


# --------------------------------------------------------------------------
# 后台:成片之后自动补一版竖版
# --------------------------------------------------------------------------
#: 扫一轮的间隔(秒)
SCAN_INTERVAL = 60
#: 一轮最多做几个 —— 竖版要把所有拍重渲一遍,别和主渲染抢 CPU
PER_ROUND = 1
#: 失败后多久再试(秒)
RETRY_AFTER = 30 * 60

_thread = None
_lock = threading.Lock()
_stop = threading.Event()


def sweep(limit: int = PER_ROUND) -> int:
    """给已完成的项目补一版竖版。返回做出来的条数。

    失败的项目不立刻重试 —— 素材池里没有可用素材时,每分钟重试一次就是白烧 CPU。
    半小时后再试。
    """
    if not config.PUBLISH_ENABLED:
        return 0
    done = 0
    now = time.time()
    for row in db.list_projects(limit=50):
        if row["state"] != "DONE" or exists(row["id"]):
            continue
        marker = db.read_json(row["id"], "render_vertical.json", {}) or {}
        last = float(marker.get("last_attempt") or 0)
        if last and now - last < RETRY_AFTER:
            continue
        try:
            build(row["id"])
        except Exception as exc:  # noqa: BLE001 - 竖版失败不该影响主流程
            db.write_json(row["id"], "render_vertical.json",
                          {**marker, "last_attempt": now, "error": str(exc)[:400]})
            db.add_event(row["id"], "vertical", reason=f"竖版生成失败:{exc}")
            continue
        done += 1
        if done >= limit:
            break
    return done


def _loop() -> None:
    while not _stop.is_set():
        try:
            sweep()
        except Exception:  # noqa: BLE001
            pass
        _stop.wait(SCAN_INTERVAL)


def start() -> None:
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="vertical-worker", daemon=True)
        _thread.start()


def stop() -> None:
    _stop.set()
