"""成片逐帧 + 音频 QA。Claude 代审成片时先跑这个,再按报告里的联系表逐张看画面。

    python scripts/review_render.py <project_id> [--artifact output_vertical.mp4] [--fps 1] [--all-frames]

只读:不改库、不动成片。产出写进 `data/projects/<id>/_render/qa/`:
  qa_report.md      检查结果(通过 / 警告 / 不通过)
  sheets/*.jpg      联系表,每张 4x3 = 12 帧,带时间戳;默认每秒 1 帧 + 每个场景切换点前后帧
  frames/*.jpg      --all-frames 时每一帧单独导出(90 秒竖屏约 2700 张,很慢)

退出码:0 = 无不通过项;1 = 有不通过项(Claude 不得批准发布)。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.pipeline import cut_review, render  # noqa: E402

LOUD_TARGET = config.TARGET_LUFS  # LUFS,和合成管线同一个目标
LOUD_TOLERANCE = 3.0
TRUE_PEAK_MAX = -1.0      # dBTP
SILENCE_MAX = 1.5         # 秒:超过算长静音
EDGE_SILENCE_MAX = 0.6    # 秒:开头 / 结尾静音
DURATION_TOLERANCE = 0.05


def run(cmd: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def probe(video: Path) -> dict:
    out = run([str(config.FFPROBE_PATH), "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,width,height,r_frame_rate,nb_frames",
               "-of", "json", str(video)]).stdout
    return json.loads(out or "{}")


def fps_of(info: dict) -> float:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            a, _, b = str(s.get("r_frame_rate", "30/1")).partition("/")
            try:
                return float(a) / float(b or 1)
            except (ValueError, ZeroDivisionError):
                return 30.0
    return 30.0


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []   # (level, item, detail)

    def add(self, level: str, item: str, detail: str) -> None:
        self.rows.append((level, item, detail))

    @property
    def failed(self) -> bool:
        return any(level == "FAIL" for level, _, _ in self.rows)

    def markdown(self, title: str, extra: list[str]) -> str:
        mark = {"OK": "通过", "WARN": "警告", "FAIL": "不通过", "SKIP": "未验证"}
        lines = [f"# 成片 QA:{title}", "",
                 f"**结论:{'不通过,不得批准发布' if self.failed else '无不通过项'}**", "",
                 "| 结果 | 检查项 | 说明 |", "|---|---|---|"]
        lines += [f"| {mark[lv]} | {item} | {detail} |" for lv, item, detail in self.rows]
        return "\n".join(lines + [""] + extra) + "\n"


def check_decode(video: Path, rep: Report) -> None:
    proc = run([str(config.FFMPEG_PATH), "-v", "error", "-i", str(video), "-f", "null", "-"])
    err = proc.stderr.strip()
    if err:
        rep.add("FAIL", "解码完整性", err.splitlines()[0][:160] + f"(共 {len(err.splitlines())} 行)")
    else:
        rep.add("OK", "解码完整性", "ffmpeg 解码无报错")


def check_duration(info: dict, expected: float | None, rep: Report) -> float:
    dur = float(info.get("format", {}).get("duration") or 0)
    if not expected:
        rep.add("SKIP", "时长", f"成片 {dur:.1f} 秒;项目没有预估时长可比")
    elif abs(dur - expected) / expected > DURATION_TOLERANCE:
        rep.add("WARN", "时长", f"成片 {dur:.1f} 秒,脚本预估 {expected:.1f} 秒,偏差超过 5%")
    else:
        rep.add("OK", "时长", f"成片 {dur:.1f} 秒,脚本预估 {expected:.1f} 秒")
    return dur


def check_frames(video: Path, fps: float, dur: float, rep: Report) -> None:
    """用 ffprobe 逐帧时间戳查丢帧、卡顿、重复帧。"""
    out = run([str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "v:0",
               "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(video)]).stdout
    ts = []
    for line in out.splitlines():
        try:
            ts.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    if len(ts) < 2:
        rep.add("FAIL", "逐帧时间戳", "读不到视频帧")
        return
    step = 1 / fps
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > step * 2.5]
    dup = sum(1 for a, b in zip(ts, ts[1:]) if abs(b - a) < 1e-6)
    expect = dur * fps
    if gaps:
        lost = sum(b - a for a, b in gaps)
        rep.add("FAIL", "丢帧/卡顿", f"{len(gaps)} 处时间戳空洞,共 {lost:.1f} 秒(首处 {gaps[0][0]:.2f}s)")
    elif dup:
        rep.add("WARN", "重复帧", f"{dup} 个重复时间戳")
    elif abs(len(ts) - expect) / max(expect, 1) > 0.03:
        rep.add("WARN", "帧数", f"{len(ts)} 帧,按时长与帧率应约 {expect:.0f} 帧")
    else:
        rep.add("OK", "逐帧时间戳", f"{len(ts)} 帧,时间戳连续")


def check_black_freeze_scenes(video: Path, rep: Report) -> list[float]:
    """全帧自动检测:黑帧、静止帧、场景切换点。返回切换点秒数。"""
    proc = run([str(config.FFMPEG_PATH), "-v", "info", "-i", str(video), "-vf",
                "blackdetect=d=0.2:pic_th=0.98,freezedetect=n=-55dB:d=2,select='gt(scene,0.35)',showinfo",
                "-an", "-f", "null", "-"])
    text = proc.stderr
    blacks = re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", text)
    freezes = re.findall(r"freeze_start: ([\d.]+)", text)
    scenes = [float(m) for m in re.findall(r"pts_time:([\d.]+)", text)]
    if blacks:
        rep.add("FAIL", "黑帧", f"{len(blacks)} 段,首处 {blacks[0][0]}s–{blacks[0][1]}s")
    else:
        rep.add("OK", "黑帧", "没有")
    if freezes:
        rep.add("WARN", "静止画面", f"{len(freezes)} 段超过 2 秒几乎不动,首处 {freezes[0]}s(确认是不是卡住)")
    else:
        rep.add("OK", "静止画面", "没有超过 2 秒的静止段")
    return sorted(set(scenes))


def check_audio(video: Path, dur: float, rep: Report, project_dir: Path) -> None:
    mix = project_dir / "_render" / "mix.wav"
    rep.add("OK" if mix.is_file() else "WARN", "混音文件", "_render/mix.wav 存在" if mix.is_file()
            else "没找到 _render/mix.wav(可能已清理,不影响成片检查)")
    packets = run([str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "a:0",
                   "-show_entries", "packet=pts_time,duration_time", "-of", "csv=p=0", str(video)]).stdout
    problem = render.audio_problem(packets, dur)
    rep.add("FAIL" if problem else "OK", "音轨完整性", problem or "音频包连续,时长够")
    if problem == "成片没有音轨":
        return

    ebu = run([str(config.FFMPEG_PATH), "-nostats", "-i", str(video), "-af", "ebur128=peak=true",
               "-vn", "-f", "null", "-"]).stderr
    lufs = re.findall(r"I:\s+(-?[\d.]+) LUFS", ebu)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", ebu)
    if lufs:
        v = float(lufs[-1])
        level = "OK" if abs(v - LOUD_TARGET) <= LOUD_TOLERANCE else "WARN"
        rep.add(level, "响度", f"{v:.1f} LUFS(目标约 {LOUD_TARGET:.0f})")
    if peak:
        v = float(peak[-1])
        rep.add("OK" if v <= TRUE_PEAK_MAX else "WARN", "真峰值", f"{v:.1f} dBTP(上限 {TRUE_PEAK_MAX})")

    sil = run([str(config.FFMPEG_PATH), "-nostats", "-i", str(video), "-af",
               f"silencedetect=noise=-45dB:d={SILENCE_MAX}", "-vn", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", sil)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", sil)]
    long_ = [(s, e) for s, e in zip(starts, ends) if e - s > SILENCE_MAX and s > EDGE_SILENCE_MAX]
    if long_:
        rep.add("WARN", "长静音", f"{len(long_)} 段超过 {SILENCE_MAX}s,首处 {long_[0][0]:.1f}s–{long_[0][1]:.1f}s")
    else:
        rep.add("OK", "长静音", "没有超过 1.5 秒的静音")
    head = run([str(config.FFMPEG_PATH), "-nostats", "-t", "2", "-i", str(video), "-af",
                f"silencedetect=noise=-45dB:d={EDGE_SILENCE_MAX}", "-vn", "-f", "null", "-"]).stderr
    if re.search(r"silence_start: 0(\.0+)?\b", head):
        rep.add("WARN", "开头静音", f"开头超过 {EDGE_SILENCE_MAX}s 没声音,前 3 秒留不住人")

    stats = run([str(config.FFMPEG_PATH), "-nostats", "-i", str(video), "-af",
                 "astats=metadata=0:reset=0", "-vn", "-f", "null", "-"]).stderr
    clip = re.findall(r"Number of (?:Infs|NaNs|clipped samples)[^:]*:\s*(\d+)", stats)
    clipped = [int(x) for x in re.findall(r"Peak count:\s*(\d+)", stats)]
    if any(int(c) > 0 for c in clip):
        rep.add("WARN", "削波/异常采样", "检测到削波或异常采样,听一下爆音")
    elif clipped and max(clipped) > 50:
        rep.add("WARN", "削波", f"峰值样本出现 {max(clipped)} 次,可能轻微削波")
    else:
        rep.add("OK", "削波", "没有")


def check_narration(project_dir: Path, rep: Report) -> None:
    """旁白对稿:本地没有语音识别时降级,只检查字幕/旁白文本里屏上数字是否都被说到。"""
    narr = project_dir / "narration.json"
    if not narr.is_file():
        rep.add("SKIP", "旁白对稿", "没有 narration.json;需要人工听感抽查")
        return
    rep.add("SKIP", "旁白实读对照", "本机没有接入语音识别,未验证实读;请抽听开头 10 秒、数字句和结尾")


def contact_sheets(video: Path, out: Path, fps: float, scenes: list[float], dur: float) -> list[Path]:
    """每秒 1 帧 + 每个切换点前后各 1 帧,拼成 4x3 联系表(带时间戳)。"""
    frames_dir = out / "frames_sampled"
    frames_dir.mkdir(parents=True, exist_ok=True)
    stamps = {round(t, 1) for t in (i / fps for i in range(int(dur * fps)))}
    for s in scenes:
        stamps.update({round(max(0, s - 0.1), 1), round(s + 0.1, 1)})
    picked = sorted(t for t in stamps if t <= dur)
    jobs = []
    for i, t in enumerate(picked):
        dst = frames_dir / f"f{i:05d}_{t:07.1f}.jpg"
        jobs.append((t, dst))
        run([str(config.FFMPEG_PATH), "-v", "error", "-y", "-ss", f"{t}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=360:-1", str(dst)], timeout=60)
    sheets_dir = out / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    sheets = []
    for n in range(0, len(jobs), 12):
        chunk = [(t, d) for t, d in jobs[n:n + 12] if d.is_file()]
        sheet = sheets_dir / f"sheet_{n // 12:03d}.jpg"
        if chunk and _paste_sheet(chunk, sheet):
            sheets.append(sheet)
    if not sheets:
        # 原来抽帧不看返回码:ffmpeg 出错时联系表是空的,报告照样写「0 张,逐张查看」
        raise RuntimeError(f"联系表一张都没出来(要抽 {len(jobs)} 帧,ffmpeg: {config.FFMPEG_PATH})")
    return sheets


def _paste_sheet(chunk: list[tuple[float, Path]], sheet: Path, cols: int = 4) -> bool:
    """拼一张联系表(实现在 `cut_review.paste_sheet`,服务里的成片机审也用它)。"""
    return cut_review.paste_sheet(chunk, sheet, cols)


def export_all_frames(video: Path, out: Path) -> int:
    d = out / "frames"
    d.mkdir(parents=True, exist_ok=True)
    run([str(config.FFMPEG_PATH), "-v", "error", "-y", "-i", str(video), "-vf", "scale=360:-1",
         "-q:v", "4", str(d / "%06d.jpg")], timeout=3600)
    return len(list(d.glob("*.jpg")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--artifact", default="")
    ap.add_argument("--fps", type=float, default=1.0, help="联系表采样帧率,默认每秒 1 帧")
    ap.add_argument("--all-frames", action="store_true", help="每一帧单独导出(很慢)")
    args = ap.parse_args()

    if not config.FFMPEG_PATH or not config.FFPROBE_PATH:
        print("找不到 ffmpeg / ffprobe,请在 .env 配置 FFMPEG_BIN", file=sys.stderr)
        return 2
    pdir = config.project_dir(args.project_id)
    name = args.artifact or next(
        (n for n in ("output_vertical.mp4", "output.mp4") if (pdir / n).is_file()), "")
    video = pdir / name
    if not name or not video.is_file():
        print(f"项目 {args.project_id} 里没有成片", file=sys.stderr)
        return 2

    expected = None
    narr = pdir / "narration.json"
    if narr.is_file():
        try:
            data = json.loads(narr.read_text(encoding="utf-8"))
            expected = float(data.get("total_duration") or 0) or None
        except (ValueError, OSError, AttributeError):
            expected = None

    out = pdir / "_render" / "qa"
    out.mkdir(parents=True, exist_ok=True)
    rep = Report()
    info = probe(video)
    fps = fps_of(info)
    check_decode(video, rep)
    dur = check_duration(info, expected, rep)
    check_frames(video, fps, dur, rep)
    scenes = check_black_freeze_scenes(video, rep)
    check_audio(video, dur, rep, pdir)
    check_narration(pdir, rep)
    try:
        sheets = contact_sheets(video, out, args.fps, scenes, dur)
    except RuntimeError as exc:
        sheets = []
        rep.add("FAIL", "联系表", str(exc))
    extra = [f"## 联系表({len(sheets)} 张,逐张查看)", ""]
    extra += [f"- {s}" for s in sheets]
    extra += ["", f"场景切换点 {len(scenes)} 个:" + ", ".join(f"{s:.1f}s" for s in scenes[:60]),
              "", "## 画面人工核对清单", "",
              "- 花屏 / 白帧 / 切换闪帧", "- 字幕错字、被截断、遮挡主体", "- 屏上数字都在旁白里说过",
              "- 画面素材与车型是否对得上", "- 文字溢出画面边缘"]
    if args.all_frames:
        extra += ["", f"已导出全部帧:{export_all_frames(video, out)} 张,位于 {out / 'frames'}"]
    report = out / "qa_report.md"
    report.write_text(rep.markdown(f"{args.project_id} / {name}", extra), encoding="utf-8")
    print(f"报告:{report}")
    print(f"结论:{'不通过' if rep.failed else '无不通过项'}")
    for lv, item, detail in rep.rows:
        print(f"[{lv}] {item}: {detail}")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
