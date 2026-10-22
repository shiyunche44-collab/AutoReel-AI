"""⑨ 旁白音轨:解说词 → TTS → 时间轴 → ASS 字幕。

旁白时长决定每拍时长(`explainer.time_beats`),所以旁白先于画面生成。

长视频的两个关键处理:
- 句子按 ≤30 字切分,TTS 语调才不会出现疲劳感;
- 时间轴用 **TTS 实际输出时长** 累加,而不是预估 —— 预估必然漂移。
"""
from __future__ import annotations

import hashlib
import os
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
    # 字幕用原文:TTS 文本把数字归一化成了中文读法("六百四十三公里"),
    # 上屏得是"643 公里""kWh",观众才能扫读、截图,也才和画面上的大字对得上
    used_texts = [pairs[i][0].strip() for i in order]
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
    if with_bgm and profile is not None and config.BGM_ENABLED:
        bgm, reused = library_bgm(profile.music_prompt, audio_dir, project_id, profile)
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


def _own_bgm(project_id: str, profile: Any = None) -> Path | None:
    """自备曲库按项目 id 轮换挑一首:先看 `<BGM_DIR>/<品类>/`,再看 `<BGM_DIR>/` 根目录。
    都没有、且 profile 声明了 bgm_genres,就从 Mixkit 自动收一批(失败当没有)。"""
    root = config.BGM_DIR or (config.DATA_DIR / "bgm")
    exts = (".mp3", ".m4a", ".wav", ".aac")

    def _tracks(d: Path) -> list[Path]:
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.suffix.lower() in exts and p.stat().st_size > 0)

    sub = root / getattr(profile, "name", "") if profile is not None else root
    tracks = _tracks(sub) or _tracks(root)
    genres = getattr(profile, "bgm_genres", ()) if profile is not None else ()
    if not tracks and genres and config.BGM_AUTO_COLLECT:
        from ..providers import bgm_source

        bgm_source.collect(genres, sub)
        tracks = _tracks(sub)
    if not tracks:
        return None
    return tracks[int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(tracks)]


def library_bgm(prompt: str, audio_dir: Path, project_id: str, profile: Any = None) -> tuple[Path | None, bool]:
    """给项目一首 BGM。返回 (路径, 是否复用)。

    - 这个项目已经有 bgm.mp3(门禁 A 重新确认)→ 原样复用;
    - 曲库没攒满 → 生成一首新的放进曲库;生成失败退回曲库里已有的;
    - 曲库攒满 → 按项目 id 轮换挑一首。
    """
    dest = audio_dir / "bgm.mp3"
    if dest.is_file() and dest.stat().st_size > 0:
        return dest, True
    own = _own_bgm(project_id, profile)
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


#: 封面抽帧时刻:开场前 3 秒是冷开场的大字(黄金三秒),取它之后的干净画面,
#: 免得封面标题和视频里的大字叠在一起
COVER_AT = 3.6

#: 封面版式:留白、强调色、正文色
COVER_MARGIN = 76
COVER_ACCENT = (44, 200, 232)
COVER_TEXT = (245, 247, 250)
COVER_SUB = (198, 206, 216)
#: 浅底(白底棚拍图)上的配色:深字 + 白描边
COVER_DARK_TEXT = (16, 21, 28)
COVER_DARK_SUB = (74, 82, 94)
COVER_DARK_ACCENT = (0, 116, 146)
#: 封面标题最多几行、最大字号、最小字号
COVER_MAX_LINES = 2
COVER_TITLE_MAX = 92
COVER_TITLE_MIN = 40
#: 文字区(画面下三分之一)平均亮度高于它就按浅底排版 —— 白底图上用白字等于隐形
COVER_LIGHT_LUMA = 138


def _cover_tokens(text: str) -> list[str]:
    """切成排版单元:中文逐字,拉丁字母与数字不拆词。"""
    out: list[str] = []
    buf = ""
    for ch in text or "":
        if ch.isascii() and (ch.isalnum() or ch in "._-%"):
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
    if buf:
        out.append(buf)
    return out


