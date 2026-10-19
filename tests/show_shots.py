"""打印当前项目的镜头运行态(开发期用)。

用法:
    python -m tests.show_shots            # 草稿档
    python -m tests.show_shots --stage final
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    from app import db

    stage = "draft"
    if "--stage" in argv:
        stage = argv[argv.index("--stage") + 1]

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()
    rows = db.get_shots(project_id, stage)
    print(f"project={project_id}  stage={stage}  shots={len(rows)}\n")
    for r in rows:
        task = r.get("provider_task_id") or "-"
        path = r.get(f"{stage}_path") or "-"
        print(f"  #{r['idx']:<2} {r['status']:<10} chain={r.get('chain_id')} "
              f"pos={r.get('chain_pos')} retry={r.get('retry_count', 0)}")
        print(f"      model={r.get('model')}  task={task}")
        if path != "-":
            print(f"      file={path}")
        if r.get("error"):
            print(f"      ERROR: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
