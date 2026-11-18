"""BGM 节拍分析:BPM、拍点、强拍(小节线)、起势点(drop)。只用 numpy,不引入 librosa / numba / scipy。

流程(每首曲子离线算一次,结果写回曲库目录的 `_licenses.json`,配乐编辑 `edit/music.py` 读):

1. ffmpeg 解码成 22.05k 单声道;STFT → 40 条 mel 带的对数能量 → 半波整流的谱通量 = onset 包络;
2. 包络自相关,在 60–200 BPM 上用"基频 + 倍频"梳状打分(带 120 BPM 附近的对数高斯先验,
   压住快一倍 / 慢一倍的误判),再在粗估附近用长滞后的倍频细化到 0.01 BPM;
3. 动态规划找拍点(Ellis 2007:局部 onset 强度 + 偏离节拍周期的惩罚),首尾弱拍修掉;
4. 每 4 拍里"重音"(全频 + 低频通量)平均最高的那个相位当强拍,即小节线;
5. 能量曲线(帧 RMS,dB)前后各 4 秒均值的差找起势点,吸到最近的强拍上。

    python -m app.pipeline.beat_track            # 分析 data/bgm 下所有还没分析的曲子
    python -m app.pipeline.beat_track --force    # 全部重算
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

from .. import config

_log = logging.getLogger(__name__)

SR = 22050
N_FFT = 1024
HOP = 256
FPS = SR / HOP
N_MELS = 40
#: 低于这个频率的 mel 带算"低频"(底鼓 / 贝斯),强拍判断时额外加权
LOW_HZ = 220.0
BPM_MIN, BPM_MAX = 60.0, 200.0
#: 速度先验:以 120 BPM 为中心、标准差 1 个八度的对数高斯
PRIOR_BPM, PRIOR_OCTAVES = 120.0, 1.0
#: DP 的"守拍"强度:越大越不许拍距偏离估计周期
TIGHTNESS = 100.0
#: onset 包络相对真实起音的系统性提前(窗口一半进入时通量就涨了):合成节拍器上实测约 9 毫秒,补回来
LAG_S = 0.009
#: 起势点:前后窗口秒数、后窗比前窗至少高这么多 dB、两个起势点至少隔这么久
DROP_WINDOW = 4.0
DROP_MIN_DB = 3.0
DROP_MIN_GAP = 8.0
DROP_MAX = 6
#: 结果格式版本;算法改了就加一,`analyze_library` 会重算旧版本
VERSION = 1


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------
def decode(path: str | Path, sr: int = SR) -> np.ndarray:
    """ffmpeg 解码成单声道 float32。失败抛 RuntimeError。"""
    if not config.FFMPEG_PATH or not Path(config.FFMPEG_PATH).is_file():
        raise RuntimeError("未找到 ffmpeg(.env 的 FFMPEG_BIN)")
    proc = subprocess.run(
        [str(config.FFMPEG_PATH), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        capture_output=True, timeout=600,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"解码失败:{proc.stderr.decode('utf-8', 'replace')[-200:]}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)


# --------------------------------------------------------------------------
# 1. onset 包络
# --------------------------------------------------------------------------
def _mel_filters(sr: int, n_fft: int, n_mels: int, fmin: float = 30.0,
                 fmax: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """三角 mel 滤波器组 (n_mels, n_fft//2+1) 和各带中心频率。"""
    fmax = fmax or sr / 2

    def hz2mel(f: np.ndarray | float) -> np.ndarray:
        return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)

    def mel2hz(m: np.ndarray) -> np.ndarray:
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    edges = mel2hz(np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2))
    freqs = np.linspace(0, sr / 2, n_fft // 2 + 1)
    fb = np.zeros((n_mels, len(freqs)))
    for m in range(n_mels):
        lo, c, hi = edges[m], edges[m + 1], edges[m + 2]
        up = (freqs - lo) / max(c - lo, 1e-9)
        down = (hi - freqs) / max(hi - c, 1e-9)
        fb[m] = np.maximum(0.0, np.minimum(up, down))
        if not fb[m].any():  # 低频带比一个 bin 还窄:取最近的 bin
            fb[m, int(np.argmin(np.abs(freqs - c)))] = 1.0
    return fb, edges[1:-1]


def features(y: np.ndarray, sr: int = SR) -> dict[str, np.ndarray]:
    """逐帧特征:onset(全频谱通量)、low(低频通量)、rms_db(帧能量)。帧率 sr/HOP。"""
    y = np.asarray(y, dtype=np.float64)
    pad = N_FFT // 2
    yp = np.pad(y, (pad, pad))
    n_frames = 1 + (len(yp) - N_FFT) // HOP
    if n_frames < 4:
        z = np.zeros(max(n_frames, 0))
        return {"onset": z, "low": z, "rms_db": z - 120.0}
    frames = np.lib.stride_tricks.sliding_window_view(yp, N_FFT)[::HOP][:n_frames]
    win = np.hanning(N_FFT)
    fb, centers = _mel_filters(sr, N_FFT, N_MELS)
    mel = np.empty((n_frames, N_MELS))
    rms = np.empty(n_frames)
    block = 2048
    for i in range(0, n_frames, block):
        f = frames[i:i + block]
        rms[i:i + block] = np.sqrt(np.mean(f * f, axis=1))
        spec = np.abs(np.fft.rfft(f * win, axis=1)) ** 2
        mel[i:i + block] = spec @ fb.T
    db = 10.0 * np.log10(np.maximum(mel, 1e-10))
    db = np.maximum(db, db.max() - 80.0)
    flux = np.maximum(0.0, np.diff(db, axis=0, prepend=db[:1]))
    low = centers < LOW_HZ
    return {
        "onset": flux.mean(axis=1),
        "low": flux[:, low].mean(axis=1) if low.any() else flux[:, 0],
        "rms_db": 20.0 * np.log10(np.maximum(rms, 1e-6)),
    }


def _smooth(x: np.ndarray, width: int) -> np.ndarray:
    if width <= 1 or len(x) == 0:
        return x
    k = np.hanning(width + 2)[1:-1]
    return np.convolve(x, k / k.sum(), mode="same")


def _normalize(env: np.ndarray) -> np.ndarray:
    env = env - env.mean()
    sd = float(env.std())
    return env / sd if sd > 1e-12 else env


# --------------------------------------------------------------------------
# 2. BPM
# --------------------------------------------------------------------------
def _autocorr(x: np.ndarray, max_lag: int) -> np.ndarray:
    x = x - x.mean()
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(max(2 * n, 2))))
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[: max_lag + 1]
    return ac / ac[0] if ac[0] > 0 else ac


