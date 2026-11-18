"""音效:真实采样(CC0 采样包),全片只在三处出声 —— 开场首刀、换章 whip、大字入场。

- 采样包在 `data/sfx/pack/`,`_licenses.json` 记每个文件的来源页、作者、许可(只收 CC0)。
  `fetch_pack()`(或 `python -m app.pipeline.sfx fetch`)按 `PACK_MANIFEST` 下 Freesound 的预览
  mp3,ffmpeg 转成 48k 单声道 wav、峰值归一到 -1 dBFS。台账里没有的文件不用。
- 事件表 `[(秒, 事件)]`,事件是 `open` / `whip` / `headline`。秒数是**画面上那一下**的时刻
  (切点、大字落定),采样按自己的包络峰对齐过去:whoosh 的"呼"最响处压在切点上,riser 的
  顶点落在首刀,hit 的冲击点对齐大字。
- 两个音效至少隔 `MIN_GAP` 秒,撞了按 open > whip > headline 留一个。
- 没下采样包:只有换章 whip 用合成的 whoosh,开场和大字不出声,记 `degrade.note("sfx.no_pack")`。
- 逐关键词的 pop 删掉了(2026-11-12 用户反馈像"叮叮叮")。旧接口 `events_for` / `build_track`
  保留给旧合成(`edit/project.py`、`explainer`),见各自的说明。

`build_track` 把事件摆进一条与成片等长的 wav,由 `render._mix_audio` 当第三路混进去。
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import wave
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, degrade

_log = logging.getLogger(__name__)

SR = 48000
LICENSES = "_licenses.json"

#: 两个音效之间至少隔这么久
MIN_GAP = 4.0
#: 首刀离片头不到这么久就放不下 riser,改成一记 hit
RISER_MIN_LEAD = 0.8
#: 事件 → 用哪类采样(open 的 riser / hit 二选一在 `_sound_for` 里定)
EVENTS = ("open", "whip", "headline")
SOUND = {"open": "riser", "whip": "whoosh", "headline": "hit"}
#: 旧事件名:whoosh 当换章 whip,pop 已删(旧调用方传进来直接忽略)
LEGACY = {"whoosh": "whip", "pop": None}
#: 各类音效在锚点前后 `BODY` 秒内的平均电平(RMS,dBFS)。混音时旁白在 -16 LUFS(说话时
#: 400ms 块约 -15 dB),音效比它低 9 dB 上下:听得清"有一下",盖不住字(2026-11-18 用户试听后
#: 要求再响一点,整体抬了 4 dB)。按 RMS 而不按峰值定:按峰值时短促的 whoosh 和拖长的 hit 听感
#: 能差 13 dB(同日样片实测)
LEVEL_DB = {"whoosh": -21.0, "hit": -21.0, "riser": -23.0}
BODY = 0.15
#: 再响也不超过这个峰值(-4 dBFS),防个别尖峰采样
PEAK_CAP = 0.63
#: 以包络峰为锚点,往前留多少、往后留多少秒(长尾混响切掉,免得糊到下一句)
PRE = {"whoosh": 1.0, "hit": 0.08, "riser": 3.0}
POST = {"whoosh": 1.0, "hit": 2.0, "riser": 0.6}
EDGE_FADE = 0.02
TAIL_FADE = 0.35
#: 包络平滑窗(秒),找锚点用
ENV_WIN = 0.02
PEAK = 0.89  # -1 dBFS

#: 采样包清单(Freesound,全部 CC0 1.0;作者署名非必须,台账照记)。
#: 下载前要先在对话里问过用户(体积、来源),见 `fetch_pack`。
FREESOUND_CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
PACK_MANIFEST: tuple[dict[str, Any], ...] = (
    {"kind": "whoosh", "id": 683101, "title": "quick woosh", "author": "florianreichelt",
     "url": "https://cdn.freesound.org/previews/683/683101_6253486-hq.mp3"},
    {"kind": "whoosh", "id": 169867, "title": "swoosh", "author": "Halgrimm",
     "url": "https://cdn.freesound.org/previews/169/169867_2834921-hq.mp3"},
    {"kind": "whoosh", "id": 425706, "title": "Woosh_Medium_Short_01", "author": "moogy73",
     "url": "https://cdn.freesound.org/previews/425/425706_760420-hq.mp3"},
    {"kind": "whoosh", "id": 243400, "title": "woosh", "author": "freakinbehemoth",
     "url": "https://cdn.freesound.org/previews/243/243400_4146245-hq.mp3"},
    {"kind": "hit", "id": 560156, "title": "Soft Cinematic Impact", "author": "Rizzard",
     "url": "https://cdn.freesound.org/previews/560/560156_10825267-hq.mp3"},
    # 428073 "Sub Drop Smooth" 试过又删了:是持续 2 秒以上的低频长音,不是"一下"。
    {"kind": "hit", "id": 151812, "title": "Orch 002 boom", "author": "Karma-Ron",
     "url": "https://cdn.freesound.org/previews/151/151812_16058-hq.mp3"},
    {"kind": "hit", "id": 512389, "title": "Bass Impact - Ki_01", "author": "Kievinay",
     "url": "https://cdn.freesound.org/previews/512/512389_3997858-hq.mp3"},
    {"kind": "riser", "id": 685256, "title": "Riser sound effect short", "author": "syntheffects",
     "url": "https://cdn.freesound.org/previews/685/685256_12265588-hq.mp3"},
    {"kind": "riser", "id": 561207, "title": "Riser", "author": "Rizzard",
     "url": "https://cdn.freesound.org/previews/561/561207_10825267-hq.mp3"},
    {"kind": "riser", "id": 859482, "title": "Cinematic Riser #3 subtle", "author": "BeaconStudio",
     "url": "https://cdn.freesound.org/previews/859/859482_18689440-hq.mp3"},
)


# --------------------------------------------------------------------------
# 事件
# --------------------------------------------------------------------------
def cue_events(*, open_at: float | None = None, whips: Sequence[float] = (),
               headlines: Sequence[float] = (), min_gap: float = MIN_GAP,
               ) -> list[tuple[float, str]]:
    """全片的音效事件 `[(秒, 事件)]`,按时间排好。

    - `open_at`:开场首刀(第一个切点)的时刻;0 / None 不放。
    - `whips`:换章 whip 转场的切点;片头 0.5 秒内的忽略(第 0 章就从片头开始)。
    - `headlines`:大字(整屏标题 / 大数字)落定的时刻。

    任意两个事件至少隔 `min_gap` 秒;撞了先留 open,再 whip,再 headline,同级先到先得。
    """
    cands: list[tuple[int, float, str]] = []
    if open_at is not None and float(open_at) > 0.1:
        cands.append((0, float(open_at), "open"))
    cands += [(1, float(t), "whip") for t in whips if float(t) > 0.5]
    cands += [(2, float(t), "headline") for t in headlines if float(t) > 0.1]
    kept: list[tuple[float, str]] = []
    for _, t, ev in sorted(cands):
        if all(abs(t - k) >= min_gap - 1e-9 for k, _ in kept):
            kept.append((t, ev))
    return sorted(kept)


def events_for(cues: Sequence[Mapping[str, Any]], chapter_starts: Sequence[float],
               reveal_times: Sequence[float] = (),
               ) -> list[tuple[float, str]]:
    """旧接口(`edit/project.py._sfx`、`explainer._sfx_track` 在用):章节起点当换章 whip,
    参数 / 大数字上屏时刻当大字入场。`cues` 不再用 —— 逐关键词的 pop 删掉了;旧调用方给不出
    首刀时刻,所以没有 open。间隔、优先级同 `cue_events`。"""
    del cues
    return cue_events(whips=chapter_starts, headlines=reveal_times)


# --------------------------------------------------------------------------
# 采样包
# --------------------------------------------------------------------------
def pack_dir() -> Path:
    """采样包目录 `data/sfx/pack/`(跟着 `config.DATA_DIR` 走,测试里自动落到临时目录)。"""
    return config.DATA_DIR / "sfx" / "pack"


def _ledger(root: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((root / LICENSES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pack(root: Path | None = None) -> dict[str, list[Path]]:
    """采样包里能用的文件,按类别:`{"whoosh": [...], "hit": [...], "riser": [...]}`。
    只认台账里登记过、许可是 CC0、文件还在的。"""
    root = root or pack_dir()
    out: dict[str, list[Path]] = {}
    for name, meta in sorted(_ledger(root).items()):
        if not isinstance(meta, Mapping) or "CC0" not in str(meta.get("license") or ""):
            continue
        path = root / name
        if path.is_file() and str(meta.get("kind")) in LEVEL_DB:
            out.setdefault(str(meta["kind"]), []).append(path)
    return out


def _whoosh() -> np.ndarray:
    """合成 whoosh(没下采样包时换章用):带通噪声由闷到亮,固定种子。"""
    n = int(SR * 0.5)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    u = np.linspace(0, 1, n)
    env = np.sin(np.pi * u) ** 2.2 * (0.35 + 0.65 * u)  # 渐强、尾部收得快
    alpha = 0.02 + 0.5 * u  # 单极点低通的截止随时间升高
    out = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc += alpha[i] * (noise[i] - acc)
        out[i] = acc
    out *= env
    return out / max(1e-9, float(np.abs(out).max())) * PEAK


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SR or w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: 不是 {SR} Hz / 16 bit")
        raw = w.readframes(w.getnframes())
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    return data.reshape(-1, ch).mean(axis=1) if ch > 1 else data


def _write(path: Path, data: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())


def anchor(data: np.ndarray) -> int:
    """采样的锚点:平滑后包络最响的位置(whoosh 的"呼"、riser 的顶点、hit 的冲击)。"""
    if not len(data):
        return 0
    win = max(1, int(ENV_WIN * SR))
    env = np.convolve(data * data, np.ones(win) / win, mode="same")
    return int(np.argmax(env))


_CACHE: dict[tuple[str, float], tuple[np.ndarray, int]] = {}


def _load(path: Path) -> tuple[np.ndarray, int] | None:
    try:
        key = (str(path), path.stat().st_mtime)
        if key not in _CACHE:
            data = _read(path)
            _CACHE[key] = (data, anchor(data))
        return _CACHE[key]
    except (OSError, wave.Error, ValueError) as exc:
        _log.warning("音效采样读不了 %s:%s", path, exc)
        return None


def _sound_for(event: str, t: float) -> str:
    if event == "open":
        return "riser" if t >= RISER_MIN_LEAD else "hit"
    return SOUND[event]


def _shape(data: np.ndarray, a: int, sound: str) -> tuple[np.ndarray, int]:
    """按锚点截出 [锚点 - PRE, 锚点 + POST],截过的边做淡入 / 淡出。返回 (片段, 片段内锚点)。"""
    lo = max(0, a - int(PRE[sound] * SR))
    hi = min(len(data), a + int(POST[sound] * SR))
    seg = data[lo:hi].copy()
    if lo > 0:
        n = min(len(seg), int(EDGE_FADE * SR))
        seg[:n] *= np.linspace(0.0, 1.0, n)
    if hi < len(data):
        n = min(len(seg), int(TAIL_FADE * SR))
        seg[len(seg) - n:] *= np.cos(np.linspace(0.0, np.pi / 2, n)) ** 2
    return seg, a - lo


def _level(seg: np.ndarray, a: int, sound: str) -> np.ndarray:
    """按锚点前后 `BODY` 秒的 RMS 调到 `LEVEL_DB`,峰值不超过 `PEAK_CAP`。"""
    half = int(BODY * SR)
    body = seg[max(0, a - half): a + half]
    rms = float(np.sqrt(np.mean(body * body))) if len(body) else 0.0
    peak = float(np.abs(seg).max()) if len(seg) else 0.0
    if rms <= 0 or peak <= 0:
        return seg
    return seg * min(10 ** (LEVEL_DB[sound] / 20) / rms, PEAK_CAP / peak)


def _rot(seed: str, n: int) -> int:
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8], 16) % n if n and seed else 0


# --------------------------------------------------------------------------
# 音效轨
# --------------------------------------------------------------------------
def build_track(events: Sequence[tuple[float, str]], seconds: float, dest: Path, *,
                seed: str = "", root: Path | None = None) -> Path | None:
    """事件按时间摆进一条 `seconds` 秒的 48k 单声道 wav。没有事件 / 一个都放不下 / 写失败返回 None。

    `events` 用 `cue_events` 的事件名;旧名 whoosh 当 whip,pop 忽略。同类采样轮着用
    (`seed` 决定从第几个开始,同一项目每次一样)。没有采样包时只有 whip 出声(合成 whoosh)。
    """
    if not events or seconds <= 0:
        return None
    files = pack(root)
    buf = np.zeros(int(seconds * SR))
    used: dict[str, int] = {}
    missing: set[str] = set()
    placed = 0
    for t, raw in sorted(events):
        ev = LEGACY.get(raw, raw)
        if ev not in EVENTS:
            continue
        sound = _sound_for(ev, float(t))
        choices = files.get(sound) or []
        clip: tuple[np.ndarray, int] | None = None
        if choices:
            k = used.get(sound, 0)
            used[sound] = k + 1
            clip = _load(choices[(_rot(seed, len(choices)) + k) % len(choices)])
        if clip is None:
            missing.add(sound)
            if sound != "whoosh":
                continue
            synth = _whoosh()
            clip = (synth, anchor(synth))
        seg, a = _shape(clip[0], clip[1], sound)
        seg = _level(seg, a, sound)
        start = int(round(float(t) * SR)) - a
        if start < 0:  # 锚点前面放不下:砍头 + 淡入(片头的 riser 常见)
            seg = seg[-start:].copy()
            n = min(len(seg), int(EDGE_FADE * SR))
            seg[:n] *= np.linspace(0.0, 1.0, n)
            start = 0
        end = min(len(buf), start + len(seg))
        if end <= start:
            continue
        buf[start:end] += seg[: end - start]
        placed += 1
    if missing:
        degrade.note("sfx.no_pack", "采样包缺 " + "/".join(sorted(missing))
                     + (",换章用合成 whoosh" if "whoosh" in missing else "")
                     + ";其余不出声")
    if not placed:
        return None
    try:
        _write(dest, buf)
    except OSError as exc:
        _log.warning("音效轨写入失败:%s", exc)
        return None
    return dest


# --------------------------------------------------------------------------
# 下载采样包(先问过用户再跑)
# --------------------------------------------------------------------------
def fetch_pack(dest: Path | None = None, *, manifest: Sequence[Mapping[str, Any]] = PACK_MANIFEST,
               client: Any = None) -> dict[str, list[str]]:
    """按清单下载采样包到 `dest`(默认 `data/sfx/pack/`),转 48k 单声道 wav、峰值 -1 dBFS,
    写台账。已有的跳过。返回 `{"done": [...], "skipped": [...], "failed": [...]}`。"""
    import httpx

    from .beat_track import decode

    dest = dest or pack_dir()
    dest.mkdir(parents=True, exist_ok=True)
    ledger = _ledger(dest)
    result: dict[str, list[str]] = {"done": [], "skipped": [], "failed": []}
    own = client is None
    http = client or httpx.Client(headers={"User-Agent": "Mozilla/5.0 (AutoReel)"}, timeout=60,
                                  follow_redirects=True)
    try:
        for item in manifest:
            name = f"{item['kind']}_{item['id']}.wav"
            if name in ledger and (dest / name).is_file():
                result["skipped"].append(name)
                continue
            try:
                resp = http.get(str(item["url"]))
                resp.raise_for_status()
                with tempfile.TemporaryDirectory(dir=dest) as tmp:
                    src = Path(tmp) / "src.mp3"
                    src.write_bytes(resp.content)
                    data = decode(src, SR)
                if not len(data) or float(np.abs(data).max()) < 1e-4:
                    raise RuntimeError("解出来是空的 / 静音")
                _write(dest / name, data / float(np.abs(data).max()) * PEAK)
            except (OSError, RuntimeError, httpx.HTTPError, subprocess.SubprocessError) as exc:
                _log.warning("音效 %s 下载失败:%s", name, exc)
                result["failed"].append(name)
                continue
            ledger[name] = {
                "kind": item["kind"], "title": item["title"], "author": item["author"],
                "seconds": round(len(data) / SR, 2),
                "source": f"https://freesound.org/people/{item['author']}/sounds/{item['id']}/",
                "license": "CC0 1.0", "license_url": FREESOUND_CC0,
            }
            result["done"].append(name)
            tmp_ledger = dest / (LICENSES + ".tmp")
            tmp_ledger.write_text(json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp_ledger.replace(dest / LICENSES)
    finally:
        if own:
            http.close()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["fetch"]:
        print("用法: python -m app.pipeline.sfx fetch [目标目录]")
        return 2
    res = fetch_pack(Path(args[1]) if len(args) > 1 else None)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if not res["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
