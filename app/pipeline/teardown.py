"""参考视频逐帧拆解:对一条成片(或手机录屏)抽出节奏/口播/字幕/响度,生成可读报告。

用法:python -m app.pipeline.teardown <mp4> --out analysis/teardown_i6 [--chapters chapters.json]

只做"量化 + 备好关键帧",版式/素材类型这类需要看图的判断由人(或 VLM)读联系表来做。
产出(都在 --out 下):
  proxy.mp4 / audio.wav   裁掉录屏边框后的 960 宽代理
  metrics.json            机读指标
  sheets/*.jpg            2fps 联系表;opening/ 前 3 秒 10fps
  report.html             时间轴 + 联系表 + 口播稿

风格指标(不出报告,只打印 JSON;对标基线和成片验收用,见 `style_metrics`):
  python -m app.pipeline.teardown a.mp4 b.mp4 --style [--speech]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import config
from ..pipeline import voice_qa

PROXY_W = 960
SHEET_STEP = 0.5          # 联系表抽帧间隔(秒)
SHEET_COLS, SHEET_ROWS = 6, 6
OPENING_SECONDS = 3.0
OPENING_FPS = 10
SCENE_THRESHOLD = 0.3
SUB_BAND = (0.78, 0.97)   # 字幕大致所在的画面纵向区间

# 风格指标(`style_metrics`)。动态 / 切的阈值与 2026-11-18 在抖音上抽样对标片时的口径一致
HOOK_SECONDS = 5.0        # 开场钩子段:起点在这之前的镜头单独统计
MOTION_STEP = 0.5         # 抽小图的间隔(秒)
MOTION_SIZE = (64, 36)    # 小图尺寸(宽, 高),灰度
MOTION_DIFF = 8.0         # 相邻小图平均像素差(0–255)大于它算"动"
CUT_DIFF = 70.0           # 大于它算"切"(换镜头),不计入动 / 静
STILL_MIN_SECONDS = 1.0   # 连续这么久没动才算一段静帧
CUT_MERGE_SECONDS = 0.25  # 场景检测相隔不到这么久的切点并成一个(白闪进出会报两次)
SCENE_WIDTH = 960         # 场景检测前先缩到这个宽度,和 `run` 的代理同口径
# 音乐底粗判(`music_bed`):看每 10 秒窗口里最安静的 10% 帧(多半是句间、词间停顿)
# 比旁白低多少、频谱是不是音调性的。2026-11-18 用本库成片标定(150–3500 Hz,窗口中位数):
#   没配乐(11-17、11-18 BGM 关的成片 ×3,纯旁白 narration.wav):停顿比旁白低 61–64 dB,平坦度 0.21–0.25
#   配了乐(10-30 至 11-04 BGM 开的成片 ×4):只低 33–40 dB,平坦度 0.01–0.05;八条全判对
# 只看电平不够:连续合成的 TTS 停顿极短,0.1 秒帧的"底"只比旁白低 20 多 dB,干声也会误判成有乐
BED_FRAME = 512             # 一帧采样数(16 kHz 下 32 毫秒),帧移一半
BED_BAND = (150.0, 3500.0)  # 只看这段频率:避开低频嗡声和高频底噪
BED_WINDOW = 10.0           # 窗口秒数
BED_QUIET = 10.0            # 每窗口取最安静的这个百分比的帧
BED_GAP_DB = 50.0           # 停顿离旁白(全片 p95)不到这么多 dB ……
BED_TONAL = 0.1             # …… 且频谱平坦度低于它(音调性,白噪声约 0.5):这个窗口垫着乐
CLEAN_GAP_DB = 55.0         # 停顿比旁白低超过这么多,或
CLEAN_FLAT = 0.2            # 频谱平坦度高于它(底噪 / 环境声):这个窗口没垫乐
BED_YES = 0.7               # 垫乐窗口 ≥70% 判有、没垫窗口 ≥70% 判无,其余不下结论


def _ff(*args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run([str(config.FFMPEG_PATH or "ffmpeg"), "-v", "error", "-y", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def probe(path: Path) -> dict:
    out = subprocess.run(
        [str(config.FFPROBE_PATH or "ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,codec_name:format=duration,bit_rate",
         "-of", "json", str(path)], capture_output=True, text=True, timeout=60).stdout
    d = json.loads(out)
    s, f = d["streams"][0], d["format"]
    num, den = s["r_frame_rate"].split("/")
    return {"width": s["width"], "height": s["height"], "codec": s["codec_name"],
            "fps": round(int(num) / int(den), 2), "duration": float(f["duration"]),
            "bit_rate": int(f.get("bit_rate") or 0)}


# --------------------------------------------------------------------------
# 找出真正的视频区域(手机录屏有黑边/悬浮按钮)
# --------------------------------------------------------------------------
def detect_region(path: Path, duration: float) -> tuple[int, int, int, int]:
    """多帧差分:随时间变化的列/行才是视频本体。返回 (x, y, w, h),已对齐到偶数。"""
    import io

    import numpy as np
    from PIL import Image

    frames = []
    for k in range(1, 9):
        r = subprocess.run(
            [str(config.FFMPEG_PATH or "ffmpeg"), "-v", "error", "-ss", f"{duration * k / 9:.2f}", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=iw/4:ih/4", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=120)
        if r.stdout:
            frames.append(np.asarray(Image.open(io.BytesIO(r.stdout)).convert("L")).astype(float))
    h, w = frames[0].shape
    diff = np.mean([np.abs(a - b) for a, b in zip(frames, frames[1:])], axis=0)
    cols, rows = diff.mean(0), diff.mean(1)
    cx = np.where(cols > cols.max() * 0.25)[0]
    cy = np.where(rows > rows.max() * 0.25)[0]
    x0, x1, y0, y1 = int(cx.min()), int(cx.max()) + 1, int(cy.min()), int(cy.max()) + 1
    x, y, ww, hh = x0 * 4, y0 * 4, (x1 - x0) * 4, (y1 - y0) * 4
    return x & ~1, y & ~1, ww & ~1, hh & ~1


def build_proxy(src: Path, out: Path, region: tuple[int, int, int, int]) -> Path:
    proxy = out / "proxy.mp4"
    if proxy.exists():
        return proxy
    x, y, w, h = region
    r = _ff("-i", str(src), "-vf", f"crop={w}:{h}:{x}:{y},scale={PROXY_W}:-2,fps=30", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(proxy))
    if r.returncode:
        raise RuntimeError(r.stderr[-500:])
    return proxy


def extract_audio(src: Path, out: Path) -> Path:
    wav = out / "audio.wav"
    if not wav.exists():
        r = _ff("-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(wav))
        if r.returncode:
            raise RuntimeError(r.stderr[-500:])
    return wav


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def scene_cuts(proxy: Path, threshold: float = SCENE_THRESHOLD,
               width: int | None = None) -> list[float]:
    """场景检测的切点(秒)。`width` 给了就先缩到这个宽度再检测(原片直接测时用)。"""
    vf = f"select='gt(scene,{threshold})',showinfo"
    if width:
        vf = f"scale={width}:-2," + vf
    p = subprocess.run(
        [str(config.FFMPEG_PATH or "ffmpeg"), "-hide_banner", "-i", str(proxy), "-an",
         "-vf", vf, "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    return [round(float(m), 2) for m in re.findall(r"pts_time:([\d.]+)", p.stderr)]


def cut_density(cuts: list[float], duration: float, bucket: float = 10.0) -> list[dict]:
    n = int(duration // bucket) + 1
    return [{"t": round(i * bucket, 1), "cuts": sum(1 for c in cuts if i * bucket <= c < (i + 1) * bucket)}
            for i in range(n)]


def transcribe(wav: Path, model_name: str) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segs, _ = model.transcribe(voice_qa._decode_16k(wav), language="zh", beam_size=5, vad_filter=True,
                               initial_prompt="以下是普通话的汽车评测解说。")
    return [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]


def speech_stats(segs: list[dict], duration: float) -> dict:
    # 只数字母 / 数字 / 汉字:转写里的全角逗号、引号不算字
    chars = sum(len(re.sub(r"[\W_]", "", s["text"])) for s in segs)
    spoken = sum(s["end"] - s["start"] for s in segs)
    lens = sorted(len(s["text"]) for s in segs) or [0]
    return {"chars": chars, "spoken_seconds": round(spoken, 1),
            "coverage": round(spoken / duration, 3) if duration else 0,
            "chars_per_second": round(chars / spoken, 2) if spoken else 0,
            "sentence_len_median": lens[len(lens) // 2], "sentences": len(segs)}


def loudness(wav: Path) -> dict:
    # -vn:直接喂 mp4 时不解视频
    p = subprocess.run([str(config.FFMPEG_PATH or "ffmpeg"), "-hide_banner", "-i", str(wav),
                        "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    tail = p.stderr[-800:]
    pick = lambda k: (re.search(rf"{k}:\s+(-?[\d.]+)", tail) or [None, None])[1]  # noqa: E731
    return {"integrated_lufs": pick("I"), "lra": pick("LRA"), "true_peak_dbfs": pick("Peak")}


def subtitle_style(proxy: Path, duration: float) -> dict:
    """在字幕带里找近白色像素:估字幕占画高比、是否有深色底条、位置。采 40 帧取中位数。"""
    import io

    import numpy as np
    from PIL import Image

    heights, bg_dark, centers = [], [], []
    for k in range(40):
        t = duration * (k + 0.5) / 40
        r = subprocess.run([str(config.FFMPEG_PATH or "ffmpeg"), "-v", "error", "-ss", f"{t:.2f}", "-i", str(proxy),
                            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=60)
        if not r.stdout:
            continue
        a = np.asarray(Image.open(io.BytesIO(r.stdout)).convert("L"))
        H = a.shape[0]
        band = a[int(H * SUB_BAND[0]):int(H * SUB_BAND[1])]
        white = (band > 235).sum(1)
        rows = np.where(white > band.shape[1] * 0.004)[0]
        if len(rows) < 6:
            continue
        heights.append((rows.max() - rows.min()) / H)
        centers.append((rows.mean() + int(H * SUB_BAND[0])) / H)
        bg_dark.append(float(np.median(band[rows.min():rows.max()])) < 70)
    if not heights:
        return {"samples": 0}
    return {"samples": len(heights), "text_height_ratio_median": round(float(np.median(heights)), 3),
            "center_y_ratio_median": round(float(np.median(centers)), 3),
            "dark_backing_share": round(sum(bg_dark) / len(bg_dark), 2)}


# --------------------------------------------------------------------------
# 风格指标:切点分布 / 动态占比 / 静帧 / 响度 / 音乐底(只用 ffmpeg + numpy)
# --------------------------------------------------------------------------
def _pct(xs: list[float], q: float) -> float | None:
    import numpy as np

    return round(float(np.percentile(xs, q)), 2) if xs else None


def merge_cuts(cuts: list[float], gap: float = CUT_MERGE_SECONDS) -> list[float]:
    """相隔不到 `gap` 秒的切点只留第一个(白闪、快闪进出会连报两次);贴着片头的也丢掉。"""
    out: list[float] = []
    last = 0.0
    for c in sorted(cuts):
        if c - last >= gap:
            out.append(c)
            last = c
    return out


def shot_stats(cuts: list[float], duration: float, hook: float = HOOK_SECONDS) -> dict:
    """切点 → 每刀秒数的 p10 / 中位数 / p90。起点在 `hook` 秒之前的镜头算钩子段,其余算正片。"""
    bounds = [0.0, *[c for c in cuts if 0 < c < duration], duration]
    shots = [(a, b - a) for a, b in zip(bounds, bounds[1:]) if b > a]

    def agg(xs: list[float]) -> dict:
        return {"shots": len(xs), "p10": _pct(xs, 10), "median": _pct(xs, 50),
                "p90": _pct(xs, 90)}

    return {"hook": {"seconds": hook, **agg([n for s, n in shots if s < hook])},
            "body": agg([n for s, n in shots if s >= hook])}


def gray_frames(path: Path, step: float = MOTION_STEP,
                size: tuple[int, int] = MOTION_SIZE) -> Any:
    """每 `step` 秒抽一张 `size` 的灰度小图,返回 (n, 高, 宽) 的 uint8 数组。"""
    import numpy as np

    w, h = size
    p = subprocess.run(
        [str(config.FFMPEG_PATH or "ffmpeg"), "-v", "error", "-i", str(path), "-an", "-sn", "-dn",
         "-vf", f"fps={1 / step},scale={w}:{h}:flags=area,format=gray",
         "-f", "rawvideo", "-"], capture_output=True, timeout=1800)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-500:])
    a = np.frombuffer(p.stdout, dtype=np.uint8)
    n = a.size // (w * h)
    return a[: n * w * h].reshape(n, h, w)


def motion_profile(frames: Any, step: float = MOTION_STEP) -> dict:
    """相邻小图的平均像素差 → 每个间隔判 静(≤8)/ 动 / 切(>70)。

    dynamic_ratio = 动 /(动 + 静),切的间隔不计;still_ratio = 落在连续 ≥1 秒
    没动的段里的间隔 / 全部间隔。慢推的静图(推镜幅度在小图上不到 1 像素)算静。
    """
    import numpy as np

    if len(frames) < 2:
        return {"step": step, "samples": len(frames), "static": 0, "moving": 0, "cut": 0,
                "diff_median": None, "dynamic_ratio": None, "still_ratio": None}
    d = np.abs(np.diff(frames.astype(np.int16), axis=0)).mean(axis=(1, 2))
    cut = d > CUT_DIFF
    moving = (d > MOTION_DIFF) & ~cut
    static = ~moving & ~cut
    need = max(1, round(STILL_MIN_SECONDS / step))
    still = run = 0
    for s in [*static.tolist(), False]:
        if s:
            run += 1
            continue
        if run >= need:
            still += run
        run = 0
    n_mov, n_sta = int(moving.sum()), int(static.sum())
    return {"step": step, "samples": len(frames), "static": n_sta, "moving": n_mov,
            "cut": int(cut.sum()), "diff_median": round(float(np.median(d)), 2),
            "dynamic_ratio": round(n_mov / (n_mov + n_sta), 3) if n_mov + n_sta else None,
            "still_ratio": round(still / len(d), 3)}


def music_bed(x: Any, sr: int = 16000) -> dict:
    """粗判有没有持续的音乐底。`x` 是单声道 float 采样(默认 16 kHz)。

    干声旁白的停顿里只剩底噪(很低、频谱平),垫了 BGM 的停顿里还响着乐(不太低、
    频谱有峰)。每 10 秒窗口取最安静 10% 帧,看它们比旁白(全片 p95)低多少 dB、
    频谱平坦度多少,分成"垫乐 / 没垫 / 说不清"三种窗口(阈值见 BED_* / CLEAN_*)。
    垫乐窗口 ≥70% 判有,没垫窗口 ≥70% 判无;只在部分段落垫乐、或环境声 / 现场原声
    和乐分不清时返回 None,不硬下结论。
    """
    import numpy as np

    n, hop = BED_FRAME, BED_FRAME // 2
    per = int(BED_WINDOW * sr / hop)                      # 每窗口帧数
    x = np.asarray(x, dtype=np.float64)
    m = (len(x) - n) // hop + 1 if len(x) >= n else 0
    if m < per:
        return {"verdict": None, "windows": 0}
    freqs = np.fft.rfftfreq(n, 1 / sr)
    band = (freqs >= BED_BAND[0]) & (freqs <= BED_BAND[1])
    win = np.hanning(n)
    levels, quiet = [], []                                # 每窗口:各帧电平;最安静帧的(电平, 平坦度)
    for s in range(0, m - per + 1, per):
        idx = (s + np.arange(per))[:, None] * hop + np.arange(n)[None, :]
        p = np.abs(np.fft.rfft(x[idx] * win, axis=1))[:, band] ** 2 + 1e-12
        lvl = 10 * np.log10(p.sum(axis=1))
        q = lvl <= np.percentile(lvl, BED_QUIET)
        flat = np.exp(np.log(p[q]).mean(axis=1)) / p[q].mean(axis=1)
        levels.append(lvl)
        quiet.append((float(np.median(lvl[q])), float(np.median(flat))))
    voice = float(np.percentile(np.concatenate(levels), 95))
    gaps = [voice - lv for lv, _ in quiet]
    bed = [g < BED_GAP_DB and f < BED_TONAL for g, (_, f) in zip(gaps, quiet)]
    clean = [g > CLEAN_GAP_DB or f > CLEAN_FLAT for g, (_, f) in zip(gaps, quiet)]
    share, dry = sum(bed) / len(bed), sum(clean) / len(clean)
    verdict = True if share >= BED_YES else False if dry >= BED_YES else None
    return {"verdict": verdict, "bed_share": round(share, 2), "dry_share": round(dry, 2),
            "windows": len(bed), "quiet_gap_db": round(float(np.median(gaps)), 1),
            "quiet_flatness": round(float(np.median([f for _, f in quiet])), 3)}


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def style_metrics(mp4: Path) -> dict:
    """一条成片的风格指标(对标基线、成片验收用)。只用 ffmpeg + numpy,不转写、不看图。

    duration / width / height / fps   基本信息
    cuts             场景检测切点(秒,0.25 秒内的重复已并掉)
    cuts_per_minute  每分钟切点数
    hook / body      每刀秒数 {shots, p10, median, p90};起点在前 5 秒的镜头算 hook,其余算正片
    motion           0.5 秒一张 64×36 灰度图的判定计数 {step, samples, static, moving, cut, diff_median}
    dynamic_ratio    动态画面占比 = 动 /(动 + 静)
    still_ratio      静帧段占比 = 落在连续 ≥1 秒没动的段里的时间占比
    lufs / lra / true_peak   整片 EBU R128 响度(LUFS)、响度范围(LU)、真峰值(dBTP)
    music_bed        有持续音乐底 True / 没有 False / 判不准 None;判据在 music_bed_detail
    """
    info = probe(mp4)
    dur = info["duration"]
    cuts = [c for c in merge_cuts(scene_cuts(mp4, width=SCENE_WIDTH))
            if c < dur - CUT_MERGE_SECONDS]
    mot = motion_profile(gray_frames(mp4))
    loud = loudness(mp4)
    try:
        bed = music_bed(voice_qa._decode_16k(mp4))
    except Exception as exc:  # noqa: BLE001 - 没有音轨 / 解不出来:这一项不下结论
        bed = {"verdict": None, "error": str(exc)[:200]}
    return {
        "duration": round(dur, 2), "width": info["width"], "height": info["height"],
        "fps": info["fps"], "cuts": cuts,
        "cuts_per_minute": round(len(cuts) / dur * 60, 1) if dur else 0.0,
        **shot_stats(cuts, dur),
        "motion": {k: mot[k] for k in ("step", "samples", "static", "moving", "cut", "diff_median")},
        "dynamic_ratio": mot["dynamic_ratio"], "still_ratio": mot["still_ratio"],
        "lufs": _num(loud["integrated_lufs"]), "lra": _num(loud["lra"]),
        "true_peak": _num(loud["true_peak_dbfs"]),
        "music_bed": bed.get("verdict"), "music_bed_detail": bed,
    }


def speech_rate(mp4: Path, model: str | None = None) -> dict:
    """旁白语速。faster-whisper 转写(默认和 `word_timing` 同一个模型 VOICE_QA_MODEL)。

    chars_per_second = 字数 ÷ 转写段时长之和(段内短停顿算在内,同 `speech_stats`);
    cps_span = 字数 ÷ 首段起到末段止,和 `script_gen.fit` 的「字数 ÷ 成片秒数」口径最接近;
    cps_overall = 字数 ÷ 整片时长。字数只数汉字 / 字母 / 数字。
    """
    name = model or config.VOICE_QA_MODEL
    dur = probe(mp4)["duration"]
    segs = transcribe(mp4, name)
    st = speech_stats(segs, dur)
    span = segs[-1]["end"] - segs[0]["start"] if segs else 0.0
    return {**st, "model": name, "span_seconds": round(span, 1),
            "cps_span": round(st["chars"] / span, 2) if span else 0.0,
            "cps_overall": round(st["chars"] / dur, 2) if dur else 0.0}


# --------------------------------------------------------------------------
# 联系表
# --------------------------------------------------------------------------
def contact_sheets(proxy: Path, out: Path, duration: float) -> list[str]:
    sheets = out / "sheets"
    sheets.mkdir(exist_ok=True)
    per = SHEET_COLS * SHEET_ROWS
    span = per * SHEET_STEP
    names = []
    for i in range(int(duration // span) + 1):
        name = f"sheet_{i:02d}.jpg"
        names.append(name)
        if (sheets / name).exists():
            continue
        _ff("-ss", f"{i * span:.2f}", "-t", f"{span:.2f}", "-i", str(proxy),
            "-vf", f"fps={1 / SHEET_STEP},scale=320:-2,tile={SHEET_COLS}x{SHEET_ROWS}", "-frames:v", "1",
            "-q:v", "4", str(sheets / name))
    op = out / "opening"
    op.mkdir(exist_ok=True)
    if not any(op.iterdir()):
        _ff("-t", str(OPENING_SECONDS), "-i", str(proxy), "-vf", f"fps={OPENING_FPS},scale=480:-2", "-q:v", "3",
            str(op / "f_%03d.jpg"))
    return names


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def _mmss(t: float) -> str:
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def render_report(out: Path, m: dict) -> Path:
    e = html.escape
    chap = m.get("chapters") or []
    cuts = m["cuts"]
    dens = m["cut_density"]
    peak = max((d["cuts"] for d in dens), default=1) or 1
    bars = "".join(
        f'<div class="b" title="{_mmss(d["t"])} 起 {d["cuts"]} 个切点" style="height:{4 + 56 * d["cuts"] / peak:.0f}px"></div>'
        for d in dens)
    chap_rows = ""
    for i, c in enumerate(chap):
        end = chap[i + 1]["t"] if i + 1 < len(chap) else m["probe"]["duration"]
        n = sum(1 for x in cuts if c["t"] <= x < end)
        words = sum(len(re.sub(r"\W", "", s["text"])) for s in m["transcript"] if c["t"] <= s["start"] < end)
        chap_rows += (f"<tr><td>{_mmss(c['t'])}</td><td>{e(c['title'])}</td><td>{end - c['t']:.0f}s</td>"
                      f"<td>{n}</td><td>{(end - c['t']) / max(n, 1):.1f}s</td><td>{words / max(end - c['t'], 1):.1f}</td></tr>")
    tr = "".join(f"<p><b>{_mmss(s['start'])}</b> {e(s['text'])}</p>" for s in m["transcript"])
    sheets = "".join(f'<img src="sheets/{n}">' for n in m["sheets"])
    opening = "".join(f'<img src="opening/{p.name}">' for p in sorted((out / "opening").glob("*.jpg")))
    pr = m["probe"]
    doc = f"""<!doctype html><meta charset="utf-8"><title>拆解 {e(out.name)}</title>
