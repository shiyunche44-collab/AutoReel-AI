"""成片精修:把成片导出成 OpenFilm 项目,在 Studio 里改字幕,再把改动读回来。

做「导出 / 起 Studio / 读回改动 / 检查并应用」。应用 = 改 narration.json 和 ASS 里的字,
再用我们自己的合成重出成片(`explainer.recompose`),OpenFilm 不参与渲染。
OpenFilm 的细节(film.html 写法、vtt 约定、CLI 怎么起)都收在这个模块里,别处不知道它。

 - 导出目录 `<project>/film/`:`film.html`、`assets/beat_NNN.mp4`(各拍成片,叠加层已经烧进去)、
   `assets/mix.wav` + `assets/mix.vtt`(旁白混音 + 字幕,vtt 是 Studio 里字幕的来源和落点)。
 - 原始字幕记在 `<project>/refine.json`:导出目录里以点开头的东西归 OpenFilm 所有,不往里放自己的文件。
 - 字幕按序号一一对应:Studio 里改字、不增删句,就能逐句比对出改了哪几句。
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import config, db
from . import aspect, timeline

_log = logging.getLogger(__name__)

FILM_DIR = "film"
STATE_FILE = "refine.json"
#: vtt 开头这一行告诉 Studio「每条 cue 就是一条字幕」,不要把长句再切开(OpenFilm SPEC 的 Words 一节)
VTT_NOTE = "NOTE Each cue is one subtitle, as cut by hand in OpenFilm Studio."
_URL_RE = re.compile(r"Studio\s+(http://\S+)")


class RefineError(RuntimeError):
    pass


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Edit:
    """一句字幕的改动。index 从 1 起,和 narration.cues 的 index 同号。"""
    index: int
    old: str
    new: str
    start: float
    end: float


@dataclass
class Edits:
    changed: list[Edit] = field(default_factory=list)
    #: 句数变了(Studio 里增删或合并了字幕)。v1 不支持,调用方应当拒绝应用
    structural: bool = False
    original_count: int = 0
    current_count: int = 0

    @property
    def any(self) -> bool:
        return bool(self.changed) or self.structural


@dataclass(frozen=True)
class Session:
    folder: Path
    beats: int
    cues: int
    seconds: float


# --------------------------------------------------------------------------
# vtt
# --------------------------------------------------------------------------
def _stamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def to_vtt(cues: Sequence[Cue]) -> str:
    body = "\n".join(f"{_stamp(c.start)} --> {_stamp(c.end)}\n{c.text}\n" for c in cues)
    return f"WEBVTT\n\n{VTT_NOTE}\n\n{body}"


_TIME_RE = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d{1,3})?)")
#: 行内的逐字时间戳 <00:00:01.200> 和 <c> / <v 说话人> 之类的标签
_INLINE_TAG_RE = re.compile(r"</?[^>\s]+(?:\s[^>]*)?>")


def _secs(h: str | None, m: str, s: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + float(s)


def parse_vtt(text: str) -> list[Cue]:
    """读 WebVTT。容忍 cue 标识行、NOTE / STYLE 块、行内逐字时间戳和标签。"""
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\r?\n+", text.replace("﻿", "").strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        for i, ln in enumerate(lines):
            m = _TIME_RE.match(ln.strip())
            if m:
                body = " ".join(_INLINE_TAG_RE.sub("", x).strip() for x in lines[i + 1:])
                cues.append(Cue(_secs(m[1], m[2], m[3]), _secs(m[4], m[5], m[6]), body.strip()))
                break
    return cues


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def film_dir(project_id: str) -> Path:
    return config.project_dir(project_id) / FILM_DIR


def _link(src: Path, dst: Path) -> None:
    """硬链接(同盘,不占空间);不行就复制。"""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def build_film_html(size: tuple[int, int], beats: Sequence[tuple[str, float, float, float]],
                    audio: str, total: float) -> str:
    """beats = [(相对路径, 起点秒, 分镜时长秒, 文件实际时长秒)]。各拍首尾相接放同一条轨,混音单独一条轨。

    起点按分镜时长累加(旁白就是按它对齐的);`#t` 的出点不能超过文件实际长度,
    拍成片常比分镜短一两帧,超了 Studio 会整片「无法绘制预览」。"""
    w, h = size
    rows = [
        f'<video id="beat_{i:03d}" src="{src}#t=0,{min(dur, real) if real > 0 else dur:.3f}" '
        f'at="{at:.3f}" muted style="left:0;top:0;width:{w}px;height:{h}px"></video>'
        for i, (src, at, dur, real) in enumerate(beats)
    ]
    return "\n".join([
        "<!doctype html>", "<html><head>", '<meta charset="utf-8">',
        f'<meta name="viewport" content="width={w}, height={h}">',
        "<style>video{object-fit:cover}</style>", "</head><body>",
        "<section>", *rows, "</section>",
        f'<section><audio id="mix" src="{audio}#t=0,{total:.3f}"></audio></section>',
        "</body></html>", "",
    ])


def _beat_files(project_id: str) -> list[tuple[Path, float]]:
    """各拍成片文件和时长,顺序与 explainer.compose 一致(时长为 0 的拍不进成片)。"""
    storyboard = db.read_json(project_id, "storyboard.json", []) or []
    rows = {int(r["idx"]): r for r in db.get_shots(project_id, "final")}
    out: list[tuple[Path, float]] = []
    for spec in storyboard:
        dur = float(spec.get("duration") or 0)
        if dur <= 0:
            continue
        path = (rows.get(int(spec["idx"])) or {}).get("final_path")
        if not path or not Path(path).is_file():
            raise RefineError(f"第 {spec['idx']} 拍没有成片文件,先把成片出完再精修")
        out.append((Path(path), dur))
    if not out:
        raise RefineError("项目还没有成片镜头")
    return out


def _cues_of(project_id: str) -> list[Cue]:
    nar = db.read_json(project_id, "narration.json", {}) or {}
    cues = nar.get("cues") or []
    if not cues:
        raise RefineError("narration.json 里没有字幕 cue,先走完旁白生成")
    return [Cue(float(c["start"]), float(c["end"]), str(c["text"])) for c in cues]


def _audio_of(project_id: str) -> Path:
    project_dir = config.project_dir(project_id)
    mix = project_dir / "_render" / "mix.wav"
    if mix.is_file():
        return mix
    nar = db.read_json(project_id, "narration.json", {}) or {}
    path = Path(nar.get("narration_path") or "")
    if not path.is_file():
        raise RefineError("找不到旁白音频")
    return path


def export(project_id: str, *, force: bool = False) -> Session:
    """生成精修目录。已有未应用的改动时拒绝覆盖(force=True 才丢掉)。"""
    project = db.get_project(project_id)
    if not project:
        raise RefineError("项目不存在")
    folder = film_dir(project_id)
    if folder.exists() and not force:
        pending = read_edits(project_id)
        if pending.any:
            raise RefineError("精修目录里有还没应用的字幕改动,先应用或用 force 丢弃")
    beats = _beat_files(project_id)
    cues = _cues_of(project_id)
    audio = _audio_of(project_id)

    # 只换 film.html 和 assets/:点开头的目录(.film 的项目 id、版本历史)归 OpenFilm,
    # 清掉它 Studio 里已经打开的项目就失效(410)
    shutil.rmtree(folder / "assets", ignore_errors=True)
    assets = folder / "assets"
    assets.mkdir(parents=True)
    placed: list[tuple[str, float, float, float]] = []
    at = 0.0
    for i, (src, dur) in enumerate(beats):
        rel = f"assets/beat_{i:03d}{src.suffix or '.mp4'}"
        _link(src, folder / rel)
        placed.append((rel, at, dur, timeline.probe_safe(src)))
        at += dur
    _link(audio, assets / "mix.wav")
    (assets / "mix.vtt").write_text(to_vtt(cues), encoding="utf-8")
    (folder / "film.html").write_text(
        build_film_html(aspect.frame_size(project), placed, "assets/mix.wav", at), encoding="utf-8")
    state = {"cues": [{"index": i + 1, "start": c.start, "end": c.end, "text": c.text}
                      for i, c in enumerate(cues)],
             "beats": len(beats), "seconds": round(at, 3), "openfilm": config.OPENFILM_VERSION}
    db.write_json(project_id, STATE_FILE, state)
    return Session(folder=folder, beats=len(beats), cues=len(cues), seconds=round(at, 3))


# --------------------------------------------------------------------------
# 读回改动
# --------------------------------------------------------------------------
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def diff_cues(original: Sequence[Mapping[str, Any]], current: Sequence[Cue]) -> Edits:
    edits = Edits(original_count=len(original), current_count=len(current))
    if len(original) != len(current):
        edits.structural = True
        return edits
    for orig, cur in zip(original, current):
        if _norm(str(orig["text"])) != _norm(cur.text):
            edits.changed.append(Edit(int(orig["index"]), str(orig["text"]), cur.text,
                                      cur.start, cur.end))
    return edits


def read_edits(project_id: str) -> Edits:
    state = db.read_json(project_id, STATE_FILE, None)
    vtt = film_dir(project_id) / "assets" / "mix.vtt"
    if not state or not vtt.is_file():
        raise RefineError("还没有导出过精修目录")
    return diff_cues(state["cues"], parse_vtt(vtt.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# 应用:检查 → 写回字幕 → 重出成片
# --------------------------------------------------------------------------
#: 一句改掉的比例超过这个就提醒:旁白是固定的,字幕改太多等于声画不一致
CHANGE_WARN_RATIO = 0.5


@dataclass(frozen=True)
class Problem:
    index: int
    level: str          # "red" 不能应用;"warn" 只提醒
    text: str

    def __str__(self) -> str:
        return f"第 {self.index} 句 {self.text}"


def check_edits(project_id: str, changed: Sequence[Edit]) -> list[Problem]:
    """人改的字也过「LLM 写、代码查」的那几道:禁用词、数字出处(和包装标题同一口径),外加改动幅度。"""
    from . import orchestrator, packaging

    project = db.get_project(project_id) or {}
    inp = packaging.build_inputs(project_id, orchestrator._profile(project))
    out: list[Problem] = []
    for e in changed:
        new = _norm(e.new)
        if not new:
            out.append(Problem(e.index, "red", "改成了空句,不能应用(旁白还在念,字幕不能没有)"))
            continue
        before = set(packaging.forbidden_hits(e.old, inp["terms"]))
        hits = [t for t in packaging.forbidden_hits(new, inp["terms"]) if t not in before]
        if hits:
            out.append(Problem(e.index, "red", "用了禁用词:" + "、".join(hits)))
        loose = sorted(packaging.figures_in(new) - inp["allowed"])
        if loose:
            out.append(Problem(e.index, "red", "出现了没有事实依据的数字:" + "、".join(loose)))
        ratio = 1 - difflib.SequenceMatcher(None, _norm(e.old), new).ratio()
        if ratio > CHANGE_WARN_RATIO:
            out.append(Problem(e.index, "warn", f"改动超过一半({ratio:.0%}),旁白念的还是原句,小心声画对不上"))
    return out


def apply(project_id: str) -> dict[str, Any]:
    """把 Studio 里改的字幕应用到成片。持项目锁;检查有红项、句数变了、成片没完成都拒绝。

    只改字幕文字,时间轴、旁白、各拍画面都不动:改 narration.json 的 cue 和 ASS,
    再 `explainer.recompose`。重出失败就把字幕文件原样放回去。"""
    from . import explainer, locks, narration, orchestrator
    from ..models import SubtitleCue
    from ..state import State

    edits = read_edits(project_id)
    if edits.structural:
        raise RefineError(f"Studio 里字幕句数变了({edits.original_count}→{edits.current_count}),"
                          "v1 只支持改字,不能增删或合并句子")
    if not edits.changed:
        return {"changed": 0, "edits": [], "warnings": []}
    problems = check_edits(project_id, edits.changed)
    red = [p for p in problems if p.level == "red"]
    if red:
        raise RefineError("字幕改动没通过检查:" + ";".join(str(p) for p in red))

    with locks.require_project_lock(project_id):
        project = db.get_project(project_id)
        if not project or State(project["state"]) != State.DONE:
            raise RefineError("成片还没完成,不能应用精修")
        nar = db.read_json(project_id, "narration.json", {}) or {}
        old_nar = json.loads(json.dumps(nar))
        new_text = {e.index: _norm(e.new) for e in edits.changed}
        nar["cues"] = [{**c, "text": new_text.get(int(c["index"]), c["text"])} for c in nar.get("cues") or []]
        ass = Path(nar["ass_path"]) if nar.get("ass_path") else None
        old_ass = ass.read_text(encoding="utf-8") if ass and ass.is_file() else None
        try:
            db.write_json(project_id, "narration.json", nar)
            if ass is not None:
                narration.retext_ass(ass, [SubtitleCue(**c) for c in nar["cues"]],
                                     size=aspect.frame_size(project),
                                     line_chars=aspect.line_chars(project))
            payload = explainer.recompose(project_id, orchestrator._specs(project_id))
        except Exception as exc:
            db.write_json(project_id, "narration.json", old_nar)
            if ass is not None and old_ass is not None:
                ass.write_text(old_ass, encoding="utf-8")
            raise RefineError(f"重出成片失败,字幕已还原:{exc}") from exc
        state = db.read_json(project_id, STATE_FILE, {}) or {}
        for c in state.get("cues") or []:
            c["text"] = new_text.get(int(c["index"]), c["text"])
        db.write_json(project_id, STATE_FILE, state)
        db.add_event(project_id, "team", reason=f"精修字幕 {len(edits.changed)} 句,成片已重出")
    return {"changed": len(edits.changed),
            "edits": [{"index": e.index, "old": e.old, "new": _norm(e.new)} for e in edits.changed],
            "warnings": [str(p) for p in problems if p.level == "warn"] + list(payload.get("warnings") or []),
            "duration": payload.get("duration")}


# --------------------------------------------------------------------------
# Studio
# --------------------------------------------------------------------------
def _npx() -> str:
    exe = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    return exe or ""


def studio_url(project_id: str, *, timeout: float = 120.0) -> str:
    """起 Studio 并返回地址。要 Node 22+;CLI 退出时有 libuv 的断言(Windows),退出码不看,只看地址。

    OPENFILM_NO_BROWSER=1:不让它自己去开默认浏览器,地址交给调用方。"""
    node = config.motion_node()
    npx = _npx()
    if not node or not npx:
        raise RefineError(f"找不到 node {config.MOTION_NODE_MIN}+ 或 npx,没法起 Studio")
    folder = film_dir(project_id)
    if not (folder / "film.html").is_file():
        raise RefineError("还没有导出过精修目录")
    env = {**os.environ, "OPENFILM_NO_BROWSER": "1",
           "PATH": str(Path(node).parent) + os.pathsep + os.environ.get("PATH", "")}
    try:
        proc = subprocess.run([npx, "-y", f"openfilm@{config.OPENFILM_VERSION}", "open"],
                              cwd=folder, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RefineError(f"起 Studio 失败:{exc}") from exc
    found = _URL_RE.search(proc.stdout or "")
    if not found:
        raise RefineError("没拿到 Studio 地址:" + ((proc.stderr or proc.stdout or "")[-300:]).strip())
    return found.group(1)


# --------------------------------------------------------------------------
# 命令行:python -m app.pipeline.refine export|open|edits <project_id>
# --------------------------------------------------------------------------
def _main(argv: Sequence[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m app.pipeline.refine")
    ap.add_argument("cmd", choices=["export", "open", "edits", "apply"])
    ap.add_argument("project_id")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(list(argv))
    try:
        if args.cmd == "export":
            s = export(args.project_id, force=args.force)
            print(f"已导出:{s.folder}  {s.beats} 拍 · {s.cues} 句字幕 · {s.seconds}s")
        elif args.cmd == "open":
            print(studio_url(args.project_id))
        elif args.cmd == "apply":
            res = apply(args.project_id)
            print(f"已应用 {res['changed']} 句" + (f",成片 {res['duration']}s" if res["changed"] else ""))
            for w in res["warnings"]:
                print(f"  提醒:{w}")
        else:
            e = read_edits(args.project_id)
            print(f"句数 {e.original_count} → {e.current_count}" + ("(增删过句,v1 不支持)" if e.structural else ""))
            for c in e.changed:
                print(f"  #{c.index}: {c.old!r} → {c.new!r}")
            if not e.any:
                print("  没有改动")
    except RefineError as exc:
        print(f"失败:{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