def _cover_wrap(text: str, font, max_width: int, max_lines: int) -> list[str]:
    """按像素宽度折行,超出的部分用省略号收尾。"""
    lines: list[str] = []
    cur = ""
    rest = False
    for tok in _cover_tokens(text):
        trial = cur + tok
        if not cur or font.getlength(trial) <= max_width:
            cur = trial
            continue
        if len(lines) + 1 >= max_lines:
            rest = True
            break
        lines.append(cur)
        cur = tok
    if cur and not rest:
        lines.append(cur)
    if rest and lines:
        while lines and font.getlength(lines[-1] + "…") > max_width:
            lines[-1] = lines[-1][:-1]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines[:max_lines]


def _cover_fit(text: str, font_path: str | None, max_width: int) -> tuple[list[str], Any]:
    """标题自适应缩排:在允许的字号里挑最大的那个,排成不超过两行。"""
    from PIL import ImageFont

    size = COVER_TITLE_MAX
    lines: list[str] = []
    font = None
    while size >= COVER_TITLE_MIN:
        try:
            font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
        except OSError:
            font = ImageFont.load_default()
        lines = _cover_wrap(text, font, max_width, COVER_MAX_LINES)
        if len(lines) <= COVER_MAX_LINES and all(
                font.getlength(line) <= max_width for line in lines):
            return lines, font
        size -= 6
    return lines, font


def _cover_is_light(image: Any) -> bool:
    """判断底图是不是浅色(白底棚拍官图)。

    浅底上白字等于隐形,必须换成深字。判据:画面下三分之一(文字要放的地方)
    的平均亮度 —— 棚拍白底图那一带接近纯白,实拍/视频帧那一带是暗的。
    """
    from PIL import ImageStat

    try:
        w, h = image.size
        band = image.crop((0, int(h * 0.66), w, h)).convert("L").resize((64, 24))
        return float(ImageStat.Stat(band).mean[0]) >= COVER_LIGHT_LUMA
    except Exception:  # noqa: BLE001
        return False