<style>body{{font:14px/1.6 system-ui;max-width:1200px;margin:24px auto;padding:0 16px}}
.b{{width:12px;background:#e8590c;display:inline-block;margin-right:2px;vertical-align:bottom}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 10px}}
img{{max-width:100%;margin:2px}}#op img{{width:160px}}pre{{background:#f4f4f4;padding:10px}}</style>
<h1>拆解:{e(out.name)}</h1>
<p>时长 {_mmss(pr['duration'])}({pr['duration']:.0f}s) · 视频区 {m['region'][2]}×{m['region'][3]} · 切点 {len(cuts)} 个 ·
平均镜长 {pr['duration'] / max(len(cuts), 1):.1f}s · 语速 {m['speech']['chars_per_second']} 字/秒 · 口播覆盖 {m['speech']['coverage']:.0%}</p>
<h2>镜头密度(每 10 秒切点数)</h2><div>{bars}</div>
<h2>分章</h2><table><tr><th>起</th><th>章</th><th>时长</th><th>切点</th><th>平均镜长</th><th>字/秒</th></tr>{chap_rows}</table>
<h2>字幕 / 响度</h2><pre>{e(json.dumps({'subtitle': m['subtitle'], 'loudness': m['loudness']}, ensure_ascii=False, indent=1))}</pre>
<h2>前 3 秒(10fps)</h2><div id="op">{opening}</div>
<h2>联系表(2fps,每格 0.5s,6×6)</h2>{sheets}
<h2>口播稿</h2>{tr}"""
    p = out / "report.html"
    p.write_text(doc, encoding="utf-8")
    return p


def run(src: Path, out: Path, chapters: list[dict] | None = None, model: str = "small") -> dict:
    out.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    region = detect_region(src, info["duration"])
    proxy = build_proxy(src, out, region)
    wav = extract_audio(src, out)
    dur = probe(proxy)["duration"]
    cuts = scene_cuts(proxy)
    segs = transcribe(wav, model)
    m = {"probe": {**info, "duration": dur}, "region": list(region), "cuts": cuts,
         "cut_density": cut_density(cuts, dur), "avg_shot_seconds": round(dur / max(len(cuts), 1), 2),
         "transcript": segs, "speech": speech_stats(segs, dur), "loudness": loudness(wav),
         "subtitle": subtitle_style(proxy, dur), "chapters": chapters or [],
         "sheets": contact_sheets(proxy, out, dur)}
    (out / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    render_report(out, m)
    return m


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", type=Path, nargs="+")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--chapters", type=Path, help='JSON: [{"t": 0, "title": "引言"}, ...]')
    ap.add_argument("--model", help="faster-whisper 模型名(报告默认 small,--speech 默认 VOICE_QA_MODEL)")
    ap.add_argument("--style", action="store_true", help="只打印 style_metrics 的 JSON(可多条)")
    ap.add_argument("--speech", action="store_true",
                    help="配合 --style:再转写测语速(模型默认 VOICE_QA_MODEL)")
    a = ap.parse_args()
    if a.style:
        for v in a.video:
            row: dict[str, Any] = {"file": str(v), **style_metrics(v)}
            if a.speech:
                row["speech"] = speech_rate(v, a.model)
            print(json.dumps(row, ensure_ascii=False))
        return
    if a.out is None or len(a.video) != 1:
        ap.error("拆解报告要一条视频加 --out;多条只能配 --style")
    ch = json.loads(a.chapters.read_text(encoding="utf-8")) if a.chapters else None
    m = run(a.video[0], a.out, ch, a.model or "small")
    print(f"切点 {len(m['cuts'])} · 平均镜长 {m['avg_shot_seconds']}s · 语速 {m['speech']['chars_per_second']} 字/秒 -> {a.out / 'report.html'}")


if __name__ == "__main__":
    main()
