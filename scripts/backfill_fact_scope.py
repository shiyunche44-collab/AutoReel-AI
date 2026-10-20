"""给「事实分层」功能上线前跑出来的项目补齐 scope 字段。

分层字段是新加的,旧项目的 facts.json 里没有,页面上会渲染出一个空的
层级标签。本脚本用与流水线完全相同的 classify_scope() 规则回填,
不调用任何外部接口,不消耗额度。

用法:
    python -m scripts.backfill_fact_scope            # 补全部项目
    python -m scripts.backfill_fact_scope p_xxx      # 只补指定项目

幂等:已带 scope 的事实不覆盖;report 里已有 scope_counts 则跳过。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def backfill(project_id: str) -> str:
    from app import config, db
    from app.models import Fact
    from app.pipeline.intake import classify_scope, product_gap_warning, summarise_scopes

    pdir = config.project_dir(project_id)
    facts_file = pdir / "facts.json"
    report_file = pdir / "facts_report.json"
    if not facts_file.exists():
        return f"{project_id}: 无 facts.json,跳过"

    raw = json.loads(facts_file.read_text(encoding="utf-8"))
    facts = [Fact.model_validate(r) for r in raw]

    # 关键:判断「旧数据缺 scope」必须看原始 JSON,不能看校验后的模型 ——
    # Fact.scope 有默认值 "company",Pydantic 会把缺失字段悄悄填成 company,
    # 于是所有旧事实看起来都「已有 scope」,回填静默变成空操作。
    missing = [i for i, r in enumerate(raw) if not r.get("scope")]

    report: dict = {}
    if report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))

    # profile 只用于取标签,失败不应中断回填
    from app import categories
    detail = db.read_json(project_id, "project.json", {}) or {}
    try:
        profile = categories.load_profile(str(detail.get("category", "auto")))
    except Exception:  # noqa: BLE001
        profile = categories.load_profile("auto_long")

    changed = 0
    for i in missing:
        facts[i].scope = classify_scope(facts[i].claim, facts[i].topic)
        changed += 1

    # 幂等判断要同时看两处:事实都补齐了、且 report 已有统计,才算真正做完了
    if not missing and "scope_counts" in report:
        return f"{project_id}: 已是最新,跳过"

    if missing:
        db.write_json(project_id, "facts.json", [f.model_dump() for f in facts])

    report["scope_counts"] = summarise_scopes(facts)
    report["product_gap"] = product_gap_warning(facts, profile)
    db.write_json(project_id, "facts_report.json", report)
    gap = "有" if report["product_gap"] else "无"
    return (f"{project_id}: 回填 {changed} 条 scope,分布 "
            f"{report['scope_counts']},product_gap={gap}")


def main(argv: list[str]) -> int:
    from app import config

    if argv:
        targets = argv
    else:
        root = config.PROJECTS_DIR
        targets = sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []
    if not targets:
        print("没有可回填的项目")
        return 0
    for pid in targets:
        print(" ", backfill(pid))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
