"""⑨ 旁白音轨:解说词 → TTS → 时间轴 → ASS 字幕。

旁白时长决定每拍时长(`explainer.time_beats`),所以旁白先于画面生成。

长视频的两个关键处理:
- 句子按 ≤30 字切分,TTS 语调才不会出现疲劳感;
- 时间轴用 **TTS 实际输出时长** 累加,而不是预估 —— 预估必然漂移。
"""
from __future__ import annotations

import hashlib
import shutil
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
    #: 补跑后仍没有音频的句子(在 sentences 里的位置)
    missing_sentences: list[int] = field(default_factory=list)
    #: 命中句子缓存的句数,以及这一次真正送去合成的字数(= 这次 TTS 的花费)
    cached_sentences: int = 0
    synth_chars: int = 0
    #: BGM 是否来自共享曲库(没有新生成)
    bgm_reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sentence_count": len(self.sentences),
            "total_chars": self.total_chars,
            "total_duration": round(self.total_duration, 3),
            "ass_path": str(self.ass_path) if self.ass_path else None,
            "narration_path": str(self.narration_path) if self.narration_path else None,
            "bgm_path": str(self.bgm_path) if self.bgm_path else None,
            "cached_sentences": self.cached_sentences,
            "synth_chars": self.synth_chars,
            "bgm_reused": self.bgm_reused,
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
    voice_id: str = "",
    speed: float = 1.0,
    with_bgm: bool = True,
    profile=None,
    size: tuple[int, int] | None = None,
    line_chars: int | None = None,
) -> NarrationResult:
    """生成旁白、字幕与 BGM。任一环节失败都尽量降级而非抛出。

    `size` / `line_chars`:字幕画布尺寸与单行字数,横屏解说轨用(默认竖屏)。
    `voice_id` 空 = 品类 profile 声明的音色,再没有就用全局默认。
    """
    voice_id = voice_id or getattr(profile, "voice_id", "") or audio.DEFAULT_VOICE_ID
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

    texts = [normalize_for_tts(t) for t, _ in pairs]
    shot_indices = [idx for _, idx in pairs]
    result.sentences = texts
    result.total_chars = sum(len(t) for t in texts)

    # ---- TTS ----
    # 同一句话、同一音色语速合成过就直接用缓存:门禁 A 改几句稿子重新确认时,
    # 只为改动的句子付钱(以前是整片重合成)
    by_pos = cached_tts(texts, voice_id=voice_id, speed=speed, dest_dir=audio_dir)
    result.cached_sentences = len(by_pos)
    todo = [i for i in range(len(texts)) if i not in by_pos]
    if todo:
        result.synth_chars += sum(len(texts[i]) for i in todo)
        try:
            assets = audio.synthesize_batch(
                [texts[i] for i in todo],
                voice_id=voice_id,
                speed=speed,
                output_dir=audio_dir,
            )
        except Exception as exc:  # noqa: BLE001
            if not by_pos:
                result.warnings.append(f"TTS 失败:{exc}")
                return result
            assets = []
            result.warnings.append(f"TTS 部分失败,将补跑:{exc}")
        # 失败的句子不在结果里,而且后面的会整体前移 —— 必须按输入位置对回原句,
        # 否则一句失败,之后每条字幕都挂到错的镜头上。
        for n, a in enumerate(assets):
            by_pos[todo[a.index if getattr(a, "index", -1) >= 0 else n]] = a
    missing = [i for i in range(len(texts)) if i not in by_pos]
    if missing:
        # 批量接口偶发整批失败(限流、超时),补跑一次通常就齐了
        try:
            retry = audio.synthesize_batch([texts[i] for i in missing], voice_id=voice_id,
                                           speed=speed, output_dir=audio_dir,
                                           file_prefix="tts_retry")
        except Exception as exc:  # noqa: BLE001
            retry = []
            result.warnings.append(f"TTS 补跑失败:{exc}")
        for n, a in enumerate(retry):
            pos = missing[a.index if getattr(a, "index", -1) >= 0 else n]
            by_pos[pos] = a
        missing = [i for i in range(len(texts)) if i not in by_pos]
    store_tts(texts, by_pos, voice_id=voice_id, speed=speed)
    if missing:
        result.missing_sentences = missing
        result.warnings.append(
            f"TTS 返回 {len(texts) - len(missing)} 条,期望 {len(texts)} 条,"
            f"缺失第 {[i + 1 for i in missing[:10]]} 句,这几句将无旁白"
        )
    if not by_pos:
        result.warnings.append("TTS 未产出任何音频")
        return result

    order = sorted(by_pos)
    assets = [by_pos[i] for i in order]
    result.audio_paths = [a.local_path for a in assets]
    used_texts = [texts[i] for i in order]
    used_shots = [shot_indices[i] for i in order]

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

    w, h = size or (None, None)
    ass_content = format_ass(cues, width=w, height=h, font_name=_pick_font_name(),
                             line_chars=line_chars)
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
        bgm, reused = library_bgm(profile.music_prompt, audio_dir, project_id)
        if bgm:
            result.bgm_path = bgm
            result.bgm_reused = reused
        else:
            result.warnings.append("BGM 生成失败,将输出无背景音乐的成片")

    result.ok = True
    db.write_json(project_id, "narration.json", result.to_dict())
    return result


# --------------------------------------------------------------------------
# 缓存:逐句 TTS 与共享 BGM 曲库(跨项目,放在 data/_cache 下)
# --------------------------------------------------------------------------
#: 同一条 BGM 提示词最多攒几首。攒满后只复用不再生成,按项目轮换免得每片同一首
BGM_LIBRARY_SIZE = 3


