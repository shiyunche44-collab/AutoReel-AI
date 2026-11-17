"""剪辑 v2 试片:拿一条**已经出过片**的竖屏项目,用新剪辑重新合成一份,和旧成片并排对比。

    python -I scripts/edit_v2_trial.py --list            # 列出能试的项目(已完成的竖屏)
    python -I scripts/edit_v2_trial.py                   # 自动挑最近一条
    python -I scripts/edit_v2_trial.py p_xxxxxxxxxxxx    # 指定项目
    python -I scripts/edit_v2_trial.py p_xxx --no-rerank # 不让文字模型复选素材(不花 LLM 额度)

不改项目状态、不覆盖原成片:新片写成项目目录下的 `output_v2_trial.mp4`,
对比图写成 `_edit/compare.jpg`(上排旧成片、下排新成片,同一时刻各取一帧)。
会顺带写 `edit_timeline.json`(每一刀的取材依据)和覆盖 `_render/mix.wav`(合成中间文件)。

只碰 DONE 状态的项目:调度器不推进终态项目,所以不用先停服务;别的状态一律拒绝。
需要:Node 22+、`remotion/` 下 `npm ci` 过、`.env` 里有浏览器路径(和花字层同一套)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402
from app.categories import load_profile  # noqa: E402
from app.pipeline import aspect, caption_layer  # noqa: E402
from app.pipeline import render as render_mod  # noqa: E402

OUTPUT = "output_v2_trial.mp4"
SHEET_TIMES = 8


def candidates(limit: int = 200) -> list[dict]:
    out = []
    for p in db.list_projects(limit):
        if p.get("state") != "DONE" or not aspect.is_portrait(p):
            continue
        if not aspect.output_path(p["id"]).is_file():
            continue
        if not (db.read_json(p["id"], "narration.json", {}) or {}).get("cues"):
            continue
        out.append(p)
    return out


def compare_sheet(old: Path, new: Path, dest: Path, seconds: float) -> bool:
    """上排旧片、下排新片,同一时刻各一帧(跳过开头 0.5 秒和片尾卡)。"""
    span = max(1.0, seconds - 3.0)
    times = [0.5 + span * k / (SHEET_TIMES - 1) for k in range(SHEET_TIMES)]
    work = dest.parent / "_compare"
    work.mkdir(parents=True, exist_ok=True)
    rows = []
    for tag, video in (("old", old), ("new", new)):
        frames = []
        for k, t in enumerate(times):
            f = work / f"{tag}_{k:02d}.jpg"
            render_mod._run(["-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                             "-frames:v", "1", "-vf", "scale=270:480", str(f)], timeout=120)
            if f.is_file():
                frames.append(f)
        rows.append(frames)
    if not all(len(r) == SHEET_TIMES for r in rows):
        return False
    args = ["-y", "-v", "error"]
    for f in rows[0] + rows[1]:
        args += ["-i", str(f)]
    n = SHEET_TIMES
    graph = (f"{''.join(f'[{i}:v]' for i in range(n))}hstack={n}[top];"
             f"{''.join(f'[{i}:v]' for i in range(n, 2 * n))}hstack={n}[bot];[top][bot]vstack=2")
    proc = render_mod._run(args + ["-filter_complex", graph, "-q:v", "3", str(dest)], timeout=120)
    return proc.returncode == 0 and dest.is_file()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project_id", nargs="?")
    ap.add_argument("--list", action="store_true", help="列出能试的项目")
    ap.add_argument("--no-rerank", action="store_true", help="不让文字模型复选素材")
    args = ap.parse_args()

    found = candidates()
    if args.list:
        for p in found[:30]:
            print(f"{p['id']}  {p.get('subject', '')}  {p.get('title', '')}")
        print(f"共 {len(found)} 条已完成的竖屏项目")
        return 0
    if args.project_id:
        project = db.get_project(args.project_id)
        if not project:
            print(f"没有项目 {args.project_id}")
            return 2
        if project.get("state") != "DONE":
            print(f"{args.project_id} 状态是 {project.get('state')},只试 DONE 的项目(别的状态调度器会动它)")
            return 2
        if not aspect.is_portrait(project):
            print("这是横屏项目;试点只做竖屏")
            return 2
    else:
        if not found:
            print("没有已完成、带旁白的竖屏项目可试")
            return 2
        project = found[0]
    pid = project["id"]

    ok, why = caption_layer.available()
    if not ok:
        print(f"Remotion 不可用:{why}")
        return 2
    if args.no_rerank:
        os.environ["EDIT_V2_RERANK"] = "0"

    from app.pipeline import orchestrator
    from app.pipeline.edit import project as edit_project

    specs = orchestrator._specs(pid)
    rows = {r["idx"]: r for r in db.get_shots(pid, "final")}
    missing = [s.idx for s in specs if s.duration > 0 and not (rows.get(s.idx) or {}).get("final_path")]
    if missing:
        print(f"还有 {len(missing)} 拍没有成片:{missing[:12]}")
        return 2
    nar = db.read_json(pid, "narration.json", {}) or {}
    profile = load_profile(project["category"])
    llm = None if args.no_rerank else orchestrator._llm()
    print(f"试片:{pid} {project.get('subject', '')}《{project.get('title', '')}》"
          f",{len(specs)} 拍,文字模型复选:{'开' if llm else '关'}")

    t0 = time.time()
    result = edit_project.compose(
        pid, specs, rows, nar, size=aspect.frame_size(project),
        title=str(project.get("title") or ""), subject=str(project.get("subject") or ""),
        output_name=OUTPUT, vocab=profile.vocab, llm=llm)
    spent = time.time() - t0
    project_dir = config.project_dir(pid)
    new = project_dir / OUTPUT
    old = aspect.output_path(pid)
    saved = db.read_json(pid, "edit_timeline.json", {}) or {}
    sheet = project_dir / "_edit" / "compare.jpg"
    has_sheet = compare_sheet(old, new, sheet, result.duration)
    report = {
        "project": pid, "ok": result.ok, "seconds": round(result.duration, 1),
        "render_seconds": round(spent, 1), "clips": len(saved.get("clips") or []),
        "match": saved.get("match"), "warnings": result.warnings,
        "new": str(new), "old": str(old), "compare": str(sheet) if has_sheet else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
