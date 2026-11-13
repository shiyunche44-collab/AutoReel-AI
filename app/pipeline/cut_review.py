"""成片机审:让视觉模型把成片从头到尾看一遍,替掉「人逐张看联系表」。

原来所有自动质量门都不看成片画面(视觉模型只用在素材池打标,`final_qa` 只出警告,
门禁 B 只查每拍渲出来了),缺陷只能靠定时 Claude 任务一张张看联系表发现。首版成片
几乎全被打回,理由高度重复:别家水印 / 车牌上的图库标、串车型、文字被挡、屏上数字
和旁白对不上、黑场。这些机器看得出。

流程:
1. 抽帧拼联系表(`contact_sheets`,`scripts/review_render.py` 也用它);
2. 每张联系表问一次视觉模型(固定清单),回答连同这段时间的旁白交给 LLM 转成带时间的问题;
3. **会打回的问题先复核**:取那一秒的大图单独再问一次,两次都说有才算数。
   一次误判的代价是整片重做一遍并吃掉一次返工名额,所以宁可漏给终审,不乱打回;
4. 并入 `final_qa` 的技术检测(黑场、长时间静止会打回,响度只提示)。

任何一步失败都只是「机审没看完」(`inconclusive`):片子照常进待审,由终审全看。
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import config
from . import final_qa, timeline, vision_verdict
from .grounding import numbers_in

log = logging.getLogger(__name__)

#: 多久抽一帧(秒)。长片放宽,不然联系表太多
SAMPLE_EVERY = 1.0
SAMPLE_EVERY_LONG = 2.0
LONG_FILM = 100.0
#: 联系表一格的宽度(像素)、一张几格。格子太小视觉模型看不清角标
TILE_WIDTH = 480
SHEET_TILES = 8
SHEET_COLS = 4
#: 复核用的单帧宽度
CONFIRM_WIDTH = 960
#: 一条片最多复核几处(每处一次视觉调用)
CONFIRM_MAX = 6
#: 问题点前后各算这么多秒是「有问题的时间段」(重做时这段用到的素材整条排除)
RANGE_PAD = 1.5
#: 画面静止到这么久才打回(数据卡停 4–5 秒是正常的,`final_qa` 的 4 秒线只提示)
FREEZE_BLOCK_SECONDS = 8.0
#: 屏上数字要在这格前后这么多秒的旁白里被说到
SAID_PAD = 6.0

#: 问题种类。前五种会打回,后两种只提示给终审
KINDS = {
    "watermark": "别家水印 / 台标",
    "wrong_model": "不是目标车型",
    "text_blocked": "文字被挡或出画",
    "blank": "黑屏 / 白屏 / 花屏",
    "technical": "技术检测",
    "plate": "车牌可读",
    "number_mismatch": "屏上数字旁白没说",
}
BLOCKING = frozenset({"watermark", "wrong_model", "text_blocked", "blank", "technical"})
#: 这几种打回前要单帧复核
CONFIRM_KINDS = frozenset({"watermark", "wrong_model", "text_blocked"})

REPORT_FILE = "_render/qa/machine_review.json"

SHEET_PROMPT = (
    "这是一条{shape}汽车解说视频的联系表:{n} 格按时间从左到右、从上到下排,每格左上角的黄字是"
    "时间(秒),那是我加的标记,不算画面内容。片子讲的是「{subject}」。片子自己的栏目名、"
    "账号名(含「柒哥」字样的)和它自己做的数据卡、字幕都是正常内容。\n"
    "请只依据看到的内容,用文字逐条回答,有问题要写明是哪一格、黄字时间是多少:\n"
    "1. 有没有**别的**平台或账号的水印、台标、logo(例如 汽车之家 / AUTOHOME、懂车帝、"
    "bilibili、易车、别的抖音号),包括印在车牌位置上的;\n"
    "2. 有没有能读出号码的真实车牌;\n"
    "3. 哪几格里的车明显不是「{subject}」(看车标、车尾字标、车型);看不出来就说看不出;\n"
    "4. 字幕、标题、数据卡的文字有没有被别的东西挡住、超出画面、互相重叠,"
    "有没有卡通人物头像压在文字或数字上;\n"
    "5. 有没有整格全黑、全白或花屏;\n"
    "6. 把每格屏上出现的大号数字连同单位抄出来(字幕里的不用抄)。\n"
    "没问题的格不用逐格说。最后用一句话概括。"
)

REVIEW_SYSTEM = """你把视觉模型对视频联系表的描述转成质检问题清单。片子讲的是「{subject}」。

