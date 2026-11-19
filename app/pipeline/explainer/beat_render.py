"""单拍渲染:版式滤镜、背景、概念素材、render_beat。"""
from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ... import db, degrade
from ...models import ShotSpec
from .. import aspect, cards, library, looks, mechanism_art, motion, motion_assets, overlays
from .. import render as render_mod
from . import mech
from .consts import (
    _ENC,
    HOOK_CUT,
    HOOK_CUT_RETRIES,
    HOOK_SECONDS,
    SAFE_LAYOUT,
    SPLIT_MIN_SECONDS,
    _log,
)
from .ffmpeg import (
    CONCEPT_FOOTAGE_HINT,
    _clip_luma,
    _ff,
    _framed,
    _piece_clip,
    background_clip,
    concept_background,
    footage_size,
    motion_for,
    split_clip,
)
from .planning_b import broll_style


def chapters_of(specs: Sequence[ShotSpec]) -> tuple[list[tuple[str, float]], list[int]]:
    """相邻同章的拍合成一章。返回 ([(章名, 时长)], 每拍所在的章序号)。"""
    chapters: list[tuple[str, float]] = []
    where: list[int] = []
    for s in specs:
        name = s.chapter or s.section or "正片"
        if chapters and chapters[-1][0] == name:
            chapters[-1] = (name, chapters[-1][1] + s.duration)
        else:
            chapters.append((name, s.duration))
        where.append(len(chapters) - 1)
    return chapters, where


def _spec_rows(spec: ShotSpec) -> list[tuple[str, str]]:
    """这一拍带的参数行(都在分镜阶段核对过事实):面板 / 概念动画 / 参数卡的行,或大数字那一个。"""
    ov = spec.overlay or {}
    if ov.get("kind") == "stat":
        return [(str(ov.get("label") or ""), str(ov.get("value") or ""))]
    box = ov if ov.get("kind") in ("panel", "concept") else None
    if box is None and (spec.card or {}).get("kind") == "spec":
        box = spec.card
    return [(str(r.get("label") or ""), str((r.get("values") or [""])[0]))
            for r in (box or {}).get("rows") or []]


#: 旁白里能上清单的数值:带这些单位的才算参数(年份、个数、名次不上)
_LEDGER_UNIT = (r"(?:毫米|厘米|千瓦时|千瓦|马力|牛·米|牛米|公里/小时|升/100公里|公里|公斤|千克|吨"
                r"|升|秒|英寸|°|万元)")
_LEDGER_NUM = re.compile(r"(\d+(?:\.\d+)?)" + _LEDGER_UNIT)
#: 参数名:紧挨着数字前面、同一个分句里的词(「WLTP纯电续航」「V2L放电」也算)
_LEDGER_LABEL = re.compile(r"([A-Za-z][A-Za-z0-9]*[一-龥]{1,8}|[一-龥]{1,10})$")
#: 参数名两头的虚词:「它有」「最高」这类不进清单
_LEDGER_HEAD = ("它的", "它有", "其", "它", "约", "官方", "但", "就")
_LEDGER_TAIL = ("最大可达", "可达", "达到", "最高", "约", "达", "为", "是", "有")
#: 「从 A 升到 B」这类变化句、只有动词的不算参数名
_LEDGER_BAD_END = ("从", "到", "至", "比")
_LEDGER_BAD = frozenset(("输出", "纵置", "提升", "增加", "减少", "降低", "多", "少", "快", "慢"))


def _narration_rows(text: str) -> list[tuple[str, str]]:
    """从旁白里抽「参数名 数值」:「车长5457毫米、宽1971毫米」→ [(车长, 5457毫米), (宽, 1971毫米)]。

    旁白里的数字在写稿阶段都核对过事实(没有出处的过不了门禁 A),这里只是换个地方再摆一次。
    数字前面认不出参数名的(「436马力、321千瓦」的第二个)不上,宁缺毋滥。"""
    out: list[tuple[str, str]] = []
    text = re.sub(r"0-100公里/小时", "百公里", text or "")
    for m in _LEDGER_NUM.finditer(text):
        before = re.split(r"[，,。；;：:、！!？?\s\-—（(]", text[:m.start()])[-1]
        hit = _LEDGER_LABEL.search(before)
        if not hit:
            continue
        label = hit.group(1)
        for head in _LEDGER_HEAD:
            if label.startswith(head) and len(label) > len(head):
                label = label[len(head):]
        for tail in _LEDGER_TAIL:
            if label.endswith(tail) and len(label) > len(tail):
                label = label[:-len(tail)]
        if label in _LEDGER_BAD or label.endswith(_LEDGER_BAD_END) or not 1 <= len(label) <= 8:
            continue
        out.append((label, m.group(0)))
    return out


def ledger_rows(specs: Sequence[ShotSpec], where: Sequence[int], pos: int) -> list[tuple[str, str]]:
    """常驻参数清单在第 `pos` 拍该显示哪些行:本章从头到这一拍讲过的参数,同名的留最新的。

    参数来自这一拍的数据卡 / 大数字,和旁白里念到的带单位数值(解说向视觉包里大部分拍是纯实拍,
    只看数据卡清单会一直是空的)。换章清空;开头章和最后一章不显示(开头有片头标和大字,
    结论不再报参数)。"""
    ch = where[pos]
    if ch == 0 or ch == max(where):
        return []
    rows: dict[str, str] = {}
    for n in range(pos + 1):
        if where[n] != ch:
            continue
        for label, value in _spec_rows(specs[n]) + _narration_rows(specs[n].narration_zh):
            if label and value:
                rows.pop(label, None)
                rows[label] = value
    return list(rows.items())


