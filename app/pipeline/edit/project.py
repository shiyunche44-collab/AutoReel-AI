"""把一个项目接到剪辑 v2:从库里取分镜 / 旁白 / 逐拍成品,取材,编时间线,合成。

只由 `explainer.compose` 调用;任何失败都抛出,由调用方退回旧合成。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ... import config, db
from ...categories import Vocab
from ...models import ShotSpec
from .. import caption_layer, library, looks, outro, sfx
from .. import render as render_mod
from . import match
from . import render as edit_render
from .edl import Timeline, build_timeline

log = logging.getLogger(__name__)


def _picker(subject: str, vocab: Vocab | None, llm: Any = None) -> Any:
    """按每一刀念到的半句取材(全片共享已用计数,尽量不重复):

    - `library.fill(detail=半句, views=半句点到的视角)`;
    - 有文字模型且 `EDIT_V2_RERANK` 没关:排序前几名再让模型按素材描述挑一次;
    - 每段记下 need / match / via,汇总成对题率。
    """
    used: dict[str, int] = {}
    recent: list[str] = []
    items: dict[str, dict[str, Any]] = {}
    chooser = (match.llm_chooser(llm, subject)
               if llm is not None and vocab is not None and match.rerank_enabled() else None)

    def lookup(car: str, item_id: str) -> Any:
        if car not in items:
            items[car] = {i.id: i for i in library.Library.load(car).items}
        return items[car].get(library.base_item_id(item_id)) or items[car].get(item_id)

    def pick(spec: ShotSpec, seconds: float, text: str, role: str) -> list[dict[str, Any]]:
        car = spec.subject or subject
        views = match.views_for(text, vocab) if vocab is not None else set()
        chose: list[int | None] = []

        def choose(detail: str, cands: list[Any]) -> int | None:
            k = chooser(detail, cands) if chooser else None
            chose.append(k)
            return k

        try:
            pieces = library.fill(car, seconds, hint=spec.view, used=used, recent=recent,
                                  detail=text, role=role, views=views or None,
                                  choose=choose if chooser else None)
        except Exception:  # noqa: BLE001 —— 一刀取不到就退回逐拍成品,不拦整片
            log.warning("第 %s 拍取材失败", spec.idx, exc_info=True)
            return []
        out = []
        for n, p in enumerate(pieces):
            d = dict(p.__dict__)
            if vocab is not None:
                d["need"], d["match"] = match.judge(text, lookup(car, p.item_id), vocab)
                if n > 0:
                    d["need"] = False  # 补足这一刀的后几段不计对题率
            d["via"] = "llm" if n == 0 and chose and chose[0] is not None else "rank"
            out.append(d)
        return out

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
            output_name: str, vocab: Vocab | None = None,
            llm: Any = None) -> render_mod.RenderResult:
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
        beats, cues, size=size, fps=render_mod.FPS, pick=_picker(subject, vocab, llm),
        beat_files={s.idx: str((rows.get(s.idx) or {}).get("final_path") or "") for s in beats},
        title=title, accent=accent, captions=captions,
        outro=(str(card[0]), float(card[1])) if card else None,
    )
    stats = tl.match_stats()
    db.write_json(project_id, "edit_timeline.json",
                  tl.to_props() | {"notes": tl.notes, "match": stats})
    result = edit_render.compose(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        sfx_path=_sfx(project_dir, tl), output_name=output_name,
    )
    if stats["need"]:
        result.warnings = [f"画面对题 {stats['hit']}/{stats['need']} 刀"
                           f"(文字模型复选 {stats['llm']} 刀,兜底 {stats['fallback']} 刀)",
                           *(result.warnings or [])]
    return result


def recompose(project_id: str, nar: Mapping[str, Any], *, output_name: str) -> render_mod.RenderResult | None:
    """只改了字幕文字时重出成片:沿用上次的时间线(同一批取材、同样的刀口),只换花字字幕。

    没有上次的时间线(上次走的是旧合成)返回 None,由调用方走旧的 recompose。"""
    saved = db.read_json(project_id, "edit_timeline.json", None)
    if not saved or not saved.get("clips"):
        return None
    tl = Timeline.from_props(saved)
    lk = looks.current()
    cues = list(nar.get("cues") or [])
    tl.captions = caption_layer.build_layer(cues, (tl.width, tl.height), tl.fps, lk.accent,
                                            duration=tl.duration)["cues"] if cues else []
    db.write_json(project_id, "edit_timeline.json", tl.to_props() | {
        "notes": tl.notes, "match": saved.get("match") or tl.match_stats()})
    project_dir = config.project_dir(project_id)
    return edit_render.compose(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        sfx_path=_sfx(project_dir, tl), output_name=output_name,
    )