每张图的描述前面有这张联系表覆盖的时间段和这段时间的旁白。
只依据描述里**明确写出**的内容,保守,拿不准就不报:
- "watermark":明确说出现了别的平台 / 账号的水印、台标、logo(写出了是哪家)。
  片子自己的栏目名、账号名(含「柒哥」)不算;只说"疑似""可能有"的不算。
- "plate":明确说能读出真实车牌号码。
- "wrong_model":明确说某格里的车是另一个品牌或另一款车(写出了依据:车标、字标)。
  "看不出来""无法确认"不算;内饰、局部特写认不出也不算。
- "text_blocked":明确说文字被挡住、超出画面、互相重叠,或头像压在文字 / 数字上。
- "blank":明确说某格全黑、全白或花屏。
- screen_numbers:描述里抄出来的屏上大号数字(只要数字本身,如 "13.98"、"510"),
  连同它所在那格的时间;左上角的黄字时间不算。

输出 JSON 对象 {{"verdicts": [...]}},每张图一项:
{{"index": 图的编号, "issues": [{{"kind": "...", "at": 那一格的黄字时间(秒,数字), "detail": "一句话"}}],
  "screen_numbers": [{{"at": 秒, "value": "数字"}}]}}
没有问题时 issues 为空数组。"""

CONFIRM_PROMPTS = {
    "watermark": "这是视频里的一帧。片子自己的栏目名、账号名(含「柒哥」字样)和字幕、数据卡是正常内容。"
                 "除此之外,画面里(包括车牌位置)有没有**别的**平台或账号的水印、台标、logo?"
                 "有就写出它的文字和位置;没有就明确说没有。",
    "wrong_model": "这是讲「{subject}」的视频里的一帧。画面里的车是不是「{subject}」?"
                   "请说出你看到的车标、字标或车型特征,再给结论:是 / 不是 / 看不出来。",
    "text_blocked": "这是竖屏或横屏解说视频里的一帧。字幕、标题、数据卡上的文字有没有被别的元素"
                    "(比如卡通人物头像)挡住、超出画面边缘、或互相重叠看不清?"
                    "有就说明是哪段文字被什么挡住;没有就明确说没有。",
}

CONFIRM_SYSTEM = """你复核视频质检里的疑点。每张图的描述前面写着要复核的问题。
只依据描述判断这个问题是否**确实存在**,保守:描述说"没有""看不出来""无法确认" → false。
片子自己的栏目名、账号名(含「柒哥」)不算水印。
输出 JSON 对象 {{"verdicts": [{{"index": 编号, "confirmed": true 或 false, "detail": "一句话依据"}}]}}"""


# --------------------------------------------------------------------------
# 抽帧与联系表
# --------------------------------------------------------------------------
def sample_every(duration: float) -> float:
    return SAMPLE_EVERY_LONG if duration > LONG_FILM else SAMPLE_EVERY


def extract_frames(video: Path, out_dir: Path, duration: float, *, every: float | None = None,
                   width: int = TILE_WIDTH, extra: Sequence[float] = ()) -> list[tuple[float, Path]]:
    """按固定间隔抽帧(一次 ffmpeg 调用),`extra` 里的时间点另外各抽一帧。

    返回 [(时间, 文件)],按时间排序。**一帧都没抽到就抛错**:原来逐帧调用不看返回码,
    ffmpeg 出错时联系表是空的,质检照样「通过」。"""
    if not config.FFMPEG_PATH:
        raise RuntimeError("没有配置 ffmpeg(FFMPEG_BIN),抽不了帧")
    every = every or sample_every(duration)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    proc = subprocess.run(
        [str(config.FFMPEG_PATH), "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(video),
         "-vf", f"fps=1/{every:g},scale={width}:-2", "-q:v", "4", str(out_dir / "s%05d.jpg")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    # fps 滤镜取每个间隔中点附近的帧:第 k 张约在 (k + 0.5) * every 秒
    frames = [(min(duration, (k + 0.5) * every) if duration else (k + 0.5) * every, p)
              for k, p in enumerate(sorted(out_dir.glob("s*.jpg")))]
    for n, t in enumerate(sorted({round(max(0.0, x), 1) for x in extra})):
        path = out_dir / f"x{n:04d}.jpg"
        if grab_frame(video, t, path, width=width):
            frames.append((t, path))
    if not frames:
        raise RuntimeError(f"抽帧失败,一帧都没有:{(proc.stderr or '').strip()[:200]}")
    return sorted(frames, key=lambda f: f[0])


def grab_frame(video: Path, at: float, dest: Path, *, width: int = CONFIRM_WIDTH) -> bool:
    """取某一秒的单帧。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [str(config.FFMPEG_PATH), "-hide_banner", "-nostdin", "-v", "error", "-y",
             "-ss", f"{max(0.0, at):.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "3", str(dest)],
            capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0


def paste_sheet(chunk: Sequence[tuple[float, Path]], sheet: Path, cols: int = SHEET_COLS) -> bool:
    """用 PIL 拼一张联系表,每格左上角画时间戳。时间戳画不上也照样出图。"""
    from PIL import Image, ImageDraw, ImageFont

    tiles = []
    for t, path in chunk:
        try:
            with Image.open(path) as im:
                tiles.append((t, im.convert("RGB")))
        except OSError:
            continue
    if not tiles:
        return False
    w, h = tiles[0][1].size
    rows = (len(tiles) + cols - 1) // cols
    canvas = Image.new("RGB", (w * cols, h * rows), "black")
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=max(18, w // 16))
    except Exception:  # noqa: BLE001 - 老版本 Pillow 不收 size
        font = ImageFont.load_default()
    for i, (t, im) in enumerate(tiles):
        x, y = (i % cols) * w, (i // cols) * h
        canvas.paste(im if im.size == (w, h) else im.resize((w, h)), (x, y))
        try:
            label = f"{t:.1f}s"
            box = draw.textbbox((x + 6, y + 6), label, font=font)
            draw.rectangle((box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3), fill=(0, 0, 0))
            draw.text((x + 6, y + 6), label, fill=(255, 220, 0), font=font)
        except Exception:  # noqa: BLE001, S110 - 时间戳画不上不影响出图
            pass
    sheet.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(sheet, quality=88)
    return sheet.is_file()


def contact_sheets(frames: Sequence[tuple[float, Path]], sheets_dir: Path, *,
                   per_sheet: int = SHEET_TILES, cols: int = SHEET_COLS
                   ) -> list[dict[str, Any]]:
    """把帧拼成联系表。返回 [{"path", "start", "end", "times"}]。"""
    sheets_dir.mkdir(parents=True, exist_ok=True)
    for old in sheets_dir.glob("sheet_*.jpg"):
        old.unlink()
    out: list[dict[str, Any]] = []
    for n in range(0, len(frames), per_sheet):
        chunk = [(t, p) for t, p in frames[n:n + per_sheet] if p.is_file()]
        sheet = sheets_dir / f"sheet_{n // per_sheet:03d}.jpg"
        if chunk and paste_sheet(chunk, sheet, cols):
            times = [t for t, _ in chunk]
            out.append({"path": sheet, "start": times[0], "end": times[-1], "times": times})
    return out


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def narration_between(cues: Sequence[dict], start: float, end: float) -> str:
    return "".join(str(c.get("text") or "") for c in cues
                   if float(c.get("end") or 0) >= start and float(c.get("start") or 0) <= end)


def _seconds(value: Any, default: float) -> float:
    try:
        return float(str(value).lower().replace("s", "").replace("秒", "").strip())
    except (TypeError, ValueError):
        return default


def collect_issues(verdicts: Sequence[dict | None], sheets: Sequence[dict],
                   cues: Sequence[dict]) -> list[dict[str, Any]]:
    """LLM 的逐表结论 → 问题清单;屏上数字和附近的旁白对一遍(对不上只提示)。"""
    issues: list[dict[str, Any]] = []
    for v, sheet in zip(verdicts, sheets, strict=False):
        if not isinstance(v, dict):
            continue
        lo, hi = float(sheet["start"]), float(sheet["end"])
        for it in v.get("issues") or []:
            if not isinstance(it, dict) or it.get("kind") not in KINDS:
                continue
            at = min(hi, max(lo, _seconds(it.get("at"), lo)))
            issues.append({"kind": it["kind"], "at": round(at, 1),
                           "detail": str(it.get("detail") or "")[:120]})
        loose: dict[float, list[str]] = {}
        for num in v.get("screen_numbers") or []:
            if not isinstance(num, dict):
                continue
            at = min(hi, max(lo, _seconds(num.get("at"), lo)))
            want = numbers_in(str(num.get("value") or ""))
            said = numbers_in(narration_between(cues, at - SAID_PAD, at + SAID_PAD))
            # 个位数、年份不查:序号、款型年份上屏不需要旁白念
            miss = [n for n in want if len(n) >= 2 and not (len(n) == 4 and n[:2] in ("19", "20"))
                    and n not in said]
            if miss:
                loose.setdefault(round(at, 1), []).append(str(num.get("value")))
        for at, values in loose.items():
            issues.append({"kind": "number_mismatch", "at": at,
                           "detail": f"屏上的 {'、'.join(dict.fromkeys(values))} 这几秒的旁白没说到"})
    return sorted(issues, key=lambda i: (i["at"], i["kind"]))


def confirm(issues: list[dict[str, Any]], video: Path, work: Path, subject: str, llm: Any,
            errors: list[str]) -> None:
    """要打回的视觉问题逐个取大图复核,结果写回 issue["confirmed"]。

    同一种问题相隔不到 3 秒的只复核头一个(同一个镜头),其余跟它的结论。复核不出结论 = 不算数。"""
    from ..providers import images

    todo: list[dict[str, Any]] = []
    for issue in issues:
        if issue["kind"] not in CONFIRM_KINDS:
            continue
        lead = next((t for t in todo if t["kind"] == issue["kind"]
                     and abs(t["at"] - issue["at"]) < 3.0), None)
        if lead is not None:
            issue["_lead"] = lead
        elif len(todo) < CONFIRM_MAX:
            todo.append(issue)
        else:
            issue["confirmed"] = False
            issue["detail"] += "(超出复核名额,交终审)"
    urls, prompts, ctx, slots = [], [], [], []
    for n, issue in enumerate(todo):
        frame = work / f"confirm_{n:02d}.jpg"
        issue["confirmed"] = False
        if not grab_frame(video, issue["at"], frame):
            errors.append(f"复核取帧失败({issue['at']} 秒)")
            continue
        try:
            urls.append(images.cli.upload_temp_url(frame))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"复核帧上传失败:{str(exc)[:100]}")
            continue
        prompts.append(CONFIRM_PROMPTS[issue["kind"]].format(subject=subject))
        ctx.append(f"要复核的问题:{KINDS[issue['kind']]}({issue['detail']})")
        slots.append(issue)
    if urls:
        verdicts, _, _ = vision_verdict.judge(urls, prompts, CONFIRM_SYSTEM, llm, errors,
                                              what="帧", contexts=ctx)
        for issue, v in zip(slots, verdicts, strict=False):
            issue["confirmed"] = bool(isinstance(v, dict) and v.get("confirmed") is True)
            if isinstance(v, dict) and v.get("detail"):
                issue["confirm_detail"] = str(v["detail"])[:120]
    for issue in issues:
        lead = issue.pop("_lead", None)
        if lead is not None:
            issue["confirmed"] = bool(lead.get("confirmed"))


def technical_issues(video: Path, duration: float) -> tuple[list[dict[str, Any]], list[str]]:
    """`final_qa` 的测量:黑场和长时间静止算问题(会打回),其余原样作为提示返回。"""
    result = final_qa.measure(video)
    notes = final_qa.problems(result, config.TARGET_LUFS) if result else []
    issues: list[dict[str, Any]] = []
    if result:
        for start, dur in result.get("black", []):
            if dur >= final_qa.BLACK_MIN_SECONDS:
                issues.append({"kind": "technical", "at": round(start, 1), "confirmed": True,
                               "detail": f"黑场 {dur:.1f} 秒"})
        for start, dur in result.get("freeze", []):
            # 片尾卡本来就是静止的,不算
            if dur >= FREEZE_BLOCK_SECONDS and (not duration or start + dur < duration - 1.0):
                issues.append({"kind": "technical", "at": round(start, 1), "confirmed": True,
                               "detail": f"画面静止 {dur:.1f} 秒"})
    return issues, notes


def blocking(issue: dict[str, Any]) -> bool:
    """这条问题会不会让片子被打回。要复核的种类必须复核确认过。"""
    if issue["kind"] not in BLOCKING:
        return False
    return bool(issue.get("confirmed")) if issue["kind"] in CONFIRM_KINDS else True


def bad_ranges(issues: Sequence[dict[str, Any]], duration: float = 0.0) -> list[list[float]]:
    """会打回的问题所在的时间段(合并相邻的),给重做时排除素材用。"""
    spans = sorted([max(0.0, i["at"] - RANGE_PAD),
                    min(duration, i["at"] + RANGE_PAD) if duration else i["at"] + RANGE_PAD]
                   for i in issues if blocking(i) and i["kind"] != "text_blocked")
    out: list[list[float]] = []
    for lo, hi in spans:
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([round(lo, 1), round(hi, 1)])
    return [[a, round(b, 1)] for a, b in out]


def summary(report: dict[str, Any]) -> str:
    """一句话结论(写进打回意见 / 卡片)。"""
    hard = [i for i in report.get("issues", []) if i.get("blocking")]
    if report.get("inconclusive"):
        return "机审没看完:" + (report.get("errors") or ["原因不明"])[0][:80]
    if not hard:
        soft = len(report.get("issues", []))
        return "机审通过" + (f",{soft} 条提示留给终审" if soft else "")
    parts = [f"{i['at']:g} 秒 {KINDS[i['kind']]}:{i['detail']}" for i in hard[:5]]
    return "机审不通过:" + ";".join(parts)


def review(video: Path, work: Path, *, subject: str, cues: Sequence[dict] = (),
           portrait: bool = True, llm: Any = None) -> dict[str, Any]:
    """审一条成片。返回报告(见模块说明),不抛错。"""
    from ..providers import images

    errors: list[str] = []
    report: dict[str, Any] = {"checked_at": time.time(), "video": str(video), "subject": subject,
                              "passed": True, "inconclusive": False, "issues": [], "notes": [],
                              "sheets": [], "errors": errors, "vlm_calls": 0}
    try:
        duration = timeline.probe_safe(video, default=0.0)
        report["duration"] = round(duration, 2)
        frames = extract_frames(video, work / "frames", duration)
        sheets = contact_sheets(frames, work / "sheets")
        if not sheets:
            raise RuntimeError("联系表一张都没拼出来")
        report["sheets"] = [str(s["path"]) for s in sheets]
        urls, prompts, ctx, used = [], [], [], []
        for sheet in sheets:
            try:
                urls.append(images.cli.upload_temp_url(sheet["path"]))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"联系表上传失败:{str(exc)[:100]}")
                continue
            prompts.append(SHEET_PROMPT.format(shape="竖屏" if portrait else "横屏",
                                               n=len(sheet["times"]), subject=subject))
            said = narration_between(cues, sheet["start"], sheet["end"])
            ctx.append(f"这张联系表覆盖 {sheet['start']:.1f}–{sheet['end']:.1f} 秒,"
                       f"各格时间:{'、'.join(f'{t:.1f}' for t in sheet['times'])}。"
                       f"这段旁白:{said or '(没有)'}")
            used.append(sheet)
        verdicts, _texts, sources = vision_verdict.judge(
            urls, prompts, REVIEW_SYSTEM.format(subject=subject), llm, errors,
            what="联系表", contexts=ctx)
        report["vlm_calls"] += len(urls)
        seen = sum(1 for v in verdicts if isinstance(v, dict))
        # 一半以上的联系表没有结论 = 没看完,不能当通过
        if not used or seen * 2 < len(sheets):
            report["inconclusive"] = True
            errors.append(f"{len(sheets)} 张联系表只有 {seen} 张有结论({sources})")
        issues = collect_issues(verdicts, used, cues)
        before = len(errors)
        confirm(issues, video, work / "confirm", subject, llm, errors)
        report["vlm_calls"] += sum(1 for i in issues if "confirm_detail" in i)
        if len(errors) > before and any(i["kind"] in CONFIRM_KINDS and not i.get("confirmed")
                                        for i in issues):
            report["inconclusive"] = True  # 有疑点但复核没跑成
        tech, notes = technical_issues(video, duration)
        report["notes"] = notes
        issues = sorted(issues + tech, key=lambda i: (i["at"], i["kind"]))
        for issue in issues:
            issue["blocking"] = blocking(issue)
        report["issues"] = issues
        report["bad_ranges"] = bad_ranges(issues, duration)
        report["passed"] = not any(i["blocking"] for i in issues)
    except Exception as exc:  # noqa: BLE001 - 机审挂了不能卡住出片,交终审全看
        log.warning("成片机审失败(%s):%s", video, exc)
        errors.append(f"机审出错:{str(exc)[:200]}")
        report["inconclusive"] = True
    report["summary"] = summary(report)
    return report


def review_project(project_id: str, llm: Any = None) -> dict[str, Any]:
    """审一个项目的成片,报告落到 `_render/qa/machine_review.json`。"""
    from .. import db
    from . import aspect

    project = db.get_project(project_id) or {}
    video = aspect.output_path(project_id)
    work = config.PROJECTS_DIR / project_id / "_render" / "qa" / "machine"
    names = [s["name"] for s in db.project_subjects(project)] if project else []
    subject = "、".join(names) or str(project.get("subject") or "")
    if not video.is_file():
        report = {"checked_at": time.time(), "passed": True, "inconclusive": True, "issues": [],
                  "notes": [], "sheets": [], "errors": ["找不到成片文件"], "vlm_calls": 0,
                  "summary": "机审没看完:找不到成片文件"}
    else:
        nar = db.read_json(project_id, "narration.json", {}) or {}
        report = review(video, work, subject=subject, cues=nar.get("cues") or [],
                        portrait=aspect.is_portrait(project), llm=llm)
    try:
        dest = config.PROJECTS_DIR / project_id / REPORT_FILE
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("机审报告写不下来(%s):%s", project_id, exc)
    return report
