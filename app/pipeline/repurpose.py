"""一稿多发:成片之外顺带出一篇小红书图文笔记和 1–2 条竖版切片。

**全部取自已有产物,不重新生成画面。**
- 笔记图片:3:4 封面 + 分镜里已经核对过的数据卡(`cards.still` 出静态整卡);
  不够 `NOTE_MIN_IMAGES` 张就让 LLM 再从事实层挑几张参数卡 / 对比卡,照样走 `cards.ground`
  逐行核数字,核不过的整张不要。
- 笔记正文:LLM 按小红书的写法改写脚本;正文里每个数字都要在事实层或脚本里找得到,
  找不到的那句删掉(和标题、置顶评论同一套 `packaging.figures_in` 口径)。没有 LLM 就用模板拼。
- 切片:没有留存曲线之前,按「信息密度」挑段落 —— 每秒挂了多少条事实、有几张数据卡。
  从章节开头切,30–45 秒,开场那一章不切(它是给整片铺垫的)。横屏成片转竖版用
  「模糊底 + 居中原画 + 顶部章节标题」;竖屏成片直接切。

产物放在项目目录的 `repurpose/` 下,清单是 `repurpose.json`。
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw

from .. import config, db
from ..models import Fact
from . import cards

log = logging.getLogger(__name__)

MANIFEST = "repurpose.json"
SUBDIR = "repurpose"
#: 图文笔记几张图(含封面)
NOTE_MIN_IMAGES = 6
NOTE_MAX_IMAGES = 9
#: 小红书标题 / 正文上限
NOTE_TITLE_MAX = 20
NOTE_BODY_MAX = 1000
#: 切片时长
CLIP_MIN = 30.0
CLIP_MAX = 45.0
MAX_CLIPS = 2
CLIP_SIZE = (1080, 1920)

_SENTENCE_RE = re.compile(r"[^。!?！？\n]+[。!?！？]?")


class RepurposeError(RuntimeError):
    pass


def folder(project_id: str) -> Path:
    return config.project_dir(project_id) / SUBDIR


def preview_names() -> set[str]:
    """能对外给预览的文件名(白名单,顺带挡住路径穿越)。"""
    return {f"note_{n:02d}.jpg" for n in range(NOTE_MAX_IMAGES)} | {f"clip{n}.mp4" for n in range(1, MAX_CLIPS + 1)}


def load(project_id: str) -> dict[str, Any] | None:
    return db.read_json(project_id, MANIFEST, None)


# --------------------------------------------------------------------------
# 切片:挑段落
# --------------------------------------------------------------------------
def shot_spans(storyboard: Sequence[dict[str, Any]], script: Sequence[dict[str, Any]]
               ) -> list[dict[str, Any]]:
    """每个镜头在成片里的起止时刻 + 章节 + 挂了几条事实 / 有没有数据卡。"""
    refs = {s.get("idx"): len(s.get("fact_refs") or []) for s in script if isinstance(s, dict)}
    out, t = [], 0.0
    for shot in storyboard:
        dur = float(shot.get("duration") or 0)
        if dur <= 0:
            continue
        out.append({"idx": shot.get("idx"), "start": round(t, 3), "end": round(t + dur, 3),
                    "chapter": str(shot.get("chapter") or shot.get("section") or ""),
                    "refs": refs.get(shot.get("idx"), 0), "card": bool(shot.get("card"))})
        t += dur
    return out


def windows(spans: Sequence[dict[str, Any]], lo: float = CLIP_MIN, hi: float = CLIP_MAX
            ) -> list[dict[str, Any]]:
    """所有候选段:从章节开头起,攒够 `lo` 秒后尽量把这一章收完,不超过 `hi`。按密度从高到低。"""
    if not spans:
        return []
    first = spans[0]["chapter"]
    out = []
    for i, s in enumerate(spans):
        if s["chapter"] == first or (i and spans[i - 1]["chapter"] == s["chapter"]):
            continue  # 开场那一章不切;只从章节开头起
        j = i
        while j + 1 < len(spans) and spans[j]["end"] - s["start"] < lo:
            j += 1
        if spans[j]["end"] - s["start"] < lo:
            break  # 剩下的不够一条
        while (j + 1 < len(spans) and spans[j + 1]["chapter"] == spans[j]["chapter"]
               and spans[j + 1]["end"] - s["start"] <= hi):
            j += 1
        dur = spans[j]["end"] - s["start"]
        if dur > hi:
            continue
        part = spans[i:j + 1]
        refs = sum(p["refs"] for p in part)
        n_cards = sum(1 for p in part if p["card"])
        chapters = list(dict.fromkeys(p["chapter"] for p in part if p["chapter"]))
        out.append({"start": s["start"], "end": spans[j]["end"], "duration": round(dur, 3),
                    "shots": [p["idx"] for p in part], "chapter": chapters[0] if chapters else "",
                    "chapters": chapters, "refs": refs, "cards": n_cards,
                    "score": round((refs + 2 * n_cards) / dur, 4)})
    return sorted(out, key=lambda w: (-w["score"], w["start"]))


def pick_clips(spans: Sequence[dict[str, Any]], n: int = MAX_CLIPS) -> list[dict[str, Any]]:
    """密度最高、互不重叠的 n 段,按在片里的先后排。一条事实都没挂的段不要。"""
    chosen: list[dict[str, Any]] = []
    for w in windows(spans):
        if w["refs"] + w["cards"] == 0:
            continue
        if any(w["start"] < c["end"] and c["start"] < w["end"] for c in chosen):
            continue
        chosen.append(w)
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda w: w["start"])


# --------------------------------------------------------------------------
# 切片:出片
# --------------------------------------------------------------------------
def headline_png(subject: str, chapter: str, dest: Path, size: tuple[int, int] = CLIP_SIZE) -> Path:
    """横转竖的透明叠层:上方车型 + 章节大字,下方一行「完整版看主页」。"""
    w, h = size
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    top = int(h * 0.17)
    tag, tag_f = cards._fit(d, subject, 52, w - 160)
    d.text((w // 2, top), tag, font=tag_f, fill=(255, 196, 64, 255), anchor="mm")
    big, big_f = cards._fit(d, chapter or "重点", 96, w - 120, bold=True)
    d.text((w // 2, top + 120), big, font=big_f, fill=(255, 255, 255, 255), anchor="mm",
           stroke_width=3, stroke_fill=(0, 0, 0, 160))
    foot, foot_f = cards._fit(d, "完整版看主页", 40, w - 160)
    d.text((w // 2, int(h * 0.74)), foot, font=foot_f, fill=(255, 255, 255, 200), anchor="mm")
    img.save(dest)
    return dest


def clip_filter(portrait_src: bool, duration: float) -> tuple[str, bool]:
    """(filter_complex, 是否要叠层输入)。音频头尾各淡一下,别从半个字切进来。"""
    fade_out = max(0.0, duration - 0.4)
    audio = f"[0:a]afade=t=in:d=0.2,afade=t=out:st={fade_out:.3f}:d=0.4[a]"
    w, h = CLIP_SIZE
    if portrait_src:
        return f"[0:v]scale={w}:{h},setsar=1,format=yuv420p[v];{audio}", False
    video = (f"[0:v]split[s0][s1];"
             f"[s0]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
             f"boxblur=24:2,eq=brightness=-0.12[bg];"
             f"[s1]scale={w}:-2[fg];[bg][fg]overlay=0:(H-h)/2[m];"
             f"[m][1:v]overlay=0:0,setsar=1,format=yuv420p[v]")
    return f"{video};{audio}", True


def cut_clip(src: Path, window: dict[str, Any], dest: Path, *, portrait_src: bool,
             subject: str) -> Path:
    from . import render as render_mod

    dest.parent.mkdir(parents=True, exist_ok=True)
    duration = float(window["duration"])
    graph, overlay = clip_filter(portrait_src, duration)
    with tempfile.TemporaryDirectory(prefix="clip_") as tmp:
        cmd = [render_mod._ffmpeg(), "-y", "-v", "error",
               "-ss", f"{window['start']:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
        if overlay:
            png = headline_png(subject, window["chapter"], Path(tmp) / "head.png")
            cmd += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(png)]
        cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-r", "30",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600)
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise RepurposeError(f"切片失败:{(proc.stderr or '')[-300:]}")
    return dest


# --------------------------------------------------------------------------
# 图文笔记:图片
# --------------------------------------------------------------------------
EXTRA_SYSTEM = """你给一篇小红书汽车图文笔记补数据卡图片。从给的事实里挑,每张卡只放事实里写着的数字。

