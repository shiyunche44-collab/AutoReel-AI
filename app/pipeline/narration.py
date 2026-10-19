"""⑨ 旁白音轨:解说词 → TTS → 时间轴 → ASS 字幕。

为什么要单独成轨(而不是让 H3 自己说):
车型名、"702 公里"、"3.8 秒"由视频模型朗读时发音错误率不可控,
且无法逐句微调语速与停顿。解说视频的旁白必须逐字准确,所以:
    H3 出画面 + 环境音(无人声) → 本模块出旁白轨 → render.py 混音。

长视频(10-30 镜头)的两个关键处理:
- 句子按 ≤30 字切分,TTS 语调才不会出现疲劳感;
- 时间轴用 **TTS 实际输出时长** 累加,而不是预估 —— 预估必然漂移。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .. import config, db
from ..models import SubtitleCue
from ..providers import audio
from .timeline import (
    build_subtitle_cues,
    format_ass,
    normalize_for_tts,
    probe_safe,
    split_sentences,
)


@dataclass
class NarrationResult:
    ok: bool
    sentences: list[str] = field(default_factory=list)
    audio_paths: list[Path] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    cues: list[SubtitleCue] = field(default_factory=list)
    ass_path: Path | None = None
    narration_path: Path | None = None
    bgm_path: Path | None = None
    total_chars: int = 0
    total_duration: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sentence_count": len(self.sentences),
            "total_chars": self.total_chars,
            "total_duration": round(self.total_duration, 3),
            "ass_path": str(self.ass_path) if self.ass_path else None,
            "narration_path": str(self.narration_path) if self.narration_path else None,
            "bgm_path": str(self.bgm_path) if self.bgm_path else None,
            "warnings": self.warnings,
            "cues": [c.model_dump() for c in self.cues],
        }


def collect_sentences(specs: Sequence[Any]) -> list[tuple[str, int]]:
    """从分镜表抽取解说词,返回 [(句子, 所属镜头序号)]。

    顺序严格按镜头序号推进 —— 旁白顺序错乱是解说视频最刺眼的低级错误。
    """
    out: list[tuple[str, int]] = []
    for spec in specs:
        text = getattr(spec, "narration_zh", "") or ""
        for sentence in split_sentences(text):
            out.append((sentence, spec.idx))
    return out


def build(
    project_id: str,
    specs: Sequence[Any],
    *,
    voice_id: str = audio.DEFAULT_VOICE_ID,
    speed: float = 1.0,
    narration_mode: str = "tts",
    with_bgm: bool = True,
    profile=None,
) -> NarrationResult:
    """生成旁白、字幕与 BGM。任一环节失败都尽量降级而非抛出。"""
    project_dir = config.project_dir(project_id)
    audio_dir = project_dir / "audio"
    # 字幕在拼接之前就要落盘,目录必须先建好 —— 否则第一次跑就会
    # 因 "No such file or directory: .../audio/subs.ass" 整段失败。
    audio_dir.mkdir(parents=True, exist_ok=True)
    result = NarrationResult(ok=False)

    pairs = collect_sentences(specs)
    if not pairs:
        result.warnings.append("分镜中没有解说词,成片将无旁白")
        return result

    # tts 模式下才需要独立音轨;native 模式下音频已烧进画面
    if narration_mode == "native":
        result.warnings.append("native 模式:旁白已由 H3 原生音频提供,跳过独立 TTS")
        result.ok = True
        return result

    texts = [normalize_for_tts(t) for t, _ in pairs]
    shot_indices = [idx for _, idx in pairs]
    result.sentences = texts
    result.total_chars = sum(len(t) for t in texts)

    # ---- TTS ----
    try:
        assets = audio.synthesize_batch(
            texts,
            voice_id=voice_id,
            speed=speed,
            output_dir=audio_dir,
        )
    except Exception as exc:  # noqa: BLE001
        result.warnings.append(f"TTS 失败:{exc}")
        return result

    if len(assets) != len(texts):
        result.warnings.append(
            f"TTS 返回 {len(assets)} 条,期望 {len(texts)} 条,缺失部分将无旁白"
        )
    if not assets:
        result.warnings.append("TTS 未产出任何音频")
        return result

    result.audio_paths = [a.local_path for a in assets]
    used_texts = [a.text for a in assets]
    used_shots = shot_indices[: len(assets)]

    # ---- 时长探测 ----
    durations: list[float] = []
    for asset in assets:
        dur = probe_safe(asset.local_path, default=0.0)
        if dur <= 0:
            dur = audio_duration_fallback(asset.local_path)
        durations.append(dur)
    result.durations = durations

    if any(d <= 0 for d in durations):
        result.warnings.append("部分音频时长探测失败,已按估算值填充")

    # ---- 字幕时间轴 ----
    cues = build_subtitle_cues(used_texts, durations, shot_indices=used_shots)
    result.cues = cues
    result.total_duration = cues[-1].end if cues else 0.0

    ass_content = format_ass(cues, font_name=_pick_font_name())
    ass_path = audio_dir / "subs.ass"
    ass_path.write_text(ass_content, encoding="utf-8")
    result.ass_path = ass_path

    # ---- 拼接旁白为单轨 ----
    narration_path = audio_dir / "narration.wav"
    ok, concat_err = _concat_audio(result.audio_paths, narration_path)
    if ok:
        result.narration_path = narration_path
    else:
        # 绝不能静默:没有旁白的成片等于废片,必须在界面和日志里说得清清楚楚。
        result.warnings.append(f"旁白拼接失败,成片将没有解说声:{concat_err}")

    # ---- BGM ----
    if with_bgm and profile is not None:
        bgm = audio.generate_music(
            prompt=profile.music_prompt,
            output_dir=audio_dir,
            output_file="bgm.mp3",
        )
        if bgm:
            result.bgm_path = bgm.local_path
        else:
            result.warnings.append("BGM 生成失败,将输出无背景音乐的成片")

    result.ok = True
    db.write_json(project_id, "narration.json", result.to_dict())
    return result


def audio_duration_fallback(path: str | Path, default: float = 2.5) -> float:
    """时长探测的兜底:按字数估算中文语速。

    宁可略有偏差,也不要因为一个音频文件探测失败就让整条流水线停摆。
    """
    from .timeline import wav_duration_hint

    dur = wav_duration_hint(path)
    if dur > 0:
        return dur
    name = Path(path).name
    # 从文件名回推不了字数时,给一个保守的默认
    return default


def _pick_font_name() -> str:
    """选择系统中文字体。优先微软雅黑,回退黑体。"""
    fonts = Path(config.FONTS_DIR)
    if (fonts / "msyh.ttc").is_file():
        return "Microsoft YaHei"
    if (fonts / "simhei.ttf").is_file():
        return "SimHei"
    return "Arial"


def _concat_audio(paths: Sequence[Path], dest: Path) -> tuple[bool, str]:
    """把逐句音频拼成单轨,句间插入静音。返回 (是否成功, 失败原因)。

    句间停顿是长稿节奏的关键:没有间隔的连续拼接听起来像机关枪。

    失败原因必须回传。之前的版本在这里 `except: return False`,
    上层拿不到任何信息,于是"人声拼接失败"被降级成一条没有旁白的成片,
    而界面照样显示合成成功 —— 配音是这套系统的核心,这种静默失败不可接受。
    """
    if not paths:
        return False, "没有待拼接的音频"
    if not config.FFMPEG_PATH:
        return False, "ffmpeg 不可用"

    gap = config.SENTENCE_GAP_MS / 1000.0
    dest.parent.mkdir(parents=True, exist_ok=True)

    inputs: list[str] = []
    filters: list[str] = []
    concat_labels = ""
    for i, p in enumerate(paths):
        inputs += ["-i", str(p)]
        # 每条输入链只产出**一个**流:归一格式后直接在链尾补静音。
        #
        # 之前有两个错误叠在一起:
        #   1) 同一条链里 apad 之后又来一条裸 `apad=…[apN]`(不引用任何输入
        #      流),整条 filter_complex 非法;
        #   2) 于是 concat 收到 [a0][ap0][a1][ap1] 四个标签,而
        #      concat=n=N:v=0:a=1 每个分段只接受一个标签,报
        #      "More input link labels ... 4 > 2"。
        filters.append(
            f"[{i}:a]aresample=48000,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=N/SR/TB,"
            f"apad=pad_dur={gap}[s{i}]"
        )
        concat_labels += f"[s{i}]"
    filters.append(f"{concat_labels}concat=n={len(paths)}:v=0:a=1[out]")

    cmd = [
        str(config.FFMPEG_PATH), "-y", "-v", "error",
        *inputs,
        "-filter_complex", ";".join(filters),
        "-map", "[out]",
        "-ac", "2", "-ar", "48000",
        str(dest),
    ]
    try:
        import subprocess

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as exc:  # noqa: BLE001
        return False, f"调用 ffmpeg 失败:{exc}"
    if proc.returncode != 0:
        return False, f"ffmpeg 退出码 {proc.returncode}:{(proc.stderr or '').strip()[:300]}"
    if not dest.is_file() or dest.stat().st_size == 0:
        return False, "ffmpeg 未产出旁白文件"
    return True, ""


def extract_cover_frame(video_path: str | Path, dest: str | Path, at: float = 1.0) -> Path | None:
    """抽取封面帧。"""
    import subprocess

    if not config.FFMPEG_PATH:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(config.FFMPEG_PATH), "-y", "-v", "error",
        "-ss", f"{at:.2f}", "-i", str(video_path),
        "-frames:v", "1", "-q:v", "2", str(dest),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    return dest if proc.returncode == 0 and dest.is_file() else None


def make_cover(video_path: str | Path, title: str, dest: str | Path,
               subtitle: str = "") -> Path | None:
    """抽帧 + Pillow 合成带标题的封面。"""
    from PIL import Image, ImageDraw, ImageFont

    dest = Path(dest)
    raw = dest.parent / "_cover_raw.jpg"
    if not extract_cover_frame(video_path, raw, at=1.0):
        return None
    try:
        image = Image.open(raw).convert("RGB")
        # 统一裁剪为 9:16
        target_ratio = config.OUTPUT_WIDTH / config.OUTPUT_HEIGHT
        w, h = image.size
        if w / h > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            image = image.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 3
            image = image.crop((0, top, w, top + new_h))
        image = image.resize((config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT), Image.LANCZOS)

        draw = ImageDraw.Draw(image, "RGBA")
        # 底部渐变条,保证文字可读
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        band_top = int(config.OUTPUT_HEIGHT * 0.52)
        for y in range(band_top, config.OUTPUT_HEIGHT):
            alpha = int(200 * (y - band_top) / (config.OUTPUT_HEIGHT - band_top))
            odraw.line([(0, y), (config.OUTPUT_WIDTH, y)], fill=(0, 0, 0, alpha))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

        draw = ImageDraw.Draw(image)
        font_path = _font_file(bold=True)
        sub_font_path = _font_file(bold=False)
        try:
            title_font = ImageFont.truetype(font_path, 72) if font_path else ImageFont.load_default()
            sub_font = ImageFont.truetype(sub_font_path, 40) if sub_font_path else ImageFont.load_default()
        except OSError:
            title_font = sub_font = ImageFont.load_default()

        y = int(config.OUTPUT_HEIGHT * 0.60)
        draw.text((70, y), title[:14], font=title_font, fill=(255, 255, 255),
                  stroke_width=4, stroke_fill=(0, 0, 0))
        if subtitle:
            draw.text((70, y + 100), subtitle[:24], font=sub_font, fill=(230, 230, 230),
                      stroke_width=3, stroke_fill=(0, 0, 0))

        image.save(dest, quality=92)
        return dest
    except Exception:  # noqa: BLE001
        return None
    finally:
        if raw.is_file():
            try:
                raw.unlink()
            except OSError:
                pass


def _font_file(bold: bool = True) -> str | None:
    fonts = Path(config.FONTS_DIR)
    candidates = (
        ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"]
        if bold
        else ["msyh.ttc", "simhei.ttf"]
    )
    for name in candidates:
        path = fonts / name
        if path.is_file():
            return str(path)
    return None


def health_check() -> dict[str, Any]:
    """音色可用性自检。"""
    voices = audio.list_voices()
    return {
        "voice_count": len(voices),
        "default_voice": audio.DEFAULT_VOICE_ID,
        "available": len(voices) > 0,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
