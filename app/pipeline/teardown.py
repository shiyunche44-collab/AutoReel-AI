"""参考视频逐帧拆解:对一条成片(或手机录屏)抽出节奏/口播/字幕/响度,生成可读报告。

用法:python -m app.pipeline.teardown <mp4> --out analysis/teardown_i6 [--chapters chapters.json]

只做"量化 + 备好关键帧",版式/素材类型这类需要看图的判断由人(或 VLM)读联系表来做。
产出(都在 --out 下):
  proxy.mp4 / audio.wav   裁掉录屏边框后的 960 宽代理
  metrics.json            机读指标
  sheets/*.jpg            2fps 联系表;opening/ 前 3 秒 10fps
  report.html             时间轴 + 联系表 + 口播稿
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
from pathlib import Path

from .. import config
from ..pipeline import voice_qa

PROXY_W = 960
SHEET_STEP = 0.5          # 联系表抽帧间隔(秒)
SHEET_COLS, SHEET_ROWS = 6, 6
OPENING_SECONDS = 3.0
OPENING_FPS = 10
SCENE_THRESHOLD = 0.3
SUB_BAND = (0.78, 0.97)   # 字幕大致所在的画面纵向区间


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
    import numpy as np
    from PIL import Image
    import io

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
def scene_cuts(proxy: Path, threshold: float = SCENE_THRESHOLD) -> list[float]:
    p = subprocess.run(
        [str(config.FFMPEG_PATH or "ffmpeg"), "-hide_banner", "-i", str(proxy), "-an",
         "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
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
    chars = sum(len(re.sub(r"[\s,。!?、,.!?]", "", s["text"])) for s in segs)
    spoken = sum(s["end"] - s["start"] for s in segs)
    lens = sorted(len(s["text"]) for s in segs) or [0]
    return {"chars": chars, "spoken_seconds": round(spoken, 1),
            "coverage": round(spoken / duration, 3) if duration else 0,
            "chars_per_second": round(chars / spoken, 2) if spoken else 0,
            "sentence_len_median": lens[len(lens) // 2], "sentences": len(segs)}


def loudness(wav: Path) -> dict:
    p = subprocess.run([str(config.FFMPEG_PATH or "ffmpeg"), "-hide_banner", "-i", str(wav),
                        "-af", "ebur128=peak=true", "-f", "null", "-"],
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
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--chapters", type=Path, help='JSON: [{"t": 0, "title": "引言"}, ...]')
    ap.add_argument("--model", default="small", help="faster-whisper 模型名")
    a = ap.parse_args()
    ch = json.loads(a.chapters.read_text(encoding="utf-8")) if a.chapters else None
    m = run(a.video, a.out, ch, a.model)
    print(f"切点 {len(m['cuts'])} · 平均镜长 {m['avg_shot_seconds']}s · 语速 {m['speech']['chars_per_second']} 字/秒 -> {a.out / 'report.html'}")


if __name__ == "__main__":
    main()
