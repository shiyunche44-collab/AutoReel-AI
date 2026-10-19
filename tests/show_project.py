"""打印项目当前产物,用于人工核对流水线质量(只读)。"""
import json
from pathlib import Path

# PowerShell 的 Out-File -Encoding utf8 会写 BOM,必须用 utf-8-sig 读,
# 否则项目 ID 前会多一个不可见的 \ufeff,静默读到空目录。
pid = Path(r"F:\code\aimovice\data\_current_project.txt").read_text(
    encoding="utf-8-sig"
).strip()
d = Path(r"F:\code\aimovice\data\projects") / pid


def load(name, default):
    p = d / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


facts = load("facts.json", [])
rep = load("facts_report.json", {})
lint = load("lint.json", [])
scr = load("script.json", [])
sb = load("storyboard.json", [])

print("=== 事实层 ===")
print("来源成功:", [t.split("__")[-1] for t in rep.get("sources_ok", [])])
print("原始素材字数:", rep.get("raw_chars"), "| 抽取事实:", len(facts), "条")
for f in facts[:6]:
    print(f"  [{f['confidence']:<6}] {f['claim'][:64]}")
    print(f"           来源 {f['source_tool'].split('__')[-1]} | 取数 {f['retrieved_at']}")

print()
print("=== 合规检查 ===")
errs = [l for l in lint if l.get("severity") == "error"]
print(f"{len(lint)} 条,error {len(errs)} 条")
for e in errs[:5]:
    print("  ", e)

print()
print("=== 解说脚本 ===")
for s in scr:
    print(f"  [{s['section']}] {s['text']}")
    print(f"      facts={s['fact_refs']}")

print()
print("=== 分镜表 ===")
for sp in sb:
    print(f"  #{sp['idx']} [{sp['section']}] {sp['camera_zh']} | angle={sp['angle']}")
    print(f"      {sp['visual_zh']}")
    if sp.get("on_screen_text"):
        print(f"      可见文字: {sp['on_screen_text']}")
print(f"  共 {len(sb)} 个镜头")
