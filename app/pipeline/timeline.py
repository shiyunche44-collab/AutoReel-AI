"""时间轴、字幕与时长探测。

竖屏长视频(10-30 镜头)里,时间轴算错会同时毁掉三件事:
镜头拼接错位、字幕对不上旁白、音画不同步。因此这里的所有计算都是纯函数并配单测。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import config
from ..models import SubtitleCue


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


# --------------------------------------------------------------------------
# 镜头时间轴
# --------------------------------------------------------------------------
def shot_timeline(durations: Sequence[float]) -> list[Segment]:
    """按顺序累加得到每个镜头的时间区间。"""
    segments: list[Segment] = []
    cursor = 0.0
    for d in durations:
        end = cursor + float(d)
        segments.append(Segment(round(cursor, 3), round(end, 3)))
        cursor = end
    return segments


def total_duration(durations: Sequence[float]) -> float:
    return round(sum(float(d) for d in durations), 3)


# --------------------------------------------------------------------------
# 句子切分
# --------------------------------------------------------------------------
_SENTENCE_END = "。!?!?;;"


def split_sentences(text: str, max_chars: int | None = None) -> list[str]:
    """把解说词切成适合 TTS 的短句。

    中文口播的经验上限是 30 字:超过后语调会疲劳、停顿会变得机械。
    超过上限的句子在逗号/顿号处二次切分,尽量不破坏语义。
    """
    limit = max_chars or config.MAX_TTS_CHARS
    text = (text or "").strip()
    if not text:
        return []

    rough: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENTENCE_END:
            rough.append(buf)
            buf = ""
    if buf.strip():
        rough.append(buf)

    out: list[str] = []
    for sentence in rough:
        s = sentence.strip()
        if not s:
            continue
        if len(s) <= limit:
            out.append(s)
            continue
        # 二次切分:优先在逗号处断开
        piece = ""
        for ch in s:
            piece += ch
            if len(piece) >= limit and ch in ",、,;:":
                out.append(piece.strip())
                piece = ""
            elif len(piece) >= limit + 6:
                out.append(piece.strip())
                piece = ""
        if piece.strip():
            out.append(piece.strip())
    return [s for s in out if s]


# --------------------------------------------------------------------------
# 中文数字规范化(降低 TTS 读错概率)
# --------------------------------------------------------------------------
_CN_DIGITS = "零一二三四五六七八九"
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def number_to_chinese(n: int) -> str:
    """把 0-99999 的整数转成中文读法(不含单位)。

    处理了中文数字的两个特殊读法:"十"而非"一十"、"一百"而非"一零零"。
    """
    if n == 0:
        return "零"
    if n < 0:
        return "负" + number_to_chinese(-n)
    if n >= 10000:
        head, rest = divmod(n, 10000)
        out = number_to_chinese(head) + "万"
        if rest:
            if rest < 1000:
                out += "零"
            out += number_to_chinese(rest)
        return out

    units = ("", "十", "百", "千")
    digits = str(n)
    length = len(digits)
    result = ""
    zero_pending = False
    for i, ch in enumerate(digits):
        value = int(ch)
        pos = length - i - 1  # 0=个位 1=十位 2=百位 3=千位
        if value == 0:
            zero_pending = True
            continue
        if zero_pending and result:
            result += "零"
        zero_pending = False
        # "十二" 读作 shí èr,十位的一不读出来(仅两位数 10-19 适用)
        if not (value == 1 and pos == 1 and length == 2):
            result += _CN_DIGITS[value]
        result += units[pos]
    return result or "零"


def _decimal_to_chinese(text: str) -> str:
    if "." not in text:
        return number_to_chinese(int(text))
    int_part, dec_part = text.split(".", 1)
    whole = number_to_chinese(int(int_part)) if int_part else "零"
    digits = "".join(_CN_DIGITS[int(c)] for c in dec_part if c.isdigit())
    return f"{whole}点{digits}"


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def normalize_for_tts(text: str) -> str:
    """把阿拉伯数字转成中文读法,并规范常见单位。

    "702公里" → "七百零二公里","3.8秒" → "三点八秒"。
    这是解说视频准确率的隐形分水岭:TTS 读阿拉伯数字在型号与小数上极易出错。
    """
    if not text:
        return ""

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        return _decimal_to_chinese(raw)

    out = _NUM_RE.sub(repl, text)
    # 常见口语化读法修正。顺序很重要:必须先匹配 kWh,否则会被 Wh 先吃掉变成 "k瓦时"。
    out = out.replace("km/h", "公里每小时").replace("KM/H", "公里每小时")
    out = out.replace("kWh", "千瓦时").replace("KWh", "千瓦时")
    out = out.replace("Wh", "瓦时")
    out = re.sub(r"\s+", " ", out)
    return out.strip()


# --------------------------------------------------------------------------
# 字幕
# --------------------------------------------------------------------------
def build_subtitle_cues(
    sentences: Sequence[str],
    audio_durations: Sequence[float],
    *,
    shot_indices: Sequence[int] | None = None,
    gap: float | None = None,
) -> list[SubtitleCue]:
    """把逐句音频的实际时长累加成字幕时间轴。

    用 TTS **实际输出**的时长而不是预估 —— 预估必然漂移,长视频里累积误差
    会让后半段字幕整体失准。
    """
    gap_s = (gap if gap is not None else config.SENTENCE_GAP_MS / 1000.0)
    cues: list[SubtitleCue] = []
    cursor = 0.0
    for i, (text, dur) in enumerate(zip(sentences, audio_durations)):
        start = round(cursor, 3)
        end = round(cursor + float(dur), 3)
        shot_idx = shot_indices[i] if shot_indices and i < len(shot_indices) else 0
        cues.append(
            SubtitleCue(index=i + 1, start=start, end=end, text=text, shot_idx=shot_idx)
        )
        cursor = end + gap_s
    return cues


def _ass_time(seconds: float) -> str:
    """ASS 时间格式 H:MM:SS.cc"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def wrap_subtitle(text: str, max_chars: int | None = None) -> str:
    """按字数折行,竖屏小屏上单行太长会顶到画面边缘。"""
    limit = max_chars or config.MAX_SUBTITLE_LINE_CHARS
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    # 只超出一点点时从中间断:在上限处切,第二行只剩「里。」「。」这种孤字(竖屏实跑见过)
    target = limit if len(text) - limit >= 4 else (len(text) + 1) // 2
    hi = min(limit, len(text) - 1)          # 第一行最多到这里
    near = sorted(range(max(1, target - 6), hi + 1), key=lambda m: (abs(m - target), -m))

    def fits(m: int) -> bool:
        # 两行放得下时第二行也不许超长;本来就超过两行的长句只管第一行
        return len(text) - m <= limit or len(text) > 2 * limit

    # 1. 逗号后面断(模型写的多是全角标点)
    mid = next((m for m in near if text[m - 1] in ",、;:，；：" and fits(m)), 0)
    # 2. 数字/英文串开头之前("752V平台15分钟补 / 405公里");不在数字和单位之间断("15 / 分钟")
    if not mid:
        mid = next((m for m in near if abs(m - target) <= 4 and _is_word(text[m])
                    and not _is_word(text[m - 1]) and fits(m)), 0)
    # 3. 硬切不能切进数字或英文串("405 公里"被拆成"4 / 05公里"):退到这串的开头
    if not mid:
        mid = k = target
        while k > 0 and _is_word(text[k - 1]) and _is_word(text[k]):
            k -= 1
        if k > target // 2:
            mid = k
    # 第二行不以标点开头:标点留在第一行行尾
    while mid < len(text) and text[mid] in _NO_LINE_START:
        mid += 1
    head, tail = text[:mid].strip(), text[mid:].strip()
    return f"{head}\\N{tail}" if tail else head


