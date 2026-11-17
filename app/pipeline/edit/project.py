"""把一个项目接到剪辑 v2:从库里取分镜 / 旁白 / 逐拍成品,取材,编时间线,合成。

只由 `explainer.compose` 调用;任何失败都抛出,由调用方退回旧合成。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ... import config, db
from ...models import ShotSpec
from .. import caption_layer, library, looks, outro, sfx
from .. import render as render_mod
from . import render as edit_render
from .edl import Timeline, build_timeline

log = logging.getLogger(__name__)


def _picker(subject: str) -> Any:
    """按每一刀念到的半句取材:`library.fill(detail=半句)`,全片共享已用计数,尽量不重复。"""
    used: dict[str, int] = {}
    recent: list[str] = []

    def pick(spec: ShotSpec, seconds: float, text: str, role: str) -> list[dict[str, Any]]:
        car = spec.subject or subject
        try:
            pieces = library.fill(car, seconds, hint=spec.view, used=used, recent=recent,
                                  detail=text, role=role)
        except Exception:  # noqa: BLE001 —— 一刀取不到就退回逐拍成品,不拦整片
            log.warning("第 %s 拍取材失败", spec.idx, exc_info=True)
            return []
        return [dict(p.__dict__) for p in pieces]

    return pick


def _sfx(project_dir: Path, tl: Timeline) -> Path | None:
    """音效:换章 / 进出整屏画面 whoosh,花字关键词与大数字 pop。"""
    if not config.SFX_ENABLED or not looks.current().sfx:
        return None
    reveals = [o.start + float(o.props.get("at") or 0.0) for o in tl.overlays if o.type == "stat"]
    events = sfx.events_for(tl.captions, tl.transitions_at(("whip", "zoom", "flash")),
                            reveal_times=reveals)
    return sfx.build_track(events, tl.duration, project_dir / "_render" / "sfx.wav")


def compose(project_id: str, specs: Sequence[ShotSpec], rows: Mapping[int, Mapping[str, Any]],
            nar: Mapping[str, Any], *, size: tuple[int, int], title: str, subject: str,
            output_name: str) -> render_mod.RenderResult:
    project_dir = config.project_dir(project_id)
    beats = [s for s in specs if s.duration > 0]
    total = sum(s.duration for s in beats)
    lk = looks.current()
    accent = "#{:02X}{:02X}{:02X}".format(*lk.accent)
    cues = list(nar.get("cues") or [])
    captions = caption_layer.build_layer(cues, size, render_mod.FPS, lk.accent,
                                         duration=total)["cues"] if cues else []
    card = outro.build(project_dir, size)
    tl = build_timeline(
        beats, cues, size=size, fps=render_mod.FPS, pick=_picker(subject),
        beat_files={s.idx: str((rows.get(s.idx) or {}).get("final_path") or "") for s in beats},
        title=title, accent=accent, captions=captions,
        outro=(str(card[0]), float(card[1])) if card else None,
    )
    db.write_json(project_id, "edit_timeline.json", tl.to_props() | {"notes": tl.notes})
    result = edit_render.compose(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        sfx_path=_sfx(project_dir, tl), output_name=output_name,
    )
    return result
