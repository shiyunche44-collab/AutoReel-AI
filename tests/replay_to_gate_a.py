"""把项目回退到事实层并重跑到门禁 A,打印脚本与 lint(开发期验证用)。

用法:
    python -m tests.replay_to_gate_a
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAX_STEPS = 12


def main() -> int:
    from app import db
    from app.pipeline import orchestrator
    from app.state import State

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()

    db.update_project(project_id, state=str(State.DRAFT_INTENT),
                      state_reason="回退重跑(事实源修复)")
    db.delete_shots(project_id, "draft")
    db.delete_shots(project_id, "final")

    for i in range(MAX_STEPS):
        out = orchestrator.advance(project_id)
        print(f"[{i + 1}] {out.get('state')}  {out.get('action', '')}")
        if out.get("waiting") or out.get("state") in ("FAILED", "DONE"):
            break
    print()

    row = db.get_project(project_id)
    print(f"state = {row['state']}  ({row.get('state_reason', '')})")
    print()

    print("--- 脚本 ---")
    for seg in db.read_json(project_id, "script.json", []) or []:
        refs = ",".join(seg.get("fact_refs", []) or [])
        print(f"  [{seg['idx']:>2}] {seg.get('section', '')}  {seg['text']}")
        if refs:
            print(f"        引用: {refs}")
    print()

    print("--- lint ---")
    issues = db.read_json(project_id, "lint.json", []) or []
    errs = [i for i in issues if i.get("severity") == "error"]
    for i in issues:
        mark = "✗" if i.get("severity") == "error" else "·"
        print(f"  {mark} [{i.get('segment_idx')}] {i.get('message')}")
    print(f"\nerror={len(errs)}  warning={len(issues) - len(errs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