def _is_word(ch: str) -> bool:
    """英文 / 数字串里的字符(小数点算数字的一部分:「0.245Cd」不能拆开)。"""
    return ch.isascii() and (ch.isalnum() or ch == ".")


#: 不许出现在行首的标点
_NO_LINE_START = "，。、；：！？,.;:!?)）」』”"


def format_ass(
    cues: Sequence[SubtitleCue],
    *,
    width: int | None = None,
    height: int | None = None,
    font_name: str = "Microsoft YaHei",
    line_chars: int | None = None,
) -> str:
    """生成 ASS 字幕文件内容。

    显式指定 `fontsdir` 与字体名,否则 libass 在 Windows 上可能回退到
    不含中文字形的字体,烧录出来是一排方块。

    **字号与底条是按手机端定的,不是按大屏定的**:
    1920×1080 烧进平台后按手机屏宽缩放,45px 的字到手机上约等于 10px ——
    等于没有字幕。所以字号取 60px(≈h×0.056),并加一层半透明黑底:
    亮背景(白墙、天空、车漆)上纯白字加描边照样看不清,底条才管用。
    """
    w = width or config.OUTPUT_WIDTH
    h = height or config.OUTPUT_HEIGHT
    # 字号按**短边**缩放,不按高度:1920×1080 与 1080×1920 的短边都是 1080,
    # 字号就一样(60px)。按高度算的话竖版会得到 107px —— 一行放不下 15 个字。
    font_size = max(24, int(min(w, h) * 0.056))
    margin_v = int(h * 0.115)
    # 半透明底条:BorderStyle=3(盒子)而不是 1(描边)。
    # 盒子模式下 **OutlineColour 才是盒子的填充色**(描边模式下它才是描边色),
    # &H80000000 = 50% 透明的黑(ASS 的颜色是 &HAABBGGRR,AA 越大越透明)。
    # 全黑会挡住画面,纯描边在亮背景(白墙 / 天空 / 车漆)上照样糊。
    border_style = 3
    outline = max(3, font_size // 6)      # 盒子模式下这个值是"盒子厚度"(留白)
    shadow = 0
    box_colour = "&H80000000"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,{box_colour},&H00000000,-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},2,{int(w * 0.06)},{int(w * 0.06)},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for cue in cues:
        body = wrap_subtitle(cue.text, line_chars)
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},"
            f"Default,,0,0,0,,{body}"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# 媒体探测
