"""横屏解说轨(profile `track: explainer`):对标「三驱兄弟」式的信息型长视频。

核心是**画面的角色**:实拍只是"这台车的背景画面",不要求对上旁白 ——
随便哪段干净的都行,短镜头连拼(`library.fill`),信息全部靠叠加层传达。

一拍 = 一段旁白 = 一个版式:

    broll      全屏背景画面
    panel      背景 + 左侧参数面板(核对过事实的 3–5 行)
    highlight  背景缩成小窗 + 两行大字关键词
    card       整屏数据卡(spec / compare / bars / timeline,沿用 cards.py)
    stat       背景压暗 + 中央一个大数字从 0 滚到目标值(核对过事实的一个参数)

时长不再是 6 秒栅格:门禁 A 确认后**先跑 TTS**,每拍的时长 = 它那几句旁白的实际
时长(含句间停顿),画面按这个时长去铺。所以字幕、旁白、画面天然对齐。

底部章节条常驻(当前章高亮),直接烧进每一拍。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .. import config, db
from ..categories import CategoryProfile, Vocab
from ..models import Fact, ScriptSegment, ShotSpec
from . import cards, library, narration, overlays
from . import render as render_mod
from .grounding import numbers_in

LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat")
#: 不需要任何数据就能成立的版式 —— 其它版式核对失败都退回它
SAFE_LAYOUT = "broll"
#: 连续几拍整屏卡片就太像 PPT 了
MAX_CARD_RUN = 2
#: 关键词每行最多几个字
KEYWORD_CHARS = 12
#: 横屏字幕单行字数(竖屏是 16)
SUBTITLE_LINE_CHARS = 22
#: 图片背景的推拉方式,按拍号轮换
STILL_MOTIONS = render_mod.STILL_MOTIONS
#: 中间产物的编码参数:拼接用 -c copy,所以所有片段必须完全一致
_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(render_mod.FPS)]

SYSTEM = """你是信息型{domain}解说视频(横屏 16:9,对标「三驱兄弟」)的剪辑导演。
画面是{subject_noun}的官方宣传片做背景,信息靠叠加层传达。你的任务:给每一拍选**版式**并填叠加数据。

版式:
- broll:全屏宣传片,无叠加。旁白在讲感受、观点、过渡时用。
- panel:宣传片 + 左侧参数面板。旁白提到 2 个以上参数时用。给 `panel`:
  {{"title": "面板标题(10 字内)", "rows": [{{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}}]}}
  rows 2–5 行。
- highlight:宣传片缩成小窗 + 两行大字。旁白在强调一个卖点 / 关键配置 / 结论时用。给 `keywords`:
  1–2 行,每行 {kw} 字以内,是这一拍最想让观众记住的词(如 {keyword_examples})。
- card:整屏数据卡。段落标了「卡片」时优先用。给 `card`:
  {{"title": "卡片标题(12 字内)", "columns": [两个对比对象名,仅对比卡需要],
    "rows": [{{"label": "...", "values": ["..."], "fact_id": "f_xxx"}}]}}
- stat:宣传片压暗 + 中央一个大数字滚动。旁白只围绕**一个**核心数字时用({stat_examples})。给 `stat`:{{"label": "参数名(8 字内)", "value": "取值含单位", "fact_id": "f_xxx"}}

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或解释文字。
2. `beats` 数组,每项 {{"idx": 序号, "layout": 版式, "view": "{view_enum}",
   "visual": "背景画面一句话(15 字内)", "panel": ..., "keywords": [...], "card": ...,
   "stat": ...}};
   用不到的键可以省略。数量与给定拍数一致,idx 从 1 开始连续。
3. layout 只能从该拍「可选版式」里选。
4. panel / card 的每一行和 stat 的数字**只能**取自「可用事实」,fact_id 填那条事实的 id;values 里的数字
   必须与事实原文一字不差(含单位),不得换算、取整或估算。找不到就换版式,不要编。
5. keywords 里的型号、芯片名、英文和数字必须在这一拍的旁白或可用事实里出现过,不得自创。
6. 节奏:不要连续 3 拍以上用 card;panel、highlight、stat 穿插着用,不要连续两拍 stat,
   全片大约四成是 broll。
7. view:{view_guide}

