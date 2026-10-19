"""非破坏性地刷新某个项目的素材层与提示词(开发期用)。

与 `replay_to_gate_a` 的区别:那个会把项目退回门禁 A 并**删掉已生成的草稿**
(已经花掉的等效用量就作废了)。这个只重算两样不影响成片的东西:

1. 参考图集 —— 直接决定"画面里是不是这台车",是修复"车型不对"的那一步;
2. 草稿档提示词 —— 直接决定"画面上会不会出现中文鬼画符",是修复乱码的那一步。

两者都只花网络和 LLM,**不产生任何视频生成消耗**。

用法:
    python -m tests.refresh_assets_and_prompts [项目ID]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_PROJECT = "p_d1851382e779"


def _report_line(label: str, report: dict) -> None:
    print(f"  {label}")
    print(f"    检索命中      {report.get('searched')}")
    print(f"    来源构成      {report.get('origin_counts')}")
    print(f"    真实照片      {report.get('has_real_photo')}")
    print(f"    硬过滤        {report.get('rejected_by_filter', 0)} 张")
    print(f"    校验弃用      {report.get('verified_rejected', 0)} 张"
          f" / 可解析 {report.get('verify_parsed')}")
    print(f"    结论来源      {report.get('verify_sources')}")


def main() -> int:
    from app import config, db
    from app.categories import load_profile
    from app.llm.deepseek import DeepSeekProvider
    from app.pipeline import asset_resolver, orchestrator

    marker = ROOT / "data" / "_current_project.txt"
    project_id = (sys.argv[1] if len(sys.argv) > 1 else
                  (marker.read_text(encoding="utf-8-sig").strip()
                   if marker.is_file() else DEFAULT_PROJECT))
    project = db.get_project(project_id)
    if not project:
        print(f"项目不存在:{project_id}")
        return 1

    subject = project["subject"]
    profile = load_profile(project["category"])
    specs = orchestrator._specs(project_id)
    project_dir = config.project_dir(project_id)

    print(f"项目 {project_id} · {subject} · {project['category']} · 状态 {project['state']}")
    print(f"已有草稿 {len(list((project_dir / 'drafts').glob('*.mp4'))) if (project_dir / 'drafts').is_dir() else 0}"
          f" 条(不会被动)")
    print()

    print("[1/2] 素材层 —— 重跑真实参考图检索")
    old = json.loads((project_dir / "refs_report.json").read_text(encoding="utf-8")) \
        if (project_dir / "refs_report.json").is_file() else {}
    _report_line("修复前", old)
    refs, report = asset_resolver.build_reference_set(
        subject=subject, profile=profile, specs=specs,
        project_dir=project_dir, limit=config.CATEGORY_REF_LIMIT, verify=True,
        llm=DeepSeekProvider(),
    )
    _report_line("修复后", report)
    for reason in report.get("rejected_reasons", [])[:5]:
        print(f"    剔除:{reason[:110]}")
    for err in report.get("errors", [])[:3]:
        print(f"    记录:{err[:150]}")
    print("    采用:")
    for r in refs:
        print(f"      [{r.origin:9}] {(r.author or '?')[:18]:20} {r.note[:34]}")
    print()

    print("[2/2] 提示词 —— 重编译草稿档,检查中文占位符")
    for path in (project_dir / "prompts").glob("draft_*.txt"):
        path.unlink()
    llm = DeepSeekProvider()
    compiled, errors = orchestrator.compile_all_prompts(
        project_id, profile=profile, subject=subject,
        narration_mode=project["narration_mode"], llm=llm, stage="draft",
    )
    print(f"  编译成功 {compiled} / {len(specs)},校验失败 {len(errors)}")
    for k, v in list(errors.items())[:5]:
        print(f"    ✗ 镜头 {k}:{v}")

    from app.pipeline.prompt_compiler import _BRACKETED_RE, _CJK_RE

    dirty: list[str] = []
    texts: list[str] = []
    for path in sorted((project_dir / "prompts").glob("draft_*.txt")):
        text = path.read_text(encoding="utf-8")
        texts.append(text)
        hits = [m.group(0) for m in _BRACKETED_RE.finditer(text)
                if _CJK_RE.search(m.group(0))]
        if hits:
            dirty.append(f"{path.name}:{hits[:3]}")
    print(f"  提示词文件 {len(texts)} 份,含中文方括号 {len(dirty)} 份")
    for d in dirty[:5]:
        print(f"    ✗ {d}")

    sample = next((t for t in texts if "on-screen text" in t), "")
    if sample:
        line = next(l.strip() for l in sample.splitlines() if "on-screen text" in l)
        print(f"  样例:{line[:150]}")

    print()
    ok = bool(refs) and report.get("has_real_photo") and not dirty and not errors
    print("✓ 素材层与提示词都已修正" if ok
          else "! 仍有问题,详见上方输出")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