# --------------------------------------------------------------------------
def probe_duration(media_path: str | Path) -> float:
    """用 ffprobe 读取媒体时长(秒)。失败抛 RuntimeError。"""
    if not config.FFPROBE_PATH:
        raise RuntimeError("ffprobe 不可用,无法探测时长")
    cmd = [
        str(config.FFPROBE_PATH), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(media_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {proc.stderr[:200]}")
    data = json.loads(proc.stdout or "{}")
    duration = (data.get("format") or {}).get("duration")
    if duration is None:
        raise RuntimeError(f"未能读取时长: {media_path}")
    return float(duration)


def probe_safe(media_path: str | Path, default: float = 0.0) -> float:
    """探测时长,失败时返回默认值 —— 用于不阻断流水线的场景。"""
    try:
        return probe_duration(media_path)
    except Exception:
        return default


def wav_duration_hint(audio_path: str | Path) -> float:
    """不依赖 ffprobe 的兜底:读 WAV 头估算时长。"""
    path = Path(audio_path)
    if path.suffix.lower() != ".wav":
        return 0.0
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return round(frames / float(rate), 3)
    except Exception:
        return 0.0


def narration_budget_chars(duration_seconds: float, chars_per_second: float = 5.8) -> int:
    """按中文口播语速估算一段视频能承载的字数。

    5.8 字/秒 ≈ 每分钟 350 字,是口播视频的常见密度。
    """
    return int(duration_seconds * chars_per_second)


def concat_list_file(paths: Iterable[Path], dest: Path) -> Path:
    """为 ffmpeg concat demuxer 生成列表文件。

    concat demuxer 要求路径为正斜杠,且单引号需转义。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for p in paths:
        safe = str(Path(p).resolve()).replace("\\", "/").replace("'", r"'\''")
        lines.append(f"file '{safe}'")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest
