"""核对脚本里的数字是否真的出现在它引用的那条事实里(开发期验证用)。

lint 目前只保证"有引用",不保证"引用的那条事实支持这个数字"。
参数讲错正是这套系统要防的头号翻车方式,所以这里单独查一遍。

注意:中文数字必须真解析,不能逐字替换。
"三十一" 逐字替换会变成 "3"+"10"+"1" = "3101",而事实里写的是 31 ——
早期版本就是这么把 5 段正常脚本全判成"数字无据"的,纯假阳性。

用法:
    python -m tests.verify_number_grounding
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.pipeline.grounding import has_unit_expression, numbers_in  # noqa: E402


def main() -> int:
    from app import db

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()

    facts = {f["id"]: f for f in (db.read_json(project_id, "facts.json", []) or [])}
    segments = db.read_json(project_id, "script.json", []) or []

    ok, manual, bad = 0, 0, 0
    print("--- 逐段核对:脚本数字是否被引用的事实支持 ---\n")
    for seg in segments:
        text = seg.get("text", "")
        refs = seg.get("fact_refs", []) or []
        script_nums = numbers_in(text)
        if not script_nums:
            continue
        cited_claims = [facts.get(r, {}).get("claim", "") for r in refs]
        cited_nums = numbers_in(" ".join(cited_claims))
        unsupported = script_nums - cited_nums

        if not unsupported:
            ok += 1
            print(f"✓ [{seg['idx']}] {sorted(script_nums)} 全部见于引用事实")
            continue

        # 脚本里有"五米一"这类单位表达时,数字必然对不齐,交人工判断
        if has_unit_expression(text) or has_unit_expression(" ".join(cited_claims)):
            manual += 1
            print(f"⚠ [{seg['idx']}] {seg.get('section','')} 含单位表达,需人工确认")
            print(f"    脚本   : {text}")
            for c in cited_claims:
                print(f"    依据   : {c}")
            print(f"    未直接命中: {sorted(unsupported)}")
            print()
            continue

        bad += 1
        print(f"✗ [{seg['idx']}] {seg.get('section','')} 数字与引用事实对不上")
        print(f"    脚本   : {text}")
        for c in cited_claims:
            print(f"    依据   : {c}")
        print(f"    未被支持: {sorted(unsupported)}")
        print()

    print()
    print(f"直接命中 {ok} 段 · 需人工确认 {manual} 段 · 确实无据 {bad} 段")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
