"""重跑素材层,验证参考图来源链(开发期用)。

用法:
    python -m tests.verify_refs
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SUBJECT = "比亚迪汉 EV"


def main() -> int:
    from app import categories, config
    from app.pipeline import asset_resolver
    from app.pipeline.orchestrator import _specs

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()
    profile = categories.load_profile("auto")
    specs = _specs(project_id)
    pdir = config.project_dir(project_id)

    refs, report = asset_resolver.build_reference_set(
        subject=SUBJECT, profile=profile, specs=specs,
        project_dir=pdir, limit=5,
    )

    print(f"主体      : {SUBJECT}")
    print(f"来源链    : {' → '.join(report['sources_tried']) or '(空)'}")
    print(f"来源构成  : {report.get('origin_counts')}")
    print(f"真实照片  : {report.get('has_real_photo')}")
    print(f"AI 兜底   : {report['generated_fallback']}")
    if report.get("warning"):
        print(f"⚠ 警告    : {report['warning']}")
    if report["errors"]:
        print(f"错误({len(report['errors'])}):")
        for e in report["errors"][:6]:
            print(f"    {e[:150]}")
    print()

    for r in refs:
        print(f"  · [{r.origin}] {r.angle}  {r.license or '(无授权信息)'}")
        print(f"    {r.note[:80]}")
        print(f"    {r.url[:120]}")
    print()

    local = sorted((pdir / "refs").glob("*.jpg"))
    print(f"已下载到本地: {[p.name for p in local]}")
    for p in local:
        print(f"    {p.name}  {p.stat().st_size / 1024:.0f} KB")

    if report.get("has_real_photo"):
        print("\n✓ 拿到了真实车型照片")
        return 0
    print("\n✗ 只有 AI 生成的虚构车型参考图")
    return 1


if __name__ == "__main__":
    sys.exit(main())