def _tts_key(text: str, voice_id: str, speed: float) -> str:
    raw = f"{config.TTS_MODEL}|{voice_id}|{speed:.3f}|{text}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_root() -> Path:
    return config.DATA_DIR / "_cache"


def tts_cache_dir() -> Path:
    return cache_root() / "tts"


def cached_tts(texts: Sequence[str], *, voice_id: str, speed: float,
               dest_dir: Path) -> dict[int, audio.AudioAsset]:
    """命中缓存的句子复制进项目目录,返回 {句子位置: 音频}。"""
    out: dict[int, audio.AudioAsset] = {}
    cache = tts_cache_dir()
    if not cache.is_dir():
        return out
    for i, text in enumerate(texts):
        src = next((p for p in cache.glob(_tts_key(text, voice_id, speed) + ".*")
                    if p.stat().st_size > 0), None)
        if src is None:
            continue
        dest = dest_dir / f"tts_cached_{i:03d}{src.suffix}"
        try:
            shutil.copyfile(src, dest)
        except OSError:
            continue
        out[i] = audio.AudioAsset(text=text, local_path=dest, node_id="cache", index=i)
    return out


def store_tts(texts: Sequence[str], by_pos: dict[int, Any], *, voice_id: str,
              speed: float) -> None:
    """新合成的句子写进缓存。写失败无所谓,下次再合成而已。"""
    cache = tts_cache_dir()
    for i, a in by_pos.items():
        if getattr(a, "node_id", "") == "cache":
            continue
        src = Path(a.local_path)
        if not src.is_file() or src.stat().st_size == 0:
            continue
        try:
            cache.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, cache / f"{_tts_key(texts[i], voice_id, speed)}{src.suffix}")
        except OSError:
            pass


def bgm_library_dir(prompt: str) -> Path:
    key = hashlib.sha1(f"{config.MUSIC_MODEL}|{prompt}".encode("utf-8")).hexdigest()[:12]
    return cache_root() / "bgm" / key


def _own_bgm(project_id: str) -> Path | None:
    """自备曲库(config.BGM_DIR,默认 data/bgm)按项目 id 轮换挑一首;没有则 None。"""
    root = config.BGM_DIR or (config.DATA_DIR / "bgm")
    if not root.is_dir():
        return None
    tracks = sorted(p for p in root.iterdir()
                    if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac") and p.stat().st_size > 0)
    if not tracks:
        return None
    return tracks[int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(tracks)]


def library_bgm(prompt: str, audio_dir: Path, project_id: str) -> tuple[Path | None, bool]:
    """给项目一首 BGM。返回 (路径, 是否复用)。

    - 这个项目已经有 bgm.mp3(门禁 A 重新确认)→ 原样复用;
    - 曲库没攒满 → 生成一首新的放进曲库;生成失败退回曲库里已有的;
    - 曲库攒满 → 按项目 id 轮换挑一首。
    """
    dest = audio_dir / "bgm.mp3"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest, True
    own = _own_bgm(project_id)
    if own:
        try:
            shutil.copyfile(own, dest)
        except OSError:
            return own, True
        return dest, True
    lib = bgm_library_dir(prompt)
    tracks = sorted(p for p in lib.glob("*.mp3") if p.stat().st_size > 0) if lib.is_dir() else []
    reused = True
    if len(tracks) < BGM_LIBRARY_SIZE:
        made = audio.generate_music(prompt=prompt, output_dir=lib,
                                    output_file=f"{len(tracks):02d}.mp3")
        if made and Path(made.local_path).is_file():
            reused = False
            tracks.append(Path(made.local_path))
    if not tracks:
        return None, False
    if reused:
        n = int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(tracks)
        pick = tracks[n]
    else:
        pick = tracks[-1]
    try:
        shutil.copyfile(pick, dest)
    except OSError:
        return pick, reused
    return dest, reused


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
               subtitle: str = "", size: tuple[int, int] | None = None) -> Path | None:
    """抽帧 + Pillow 合成带标题的封面(默认竖屏,`size` 给横屏尺寸)。"""
    from PIL import Image, ImageDraw, ImageFont

    dest = Path(dest)
    raw = dest.parent / "_cover_raw.jpg"
    if not extract_cover_frame(video_path, raw, at=1.0):
        return None
    try:
        image = Image.open(raw).convert("RGB")
        # 统一裁剪为成片画幅
        out_w, out_h = size or (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)
        target_ratio = out_w / out_h
        w, h = image.size
        if w / h > target_ratio:
            new_w = int(h * target_ratio)
            left = (w - new_w) // 2
            image = image.crop((left, 0, left + new_w, h))
        else:
            new_h = int(w / target_ratio)
            top = (h - new_h) // 3
            image = image.crop((0, top, w, top + new_h))
        image = image.resize((out_w, out_h), Image.LANCZOS)

        draw = ImageDraw.Draw(image, "RGBA")
        # 底部渐变条,保证文字可读
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        band_top = int(out_h * 0.52)
        for y in range(band_top, out_h):
            alpha = int(200 * (y - band_top) / (out_h - band_top))
            odraw.line([(0, y), (out_w, y)], fill=(0, 0, 0, alpha))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

        draw = ImageDraw.Draw(image)
        font_path = _font_file(bold=True)
        sub_font_path = _font_file(bold=False)
        try:
            title_font = ImageFont.truetype(font_path, 72) if font_path else ImageFont.load_default()
            sub_font = ImageFont.truetype(sub_font_path, 40) if sub_font_path else ImageFont.load_default()
        except OSError:
            title_font = sub_font = ImageFont.load_default()

        y = int(out_h * 0.60)
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