def make_cover(video_path: str | Path, title: str, dest: str | Path,
               subtitle: str = "", size: tuple[int, int] | None = None,
               still: str | Path | None = None, kicker: str = "") -> Path | None:
    """封面:优先用干净的官方图当底,没有才从成片抽帧。

    封面是唯一决定点击率的一帧,不能是"随便截的一帧"——从成片抽出来的画面上
    带着章节条和字幕,标题再压上去就是三层字叠在一起。所以:
    - 有官图(`still`)就用官图:干净的整车,没有烧录字;
    - 只能抽帧时把底部的章节条裁掉,再把字往上放;
    - 标题按宽度**自适应缩排**,不再 `title[:14]` 硬截断;
    - 白底棚拍图自动换成深色字,不然白字压在白底上等于没有标题。
    """
    from PIL import Image, ImageDraw, ImageFont

    dest = Path(dest)
    out_w, out_h = size or (config.OUTPUT_WIDTH, config.OUTPUT_HEIGHT)
    image = None
    from_video = False
    if still and Path(still).is_file():
        try:
            image = Image.open(still).convert("RGB")
        except Exception:  # noqa: BLE001 - 官图打不开就退回抽帧
            image = None
    if image is None:
        raw = dest.parent / "_cover_raw.jpg"
        total = probe_safe(video_path, default=0.0)
        at = min(COVER_AT, total / 2) if total > 0 else 1.0
        if not extract_cover_frame(video_path, raw, at=at):
            return None
        try:
            image = Image.open(raw).convert("RGB")
            from_video = True
        except Exception:  # noqa: BLE001
            return None
        finally:
            if raw.is_file():
                try:
                    raw.unlink()
                except OSError:
                    pass
    portrait = out_h > out_w
    try:
        if portrait:
            return _portrait_cover(image, from_video, title, dest, subtitle, (out_w, out_h), kicker)
        # 裁成成片画幅。抽帧来的先切掉底部 7%(章节条在那儿),再取景;
        # 官图不用切,直接取景。
        w, h = image.size
        if from_video:
            h = int(h * 0.93)
            image = image.crop((0, 0, w, h))
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

        light = _cover_is_light(image)
        text_color = COVER_DARK_TEXT if light else COVER_TEXT
        sub_color = COVER_DARK_SUB if light else COVER_SUB
        accent = COVER_DARK_ACCENT if light else COVER_ACCENT
        stroke = (255, 255, 255) if light else (0, 0, 0)

        # 底部渐变,保证文字可读。浅底上用白色渐变(不然白字压在白底上等于没有)
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        if light:
            # 浅底:白色渐变从更低处起、浓度也低 —— 白底棚拍图的主体就压在这
            # 一带上,渐变一早就压上去等于把车洗白。
            band_top, max_alpha, gamma = int(out_h * 0.60), 205, 1.5
            grad = (255, 255, 255, max_alpha)
        else:
            band_top, max_alpha, gamma = int(out_h * 0.42), 205, 0.85
            grad = (8, 10, 14, max_alpha)
        for y in range(band_top, out_h):
            alpha = int(grad[3] * ((y - band_top) / (out_h - band_top)) ** gamma)
            odraw.line([(0, y), (out_w, y)], fill=(grad[0], grad[1], grad[2], alpha))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

        draw = ImageDraw.Draw(image)
        font_path = _font_file(bold=True)
        sub_font_path = _font_file(bold=False)
        try:
            kicker_font = ImageFont.truetype(sub_font_path, 34) if sub_font_path \
                else ImageFont.load_default()
            sub_font = ImageFont.truetype(sub_font_path, 40) if sub_font_path \
                else ImageFont.load_default()
        except OSError:
            kicker_font = sub_font = ImageFont.load_default()

        max_width = out_w - COVER_MARGIN * 2
        lines, title_font = _cover_fit(title, font_path, max_width)
        line_h = int(title_font.getlength("测") * 1.32) if title_font else 52
        block_h = line_h * len(lines)
        # 副标题(主论点)放在标题下面
        sub_lines = _cover_wrap(subtitle, sub_font, max_width, 2) if subtitle else []
        if sub_lines:
            block_h += 16 + line_h * 0.8 * len(sub_lines)
        y = out_h - COVER_MARGIN - block_h

        if kicker:
            draw.rectangle([COVER_MARGIN, y, COVER_MARGIN + 6, y + 34], fill=accent)
            draw.text((COVER_MARGIN + 18, y), kicker, font=kicker_font, fill=accent,
                      stroke_width=2, stroke_fill=stroke)
            y += 52

        for line in lines:
            draw.text((COVER_MARGIN, y), line, font=title_font, fill=text_color,
                      stroke_width=3, stroke_fill=stroke)
            y += line_h
        if sub_lines:
            y += 16
            for line in sub_lines:
                draw.text((COVER_MARGIN, y), line, font=sub_font, fill=sub_color,
                          stroke_width=2, stroke_fill=stroke)
                y += int(line_h * 0.8)

        # 原子写:先写临时文件再改名。进程在保存时被杀会留下半张封面 ——
        # 封面是决定点击率的那一帧,半张比没有更糟(看着像有,其实打不开)。
        #
        # 临时文件**保留原扩展名**:PIL 靠扩展名推断格式,叫 "cover.jpg.part"
        # 它会直接抛 "unknown file extension"。
        tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
        try:
            image.save(tmp, quality=92)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return dest
    except Exception:  # noqa: BLE001
        return None


#: 小红书封面是 3:4。竖屏封面从顶上裁一张 1080×1440(标题区 + 画面带都在里面)
COVER_3X4_SUFFIX = "_3x4"


def cover_3x4_path(cover: str | Path) -> Path:
    """竖屏封面旁边那张 3:4 裁切版的路径。"""
    cover = Path(cover)
    return cover.with_name(f"{cover.stem}{COVER_3X4_SUFFIX}{cover.suffix}")


