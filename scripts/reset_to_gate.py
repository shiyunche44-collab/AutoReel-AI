"""把项目状态回退到指定门禁,并清空该阶段的镜头运行态(开发期用)。

用法:
    python -m scripts.reset_to_gate            # 回退到 GATE_A
    python -m scripts.reset_to_gate --gate B   # 回退到 GATE_B
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    from app import db
    from app.state import State

    gate = "A"
    if "--gate" in argv:
        gate = argv[argv.index("--gate") + 1].upper()
    elif "--state" in argv:
        gate = argv[argv.index("--state") + 1].upper()
    keep = "--keep-shots" in argv

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()
    row = db.get_project(project_id)
    print(f"before: {row['state']}  ({row.get('state_reason', '')})")

    if len(gate) == 1:
        gate = f"GATE_{gate}"
    State(gate)  # 先校验目标状态,再动数据:写错参数不能白白清掉镜头
    if not keep:
        for stage in ("draft", "final"):
            db.delete_shots(project_id, stage)
            print(f"  cleared shots[{stage}]")

    target = State(gate if gate.startswith("GATE_") or len(gate) > 1 else f"GATE_{gate}")
    db.update_project(project_id, state=str(target),
                      state_reason=f"开发期手动回退到 {target}")
    for name in ("prompt_errors.json", "prompt_errors_final.json"):
        db.write_json(project_id, name, {})

    row = db.get_project(project_id)
    print(f"after : {row['state']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