def _comb(ac: np.ndarray, periods: np.ndarray, harmonics: int) -> np.ndarray:
    """每个周期 P 的 mean(ac(k·P)),k=1..harmonics,分数滞后线性插值。超出范围的项不计。"""
    lags = periods[:, None] * np.arange(1, harmonics + 1)[None, :]
    valid = lags <= len(ac) - 1
    vals = np.interp(np.minimum(lags, len(ac) - 1), np.arange(len(ac)), ac)
    vals = np.where(valid, vals, 0.0)
    return vals.sum(axis=1) / np.maximum(valid.sum(axis=1), 1)


def estimate_bpm(onset: np.ndarray, fps: float = FPS) -> float:
    """onset 包络 → BPM。包络太短 / 没有周期性返回 0。"""
    return tempo(onset, fps)[0]


def tempo(onset: np.ndarray, fps: float = FPS) -> tuple[float, float]:
    """onset 包络 → (BPM, 律动清晰度)。清晰度 = 节拍周期 1–4 倍处的平均自相关(0–1):
    节拍器接近 1,鼓点清楚的流行 / 电子乐 0.3 以上,无鼓的氛围铺底只有零点几。"""
    env = _smooth(onset, 3)
    if len(env) < fps * 4 or float(env.std()) < 1e-9:
        return 0.0, 0.0
    max_lag = min(len(env) - 1, int(fps * 20))
    ac = _autocorr(env, max_lag)
    grid = np.arange(BPM_MIN, BPM_MAX + 1e-9, 0.1)
    periods = 60.0 * fps / grid
    prior = np.exp(-0.5 * (np.log2(grid / PRIOR_BPM) / PRIOR_OCTAVES) ** 2)
    comb = _comb(ac, periods, 4)
    score = comb * prior
    if not np.isfinite(score).any() or score.max() <= 0:
        return 0.0, 0.0
    i = int(np.argmax(score))
    coarse = float(grid[i])
    # 细化:周期附近 ±1.5%,用能放下的全部倍频(滞后越长,分数帧误差摊得越薄)
    fine = np.arange(coarse * 0.985, coarse * 1.015, 0.01)
    p_fine = 60.0 * fps / fine
    k = max(4, int(max_lag / p_fine.max()))
    best = float(fine[int(np.argmax(_comb(ac, p_fine, k)))])
    return round(best, 2), round(max(0.0, float(comb[i])), 3)


