"""用真实 LLM 重编译几个镜头,检查提示词是否真的点名了车型(开发期用)。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SUBJECT = "比亚迪汉 EV"


def main() -> int:
    from app import categories
    from app.pipeline import prompt_compiler
    from app.pipeline.orchestrator import _llm, _specs

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()
    profile = categories.load_profile("auto")
    llm = _llm()
    specs = _specs(project_id)

    bad = 0
    for spec in specs[:3]:
        prompt, errors = prompt_compiler.compile_with_llm(
            spec, subject=SUBJECT, profile=profile, llm=llm, narration_mode="tts",
        )
        head = prompt.split("\n\n")[0]
        print(f"--- 镜头 {spec.idx} [{spec.section}] ---")
        print(f"    主体锚点命中: {SUBJECT in prompt or 'BYD Han' in prompt}")
        print(f"    校验错误: {errors if errors else '无'}")
        print(f"    {head[:260]}")
        print()
        if errors:
            bad += 1
    print(f"{'✗' if bad else '✓'} {len(specs[:3])} 个镜头中 {bad} 个有校验错误")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