只输出 JSON:{"cards": [{"kind": "spec|compare|bars", "title": "卡片标题(12 字内)", "columns": [], "rows": [...]}]}
{guide}
- 每行必须挂 fact_id,数字原样照抄事实,带单位。
- 不要和已有卡片重复的主题:{have}"""


def storyboard_cards(storyboard: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """分镜里已经核对过的数据卡,同标题的只留第一张。"""
    out, seen = [], set()
    for shot in storyboard:
        card = shot.get("card")
        if not isinstance(card, dict) or not card.get("rows") or card.get("kind") not in cards.CARD_KINDS:
            continue
        key = str(card.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


def extra_cards(facts: Sequence[Fact], have: Sequence[dict[str, Any]], llm: Any, want: int
                ) -> tuple[list[dict[str, Any]], list[str]]:
    """LLM 从事实层再挑几张卡,逐行核数字。返回 (能用的卡, 丢弃原因)。"""
    if llm is None or want <= 0:
        return [], []
    usable = [f for f in facts if f.enabled and f.confidence != "low"]
    if not usable:
        return [], []
    guide = "\n".join(f"- {cards.KIND_GUIDE[k]}" for k in ("spec", "compare", "bars"))
    titles = "、".join(str(c.get("title") or "") for c in have) or "无"
    lines = "\n".join(f"- {f.id} | {f.claim}" + (f"({f.basis})" if f.basis else "") for f in usable[:60])
    data = llm.structured(EXTRA_SYSTEM.replace("{guide}", guide).replace("{have}", titles),
                          f"要 {want} 张。事实:\n{lines}", required_keys=["cards"], temperature=0.3)
    out, dropped = [], []
    taken = {str(c.get("title") or "") for c in have}
    for raw in data.get("cards") or []:
        kind = raw.get("kind") if isinstance(raw, dict) else None
        if kind not in ("spec", "compare", "bars"):
            continue
        card, why = cards.ground(cards.normalise(raw, kind), usable)
        dropped += why
        if card and str(card.get("title") or "") not in taken:
            taken.add(str(card.get("title") or ""))
            out.append(card)
        if len(out) >= want:
            break
    return out, dropped


def cover_for_note(project_id: str) -> Path | None:
    """小红书封面用 3:4 裁切版,没有就用原封面。"""
    from . import aspect
    from .narration import cover_3x4_path

    cover = aspect.cover_path(project_id)
    alt = cover_3x4_path(cover)
    if alt.is_file():
        return alt
    return cover if cover.is_file() else None


def note_images(project_id: str, subject: str, card_list: Sequence[dict[str, Any]]) -> list[str]:
    """封面 + 每张卡一张静图,返回相对项目目录的文件名。"""
    out_dir = folder(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    cover = cover_for_note(project_id)
    if cover:
        dest = out_dir / "note_00.jpg"
        Image.open(cover).convert("RGB").save(dest, quality=92)
        names.append(f"{SUBDIR}/{dest.name}")
    for n, card in enumerate(card_list[:NOTE_MAX_IMAGES - len(names)], start=1):
        dest = out_dir / f"note_{n:02d}.jpg"
        cards.still(card, subject, dest, background=cover)
        names.append(f"{SUBDIR}/{dest.name}")
    return names


# --------------------------------------------------------------------------
# 图文笔记:文字
# --------------------------------------------------------------------------
NOTE_SYSTEM = """你把一条汽车解说视频的脚本改写成小红书图文笔记。