# --------------------------------------------------------------------------
# 3. 拍点(动态规划)
# --------------------------------------------------------------------------
def track_beats(onset: np.ndarray, bpm: float, fps: float = FPS,
                tightness: float = TIGHTNESS) -> np.ndarray:
    """返回拍点(帧号,浮点,已做抛物线插值)。"""
    if bpm <= 0 or len(onset) < 8:
        return np.zeros(0)
    period = 60.0 * fps / bpm
    env = _normalize(onset)
    # 局部得分:包络与宽 period/32 的高斯平滑
    half = int(round(period))
    g = np.exp(-0.5 * (np.arange(-half, half + 1) * 32.0 / period) ** 2)
    local = np.convolve(env, g, mode="same")
    n = len(local)
    offsets = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    txwt = -tightness * np.log(-offsets / period) ** 2
    cum = np.zeros(n)
    back = np.full(n, -1, dtype=np.int64)
    first = True
    thresh = 0.01 * float(local.max()) if local.max() > 0 else 0.0
    for i in range(n):
        idx = i + offsets
        # 伸到 0 之前的候选:累计分按 0 算(相当于"这一拍是第一拍")
        cand = txwt + np.where(idx >= 0, cum[np.maximum(idx, 0)], 0.0)
        j = int(np.argmax(cand))
        prev = int(idx[j]) if idx[j] >= 0 else -1
        cum[i] = local[i] + float(cand[j])
        if first and local[i] < thresh:
            back[i] = -1
        else:
            back[i] = prev
            first = False
    # 最后一拍:累计分的局部极大里,不低于中位数一半的最后一个
    peaks = np.flatnonzero((cum[1:-1] > cum[:-2]) & (cum[1:-1] >= cum[2:])) + 1
    if not len(peaks):
        return np.zeros(0)
    med = float(np.median(cum[peaks]))
    good = peaks[cum[peaks] >= 0.5 * med]
    i = int(good[-1]) if len(good) else int(peaks[-1])
    beats = []
    while i >= 0:
        beats.append(i)
        i = int(back[i])
    out = np.array(beats[::-1], dtype=np.int64)
    out = _trim(out, local)
    return _refine(out, local)


def _trim(beats: np.ndarray, local: np.ndarray) -> np.ndarray:
    """首尾 onset 很弱的拍(前奏 / 尾声的静音段被 DP 外推出来的)修掉。

    门槛压得低(均方根的 0.2):起势后铺底变厚,谱通量里单拍的突出度会掉到前段的
    四分之一左右,门槛高了会把整段副歌当尾声修掉。"""
    if len(beats) < 3:
        return beats
    s = _smooth(np.maximum(local[beats], 0.0), 5)
    th = 0.2 * float(np.sqrt(np.mean(s ** 2)))
    keep = np.flatnonzero(s >= th)
    if not len(keep):
        return beats
    return beats[keep[0]: keep[-1] + 1]


def _refine(beats: np.ndarray, local: np.ndarray) -> np.ndarray:
    """拍点在局部得分上做抛物线插值,精度从整帧提到亚帧。"""
    out = beats.astype(np.float64)
    for k, b in enumerate(beats):
        if 0 < b < len(local) - 1:
            a, c, d = local[b - 1], local[b], local[b + 1]
            den = a - 2 * c + d
            if den < 0:
                out[k] = b + float(np.clip(0.5 * (a - d) / den, -0.5, 0.5))
    return out


# --------------------------------------------------------------------------
# 4. 强拍  5. 起势点
# --------------------------------------------------------------------------
def downbeat_phase(beat_frames: np.ndarray, onset: np.ndarray, low: np.ndarray) -> int:
    """每 4 拍取"重音"平均最高的相位(0–3)。重音 = 拍点 ±2 帧内全频与低频通量的峰值之和。"""
    if len(beat_frames) < 8:
        return 0
    on, lo = _normalize(onset), _normalize(low)
    acc = []
    for b in np.round(beat_frames).astype(np.int64):
        a, z = max(0, b - 2), min(len(on), b + 3)
        acc.append(float(on[a:z].max() + lo[a:z].max()) if z > a else 0.0)
    arr = np.array(acc)
    means = [float(arr[p::4].mean()) for p in range(4)]
    return int(np.argmax(means))


