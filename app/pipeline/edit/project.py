"""把一个项目接到剪辑 v2:从库里取分镜 / 旁白 / 逐拍成品,取材,编时间线,合成。

取材只做一次(`plan`,逐拍渲染完、进门禁 B 之前),结果连同每一刀的取材记录写进
`edit_timeline.json`。之后的预览、成片、只改字幕的重合成都按记录回放(`_Picker` 按
「拍号:第几刀」对上旧取材),门禁 B 审到的画面就是成片的画面。会让取材变的只有:

- 某一刀的时长 / 半句变了(TTS 重合成、改稿):那一刀重取;
- 素材文件没了、或进了排除清单(`exclude_materials.json`):那一刀重取;
- 重拍某几拍(`mark_dirty`)、审片时「换一刀」(`swap`):只重取这几刀,换下的素材往后排。

`compose` 只由 `explainer.compose` 调用;任何失败都抛出,由调用方退回旧合成(并 `supersede`)。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ... import config, db, degrade
from ...categories import Vocab
from ...models import ShotSpec
from .. import caption_layer, library, looks, outro, sfx
from .. import render as render_mod
from ..explainer.planning_b import beat_subject
from . import match
from . import render as edit_render
from .edl import PRERENDERED, Timeline, build_timeline, remotion_card

log = logging.getLogger(__name__)

#: 时间线文件(项目目录里)
TIMELINE_FILE = "edit_timeline.json"
#: 试片脚本(scripts/edit_v2_trial.py)的时间线另存一份:不顶掉正式的,也不进使用记录
TRIAL_FILE = "edit_timeline_trial.json"
#: 取材记录的格式版本;对不上的旧记录整份不认,重新取材
PLAN_VERSION = 1
#: 旧取材对得上这一刀的条件之一:时长差不超过这么多秒
SPAN_TOLERANCE = 0.05
#: 换下来 / 打回重拍的素材,在之后的排序里记成多用了这么多次(和 `explainer.rerender` 一致)
AVOID_WEIGHT = 3
#: 与 explainer.EXCLUDE_FILE 相同(这里不 import explainer 包,免得循环)
EXCLUDE_FILE = "exclude_materials.json"


# --------------------------------------------------------------------------
# 取材:新取 / 回放
# --------------------------------------------------------------------------
def _beat_of(key: str) -> int:
    try:
        return int(str(key).split(":", 1)[0])
    except ValueError:
        return -1


class _Picker:
    """`build_timeline` 的 pick 回调:按每一刀念到的半句取材,全片共享已用计数。

    - 记录里有这一刀(同一拍的第 n 次调用 = 第 n 刀)且还对得上:原样回放,不碰素材池;
    - 否则新取:`library.fill(detail=半句, views=半句点到的视角)`;有文字模型且
      `EDIT_V2_RERANK` 没关时,排序前几名再让模型按素材描述挑一次;
    - 每段记下 need / match / via,汇总成对题率。取到的都记进 `picks`,由 `plan` 落盘。
    """

    def __init__(self, subject: str, subjects: Sequence[str], vocab: Vocab | None, llm: Any,
                 *, saved: Mapping[str, Mapping[str, Any]], used: dict[str, int],
                 banned: set[str], swap_bans: Mapping[str, set[str]]) -> None:
        self.subject = subject
        self.subjects = list(subjects)
        self.vocab = vocab
        self.saved = dict(saved)
        self.used = used
        self.recent: list[str] = []
        self.banned = banned
        self.swap_bans = dict(swap_bans)
        self.calls: dict[int, int] = {}
        self.picks: dict[str, dict[str, Any]] = {}
        self.replayed = 0
        self.fresh = 0
        self._items: dict[str, dict[str, Any]] = {}
        self._chooser = (match.llm_chooser(llm, subject)
                         if llm is not None and vocab is not None and match.rerank_enabled() else None)

    def _lookup(self, car: str, item_id: str) -> Any:
        if car not in self._items:
            self._items[car] = {i.id: i for i in library.Library.load(car).items}
        return self._items[car].get(library.base_item_id(item_id)) or self._items[car].get(item_id)

    def replayable(self, old: Mapping[str, Any], seconds: float, text: str) -> bool:
        """旧取材还能不能原样用:这一刀时长、半句没变,素材文件都在,也没进排除清单。"""
        if abs(float(old.get("seconds") or 0.0) - seconds) > SPAN_TOLERANCE:
            return False
        if str(old.get("text") or "") != text:
            return False
        for p in old.get("pieces") or []:
            if not Path(str(p.get("path") or "")).is_file():
                return False
            ids = {str(p.get(k) or "") for k in ("item_id", "source_url")} - {""}
            if ids & self.banned or library.base_item_id(str(p.get("item_id") or "")) in self.banned:
                return False
        return True

    def __call__(self, spec: ShotSpec, seconds: float, text: str, role: str) -> list[dict[str, Any]]:
        n = self.calls.get(spec.idx, 0)
        self.calls[spec.idx] = n + 1
        key = f"{spec.idx}:{n}"
        old = self.saved.get(key)
        if old is not None and self.replayable(old, seconds, text):
            self.replayed += 1
            pieces = [dict(p) for p in old.get("pieces") or []]
        else:
            self.fresh += 1
            pieces = self._fill(spec, seconds, text, role, key)
        self.picks[key] = {"seconds": round(seconds, 3), "text": text, "role": role,
                           "pieces": pieces}
        return [dict(p) for p in pieces]

    def _fill(self, spec: ShotSpec, seconds: float, text: str, role: str,
              key: str) -> list[dict[str, Any]]:
        car = beat_subject(spec, self.subjects, self.subject)
        vocab = self.vocab
        views = match.views_for(text, vocab) if vocab is not None else set()
        chose: list[int | None] = []
        chooser = self._chooser

        def choose(detail: str, cands: list[Any]) -> int | None:
            k = chooser(detail, cands) if chooser else None
            chose.append(k)
            return k

        ban = self.swap_bans.get(key)
        try:
            # 换一刀:这一刀额外排除换下来的素材(嵌套会替换外层集合,所以传并集)
            ctx = library.banned_materials(sorted(self.banned | ban)) if ban else nullcontext()
            with ctx:
                pieces = library.fill(car, seconds, hint=spec.view, used=self.used,
                                      recent=self.recent, detail=text, role=role,
                                      views=views or None, choose=choose if chooser else None)
        except Exception:  # noqa: BLE001 —— 一刀取不到就退回逐拍成品,不拦整片
            log.warning("第 %s 拍取材失败", spec.idx, exc_info=True)
            return []
        out = []
        for i, p in enumerate(pieces):
            d = dict(p.__dict__)
            if vocab is not None:
                d["need"], d["match"] = match.judge(text, self._lookup(car, p.item_id), vocab)
                if i > 0:
                    d["need"] = False  # 补足这一刀的后几段不计对题率
            d["via"] = "llm" if i == 0 and chose and chose[0] is not None else "rank"
            out.append(d)
        return out


def _picker(subject: str, vocab: Vocab | None, llm: Any = None) -> Any:
    """一次性的取材回调(不回放、不落盘):试片脚本 / 测试直接用。"""
    return _Picker(subject, (), vocab, llm, saved={}, used={}, banned=set(), swap_bans={})


# --------------------------------------------------------------------------
# 视觉包 → Remotion 主题
# --------------------------------------------------------------------------
def _hex(rgb: Sequence[int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb[:3])


#: 深色卡片上的强调色至少要这么亮(相对亮度),否则往白色提:杂志包的深蓝压在深底上看不清
MIN_LUMA = 0.32


def _luma(rgb: Sequence[int]) -> float:
    r, g, b = (c / 255 for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable(rgb: Sequence[int]) -> tuple[int, int, int]:
    """把颜色往白色提到 MIN_LUMA 以上(色相大体不变)。"""
    c = tuple(int(x) for x in rgb[:3])
    k = 0.0
    while _luma(c) < MIN_LUMA and k < 1.0:
        k += 0.1
        c = tuple(round(x + (255 - x) * k) for x in rgb[:3])
    return c  # type: ignore[return-value]


def theme_of(lk: Any) -> dict[str, Any]:
    """视觉包 → Remotion 主题:强调色、第二强调色(对比卡右栏 / 柱状图)、字幕样式。"""
    return {"accent": _hex(readable(lk.accent)), "accent2": _hex(readable(lk.card_accent_b)),
            "subs": lk.subs if lk.subs in ("box", "outline", "band") else "outline",
            "subBox": _hex(lk.accent)}


def _sfx(project_dir: Path, tl: Timeline, dest: Path | None = None) -> Path | None:
    """音效:换章 / 进出整屏画面 whoosh,花字关键词与大数字 pop。默认写 `_render/sfx.wav`。"""
    if not config.SFX_ENABLED or not looks.current().sfx:
        return None
    reveals = [o.start + float(o.props.get("at") or 0.0) for o in tl.overlays if o.type == "stat"]
    events = sfx.events_for(tl.captions, tl.transitions_at(("whip", "zoom", "flash")),
                            reveal_times=reveals)
    return sfx.build_track(events, tl.duration, dest or project_dir / "_render" / "sfx.wav")


# --------------------------------------------------------------------------
# 时间线文件
# --------------------------------------------------------------------------
def _file(source: str) -> str:
    return TRIAL_FILE if source == "trial" else TIMELINE_FILE


def load(project_id: str, *, trial: bool = False) -> dict[str, Any]:
    """读时间线文件(没有 / 坏了返回空字典)。`trial` 读试片脚本那一份。"""
    saved = db.read_json(project_id, TRIAL_FILE if trial else TIMELINE_FILE, None)
    return saved if isinstance(saved, dict) else {}


def _plan_of(saved: Mapping[str, Any]) -> dict[str, Any]:
    meta = saved.get("plan")
    if not isinstance(meta, dict) or meta.get("version") != PLAN_VERSION:
        return {}
    return meta


def active(project_id: str) -> dict[str, Any] | None:
    """这个项目当前算数的时间线:有取材记录、不是试片写的、没被旧合成顶掉。

    素材使用记录 / 来源声明 / 复盘特征有它就按它算(见 `explainer.footage`),没有就读逐拍记录。"""
    saved = load(project_id)
    meta = _plan_of(saved)
    if not meta or not saved.get("clips") or meta.get("superseded"):
        return None
    return saved


def _save(project_id: str, tl: Timeline, stats: Mapping[str, Any],
          meta: Mapping[str, Any]) -> None:
    db.write_json(project_id, _file(str(meta.get("source") or "")),
                  tl.to_props() | {"notes": tl.notes, "match": dict(stats), "plan": dict(meta)})


def supersede(project_id: str, reason: str) -> None:
    """这一版成片没走剪辑 v2(没开 / 失败退回旧合成):时间线留着备查,但不再算数。"""
    saved = load(project_id)
    meta = _plan_of(saved)
    if not meta or meta.get("superseded"):
        return
    meta["superseded"] = reason[:200]
    saved["plan"] = meta
    db.write_json(project_id, TIMELINE_FILE, saved)


def reset(project_id: str) -> None:
    """全片重渲(门禁 A 之后重建 shots):旧的取材记录作废,连同时间线删掉。"""
    (config.project_dir(project_id) / TIMELINE_FILE).unlink(missing_ok=True)


def mark_dirty(project_id: str, idxs: Iterable[int]) -> list[str]:
    """重拍这几拍:删掉它们的取材记录(下次 `plan` 重取),原来的素材往后排。返回删掉的刀。"""
    saved = load(project_id)
    meta = _plan_of(saved)
    if not meta:
        return []
    beats = {int(i) for i in idxs}
    picks = dict(meta.get("picks") or {})
    gone = [k for k in picks if _beat_of(k) in beats]
    avoid = list(meta.get("avoid") or [])
    for k in gone:
        avoid += [str(p["item_id"]) for p in picks.pop(k).get("pieces") or [] if p.get("item_id")]
    meta["picks"], meta["avoid"] = picks, avoid
    saved["plan"] = meta
    db.write_json(project_id, TIMELINE_FILE, saved)
    return gone


# --------------------------------------------------------------------------
# 取材 + 编时间线
# --------------------------------------------------------------------------
def _seed_used(project_id: str, beats: Sequence[ShotSpec], subject: str, subjects: Sequence[str],
               saved: Mapping[str, Mapping[str, Any]], avoid: Iterable[str]) -> dict[str, int]:
    """排序用的已用计数:近期同车成片用过的(跨项目去重)+ 整屏预渲染拍里用掉的
    + 这次会回放的旧取材 + 换下来 / 打回的素材(多记几次)。只影响排序,不过滤。"""
    from ..explainer import cross_project_seed

    used: dict[str, int] = {}
    for car in {beat_subject(s, subjects, subject) for s in beats} | ({subject} if subject else set()):
        for k, n in cross_project_seed(project_id, car).items():
            used[k] = used.get(k, 0) + n
    for s in beats:
        if s.layout in PRERENDERED and not remotion_card(s):
            for p in db.read_json(project_id, f"beats/{s.idx:03d}.json", None) or []:
                iid = str(p.get("item_id") or "")
                if iid and iid != "bg":
                    used[iid] = used.get(iid, 0) + 1
    for old in saved.values():
        for p in old.get("pieces") or []:
            iid = str(p.get("item_id") or "")
            if iid:
                used[iid] = used.get(iid, 0) + 1
    for iid in avoid:
        used[iid] = used.get(iid, 0) + AVOID_WEIGHT
    return used


def _beat_file(row: Mapping[str, Any] | None) -> str:
    row = row or {}
    return str(row.get("final_path") or row.get("draft_path") or "")


def plan(project_id: str, specs: Sequence[ShotSpec], rows: Mapping[int, Mapping[str, Any]],
         nar: Mapping[str, Any], *, size: tuple[int, int], title: str, subject: str,
         subjects: Sequence[str] = (), vocab: Vocab | None = None, llm: Any = None,
         source: str = "pipeline", fresh: bool = False, swap: str = "") -> Timeline:
    """取材 + 编时间线,写 `edit_timeline.json` 并返回时间线。

    已有取材记录时逐刀回放(见 `_Picker`),只重取对不上的刀;`fresh` 全部重取;
    `swap` 只重取这一刀(「拍号:第几刀」),并排除这一刀原来的素材。
    `rows`:拍号 → shots 行(草稿档或成片档都行),给整屏卡片 / 取不到素材时兜底用。
    `source`:pipeline(流水线)/ swap(审片换刀)/ trial(试片脚本:读写另一份 `TRIAL_FILE`,
    不影响正式时间线和使用记录)。"""
    project_dir = config.project_dir(project_id)
    beats = [s for s in specs if s.duration > 0]
    saved = load(project_id, trial=source == "trial")
    meta = _plan_of(saved)
    saved_picks: dict[str, dict[str, Any]] = {} if fresh else dict(meta.get("picks") or {})
    avoid = list(meta.get("avoid") or [])
    swap_bans: dict[str, set[str]] = {}
    if swap:
        old = saved_picks.pop(swap, None)
        if old is None:
            raise KeyError(f"时间线里没有这一刀:{swap}")
        ids = {str(p.get("item_id")) for p in old.get("pieces") or [] if p.get("item_id")}
        swap_bans[swap] = ids
        avoid += sorted(ids)
    banned = {str(x) for x in db.read_json(project_id, EXCLUDE_FILE, []) or []}
    used = _seed_used(project_id, beats, subject, subjects, saved_picks, avoid)
    picker = _Picker(subject, subjects, vocab, llm, saved=saved_picks, used=used,
                     banned=banned, swap_bans=swap_bans)
    lk = looks.current()
    cues = list(nar.get("cues") or [])
    total = sum(s.duration for s in beats)
    captions = caption_layer.build_layer(cues, size, render_mod.FPS, lk.accent,
                                         duration=total)["cues"] if cues else []
    card = outro.build(project_dir, size)
    with library.banned_materials(sorted(banned)):
        tl = build_timeline(
            beats, cues, size=size, fps=render_mod.FPS, pick=picker,
            beat_files={s.idx: _beat_file(rows.get(s.idx)) for s in beats},
            title=title, accent=_hex(lk.accent), captions=captions,
            outro=(str(card[0]), float(card[1])) if card else None,
        )
    tl.theme = theme_of(lk)
    stats = tl.match_stats()
    if picker.fresh and stats["fallback"]:
        degrade.note("edit_v2.shot_to_beat", f"{stats['fallback']} 刀取不到素材,用了逐拍成品",
                     project_id=project_id)
    _save(project_id, tl, stats, {
        "version": PLAN_VERSION, "source": source, "planned_at": round(time.time(), 1),
        "picks": picker.picks, "avoid": avoid[-200:],
        "replayed": picker.replayed, "fresh": picker.fresh,
    })
    return tl


def compose(project_id: str, specs: Sequence[ShotSpec], rows: Mapping[int, Mapping[str, Any]],
            nar: Mapping[str, Any], *, size: tuple[int, int], title: str, subject: str,
            output_name: str, vocab: Vocab | None = None, llm: Any = None,
            subjects: Sequence[str] = (), source: str = "pipeline",
            fresh: bool = False) -> render_mod.RenderResult:
    """成片:按取材记录回放出时间线(门禁 B 审过的那一组画面),渲染 + 混音。"""
    tl = plan(project_id, specs, rows, nar, size=size, title=title, subject=subject,
              subjects=subjects, vocab=vocab, llm=llm, source=source, fresh=fresh)
    stats = tl.match_stats()
    result = edit_render.compose(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        sfx_path=_sfx(config.project_dir(project_id), tl), output_name=output_name,
    )
    if stats["need"]:
        result.warnings = [f"画面对题 {stats['hit']}/{stats['need']} 刀"
                           f"(文字模型复选 {stats['llm']} 刀,兜底 {stats['fallback']} 刀)",
                           *(result.warnings or [])]
    return result


def recompose(project_id: str, nar: Mapping[str, Any], *, output_name: str) -> render_mod.RenderResult | None:
    """只改了字幕文字时重出成片:沿用上次的时间线(同一批取材、同样的刀口),只换花字字幕。

    没有上次的时间线(上次走的是旧合成)返回 None,由调用方走旧的 recompose。"""
    saved = active(project_id)
    if not saved:
        return None
    tl = Timeline.from_props(saved)
    lk = looks.current()
    cues = list(nar.get("cues") or [])
    tl.captions = caption_layer.build_layer(cues, (tl.width, tl.height), tl.fps, lk.accent,
                                            duration=tl.duration)["cues"] if cues else []
    _save(project_id, tl, saved.get("match") or tl.match_stats(), _plan_of(saved))
    project_dir = config.project_dir(project_id)
    return edit_render.compose(
        project_id, tl,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        sfx_path=_sfx(project_dir, tl), output_name=output_name,
    )


# --------------------------------------------------------------------------
# 门禁 B 审片:刀表
# --------------------------------------------------------------------------
def cuts(project_id: str) -> list[dict[str, Any]]:
    """刀表:每一刀(同一取材键拼的几段合成一行)的起止、半句、素材、对没对上题。"""
    saved = load(project_id)
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for c in saved.get("clips") or []:
        key = str(c.get("key") or "")
        if not key:
            continue  # 整屏卡片 / 片尾:不是取材来的,换不了
        if key not in rows:
            order.append(key)
            rows[key] = {"key": key, "beat": int(c.get("beat", -1)), "start": float(c["start"]),
                         "end": float(c["start"]) + float(c["dur"]), "text": c.get("text") or "",
                         "need": bool(c.get("need")), "match": c.get("match") or "",
                         "via": c.get("via") or "", "pieces": []}
        r = rows[key]
        r["end"] = max(r["end"], float(c["start"]) + float(c["dur"]))
        r["need"] = r["need"] or bool(c.get("need"))
        r["match"] = r["match"] or str(c.get("match") or "")
        r["pieces"].append({"kind": c.get("kind"), "src": c.get("src"), "src_in": c.get("src_in"),
                            "dur": c.get("dur"), "item_id": c.get("item_id") or "",
                            "platform": c.get("platform") or "",
                            "license_status": c.get("license_status") or ""})
    for r in rows.values():
        r["start"], r["end"] = round(r["start"], 2), round(r["end"], 2)
    return [rows[k] for k in order]


#: 刀表缩略图宽(像素)
THUMB_WIDTH = 320


def cut_thumb(project_id: str, key: str) -> Path | None:
    """刀表缩略图:这一刀第一段素材中间那一帧(静图直接缩小),缓存在 `_edit/thumbs/`。
    只认时间线里有的刀(路径来自时间线,不接受外部传入)。"""
    import hashlib

    row = next((r for r in cuts(project_id) if r["key"] == key), None)
    if not row or not row["pieces"]:
        return None
    p = row["pieces"][0]
    src = Path(str(p.get("src") or ""))
    if not src.is_file():
        return None
    at = float(p.get("src_in") or 0.0) + float(p.get("dur") or 0.0) / 2
    tag = hashlib.sha1(f"{src}|{at:.2f}".encode()).hexdigest()[:16]
    dest = config.project_dir(project_id) / "_edit" / "thumbs" / f"{tag}.jpg"
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    seek = [] if p.get("kind") == "still" else ["-ss", f"{at:.2f}"]
    proc = render_mod._run([*seek, "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", "-y", str(dest)],
                           timeout=60)
    return dest if proc.returncode == 0 and dest.is_file() else None