必需键: beats"""

USER = """{subject_label}:{subject}
总拍数:{count}

逐拍清单(版式要服务于这一拍的旁白):
{beats}

可用事实:
{facts}

输出 JSON。"""


# --------------------------------------------------------------------------
# 分镜:逐拍版式 + 叠加数据
# --------------------------------------------------------------------------
def _hint(sec_angle: str, view: str, vocab: Vocab) -> str:
    """背景偏好:分镜给的 view 合法就用,否则按段落 angle 推。"""
    view = (view or "").strip().lower()
    if view in vocab.hint_groups:
        return view
    return vocab.hint_of_angle.get(sec_angle, vocab.default_hint)


def ground_keywords(lines: Any, narration_text: str, facts: Sequence[Fact]) -> list[str]:
    """核对两行大字。中文描述放行;数字、英文、型号这类**最容易编**的记号,
    必须在这一拍的旁白或启用的事实里出现过。核不过的行直接丢。"""
    if isinstance(lines, str):
        lines = [lines]
    pool = narration_text + " " + " ".join(f.claim for f in facts if f.enabled)
    pool_nums = numbers_in(pool)
    pool_up = pool.upper().replace(" ", "")
    out: list[str] = []
    for raw in lines or []:
        text = str(raw).strip()
        if not text or len(text) > KEYWORD_CHARS + 4:
            continue
        if numbers_in(text) - pool_nums:
            continue
        tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-+]*", text)
        if any(t.upper() not in pool_up for t in tokens):
            continue
        out.append(text[: KEYWORD_CHARS + 4])
    return out[:2]


def ground_stat(raw: Any, facts: Sequence[Fact]) -> tuple[dict | None, list[str]]:
    """核对大数字:当成一行参数卡交给 `cards.ground`(数字必须见于引用的事实),
    再要求取值里确实有个能滚动的数字。核不过返回 (None, 原因)。"""
    if not isinstance(raw, dict):
        return None, ["stat 缺数据"]
    row = {"label": raw.get("label"), "values": [raw.get("value")], "fact_id": raw.get("fact_id")}
    grounded, dropped = cards.ground(cards.normalise({"rows": [row]}, "spec"), facts)
    if not grounded:
        return None, dropped
    r = grounded["rows"][0]
    value = r["values"][0]
    if overlays.split_number(value) is None:
        return None, dropped + [f"「{value}」里没有可滚动的数字"]
    return {"kind": "stat", "label": r["label"][:KEYWORD_CHARS], "value": value,
            "fact_id": r["fact_id"], "source": grounded.get("source", "")}, dropped


def _beat_line(i: int, seg: ScriptSegment | None, profile: CategoryProfile) -> str:
    sec = profile.section(seg.section if seg else "")
    options = list(sec.layouts) or list(LAYOUTS)
    card = f" | 卡片:{cards.KIND_GUIDE[sec.card]}" if sec.card in cards.KIND_GUIDE else ""
    return (f"[第 {i + 1} 拍] 段落:{sec.name or '未分类'}({sec.beat or '无'}) | "
            f"可选版式:{'/'.join(options)}{card} | 旁白:{seg.text if seg else ''}")


def build_beats(*, subject: str, profile: CategoryProfile, segments: Sequence[ScriptSegment],
                facts: Sequence[Fact], llm, card_log: list[dict] | None = None) -> list[ShotSpec]:
    """一段脚本 = 一拍。LLM 挑版式、填叠加数据,这里逐项核对,核不过就降级成 broll。"""
    usable = [f for f in facts if f.enabled and f.confidence in ("high", "medium")]
    facts_payload = "\n".join(f"- {f.id}: {f.claim}" for f in usable)[:5000] or "(无)"
    vocab = profile.vocab
    user = USER.format(
        subject_label=vocab.subject_label, subject=subject, count=len(segments),
        beats="\n".join(_beat_line(i, s, profile) for i, s in enumerate(segments)),
        facts=facts_payload,
    )
    try:
        system = SYSTEM.format(
            kw=KEYWORD_CHARS, domain=vocab.domain, subject_noun=vocab.subject_noun,
            keyword_examples=vocab.keyword_examples, stat_examples=vocab.stat_examples,
            view_enum=vocab.view_enum, view_guide=vocab.view_guide)
        data = llm.structured(system, user,
                              required_keys=["beats"], temperature=0.5)
        raw = data.get("beats") or []
    except Exception:  # noqa: BLE001 - 版式选不出来也能出片:全部 broll
        raw = []
    by_idx: dict[int, dict] = {}
    for n, item in enumerate(raw):
        if isinstance(item, dict):
            try:
                by_idx[int(item.get("idx", n + 1)) - 1] = item
            except (TypeError, ValueError):
                by_idx[n] = item

    specs = [_ground_beat(i, seg, by_idx.get(i, {}), subject, profile, facts, card_log)
             for i, seg in enumerate(segments)]
    return enforce_rhythm(specs)


def _ground_beat(i: int, seg: ScriptSegment, item: dict, subject: str, profile: CategoryProfile,
                 facts: Sequence[Fact], card_log: list[dict] | None) -> ShotSpec:
    sec = profile.section(seg.section)
    options = list(sec.layouts) or list(LAYOUTS)
    layout = str(item.get("layout") or options[0]).strip()
    if layout not in options:
        layout = options[0]

    overlay: dict | None = None
    card: dict | None = None
    dropped: list[str] = []
    if layout == "panel":
        grounded, dropped = cards.ground(cards.normalise(item.get("panel"), "spec"), facts)
        if grounded and len(grounded["rows"]) >= 2:
            overlay = {**grounded, "kind": "panel"}
        else:
            layout = SAFE_LAYOUT
    elif layout == "highlight":
        lines = ground_keywords(item.get("keywords"), seg.text, facts)
        if lines:
            overlay = {"kind": "highlight", "lines": lines}
        else:
            layout = SAFE_LAYOUT
    elif layout == "stat":
        overlay, dropped = ground_stat(item.get("stat"), facts)
        if overlay is None:
            layout = SAFE_LAYOUT
    elif layout == "card":
        raw_card = item.get("card")
        kind = sec.card or (raw_card.get("kind") if isinstance(raw_card, dict) else "") or "spec"
        card, dropped = cards.ground(cards.normalise(raw_card, kind), facts)
        if card is None:
            layout = SAFE_LAYOUT
    if card_log is not None and (dropped or layout != str(item.get("layout") or layout)):
        card_log.append({"idx": i, "section": sec.name, "asked": item.get("layout"),
                         "layout": layout, "dropped": dropped})

    view = _hint(sec.angle, str(item.get("view") or ""), profile.vocab)
    visual = str(item.get("visual") or "").strip()[:30] or (sec.beat or f"{subject} 宣传画面")
    return ShotSpec(
        idx=i, duration=0.0, section=sec.name,
        visual_zh=f"[{layout}] {visual}",
        camera_zh="", ambience_zh="", narration_zh=seg.text,
        angle=sec.angle, on_screen_text=[],
        card=card, layout=layout, overlay=overlay,
        chapter=sec.chapter or sec.name, view=view,
    )


def enforce_rhythm(specs: list[ShotSpec]) -> list[ShotSpec]:
    """连续超过 MAX_CARD_RUN 拍整屏卡片时,把多出来的那拍退回 broll;
    连着两拍大数字也把后一拍退回 broll(一个接一个滚数字像在念参数表)。"""
    run = 0
    prev = ""
    for s in specs:
        if s.layout == "stat" and prev == "stat":
            s.layout, s.overlay = SAFE_LAYOUT, None
            s.visual_zh = re.sub(r"^\[stat\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
        prev = s.layout
        if s.layout == "card":
            run += 1
            if run > MAX_CARD_RUN:
                s.layout, s.card = SAFE_LAYOUT, None
                s.visual_zh = re.sub(r"^\[card\]", f"[{SAFE_LAYOUT}]", s.visual_zh)
                run = 0
        else:
            run = 0
    return specs


def fallback_beats(*, subject: str, profile: CategoryProfile,
                   segments: Sequence[ScriptSegment]) -> list[ShotSpec]:
    """没有 LLM 时:全部 broll。结构、时间轴、合成链路照样能验。"""
    out = []
    for i, seg in enumerate(segments):
        sec = profile.section(seg.section)
        out.append(ShotSpec(idx=i, duration=0.0, section=sec.name,
                            visual_zh=f"[{SAFE_LAYOUT}] {sec.beat or subject}",
                            narration_zh=seg.text, angle=sec.angle, layout=SAFE_LAYOUT,
                            chapter=sec.chapter or sec.name, view=_hint(sec.angle, "", profile.vocab)))
    return out


def validate_beats(specs: Sequence[ShotSpec]) -> list[str]:
    errors: list[str] = []
    if not specs:
        errors.append("没有任何节拍")
    for s in specs:
        if s.layout not in LAYOUTS:
            errors.append(f"第 {s.idx} 拍版式非法:{s.layout!r}")
        if not s.narration_zh.strip():
            errors.append(f"第 {s.idx} 拍没有旁白(时长由旁白决定,空拍无法定长)")
        if s.layout == "card" and not s.card:
            errors.append(f"第 {s.idx} 拍是 card 但没有卡片数据")
        if s.layout in ("panel", "highlight", "stat") and not s.overlay:
            errors.append(f"第 {s.idx} 拍是 {s.layout} 但没有叠加数据")
    return errors


# --------------------------------------------------------------------------
# 定时长:先 TTS,再按旁白回填每拍时长
# --------------------------------------------------------------------------
def beat_durations(cues: Sequence[Any], count: int, gap: float | None = None) -> list[float]:
    """按字幕 cue 把句子时长(含句间停顿)归到各拍。

    与 `narration._concat_audio` 同口径:旁白轨 = Σ(句长 + 停顿),所以
    Σ 各拍时长 == 旁白轨总长,画面和声音不会越走越偏。
    """
    gap_s = config.SENTENCE_GAP_MS / 1000.0 if gap is None else gap
    out = [0.0] * count
    for c in cues:
        if 0 <= c.shot_idx < count:
            out[c.shot_idx] += (c.end - c.start) + gap_s
    return [round(x, 3) for x in out]


def time_beats(project_id: str, specs: list[ShotSpec], profile: CategoryProfile
               ) -> tuple[list[ShotSpec], narration.NarrationResult]:
    """跑 TTS(含字幕与 BGM),把每拍时长写回分镜。"""
    nar = narration.build(project_id, specs, profile=profile,
                          size=profile.frame_size, line_chars=SUBTITLE_LINE_CHARS)
    if not nar.ok or not nar.cues:
        raise RuntimeError("旁白生成失败:" + "; ".join(nar.warnings or ["未知原因"]))
    if nar.missing_sentences:
        # 解说轨的时长由旁白决定:缺句子就是缺画面(实测丢的正好是结尾的结论段)。
        # 宁可停在这里让人再点一次确认,也不出一部没有结论的片子
        raise RuntimeError("旁白有句子没合成出来,请重新确认门禁 A 重试:"
                           + "; ".join(nar.warnings))
    durations = beat_durations(nar.cues, len(specs))
    for s, d in zip(specs, durations):
        s.duration = d
    return specs, nar


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _ff(args: Sequence[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return render_mod._run(["-y", "-v", "error", *args], timeout=timeout)


def _fit_filter(w: int, h: int, crop_bottom: float = 0.0) -> str:
    """任意比例的视频放进 w×h:前景完整缩进画幅,空出的地方用同一画面放大模糊铺满。
    横屏源基本铺满,竖屏抖音片也不会被裁掉车头车尾。

    `crop_bottom` > 0(烧录字幕段):裁掉底部这一截后放大**铺满**画幅,不留模糊边 ——
    这类段基本是横屏评测,裁完再缩进画幅会上下各空出一条模糊带,一眼就是裁过的。"""
    if crop_bottom > 0:
        return (f"[0:v]crop=iw:trunc(ih*{1 - crop_bottom:.3f}/2)*2:0:0,"
                f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"setsar=1,fps={render_mod.FPS},format=yuv420p")
    return (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur=20:2[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={render_mod.FPS},format=yuv420p")


def _piece_clip(piece: library.Piece, dest: Path, size: tuple[int, int], motion: str) -> Path:
    w, h = size
    if piece.kind == "still":
        return render_mod.still_to_clip(piece.path, dest, seconds=piece.length,
                                        motion=motion, size=size)
    # 视频不够长(原片在这一段后面就结束了)时,用最后一帧补齐
    vf = _fit_filter(w, h, piece.crop_bottom) + f",tpad=stop_mode=clone:stop_duration={piece.length:.3f}[v]"
    proc = _ff(["-ss", f"{piece.start:.3f}", "-i", piece.path, "-filter_complex", vf,
                "-map", "[v]", "-t", f"{piece.length:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"背景片段截取失败:{(proc.stderr or '')[-300:]}")
    return dest


def _solid(dest: Path, seconds: float, size: tuple[int, int]) -> Path:
    w, h = size
    proc = _ff(["-f", "lavfi", "-i", f"color=c=0x101826:s={w}x{h}:r={render_mod.FPS}",
                "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0:
        raise render_mod.RenderError(f"纯色底生成失败:{(proc.stderr or '')[-300:]}")
    return dest


def background_clip(pieces: Sequence[library.Piece], seconds: float, size: tuple[int, int],
                    work: Path, motion_seed: int = 0) -> Path:
    """把几段背景拼成一条定长背景。没有片段时给纯色底(不应发生,但不能卡死流水线)。"""
    work.mkdir(parents=True, exist_ok=True)
    dest = work / "bg.mp4"
    if not pieces:
        return _solid(dest, seconds, size)
    parts = []
    for n, p in enumerate(pieces):
        motion = STILL_MOTIONS[(motion_seed + n) % len(STILL_MOTIONS)]
        parts.append(_piece_clip(p, work / f"piece_{n:02d}.mp4", size, motion))
    if len(parts) == 1:
        shutil.copyfile(parts[0], dest)
        return dest
    listing = render_mod.timeline_mod.concat_list_file(parts, work / "pieces.txt")
    proc = _ff(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                "-t", f"{seconds:.3f}", str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"背景拼接失败:{(proc.stderr or '')[-300:]}")
    return dest


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


def layout_filter(layout: str, size: tuple[int, int], has_layer: bool) -> str:
    """把背景 [0]、章节条 [1]、叠加层 [2] 合成 [out] 的滤镜图。"""
    w, h = size
    if layout == "highlight" and has_layer:
        pip_w = int(w * 0.62) // 2 * 2
        pip_h = int(h * 0.62) // 2 * 2
        return (
            f"[0:v]split[a][b];[a]boxblur=22:2,eq=brightness=-0.18[bgb];"
            f"[b]scale={pip_w}:{pip_h},pad={pip_w + 12}:{pip_h + 12}:6:6:color=white[pip];"
            f"[bgb][pip]overlay=(W-w)/2:{int(h * 0.05)}[v1];"
            f"[2:v]format=rgba,fade=in:st=0.15:d=0.35:alpha=1[txt];"
            f"[v1][txt]overlay=0:0[v2];[v2][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "stat" and has_layer:
        # [2] 是逐帧滚动的数字(concat 关键帧),播完停在最后一帧
        return (
            f"[0:v]eq=brightness=-0.16:saturation=0.85[bgd];[2:v]format=rgba[num];"
            f"[bgd][num]overlay=0:0:eof_action=repeat[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    if layout == "panel" and has_layer:
        slide = int(w * 0.36)
        return (
            f"[0:v][2:v]overlay=x='-{slide}*(1-min(1,t/0.35))':y=0[v1];"
            f"[v1][1:v]overlay=0:0,format=yuv420p[out]"
        )
    return "[0:v][1:v]overlay=0:0,format=yuv420p[out]"


def _stat_listing(overlay: dict, subject: str, size: tuple[int, int], work: Path,
                  seconds: float) -> Path:
    """大数字滚动的关键帧写成 concat 清单:滚完后最后一帧定格到拍尾。"""
    frames = overlays.stat_frames(overlay, subject, size, work / "stat")
    lines: list[str] = []
    rolled = 0.0
    for n, (path, dur) in enumerate(frames):
        if n == len(frames) - 1:
            dur = max(0.5, seconds - rolled)
        rolled += dur
        lines += [f"file '{path.as_posix()}'", f"duration {dur:.3f}"]
    lines.append(f"file '{frames[-1][0].as_posix()}'")  # concat 要求末帧重复一次
    listing = work / "stat.txt"
    listing.write_text("\n".join(lines), encoding="utf-8")
    return listing


def render_beat(spec: ShotSpec, *, subject: str, size: tuple[int, int], out_dir: Path,
                chapters: Sequence[tuple[str, float]], chapter_idx: int,
                used: dict[str, int], card_bg: Path | None) -> tuple[Path, list[dict]]:
    """渲染一拍,返回 (成品路径, 用到的背景片段)。"""
    seconds = max(0.5, spec.duration)
    work = out_dir / "_work" / f"{spec.idx:03d}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    pieces: list[library.Piece] = []
    if spec.layout == "card" and spec.card:
        base = cards.render(spec.card, subject, work / "card.mp4", seconds=seconds,
                            background=card_bg, size=size)
    else:
        pieces = library.fill(subject, seconds, hint=spec.view, used=used)
        if not pieces and card_bg:
            pieces = [library.Piece("bg", "still", str(card_bg), 0.0, seconds)]
        base = background_clip(pieces, seconds, size, work, motion_seed=spec.idx)

    bar = overlays.chapter_bar(chapters, chapter_idx, size, work / "bar.png")
    layer: Path | None = None
    layer_input: list[str] = []
    if spec.layout == "panel" and spec.overlay:
        layer = overlays.panel(spec.overlay, subject, size, work / "layer.png")
    elif spec.layout == "highlight" and spec.overlay:
        layer = overlays.highlight(spec.overlay.get("lines") or [], size, work / "layer.png")
    elif spec.layout == "stat" and spec.overlay:
        layer = _stat_listing(spec.overlay, subject, size, work, seconds)
        layer_input = ["-f", "concat", "-safe", "0", "-i", str(layer)]
    if layer and not layer_input:
        layer_input = ["-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(layer)]

    dest = out_dir / f"{spec.idx:03d}.mp4"
    inputs = ["-i", str(base), "-loop", "1", "-framerate", str(render_mod.FPS), "-i", str(bar)]
    inputs += layer_input
    proc = _ff([*inputs, "-filter_complex", layout_filter(spec.layout, size, bool(layer)),
                "-map", "[out]", "-t", f"{seconds:.3f}", "-an", *_ENC, str(dest)])
    if proc.returncode != 0 or not dest.is_file():
        raise render_mod.RenderError(f"第 {spec.idx} 拍合成失败:{(proc.stderr or '')[-400:]}")
    shutil.rmtree(work, ignore_errors=True)
    return dest, [p.__dict__ for p in pieces]


# --------------------------------------------------------------------------
# 接入流水线
# --------------------------------------------------------------------------
def beats_dir(project_id: str) -> Path:
    d = config.project_dir(project_id) / "beats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_used(project_id: str) -> dict[str, int]:
    return db.read_json(project_id, "beats_used.json", {}) or {}


def card_background(subject: str) -> Path | None:
    """数据卡的底图:池子里一张干净的主视角官图(汽车是外观;虚化压暗后做底)。"""
    lib = library.Library.load(subject)
    stills = [i for i in lib.items if i.kind == "still" and i.usable and Path(i.path).is_file()]
    primary = library.vocab_of(lib).primary_views
    stills.sort(key=lambda i: (i.view not in primary, library._dirt(i)))
    return Path(stills[0].path) if stills else None


def init_beats(project_id: str, specs: Sequence[ShotSpec]) -> None:
    """门禁 A 之后:清掉两档旧记录,按当前分镜重建 shots 行(全部 pending)。"""
    for stage in ("draft", "final"):
        db.delete_shots(project_id, stage)
        db.upsert_shots(project_id, [
            {"idx": s.idx, "stage": stage, "model": "explainer", "provider_task_id": None,
             "submitted_at": None, "last_polled_at": None, "draft_path": None,
             "final_path": None, "error": None}
            for s in specs
        ])


def render_pending(project_id: str, specs: Sequence[ShotSpec], *, subject: str,
                   profile: CategoryProfile, limit: int = 3) -> dict[str, Any]:
    """渲染还没出片的拍(每次最多 `limit` 拍,后台循环反复调用)。

    产物写成 draft 档 succeeded —— 门禁 B 的审片页按草稿档展示。
    """
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    todo = [s for s in specs if s.duration > 0
            and rows.get(s.idx, {}).get("status") != "succeeded"][:limit]
    chapters, where = chapters_of(specs)
    used = _load_used(project_id)
    beats_dir(project_id)  # 每拍的背景片段记录放这里
    # 成品放 drafts/:审片页按 /media/{项目}/drafts/{文件} 取草稿预览
    out_dir = config.project_dir(project_id) / "drafts"
    bg = card_background(subject)
    done, errors = [], []
    for s in todo:
        try:
            path, pieces = render_beat(s, subject=subject, size=profile.frame_size,
                                       out_dir=out_dir, chapters=chapters,
                                       chapter_idx=where[s.idx], used=used, card_bg=bg)
        except (render_mod.RenderError, RuntimeError, OSError) as exc:
            errors.append(f"第 {s.idx} 拍:{exc}")
            db.update_shot(project_id, s.idx, "draft", status="failed", error=str(exc)[:500])
            continue
        db.write_json(project_id, f"beats/{s.idx:03d}.json", pieces)
        db.update_shot(project_id, s.idx, "draft", status="succeeded", draft_path=str(path),
                       provider_task_id=None, error=None, est_cost=0.0,
                       consistency="card" if s.layout == "card" else "real")
        done.append(s.idx)
    db.write_json(project_id, "beats_used.json", used)
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    remaining = [s.idx for s in specs if s.duration > 0
                 and rows.get(s.idx, {}).get("status") != "succeeded"]
    return {"rendered": done, "errors": errors, "remaining": remaining,
            "stage_complete": not remaining}


def rerender(project_id: str, idx: int) -> None:
    """门禁 B「重拍」:换一组背景重渲这一拍。刚用过的片段记成多用几次,排到后面去。"""
    pieces = db.read_json(project_id, f"beats/{idx:03d}.json", []) or []
    used = _load_used(project_id)
    for p in pieces:
        used[p["item_id"]] = used.get(p["item_id"], 0) + 3
    db.write_json(project_id, "beats_used.json", used)
    db.update_shot(project_id, idx, "draft", status="pending", draft_path=None, error=None)
    db.update_shot(project_id, idx, "final", status="pending", final_path=None, error=None)


def promote_all(project_id: str, specs: Sequence[ShotSpec]) -> int:
    """门禁 B 通过:草稿就是成片(画面不再重新生成),写进成片档。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    n = 0
    for s in specs:
        r = rows.get(s.idx)
        if r and r.get("status") == "succeeded" and r.get("draft_path"):
            db.update_shot(project_id, s.idx, "final", status="succeeded",
                           final_path=r["draft_path"], consistency=r.get("consistency") or "real",
                           est_cost=0.0, error=None)
            n += 1
    return n