def find_drops(rms_db: np.ndarray, downbeat_times: np.ndarray, fps: float = FPS,
               ) -> list[tuple[float, float]]:
    """起势点 [(秒, 抬升 dB)],按时间排序。后 4 秒比前 4 秒平均响 `DROP_MIN_DB` 以上、
    后段不弱于全曲中位电平,吸到 ±1 秒内最近的强拍。"""
    n = len(rms_db)
    w = int(round(DROP_WINDOW * fps))
    if n < 3 * w:
        return []
    lev = _smooth(rms_db, int(fps * 0.5))
    csum = np.concatenate([[0.0], np.cumsum(lev)])
    t = np.arange(w, n - w)
    before = (csum[t] - csum[t - w]) / w
    after = (csum[t + w] - csum[t]) / w
    rise = after - before
    active = np.flatnonzero(rms_db > rms_db.max() - 40.0)
    start = int(active[0]) if len(active) else 0
    floor = float(np.median(lev[start:])) - 1.0
    ok = (rise >= DROP_MIN_DB) & (after >= floor) & (t >= start + int(1.5 * fps))
    cands = [(float(rise[i]), int(t[i])) for i in np.flatnonzero(ok)
             if (i == 0 or rise[i] >= rise[i - 1]) and (i == len(rise) - 1 or rise[i] >= rise[i + 1])]
    picked: list[tuple[float, float]] = []
    for r, f in sorted(cands, reverse=True):
        sec = f / fps
        if len(downbeat_times):
            k = int(np.argmin(np.abs(downbeat_times - sec)))
            if abs(downbeat_times[k] - sec) <= 1.0:
                sec = float(downbeat_times[k])
        if all(abs(sec - p) >= DROP_MIN_GAP for p, _ in picked):
            picked.append((sec, r))
        if len(picked) >= DROP_MAX:
            break
    return sorted((round(s, 3), round(r, 1)) for s, r in picked)


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------
def analyze_signal(y: np.ndarray, sr: int = SR) -> dict[str, Any]:
    """一段单声道信号 → {bpm, pulse, beats, downbeats, drops, drop_db, duration}。时间单位秒。"""
    if sr != SR:
        raise ValueError(f"采样率须为 {SR}")
    feats = features(y, sr)
    bpm, pulse = tempo(feats["onset"])
    frames = track_beats(feats["onset"], bpm) if bpm else np.zeros(0)
    beats = frames * HOP / sr + LAG_S
    phase = downbeat_phase(frames, feats["onset"], feats["low"]) if len(frames) else 0
    downs = beats[phase::4] if len(beats) else beats
    drops = find_drops(feats["rms_db"], downs)
    return {
        "bpm": bpm,
        "pulse": pulse,
        "beats": [round(float(b), 3) for b in beats if b >= 0],
        "downbeats": [round(float(b), 3) for b in downs if b >= 0],
        "drops": [d for d, _ in drops],
        "drop_db": [r for _, r in drops],
        "duration": round(len(y) / sr, 3),
    }


def analyze(path: str | Path) -> dict[str, Any]:
    """解码一首曲子并分析;顺带测整曲响度(LUFS,配乐两首交叉时对齐电平用)。"""
    from . import render

    out = analyze_signal(decode(path))
    loud = render.measure_loudness(Path(path))
    out["lufs"] = round(loud["I"], 1) if loud else None
    out["beat_version"] = VERSION
    return out


def _ledgers(root: Path) -> Iterable[Path]:
    yield from sorted(root.rglob("_licenses.json"))


def analyze_library(root: Path | None = None, *, force: bool = False) -> dict[str, Any]:
    """给曲库里每首曲子补上节拍信息,写回各目录的 `_licenses.json`。返回 {done, skipped, failed}。"""
    root = root or config.BGM_DIR or (config.DATA_DIR / "bgm")
    report: dict[str, Any] = {"done": [], "skipped": 0, "failed": []}
    for ledger_path in _ledgers(root):
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report["failed"].append(f"{ledger_path}: {exc}")
            continue
        changed = False
        for name, meta in ledger.items():
            f = ledger_path.parent / name
            if not isinstance(meta, dict) or not f.is_file():
                continue
            if not force and meta.get("beat_version") == VERSION and meta.get("beats"):
                report["skipped"] += 1
                continue
            try:
                meta.update(analyze(f))
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                report["failed"].append(f"{f}: {exc}")
                continue
            changed = True
            report["done"].append(f"{f.relative_to(root)}  {meta['bpm']} BPM, "
                                  f"{len(meta['beats'])} 拍, 起势 {meta['drops'][:3]}")
        if changed:
            tmp = ledger_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(ledger_path)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BGM 节拍分析,写回 _licenses.json")
    ap.add_argument("root", nargs="?", type=Path, default=None)
    ap.add_argument("--force", action="store_true", help="已分析过的也重算")
    args = ap.parse_args(argv)
    rep = analyze_library(args.root, force=args.force)
    for line in rep["done"]:
        print(line)
    for line in rep["failed"]:
        print("失败:", line, file=sys.stderr)
    print(f"分析 {len(rep['done'])} 首,跳过 {rep['skipped']} 首,失败 {len(rep['failed'])} 首")
    return 1 if rep["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