只输出 JSON:{"title": "标题", "body": "正文", "tags": ["话题", ...]}
- 标题 20 字以内,口语,点出这篇能帮读者做什么判断;不要「震惊」「必看」这类词。
- 正文 300–700 字,分 4–6 小段,每段开头一个小标题(如「▍空间」),像真人写的用车笔记,不是广告。
- 只用脚本和事实里出现过的数字,原样照抄;没有出处的数字一个都不要写。
- 不用绝对化用语(最、第一、顶级、唯一……)。
- 结尾用一个问题请读者在评论区说说自己的情况。
- tags 给 5–8 个,不带 #。"""


def split_sentences(text: str) -> list[str]:
    return [m.group(0) for m in _SENTENCE_RE.finditer(text or "") if m.group(0).strip()]


def clean_body(body: str, allowed: set[str]) -> tuple[str, list[str]]:
    """删掉数字找不到出处的句子。返回 (正文, 删了哪些)。按行处理,保留分段。"""
    from .packaging import figures_in

    kept_lines, dropped = [], []
    for line in str(body or "").splitlines():
        parts = []
        for sent in split_sentences(line):
            loose = figures_in(sent) - allowed
            if loose:
                dropped.append(f"「{sent.strip()[:30]}」里的 {'、'.join(sorted(loose))} 找不到出处")
                continue
            parts.append(sent)
        if parts or not line.strip():
            kept_lines.append("".join(parts))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return text, dropped