def compose(project_id: str, specs: Sequence[ShotSpec], profile: CategoryProfile,
            title: str) -> dict[str, Any]:
    """成片:拼各拍 + 旁白/BGM 混音 + 字幕。旁白在门禁 A 之后已经生成好了。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "final")}
    beats = [s for s in specs if s.duration > 0]
    missing = [s.idx for s in beats if not (rows.get(s.idx) or {}).get("final_path")]
    if missing:
        raise RuntimeError(f"还有 {len(missing)} 拍没有成片:{missing[:12]}")
    nar = db.read_json(project_id, "narration.json", {}) or {}
    project_dir = config.project_dir(project_id)
    # 拼接缓存按序号命名,重拍过的拍必须重新归一化
    shutil.rmtree(project_dir / "_render", ignore_errors=True)
    result = render_mod.compose(
        project_id, [Path(rows[s.idx]["final_path"]) for s in beats],
        ass_path=Path(nar["ass_path"]) if nar.get("ass_path") else None,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        size=profile.frame_size, durations=[s.duration for s in beats],
    )
    if not result.ok or not result.output_path:
        raise RuntimeError("合成失败:" + "; ".join(result.warnings or []))
    total = sum(s.duration for s in beats)
    cover = render_mod.build_cover(result.output_path, title, project_dir / "cover.jpg",
                                   subtitle=f"{int(total // 60)}分{int(total % 60)}秒 · {profile.label}",
                                   size=profile.frame_size)
    payload = {**result.to_dict(), "cover": str(cover) if cover else None, "narration": nar,
               "source_note": "画面素材:品牌官方宣传片与官方图片,仅作"
               f"{profile.vocab.subject_label}展示"}
    db.write_json(project_id, "render.json", payload)
    return payload

