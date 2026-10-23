"""语音质检:合成出来的每句话,是不是它自己的词、有没有坏。

为什么要有这一步:批量 TTS 在批内某句失败时,成功项整体前移,按位置对回会把后一句的音频
挂到前一句上,而且错位的音频还会被写进跨项目的句子缓存,以后每次命中都是错的。这种问题
只有"听"才发现得了 —— 现在由机器先听一遍。

两层检查:

1. **信号检查(全部句子,纯 ffmpeg,不装任何东西)**:有声占比、整句响度、语速相对全片中位数的
   偏离。哑句、几乎没声的直接判错;语速偏离只标"可疑"(截断、拖音、错位的典型特征)。
2. **转写复核(只对可疑句)**:用 faster-whisper 把这句音频转成字,和脚本原文比对。
   只复核少数句 —— 可疑句、刚从缓存取出的句子(缓存可能带毒)、补跑批次里的句子 ——
   模型按需加载、复核完就释放,不常驻。没装 faster-whisper 就只做信号检查,并在报告里写明。

只比对汉字:数字、英文在 TTS 前被改写成读法(「643」→「六百四十三」),转写又常常写回阿拉伯数字,
逐字比对只会制造误报;错位、漏读、读错词都体现在汉字上。
"""
from __future__ import annotations

import difflib
import logging
import re
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import config
from .timeline import probe_safe

log = logging.getLogger(__name__)

#: 有声占比低于这个当哑句
MIN_VOICED_RATIO = 0.35
#: 整句平均响度低于这个(dB)当哑句
MIN_MEAN_DB = -45.0
#: 削波:峰值贴着 0 dB
CLIP_DB = -0.1
#: 语速(字/秒)相对全片中位数的比值:超出 [低, 高] 算可疑;超出 [极低, 极高] 在没有转写时直接判错
RATE_SUSPECT = (0.6, 1.5)
RATE_EXTREME = (0.4, 2.2)
#: 比对的汉字少于这个数的句子(「后 275/35 R21。」)没法靠汉字判断,不复核
MIN_CJK_FOR_ASR = 3
#: 转写比对的判错规则。小模型在普通话短句上常听岔同音字(自己句子的相似度能低到 0.3 左右),
#: 所以不拿固定高阈值,而是看"这段音频更像脚本里的哪一句":错位的音频念的是邻句,
#: 邻句的相似度会远高于自己的;相似度低于下限则是读得面目全非
OTHER_MIN = 0.5
OTHER_MARGIN = 0.2
#: 静音判定阈值(dB)与最短时长(秒)
_SILENCE_DB = -40
_SILENCE_MIN = 0.1

_CJK = re.compile(r"[一-鿿]")


@dataclass
class ClipStats:
    duration: float = 0.0
    mean_db: float = 0.0
    max_db: float = 0.0
    lead_silence: float = 0.0
    tail_silence: float = 0.0
    voiced_ratio: float = 1.0
    ok: bool = True  # False = 没能分析(没 ffmpeg 等),不据此下判断


@dataclass
class Finding:
    index: int
    level: str  # "error" | "warn"
    reason: str
    similarity: float | None = None
    heard: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "level": self.level, "reason": self.reason,
                "similarity": self.similarity, "heard": self.heard}


@dataclass
class QAResult:
    findings: list[Finding] = field(default_factory=list)
    #: 转写复核通过的句子位置(可以记进缓存,下次不再复核)
    verified: list[int] = field(default_factory=list)
    checked: int = 0
    asr_checked: int = 0
    asr_available: bool = False
    median_rate: float = 0.0

    @property
    def errors(self) -> list[int]:
        return sorted({f.index for f in self.findings if f.level == "error"})

    def to_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "asr_checked": self.asr_checked,
                "asr_available": self.asr_available,
                "median_chars_per_second": round(self.median_rate, 2),
                "errors": self.errors,
                "findings": [f.to_dict() for f in self.findings]}


