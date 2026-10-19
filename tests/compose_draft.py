"""用草稿素材走一遍"拼接 → 字幕 → 混音 → 封面",验证合成层(开发期用)。

正常流程里合成只在成片档(H3)之后发生,代价很高。这里用草稿档
(Hailuo-2.3)的镜头跑同一套合成逻辑,花几分钟就能确认字幕烧录、
配音混合、竖屏归一化、封面抽取都没问题 —— 不必等 H3 跑完几小时才发现
合成层有 bug。

用法:
    python -m tests.compose_draft
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def ffprobe(path: Path) -> str:
    from app import config

    exe = Path(str(config.FFMPEG_PATH)).with_name("ffprobe.exe")
    if not exe.is_file():
        exe = Path(str(config.FFMPEG_PATH)).with_name("ffprobe")
    out = subprocess.run(
        [str(exe), "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,channels,sample_rate",
         "-show_entries", "format=duration,size",
         "-of", "default=noprint_wrappers=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return (out.stdout or out.stderr).strip()


def main() -> int:
    from app import categories, config, db
    from app.pipeline import narration, render

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()
    row = db.get_project(project_id)
    profile = categories.load_profile(row["category"])

    rows = [r for r in db.get_shots(project_id, "draft")
            if r["status"] == "succeeded" and r.get("draft_path")]
    rows.sort(key=lambda r: r["idx"])
    if not rows:
        print("✗ 还没有成功的草稿镜头,无法合成")
        return 1

    shot_paths = [Path(r["draft_path"]) for r in rows]
    print(f"项目 {project_id}")
    print(f"可用草稿镜头 {len(shot_paths)} 个: {[p.name for p in shot_paths]}\n")

    from app.pipeline.orchestrator import _specs
    all_specs = _specs(project_id)
    used = all_specs[: len(shot_paths)]

    print("--- 旁白 / 字幕 ---")
    nar = narration.build(
        project_id, used,
        narration_mode=row["narration_mode"], profile=profile,
    )
    print(f"  narration : {nar.narration_path}")
    print(f"  bgm       : {nar.bgm_path}")
    print(f"  ass       : {nar.ass_path}")
    for label, p in (("narration", nar.narration_path),
                     ("bgm", nar.bgm_path), ("ass", nar.ass_path)):
        if p and not Path(p).is_file():
            print(f"  ✗ {label} 缺失:{p}")
            return 1
    print("  ✓ 三件产物齐全\n")

    print("--- 合成 ---")
    result = render.compose(
        project_id, shot_paths,
        ass_path=nar.ass_path,
        narration_path=nar.narration_path,
        bgm_path=nar.bgm_path,
    )
    print(f"  ok        : {result.ok}")
    print(f"  duration  : {result.duration}")
    if result.warnings:
        print(f"  warnings  : {result.warnings}")
    if not result.ok or not result.output_path:
        print("✗ 合成失败")
        return 1

    out = Path(result.output_path)
    print(f"  output    : {out}  ({out.stat().st_size / 1e6:.2f} MB)\n")
    print("--- ffprobe ---")
    print(ffprobe(out))
    print()
    print(f"✓ 草稿合成成功:{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