def template_note(inp: dict[str, Any], script: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """没有 LLM 时的笔记:每章取第一句拼起来。"""
    blocks: dict[str, list[str]] = {}
    for seg in script:
        sec = str(seg.get("section") or "").strip()
        text = str(seg.get("text") or "").strip()
        if sec and text and len(blocks.get(sec, [])) < 2:
            blocks.setdefault(sec, []).append(text)
    body = "\n\n".join(f"▍{sec}\n" + "".join(lines) for sec, lines in blocks.items())
    body += "\n\n你在纠结哪个版本?评论区说说你的用车情况。"
    return {"title": inp["title"] or inp["subject"], "body": body,
            "tags": [inp["subject"], "买车攻略", "汽车"]}


def check_note(note: dict[str, Any], inp: dict[str, Any], disclaimers: Sequence[str]) -> dict[str, Any]:
    """收口:截长度、删无出处数字的句子、补免责声明、查禁用词。`problems` 非空不能发。"""
    from .packaging import figures_in, forbidden_hits, spoken_len

    title = re.sub(r"\s+", " ", str(note.get("title") or "")).strip()
    problems: list[str] = []
    if spoken_len(title) > NOTE_TITLE_MAX:
        title = title[:NOTE_TITLE_MAX]
    if not title:
        problems.append("没有标题")
    loose = sorted(figures_in(title) - inp["allowed"])
    if loose:
        problems.append("标题数字找不到出处:" + "、".join(loose))
    body, dropped = clean_body(str(note.get("body") or ""), inp["allowed"])
    tail = "\n".join(d for d in disclaimers if d and d not in body)
    if tail:
        body = f"{body}\n\n{tail}"
    body = body[:NOTE_BODY_MAX]
    if len(body) < 80:
        problems.append("正文太短(数字没出处的句子删完不剩多少)")
    hits = forbidden_hits(title + body, inp["terms"])
    if hits:
        problems.append("命中禁用词:" + "、".join(hits))
    tags = []
    for t in note.get("tags") or []:
        t = str(t).strip().lstrip("#")[:20]
        if t and t not in tags and not forbidden_hits(t, inp["terms"]):
            tags.append(t)
    return {"title": title, "body": body, "tags": tags[:8], "dropped": dropped, "problems": problems}


def write_note(project_id: str, profile: Any, llm: Any) -> dict[str, Any]:
    from . import packaging

    inp = packaging.build_inputs(project_id, profile)
    script = db.read_json(project_id, "script.json", []) or []
    pkg = packaging.load(project_id)
    raw, source = None, "template"
    if llm is not None:
        user = "\n".join([
            f"车型:{inp['subject']}", f"片名:{inp['title']}", f"主论点:{inp['thesis']}",
            "候选标题:" + " / ".join(t.text for t in (pkg.titles if pkg else [])),
            "脚本:", "\n".join(f"[{s.get('section', '')}] {s.get('text', '')}" for s in script)[:3000],
            "事实:", "\n".join(inp["fact_lines"][:40]),
        ])
        try:
            raw = llm.structured(NOTE_SYSTEM, user, required_keys=["title", "body"], temperature=0.6)
            source = "llm"
        except Exception as exc:  # noqa: BLE001 - LLM 挂了就用模板
            log.warning("笔记改写失败 %s:%s", project_id, exc)
    if not isinstance(raw, dict) or not raw.get("body"):
        raw, source = template_note(inp, script), "template"
    note = check_note(raw, inp, list(getattr(profile, "required_disclaimers", ()) or ()))
    return {**note, "source": source}


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------
def build(project_id: str, *, llm: Any = None) -> dict[str, Any]:
    """出图文笔记 + 切片,写清单。每一件单独兜错,一件挂了不连坐另一件。"""
    from ..pipeline import aspect, orchestrator

    project = db.get_project(project_id)
    if not project:
        raise RepurposeError("项目不存在")
    src = aspect.output_path(project_id)
    if not src.is_file():
        raise RepurposeError("成片还不在")
    profile = orchestrator._profile(project)
    subject = str(project.get("subject") or "")
    storyboard = db.read_json(project_id, "storyboard.json", []) or []
    script = db.read_json(project_id, "script.json", []) or []
    manifest: dict[str, Any] = {"made_at": time.time(), "note": None, "clips": [], "errors": []}

    try:
        have = storyboard_cards(storyboard)
        want = max(0, NOTE_MIN_IMAGES - 1 - len(have))
        more, dropped = extra_cards(orchestrator._facts(project_id), have, llm, want)
        note = write_note(project_id, profile, llm)
        note["images"] = note_images(project_id, subject, [*have, *more])
        note["card_dropped"] = dropped[:10]
        if len(note["images"]) < 2:
            note["problems"].append("图片不到 2 张(没有封面也没有数据卡)")
        manifest["note"] = note
    except Exception as exc:  # noqa: BLE001
        log.warning("图文笔记失败 %s:%s", project_id, exc)
        manifest["errors"].append(f"图文笔记:{exc}")

    portrait = aspect.is_portrait(project)
    for n, w in enumerate(pick_clips(shot_spans(storyboard, script)), start=1):
        name = f"clip{n}"
        try:
            cut_clip(src, w, folder(project_id) / f"{name}.mp4", portrait_src=portrait, subject=subject)
        except Exception as exc:  # noqa: BLE001
            manifest["errors"].append(f"切片 {n}:{exc}")
            continue
        why = f"{w['refs']} 条事实" + (f"、{w['cards']} 张数据卡" if w["cards"] else "")
        manifest["clips"].append({"id": name, "file": f"{SUBDIR}/{name}.mp4", **w,
                                  "title": f"{subject}:{w['chapter']}"[:30],
                                  "why": f"{w['duration']:.0f} 秒里挂了 {why}"})
    db.write_json(project_id, MANIFEST, manifest)
    return manifest