def layout_filter(layout: str, size: tuple[int, int], has_layer: bool, pip: bool = True) -> str:
    """把背景 [0]、章节条 [1]、叠加层 [2] 合成 [out] 的滤镜图。

    `layout` 除了版式名,还可以是 broll 的样式 `lower_third`(角标从左擦入)。
    横屏 highlight 全面升级为全屏沉浸暗场托字(弃用老旧的白框画中画小窗)。"""
    w, h = size
    if layout == "highlight" and has_layer and aspect.is_portrait_size(size):
        # 竖屏:实拍本来就在画面带里,不用再缩小窗;关键词占标题区,淡入
        return (
            "[2:v]format=rgba,fade=in:st=0.12:d=0.28:alpha=1[txt];"
            "[0:v][txt]overlay=0:0[v1];[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "highlight" and has_layer:
        # 横屏全屏实拍微压暗 + 大字淡入,视野通透不破坏实拍质感
        return (
            "[0:v]eq=brightness=-0.12:saturation=0.92[bgd];"
            "[2:v]format=rgba,fade=in:st=0.12:d=0.28:alpha=1[txt];"
            "[bgd][txt]overlay=0:0[v1];[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "hook" and has_layer:
        # [2] 是大字弹出关键帧(播完透明);前 3 秒压暗托字、不放章节条,之后条再淡入
        t = HOOK_SECONDS
        return (
            f"[0:v]eq=brightness=-0.14:enable='lt(t,{t})'[bgd];[2:v]format=rgba[txt];"
            f"[bgd][txt]overlay=0:0:eof_action=pass[v1];"
            f"[1:v]format=rgba,fade=in:st={t}:d=0.4:alpha=1[bar];"
            f"[v1][bar]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "stat" and has_layer:
        # [2] 是逐帧滚动的数字(concat 关键帧),播完停在最后一帧
        return (
            "[0:v]eq=brightness=-0.16:saturation=0.85[bgd];[2:v]format=rgba[num];"
            "[bgd][num]overlay=0:0:eof_action=repeat[v1];"
            "[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "panel" and has_layer:
        slide = int(w * 0.36)
        return (
            f"[0:v][2:v]overlay=x='-{slide}*pow(1-min(1,t/0.38),3)':y=0[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "lower_third" and has_layer:
        slide = int(w * 0.45)
        return (
            f"[0:v][2:v]overlay=x='-{slide}*pow(1-min(1,max(0,(t-0.25)/0.4)),3)':y=0[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    return "[0:v][1:v]overlay=0:0,format=yuv420p[out]"


def with_sting(graph: str, index: int) -> str:
    """在合成好的 [out] 上再叠章节标牌(输入 `index`,关键帧播完就透明露出下面)。"""
    head, _, _ = graph.rpartition("[out]")
    return (f"{head}[pre];[{index}:v]format=rgba[st];"
            f"[pre][st]overlay=0:0:eof_action=pass,format=yuv420p[out]")


def _frames_listing(frames: Sequence[tuple[Path, float]], listing: Path,
                    hold_to: float | None = None) -> Path:
    """关键帧写成 concat 清单。`hold_to` 给了就把最后一帧定格到这个总时长。"""
    lines: list[str] = []
    rolled = 0.0
    for n, (path, dur) in enumerate(frames):
        if hold_to is not None and n == len(frames) - 1:
            dur = max(0.5, hold_to - rolled)
        rolled += dur
        lines += [f"file '{path.as_posix()}'", f"duration {dur:.3f}"]
    lines.append(f"file '{frames[-1][0].as_posix()}'")  # concat 要求末帧重复一次
    listing.write_text("\n".join(lines), encoding="utf-8")
    return listing


def _stat_listing(overlay: dict, subject: str, size: tuple[int, int], work: Path,
                  seconds: float) -> Path:
    """大数字滚动的关键帧写成 concat 清单:滚完后最后一帧定格到拍尾。"""
    frames = overlays.stat_frames(overlay, subject, size, work / "stat")
    return _frames_listing(frames, work / "stat.txt", hold_to=seconds)


def concept_card(overlay: dict) -> dict:
    """概念动画渲染不了时退回的数据卡(同一批核对过的行)。"""
    kind = "compare" if overlay.get("scene") == "compare" else "spec"
    return {"kind": kind, "title": overlay.get("title") or "", "rows": overlay.get("rows") or [],
            "columns": list(overlay.get("columns") or []), "source": overlay.get("source", "")}


def _broll_base(spec: ShotSpec, subject: str, seconds: float, size: tuple[int, int], work: Path,
                used: dict[str, int], recent: list[str], card_bg: Path | None,
                role: str = "body") -> tuple[Path, list[library.Piece]]:
    """broll / panel / highlight / stat 的背景。分屏样式铺 2–3 条不同的背景再并排。

    `detail` 把这一拍的旁白交给素材池:讲刹车时别配一个门把手特写
    (特写太抢戏,观众会以为屏幕上那个部件就是正在讲的东西)。
    """
    detail = spec.narration_zh or spec.visual_zh or ""
    if broll_style(spec) == "split" and seconds >= SPLIT_MIN_SECONDS:
        count = 3 if seconds <= 5 else 2
        clips, pieces = [], []
        for k in range(count):
            got = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent,
                               detail=detail, role=role)
            if not got:
                break
            pieces += got
            clips.append(background_clip(got, seconds, size, work / f"panel_{k}",
                                         motion_seed=spec.idx * 7 + k))
        if len(clips) >= 2:
            return split_clip(clips, seconds, size, work / "split.mp4"), pieces
        if clips:
            return clips[0], pieces
    pieces = library.fill(subject, seconds, hint=spec.view, used=used, recent=recent,
                          detail=detail, role=role)
    if not pieces and card_bg:
        pieces = [library.Piece("bg", "still", str(card_bg), 0.0, seconds)]
    prefer_punch = spec.layout in ("stat", "highlight")
    return background_clip(pieces, seconds, size, work, motion_seed=spec.idx,
                           prefer_punch=prefer_punch), pieces


def hook_background(subject: str, seconds: float, size: tuple[int, int], work: Path,
                    used: dict[str, int], recent: list[str], hint: str, seed: int
                    ) -> tuple[Path, list[library.Piece]]:
    """黄金三秒的背景:前几秒每 1 秒一刀硬切(不 xfade),之后接正常背景铺满这一拍。

    整拍只用确认是这台车的一方素材(`role="hook"`):前三秒观众只确认"是不是我要看的那台车",
    配错车或挂着别人的台标就划走了。一条都没有就抛错,`render_beat` 退回 highlight 版式,
    不退回第三方视频。"""
    n = 3 if seconds >= 3.5 else 2 if seconds >= 2.0 else 1
    work.mkdir(parents=True, exist_ok=True)
    cuts: list[library.Piece] = []
    parts: list[Path] = []
    prev = ""
    for k in range(n):
        # 这一刀实测是黑场(源片淡入、片名卡)就换一段重取:三秒里有一秒黑屏,
        # 观众看到的就是"一块黑底上弹出大字"
        for attempt in range(HOOK_CUT_RETRIES):
            got = library.fill(subject, HOOK_CUT, hint=hint, used=used, recent=recent, role="hook")
            if not got:
                break
            p = replace(got[0], length=HOOK_CUT)
            m = motion_for(p.item_id, seed + k, prev)
            clip = _piece_clip(p, work / f"cut_{k:02d}_{attempt}.mp4", size, m)
            if _clip_luma(clip, work / f"cut_{k:02d}_{attempt}.png") >= library.DARK_LUMA:
                break
        # 重试用完仍是黑场就用最后一次取到的(总比整拍失败强)
        if not got:
            continue
        prev = m
        cuts.append(p)
        parts.append(clip)
    if not cuts:
        raise render_mod.RenderError("开场没有可用素材")
    head = HOOK_CUT * len(cuts)
    rest = round(seconds - head, 3)
    if rest < 0.2:  # 余下太短就并进最后一刀
        last = replace(cuts[-1], length=round(seconds - HOOK_CUT * (len(cuts) - 1), 3))
        parts[-1] = _piece_clip(last, work / "cut_last.mp4", size, prev)
        cuts[-1] = last
        rest = 0.0
    pieces = list(cuts)
    if rest > 0:
        more = library.fill(subject, rest, hint=hint, used=used, recent=recent, role="hook")
        parts.append(background_clip(more, rest, size, work / "rest", motion_seed=seed + 9))
        pieces += more
    dest = work / "hook_bg.mp4"
    listing = render_mod.timeline_mod.concat_list_file(parts, work / "hook_parts.txt")
    proc = _ff(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-t", f"{seconds:.3f}", str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"开场背景拼接失败:{(proc.stderr or '')[-300:]}")
    return dest, pieces


def concept_inputs(spec: ShotSpec, subject: str, seconds: float, size: tuple[int, int], work: Path,
                   used: dict[str, int], recent: list[str], style: str | None = None,
                   role: str = "body", scene: motion.SceneRef | None = None
                   ) -> tuple[Path | None, motion_assets.Cutout | None, list[library.Piece]]:
    """concept 拍的素材:(实拍底, 车图, 用到的片段)。正式渲染和选风格预览共用,保证预览看到的就是成片的样子。
    产品镜(studio)用模板自带的影棚底,不铺实拍;通用模版自带底;其余压在一段实拍上。
    `scene` 给了(项目内片段的 Path)就按它的 meta 取底和车图,否则用分镜里写的场景名。"""
    if scene is None:
        scene = (spec.overlay or {}).get("scene", "")
    bg_clip: Path | None = None
    pieces: list[library.Piece] = []
    if motion.scene_background(scene, style) == "footage":
        # 一个长镜头放慢铺满;库里没有够长的才退回多段拼接
        take = library.long_take(subject, seconds, hint=CONCEPT_FOOTAGE_HINT, used=used, recent=recent,
                                 role=role)
        got = [take] if take else library.fill(subject, seconds, hint=CONCEPT_FOOTAGE_HINT,
                                               used=used, recent=recent, role=role)
        if got:
            try:
                fsize = footage_size(size)
                bg_clip = (concept_background(take, seconds, fsize, work) if take else
                           background_clip(got, seconds, fsize, work, motion_seed=spec.idx))
                # 竖屏也给 16:9 的画面带片段:概念动画把它放进自己的虚拟舞台(画面带)里
                pieces = got
            except Exception:
                bg_clip = None
    views = motion.scene_photo_views(scene, style)
    photo = motion_assets.hero_still(subject, views) if views else None
    return bg_clip, photo, pieces


def portrait_concept_style(scene: str, style: str | None,
                           size: tuple[int, int]) -> tuple[str | None, str]:
    """竖屏下这一拍实际用哪套模版:(模版 id 或 None, 降级说明)。

    模版文件头写了 `@portrait yes` 才算有竖屏版式;没适配的模版按竖屏尺寸渲出来
    还是横屏那套排布,会挤、会出界。顺序:选定模版 → 默认模版(场景专用)→ None(退回数据卡)。
    横屏原样返回选定模版。"""
    if not aspect.is_portrait_size(size) or motion.supports_portrait(style, scene):
        return style, ""
    chosen = motion.get_style(style).name
    if motion.supports_portrait(motion.DEFAULT_STYLE, scene):
        return motion.DEFAULT_STYLE, f"模版「{chosen}」还没有竖屏版,改用默认模版"
    return None, f"模版「{chosen}」与默认模版都还没有竖屏版,画成数据卡"


def render_mech(spec: ShotSpec, subject: str, seconds: float, size: tuple[int, int],
                work: Path, ranges: dict[str, str] | None = None) -> Path:
    """渲染一拍机构画面(mech)。用场景专用模版,不跟项目的概念动画风格走 ——
    通用模版只会排参数行,画不了机构。剖视图(cutaway)要先有图,见 `mechanism_art`。"""
    ov = spec.overlay or {}
    scene = str(ov.get("scene") or "")
    if scene not in motion.available_scenes():
        raise motion.MotionError(f"没有机构模版 {scene or '(空)'}")
    if aspect.is_portrait_size(size) and not motion.supports_portrait(motion.DEFAULT_STYLE, scene):
        raise motion.MotionError(f"机构模版 {scene} 还没有竖屏版")
    data = mech.scene_data(ov, subject, spec.idx)
    data["kicker"] = motion.KICKERS.get(scene, "")
    assets: dict[str, Path] = {}
    if scene == "cutaway":
        part = str(ov.get("part") or "")
        art, view = str(ov.get("art") or ""), int(ov.get("view") or 0)
        try:
            img = mechanism_art.cutaway(part, art, view=view)
        except mechanism_art.MechArtError:
            if not view:
                raise
            img = mechanism_art.cutaway(part, art)  # 换视角的图没出来:用整体剖视那张
        assets["part.png"] = img
        data["image"] = motion.asset_data("part.png", img)
    return motion.render(scene, data, seconds, size, work / "mech.mp4",
                         style=motion.DEFAULT_STYLE, assets=assets)


def concept_style_of(project_id: str) -> str:
    """项目选定的概念动画风格(`concept_style.json`);没单独选过 = 视觉包配的那套模版。"""
    got = db.read_json(project_id, "concept_style.json", {}) or {}
    return motion.get_style(got.get("style") or looks.of_project(project_id).concept).id


def _lint_own_scene(scene: Path, data: dict, project_id: str | None, seconds: float,
                    size: tuple[int, int]) -> None:
    """现写片段渲染前过一遍硬校验(`motion_lint`):有 error 就按渲染失败处理(退回数据卡),
    尤其是画面上的数字对不上事实 —— 那种片子不能出。"""
    from ...models import Fact
    from .. import motion_lint

    facts = [Fact(**d) for d in db.read_json(project_id, "facts.json", []) or []] if project_id else []
    errs = motion_lint.errors(motion_lint.lint(scene.read_text(encoding="utf-8"), data, facts,
                                               seconds, size))
    if errs:
        raise motion.MotionError("现写片段没过 lint:" + ";".join(e.message for e in errs[:3]))


def render_beat(spec: ShotSpec, *, subject: str, size: tuple[int, int], out_dir: Path,
                chapters: Sequence[tuple[str, float]], chapter_idx: int,
                used: dict[str, int], card_bg: Path | None,
                recent: list[str] | None = None,
                sting: tuple[int, str] | None = None,
                style: str | None = None, headline: str = "",
                notes: list[str] | None = None,
                ranges: dict[str, str] | None = None,
                role: str = "body",
                ledger: Sequence[tuple[str, str]] | None = None,
                project_id: str | None = None) -> tuple[Path, list[dict]]:
    """渲染一拍,返回 (成品路径, 用到的背景片段)。

    `recent` 是全片共享的最近用过的片段(相邻多样性,见 `library.fill`);
    `sting` = (章号, 章名) 时在开头叠章节标牌(每章第一拍)。
    `headline` 是竖屏标题区常驻的两行大标题(横屏不用)。
    `notes` 给了就往里记降级说明(竖屏模版没适配 → 换模版 / 退回数据卡),调用方写进项目事件。
    `ranges` 是品类的概念动画量程(`vocab.concept_ranges`),动画里表示数量的图形按它画。
    `role` 是这一拍在片子里的位置(`beat_role`),决定背景能不能用第三方视频(见 `library.fill`)。
    `ledger` 给了(哪怕是空的)= 这个片型开了常驻参数清单(`ledger_rows`):行画在左上角,
    参数面板不再单独画 —— 同一批行不摆两遍。
    `project_id` 给了且项目目录下有这一拍现写的片段(`motion.project_scene`),概念动画拍
    优先用它(旁边的 `<beat>.json` 合进数据),不套项目风格、不做竖屏换模版;渲染失败照样退回数据卡。

    竖屏(三段式):实拍先按画面带尺寸渲(`footage_size`),再用 `band_frame`
    放进 1080×1920;数据卡和概念动画直接按整幅渲。"""
    seconds = max(0.5, spec.duration)
    recent = recent if recent is not None else []
    work = out_dir / "_work" / f"{spec.idx:03d}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pieces: list[library.Piece] = []
    fsize = footage_size(size)
    #: 竖屏标题区要不要让位:关键词 / 开场大字占它;通用模版的概念动画是整屏设计,也占它
    clear_title = spec.layout in ("highlight", "hook")
    if spec.layout == "hook" and spec.overlay:
        try:
            base, pieces = hook_background(subject, seconds, fsize, work / "hook", used, recent,
                                           spec.view, spec.idx)
            base = _framed(base, size, work)
        except Exception:  # noqa: BLE001 - 冷开场做不出来:同一批关键词退回 highlight 版式
            lines = spec.overlay.get("lines") or []
            fallback = spec.model_copy(update={
                "layout": "highlight" if lines else SAFE_LAYOUT,
                "overlay": {"kind": "highlight", "lines": lines} if lines else None})
            return render_beat(fallback, subject=subject, size=size, out_dir=out_dir,
                               chapters=chapters, chapter_idx=chapter_idx, used=used,
                               card_bg=card_bg, recent=recent, sting=sting, style=style,
                               headline=headline, ranges=ranges, role=role, ledger=ledger)
    elif spec.layout == "card" and spec.card:
        base = cards.render(spec.card, subject, work / "card.mp4", seconds=seconds,
                            background=card_bg, size=size)
    elif spec.layout == "concept" and spec.overlay:
        scene = spec.overlay.get("scene", "")
        own = motion.project_scene(project_id, spec.idx) if project_id else None
        ref: motion.SceneRef = own if own is not None else scene
        if own is not None:
            # 现写的片段是按这个项目的画幅和这一拍写的:不套风格,也不做竖屏换模版
            use_style: str | None = motion.DEFAULT_STYLE
            why = ""
            scene = f"{scene or 'concept'}({own.name})"
        else:
            use_style, why = portrait_concept_style(scene, style, size)
        if why and notes is not None:
            notes.append(f"第 {spec.idx} 拍概念动画:{why}")
        try:
            if use_style is None:
                raise motion.MotionError(why)
            bg_clip, photo, pieces = concept_inputs(spec, subject, seconds, size, work / "concept_bg",
                                                    used, recent, use_style, role=role, scene=ref)
            data = motion.scene_data(spec.overlay, subject, ranges)
            if own is not None:
                data.update(motion.project_scene_data(own))
                _lint_own_scene(own, data, project_id, seconds, size)
            base = motion.render(ref, data, seconds, size,
                                 work / "concept.mp4", background=bg_clip, photo=photo, style=use_style)
            # 通用模版和现写片段都是整屏设计,竖屏标题区让位
            clear_title = own is not None or motion.get_style(use_style).path is not None
        except motion.MotionError as exc:
            # 动画渲染不了:同一批核对过的行画成数据卡,不让整拍失败 —— 但要留痕,
            # 否则"概念动画全变成数据卡"看起来像正常产出
            _log.warning("第 %s 拍概念动画 %s 退回数据卡:%s", spec.idx, scene, exc)
            degrade.note("concept.to_card", f"第 {spec.idx} 拍 {scene}:{exc}")
            base = cards.render(concept_card(spec.overlay), subject, work / "card.mp4",
                                seconds=seconds, background=card_bg, size=size)
    elif spec.layout == "mech" and spec.overlay:
        try:
            base = render_mech(spec, subject, seconds, size, work, ranges)
        except (motion.MotionError, mechanism_art.MechArtError) as exc:
            # 机构画面做不出来(生图失败 / 模版没有竖屏版 / 渲染失败):退回实拍底,
            # 有核对过的行就照面板画。要留痕 —— 这个片型的看点就是机构画面
            _log.warning("第 %s 拍机构画面 %s 退回实拍:%s", spec.idx, spec.overlay.get("part"), exc)
            degrade.note("mech.to_footage", f"第 {spec.idx} 拍 {spec.overlay.get('part')}:{exc}")
            if notes is not None:
                notes.append(f"第 {spec.idx} 拍机构画面({spec.overlay.get('part')})没做出来,退回实拍:{exc}")
            rows = spec.overlay.get("rows") or []
            fallback = spec.model_copy(update={
                "layout": "panel" if rows else SAFE_LAYOUT,
                "overlay": {"kind": "panel", "title": spec.overlay.get("title", ""), "rows": rows,
                            "source": spec.overlay.get("source", "")} if rows else None})
            return render_beat(fallback, subject=subject, size=size, out_dir=out_dir,
                               chapters=chapters, chapter_idx=chapter_idx, used=used,
                               card_bg=card_bg, recent=recent, sting=sting, style=style,
                               headline=headline, notes=notes, ranges=ranges, role=role,
                               ledger=ledger)
    else:
        base, pieces = _broll_base(spec, subject, seconds, fsize, work, used, recent, card_bg,
                                   role=role)
        base = _framed(base, size, work)

    # 竖屏标题区:大标题让位时只留章节进度
    bar = overlays.chapter_bar(chapters, chapter_idx, size, work / "bar.png",
                               headline="" if clear_title else headline)
    # 整屏数据卡 / 概念动画自己就是一屏参数,不再压清单
    if ledger and spec.layout not in ("card", "concept", "mech"):
        name = chapters[chapter_idx][0] if 0 <= chapter_idx < len(chapters) else ""
        overlays.ledger(ledger, name, size, bar)
    layer: Path | None = None
    layer_input: list[str] = []
    flt = spec.layout
    no_panel = spec.layout == "panel" and (not looks.current().panel or ledger is not None)
    if spec.layout == "panel" and spec.overlay and not no_panel:
        layer = overlays.panel(spec.overlay, subject, size, work / "layer.png")
    elif spec.layout == "highlight" and spec.overlay:
        layer = overlays.highlight(spec.overlay.get("lines") or [], size, work / "layer.png")
    elif spec.layout == "hook" and spec.overlay:
        frames = overlays.hook_frames(spec.overlay, subject, size, work / "hook_txt",
                                      HOOK_SECONDS)
        layer = _frames_listing(frames, work / "hook_txt.txt")
        layer_input = ["-f", "concat", "-safe", "0", "-i", str(layer)]
    elif spec.layout == "stat" and spec.overlay:
        layer = _stat_listing(spec.overlay, subject, size, work, seconds)
        layer_input = ["-f", "concat", "-safe", "0", "-i", str(layer)]
    elif broll_style(spec) == "lower_third" and not sting and not no_panel:
        name = chapters[chapter_idx][0] if 0 <= chapter_idx < len(chapters) else ""
        if name == subject:  # 横评:章名就是车名,角标改写「车名 · 段落」
            name = spec.section
        layer = overlays.lower_third(subject, name, size, work / "layer.png")
        flt = "lower_third"
    if layer and not layer_input:
        layer_input = ["-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(layer)]

    dest = out_dir / f"{spec.idx:03d}.mp4"
    inputs = ["-i", str(base), "-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(bar)]
    inputs += layer_input
    graph = layout_filter(flt, size, bool(layer), pip=looks.current().panel)
    if sting:
        frames = overlays.chapter_sting_frames(sting[0], sting[1], size, work / "sting")
        inputs += ["-f", "concat", "-safe", "0", "-i",
                   str(_frames_listing(frames, work / "sting.txt"))]
        graph = with_sting(graph, 3 if layer else 2)
    proc = _ff([*inputs, "-filter_complex", graph,
                "-map", "[out]", "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"第 {spec.idx} 拍合成失败:{(proc.stderr or '')[-400:]}")
    shutil.rmtree(work, ignore_errors=True)
    return dest, [p.__dict__ for p in pieces]


def sting_for(specs: Sequence[ShotSpec], where: Sequence[int],
              chapters: Sequence[tuple[str, float]], idx: int) -> tuple[int, str] | None:
    """这一拍是不是某章(开头章除外)的第一拍:是就返回 (章号, 章名)。"""
    pos = next((n for n, s in enumerate(specs) if s.idx == idx), None)
    if pos is None or pos == 0:
        return None
    ch = where[pos]
    if ch == 0 or where[pos - 1] == ch:
        return None
    return ch + 1, chapters[ch][0]


def chapter_starts(specs: Sequence[ShotSpec]) -> list[int]:
    """各章第一拍的位置(开头章除外):成片在这些交界短叠化。"""
    _, where = chapters_of(specs)
    return [n for n in range(1, len(where)) if where[n] != where[n - 1]]


def beat_role(where: Sequence[int], pos: int, n_chapters: int) -> str:
    """第 `pos` 拍在片子里的位置(`library.ROLES`):开头章 = hook,最后一章 = ending(结论段),
    其余 = body。只有一章的片子全按 hook 算 —— 宁严勿松,不让第三方画面进开场。"""
    ch = where[pos]
    if ch == 0:
        return "hook"
    return "ending" if ch == n_chapters - 1 else "body"
