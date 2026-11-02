"""成片级 QA:响度 / 真峰值回测,黑场与冻结帧检测。

只产出警告文案,不拦成片:黑场、冻结在转场和静态数据卡上可能是有意的,
是否重做由人看警告决定。解码完整性和音轨空洞另有 `render._check` 把关。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import config
from . import timeline

#: 综合响度偏离目标超过这个值(LU)就提示
LUFS_TOLERANCE = 1.5
#: 真峰值高于这个值(dBTP)就提示,平台转码后会削波
TRUE_PEAK_MAX = -1.0
#: 黑场至少这么长(秒)才算;开头 / 结尾的淡入淡出黑帧不到这个时长
BLACK_MIN_SECONDS = 0.5
#: 画面静止至少这么长(秒)才算冻结
FREEZE_MIN_SECONDS = 4.0

_RE_LUFS = re.compile(r"Integrated loudness:\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", re.S)
_RE_PEAK = re.compile(r"True peak:\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", re.S)
_RE_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
_RE_FREEZE_START = re.compile(r"lavfi\.(?:freezedetect\.)?freeze_start:\s*([\d.]+)")
_RE_FREEZE_DUR = re.compile(r"lavfi\.(?:freezedetect\.)?freeze_duration:\s*([\d.]+)")


def _num(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > -200 else None


def analyse(log: str, total: float = 0.0) -> dict:
    """解析 ffmpeg(ebur128 + blackdetect + freezedetect)的 info 日志。"""
    lufs = peak = None
    # ebur128 的汇总在日志末尾;取最后一处,避免被前面的逐帧输出干扰
    for m in _RE_LUFS.finditer(log):
        lufs = _num(m.group(1))
    for m in _RE_PEAK.finditer(log):
        peak = _num(m.group(1))
    blacks = [(float(a), float(d)) for a, _b, d in _RE_BLACK.findall(log)]
    starts = [float(x) for x in _RE_FREEZE_START.findall(log)]
    durs = [float(x) for x in _RE_FREEZE_DUR.findall(log)]
    freezes = list(zip(starts, durs, strict=False))
    if len(starts) > len(durs) and total > 0:
        # 冻结一直持续到片尾时 ffmpeg 只打 start,时长按片长补
        freezes.append((starts[-1], max(total - starts[-1], 0.0)))
    return {"lufs": lufs, "true_peak": peak, "black": blacks, "freeze": freezes}


def problems(result: dict, target_lufs: float) -> list[str]:
    """把测量结果翻成人能看懂的警告。"""
    out: list[str] = []
    lufs, peak = result.get("lufs"), result.get("true_peak")
    if lufs is not None and abs(lufs - target_lufs) > LUFS_TOLERANCE:
        out.append(f"成片响度 {lufs:.1f} LUFS,偏离目标 {target_lufs:g} 超过 {LUFS_TOLERANCE:g} LU")
    if peak is not None and peak > TRUE_PEAK_MAX:
        out.append(f"成片真峰值 {peak:.1f} dBTP,高于 {TRUE_PEAK_MAX:g},平台转码可能削波")
    black = [(s, d) for s, d in result.get("black", []) if d >= BLACK_MIN_SECONDS]
    if black:
        out.append(f"成片有 {len(black)} 处黑场(首处 {black[0][0]:.1f} 秒起,{black[0][1]:.1f} 秒)")
    freeze = [(s, d) for s, d in result.get("freeze", []) if d >= FREEZE_MIN_SECONDS]
    if freeze:
        out.append(f"成片有 {len(freeze)} 处画面静止(首处 {freeze[0][0]:.1f} 秒起,{freeze[0][1]:.1f} 秒)")
    return out


def inspect(video: Path, timeout: int = 900) -> list[str]:
    """对成片跑一遍测量,返回警告列表。ffmpeg 不可用或测量失败返回空列表(不阻塞出片)。"""
    ffmpeg = config.FFMPEG_PATH
    if not ffmpeg:
        return []
    try:
        proc = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "info", "-i", str(video),
             "-vf", f"blackdetect=d={BLACK_MIN_SECONDS}:pix_th=0.1,"
                    f"freezedetect=n=-60dB:d={FREEZE_MIN_SECONDS}",
             "-af", "ebur128=peak=true:framelog=quiet",
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return problems(analyse(proc.stderr or "", timeline.probe_safe(video, default=0.0)),
                    config.TARGET_LUFS)
