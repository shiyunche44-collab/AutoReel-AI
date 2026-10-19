"""渲染层端到端自检(不消耗任何生成额度)。

用 ffmpeg 的 testsrc 合成几个假镜头,再走真实的归一化 → 拼接 → 烧字幕 →
混音 → 封面链路,验证的是与生产完全相同的代码路径。

用法:
    python -m tests.verify_render
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from app import config
from app.pipeline import narration as narration_mod
from app.pipeline import render, timeline

FF = str(config.FFMPEG_PATH)


def make_fake_shot(dest: Path, seconds: int = 6, hue_shift: int = 0) -> Path:
    """用 testsrc + 正弦音频造一个时长精确的假镜头。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        FF, "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc=size=720x1280:rate=24:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency={320 + hue_shift * 60}:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"合成假镜头失败:{proc.stderr[:400]}")
    return dest


def main() -> int:
    if not config.FFMPEG_PATH:
        print("✗ 未找到 ffmpeg,无法自检")
        return 1

    project_id = "_selftest_render"
    project_dir = config.project_dir(project_id)
    shots_dir = project_dir / "shots"
    print(f"项目目录:{project_dir}")

    # 1. 造 3 个假镜头
    print("\n[1/5] 合成 3 个假镜头 ...")
    shot_paths = []
    for i in range(3):
        p = make_fake_shot(shots_dir / f"{i:03d}.mp4", hue_shift=i)
        shot_paths.append(p)
        print(f"      镜头 {i}:{p.stat().st_size // 1024} KB")

    # 2. 造旁白音轨与字幕
    print("\n[2/5] 生成旁白时间轴与 ASS 字幕 ...")
    sentences = ["这台车的续航是七百零二公里。", "百公里加速三点八秒。", "内饰做工相当扎实。"]
    durs = [2.4, 2.1, 1.9]
    cues = timeline.build_subtitle_cues(sentences, durs, shot_indices=[0, 1, 2], gap=0.3)
    ass_path = project_dir / "audio" / "subs.ass"
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(timeline.format_ass(cues), encoding="utf-8")
    print(f"      ASS 字幕:{len(cues)} 条 -> {ass_path.name}")

    # 3. 造旁白音频与 BGM
    print("\n[3/5] 合成旁白与 BGM 音轨 ...")
    narr = project_dir / "audio" / "narr.wav"
    subprocess.run(
        [FF, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=48000:duration=7.2",
         "-c:a", "pcm_s16le", str(narr)], capture_output=True, timeout=180,
    )
    bgm = project_dir / "audio" / "bgm.mp3"
    subprocess.run(
        [FF, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=180:sample_rate=48000:duration=3",
         "-c:a", "libmp3lame", str(bgm)], capture_output=True, timeout=180,
    )
    print(f"      旁白:{narr.stat().st_size // 1024} KB,BGM:{bgm.stat().st_size // 1024} KB")

    # 4. 合成
    print("\n[4/5] 执行合成(拼接 + 烧字幕 + 混音 + 响度归一化)...")
    t0 = time.time()
    result = render.compose(
        project_id, shot_paths,
        ass_path=ass_path, narration_path=narr, bgm_path=bgm,
    )
    elapsed = time.time() - t0
    if not result.ok:
        print(f"      ✗ 合成失败:{result.warnings}")
        print(result.log[-800:])
        return 1
    print(f"      ✓ 输出:{result.output_path.name} "
          f"({result.output_path.stat().st_size // 1024} KB, "
          f"{result.duration:.2f}s,耗时 {elapsed:.1f}s)")
    if result.warnings:
        print(f"      警告:{result.warnings}")

    # 5. 校验产物
    print("\n[5/5] 校验产物 ...")
    problems = []
    probe = subprocess.run(
        [str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,codec_name,r_frame_rate",
         "-show_entries", "format=duration", "-of", "json", str(result.output_path)],
        capture_output=True, text=True, timeout=120,
    )
    import json
    info = json.loads(probe.stdout or "{}")
    stream = (info.get("streams") or [{}])[0]
    duration = float((info.get("format") or {}).get("duration", 0))

    checks = [
        ("分辨率为 1080x1920",
         stream.get("width") == 1080 and stream.get("height") == 1920,
         f"{stream.get('width')}x{stream.get('height')}"),
        ("编码为 H.264", stream.get("codec_name") == "h264", str(stream.get("codec_name"))),
        ("时长为 18 秒(3×6s)", abs(duration - 18.0) < 0.6, f"{duration:.2f}s"),
        ("输出文件非空", result.output_path.stat().st_size > 100_000, ""),
    ]
    for name, ok, detail in checks:
        print(f"      {'✓' if ok else '✗'} {name}" + (f"({detail})" if detail else ""))
        if not ok:
            problems.append(name)

    # 音频流存在
    audio_streams = subprocess.run(
        [str(config.FFPROBE_PATH), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(result.output_path)],
        capture_output=True, text=True, timeout=120,
    )
    has_audio = bool((audio_streams.stdout or "").strip())
    print(f"      {'✓' if has_audio else '✗'} 含音频流")
    if not has_audio:
        problems.append("含音频流")

    # 封面
    cover = narration_mod.make_cover(result.output_path, "汉 EV 2026 款", project_dir / "cover.jpg", "60秒深度解析")
    cover_ok = cover is not None and cover.is_file()
    print(f"      {'✓' if cover_ok else '✗'} 封面生成")
    if not cover_ok:
        problems.append("封面生成")

    # 清理
    shutil.rmtree(project_dir, ignore_errors=True)

    print()
    if problems:
        print(f"✗ 自检未通过:{problems}")
        return 1
    print("✓ 渲染层自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