def _portrait_cover(image: Any, from_video: bool, title: str, dest: Path, subtitle: str,
                    size: tuple[int, int], kicker: str) -> Path | None:
    """竖屏封面,三段式:模糊压暗的同图铺底,整车完整放在画面带,标题放在上方标题区。

    宽图直接裁成 9:16 会把车头车尾裁掉 —— 封面上的车必须是完整的。
    抽帧来的(成片里截的)只取画面带那一截:标题区和数据区上已经有字了。
    另存一张 3:4 的裁切版给小红书(`cover_3x4_path`)。"""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    from .aspect import portrait_grid

    out_w, out_h = size
    g = portrait_grid(size)
    if from_video:
        fw, fh = image.size
        scale = fh / out_h
        image = image.crop((0, int(g.band.top * scale), fw, int(g.band.bottom * scale)))
    # 铺底:同图放大铺满,模糊、压暗
    w, h = image.size
    k = max(out_w / w, out_h / h)
    bg = image.resize((int(w * k) + 1, int(h * k) + 1), Image.LANCZOS)
    left, top = (bg.width - out_w) // 2, (bg.height - out_h) // 2
    bg = bg.crop((left, top, left + out_w, top + out_h)).filter(ImageFilter.GaussianBlur(32))
    bg = Image.blend(bg, Image.new("RGB", size, (8, 10, 14)), 0.55)
    # 主图:按宽度放进画面带(比画面带略大一点也行,完整优先)
    k = out_w / w
    main = image.resize((out_w, max(1, int(h * k))), Image.LANCZOS)
    y_main = max(g.title.bottom - 40, g.band.mid - main.height // 2)
    bg.paste(main, (0, y_main))

    draw = ImageDraw.Draw(bg)
    font_path = _font_file(bold=True)
    sub_font_path = _font_file(bold=False)
    try:
        kicker_font = ImageFont.truetype(sub_font_path, 38) if sub_font_path else ImageFont.load_default()
        sub_font = ImageFont.truetype(sub_font_path, 44) if sub_font_path else ImageFont.load_default()
    except OSError:
        kicker_font = sub_font = ImageFont.load_default()
    max_width = out_w - g.left * 2
    lines, title_font = _cover_fit(title, font_path, max_width)
    line_h = int(title_font.getlength("测") * 1.32) if title_font else 52
    y = g.title.top + 10
    if kicker:
        draw.rectangle([g.left, y, g.left + 6, y + 38], fill=COVER_ACCENT)
        draw.text((g.left + 18, y), kicker, font=kicker_font, fill=COVER_ACCENT,
                  stroke_width=2, stroke_fill=(0, 0, 0))
        y += 60
    for line in lines:
        draw.text((g.left, y), line, font=title_font, fill=COVER_TEXT, stroke_width=3,
                  stroke_fill=(0, 0, 0))
        y += line_h
    # 副标题(主论点)放在画面带下面的数据区
    sub_lines = _cover_wrap(subtitle, sub_font, max_width, 2) if subtitle else []
    y = max(y_main + main.height, g.data.top) + 36
    for line in sub_lines:
        draw.text((g.left, y), line, font=sub_font, fill=COVER_SUB, stroke_width=2,
                  stroke_fill=(0, 0, 0))
        y += int(line_h * 0.8)

    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    try:
        bg.save(tmp, quality=92)
        os.replace(tmp, dest)
        three_four = bg.crop((0, 0, out_w, out_w * 4 // 3))
        tmp34 = cover_3x4_path(dest).with_name(f"{cover_3x4_path(dest).stem}.part{dest.suffix}")
        three_four.save(tmp34, quality=92)
        os.replace(tmp34, cover_3x4_path(dest))
    except BaseException:
        for t in (tmp, cover_3x4_path(dest).with_name(f"{cover_3x4_path(dest).stem}.part{dest.suffix}")):
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    return dest


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
