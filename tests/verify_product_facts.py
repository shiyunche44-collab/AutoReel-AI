"""重跑事实层并打印层级分布(开发期验证用)。

用法:
    python -m tests.verify_product_facts
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SUBJECT = "比亚迪汉 EV"
DURATION = 60


def main() -> int:
    from app import categories
    from app.pipeline import intake
    from app.pipeline.orchestrator import _llm

    project_id = (ROOT / "data" / "_current_project.txt").read_text(encoding="utf-8").strip()
    profile = categories.load_profile("auto")
    llm = _llm()
    print(f"llm          : {type(llm).__name__ if llm else '不可用'}")

    facts, report = intake.run(project_id, profile, SUBJECT, llm=llm)

    print(f"project      : {project_id}")
    print(f"subject      : {SUBJECT}")
    print(f"sources_ok   : {len(report['sources_ok'])}/{len(report['sources_requested'])}")
    for f in report["sources_failed"]:
        print(f"  FAIL {f['tool']}: {f['error']}")
    print(f"raw_chars    : {report['raw_chars']}")
    print(f"facts        : {report['facts_extracted']}")
    print(f"scope_counts : {report['scope_counts']}")
    print(f"product_gap  : {'有' if report['product_gap'] else '无'}")
    if report.get("extract_error"):
        print(f"extract_error: {report['extract_error']}")
    print()

    for scope in ("product", "industry", "company"):
        rows = [f for f in facts if f.scope == scope]
        if not rows:
            continue
        print(f"--- {scope} ({len(rows)}) ---")
        for f in rows:
            mark = {"high": "H", "medium": "M", "low": "L"}[f.confidence]
            print(f"  [{mark}] {f.claim}")
        print()

    n_product = report["scope_counts"]["product"]
    if n_product >= 3:
        print(f"✓ 产品级事实 {n_product} 条,达到门禁要求(≥3)")
        return 0
    print(f"✗ 产品级事实仅 {n_product} 条,不足 3 条")
    return 1


if __name__ == "__main__":
    sys.exit(main())