# --------------------------------------------------------------------------
# 第一层:信号检查
# --------------------------------------------------------------------------
def analyze_clip(path: str | Path) -> ClipStats:
    """一次 ffmpeg 调用拿到响度和静音区间。分析不了就返回 ok=False 的空结果。"""
    ffmpeg = str(config.FFMPEG_PATH or "ffmpeg")
    dur = probe_safe(path, default=0.0)
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vn",
             "-af", f"silencedetect=n={_SILENCE_DB}dB:d={_SILENCE_MIN},volumedetect",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ClipStats(duration=dur, ok=False)
    err = proc.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", err)
    if not mean or dur <= 0:
        return ClipStats(duration=dur, ok=False)

    def _db(m: re.Match[str] | None) -> float:
        if not m or m.group(1) == "-inf":
            return -120.0
        return float(m.group(1))

    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [(float(a), float(b)) for a, b in
            re.findall(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", err)]
    silent = sum(d for _, d in ends)
    if len(starts) > len(ends):  # 一直静音到结尾,silence_end 不会打印
        silent += max(0.0, dur - max(0.0, starts[-1]))
    lead = 0.0
    if starts and starts[0] <= 0.05:
        lead = ends[0][1] if ends else dur
    tail = 0.0
    if len(starts) > len(ends):
        tail = max(0.0, dur - max(0.0, starts[-1]))
    elif ends and ends[-1][0] >= dur - 0.05:
        tail = ends[-1][1]
    return ClipStats(duration=dur, mean_db=_db(mean), max_db=_db(peak), lead_silence=lead,
                     tail_silence=tail, voiced_ratio=max(0.0, 1.0 - silent / dur))


def spoken_chars(text: str) -> int:
    """念出来的字数:去掉空白和标点(TTS 文本里数字已经是汉字读法)。"""
    return len(re.sub(r"[\W_]+", "", text or ""))


def _is_mute(st: ClipStats) -> bool:
    return st.voiced_ratio < MIN_VOICED_RATIO or st.mean_db < MIN_MEAN_DB


def _speech_seconds(st: ClipStats) -> float:
    return max(0.05, st.duration - st.lead_silence - st.tail_silence)


# --------------------------------------------------------------------------
# 第二层:转写复核
# --------------------------------------------------------------------------
Transcriber = Callable[[Path], str]

_model: Any = None
_model_failed = False


def asr_available() -> bool:
    if not config.VOICE_QA_ASR:
        return False
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def load_asr() -> Transcriber | None:
    """faster-whisper 转写函数;没装、关了、加载失败返回 None。模型进程内单例,
    用完调 `release_asr()` 释放(质检只在门禁 A 确认时跑,不常驻)。"""
    global _model, _model_failed
    if _model_failed or not asr_available():
        return None
    if _model is None:
        try:
            from faster_whisper import WhisperModel

            _model = WhisperModel(config.VOICE_QA_MODEL, device="cpu", compute_type="int8")
        except Exception as exc:  # noqa: BLE001 - 模型下载/加载失败不能拦出片
            log.warning("语音质检:转写模型加载失败,退回信号检查:%s", exc)
            _model_failed = True
            return None
    model = _model

    def transcribe(path: Path) -> str:
        # 自己用 ffmpeg 解成 16k 单声道再喂给模型:不依赖 faster-whisper 自带的 PyAV 解码
        # (版本对不上时直接 open() 报错)
        segments, _ = model.transcribe(_decode_16k(path), language="zh", beam_size=1,
                                       vad_filter=False, temperature=0.0,
                                       initial_prompt="以下是普通话的句子。")
        return "".join(s.text for s in segments)

    return transcribe


def _decode_16k(path: Path) -> Any:
    import numpy as np

    proc = subprocess.run(
        [str(config.FFMPEG_PATH or "ffmpeg"), "-v", "error", "-i", str(path), "-vn", "-ac", "1",
         "-ar", "16000", "-f", "f32le", "-"], capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("ffmpeg 解码失败")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def release_asr() -> None:
    global _model
    _model = None


def _match(i: int, heard: str, texts: Sequence[str], originals: Sequence[str]
           ) -> tuple[float, int | None, float]:
    """(这句的相似度, 最像的另一句的位置, 它的相似度)。TTS 文本里数字是读法、原文里是阿拉伯数字,
    转写两种写法都有,所以两份文本各比一次取高的。"""
    def sim(j: int) -> float:
        return max(cjk_similarity(originals[j], heard), cjk_similarity(texts[j], heard))

    own = sim(i)
    best, best_j = 0.0, None
    for j in range(len(texts)):
        if j != i and len(_CJK.findall(originals[j])) >= MIN_CJK_FOR_ASR:
            v = sim(j)
            if v > best:
                best, best_j = v, j
    return own, best_j, best


def cjk_similarity(expected: str, heard: str) -> float:
    """只比汉字的相似度(0–1)。"""
    a = "".join(_CJK.findall(expected or ""))
    b = "".join(_CJK.findall(heard or ""))
    if not a:
        return 1.0
    if not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# --------------------------------------------------------------------------
# 总入口
# --------------------------------------------------------------------------
def check(
    texts: Sequence[str],
    clips: Mapping[int, Path],
    *,
    originals: Sequence[str] | None = None,
    hint: set[int] | None = None,
    transcribe: Transcriber | None = None,
    min_similarity: float | None = None,
) -> QAResult:
    """质检一批句子。

    `texts[i]`:TTS 文本(数字已改成读法,用来算语速);`originals[i]`:脚本原文(比对汉字用,
    不给就用 `texts`);`clips`:{位置: 音频路径};`hint`:不管语速是否正常都要转写复核的位置
    (从缓存取的、补跑的);`transcribe`:转写函数,不给就自动加载 faster-whisper。
    """
    thr = config.VOICE_QA_MIN_SIMILARITY if min_similarity is None else min_similarity
    originals = originals if originals is not None else texts
    res = QAResult()
    stats = {i: analyze_clip(p) for i, p in clips.items()}
    res.checked = len(stats)

    rates: dict[int, float] = {}
    for i, st in stats.items():
        if st.ok and not _is_mute(st):  # 哑句的"语速"没有意义,别拉偏中位数
            rates[i] = spoken_chars(texts[i]) / _speech_seconds(st)
    # 太短的句子语速噪声大,不参与中位数
    pool = [r for i, r in rates.items() if spoken_chars(texts[i]) >= 6]
    res.median_rate = statistics.median(pool or list(rates.values()) or [0.0])

    suspects: set[int] = set(hint or ()) & set(stats)
    extreme: set[int] = set()
    for i, st in stats.items():
        if not st.ok:
            continue
        if _is_mute(st):
            res.findings.append(Finding(i, "error", f"哑句:有声占比 {st.voiced_ratio:.0%}、"
                                                    f"平均响度 {st.mean_db:.0f} dB"))
            continue
        if st.max_db >= CLIP_DB:
            res.findings.append(Finding(i, "warn", "削波:峰值贴着 0 dB"))
        if res.median_rate > 0 and spoken_chars(texts[i]) >= 4:
            ratio = rates[i] / res.median_rate
            if ratio < RATE_SUSPECT[0] or ratio > RATE_SUSPECT[1]:
                suspects.add(i)
                if ratio < RATE_EXTREME[0] or ratio > RATE_EXTREME[1]:
                    extreme.add(i)

    bad = {f.index for f in res.findings if f.level == "error"}
    todo = sorted(suspects - bad)
    asr = transcribe if transcribe is not None else (load_asr() if todo else None)
    res.asr_available = asr is not None or transcribe is not None
    for i in todo:
        if asr is not None and len(_CJK.findall(originals[i])) >= MIN_CJK_FOR_ASR:
            try:
                heard = asr(Path(clips[i]))
            except Exception as exc:  # noqa: BLE001
                log.warning("语音质检:第 %d 句转写失败:%s", i + 1, exc)
                heard = None
            if heard is not None:
                res.asr_checked += 1
                sim, other, other_sim = _match(i, heard, texts, originals)
                wrong = sim < thr or (other is not None and other_sim >= OTHER_MIN
                                      and other_sim - sim >= OTHER_MARGIN)
                if wrong:
                    why = f"读错词或音频错位(听起来更像第 {other + 1} 句)"                         if other is not None and other_sim - sim >= OTHER_MARGIN else "读错词或音频错位"
                    res.findings.append(Finding(i, "error", why, round(sim, 2), heard.strip()[:60]))
                else:
                    res.verified.append(i)
                continue
        # 没有转写手段(或这句判断不了):只有语速极端才判错,其余只提醒
        res.findings.append(Finding(
            i, "error" if i in extreme else "warn",
            "语速异常(未转写复核),疑似截断或错位" if i in extreme else "语速偏离全片(未转写复核)"))
    if asr is not None and transcribe is None:
        release_asr()
    return res
