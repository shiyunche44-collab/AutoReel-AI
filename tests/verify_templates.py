"""模板自检(开发期用,也可在 CI 中跑)。

只做语法检查是不够的 —— 模板里引用一个模型上不存在的字段,
语法完全合法,只有真正渲染时才抛 UndefinedError。这里用构造好的
样例数据把每个页面都渲染一遍,提前暴露这类问题。

用法:
    python -m tests.verify_templates
"""
from __future__ import annotations

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "app" / "web" / "templates"


def _sample_contexts() -> dict[str, dict]:
    """构造足以覆盖模板全部分支的样例数据。"""
    from app import costs
    from app.models import Fact, ProjectDetail, ScriptSegment, Shot, ShotSpec

    spec = ShotSpec(
        idx=0, duration=6.0, section="开场钩子",
        visual_zh="车辆低机位切入", camera_zh="Push In",
        narration_zh="这台车有点东西。", angle="exterior_45",
    )
    shot = Shot(spec=spec, status="succeeded", stage="final",
                model="explainer", final_path="shots/000.mp4", consistency="real")

    detail = ProjectDetail(
        id="p_test", title="测试项目", subject="比亚迪汉 EV", category="auto_long",
        theme="buyguide", theme_label="单车选购深讲",
        duration=60, shot_count=10, state="FINAL_RENDERING", est_cost=0.4,
        spent_cost=3.2, quota_budget=40.0, created_at=1.0, updated_at=2.0,
        has_output=True, narration_mode="tts", state_reason="各拍转为成片",
        facts=[
            Fact(id="f_1", claim="续航 702 公里", source_tool="ifind.financials",
                 source_query="比亚迪 财务", retrieved_at="2026-10-19",
                 confidence="high", scope="product"),
            Fact(id="f_c1", claim="注册资本 91.7 亿元", source_tool="qichacha.basic",
                 source_query="比亚迪", retrieved_at="2026-10-19",
                 confidence="high", scope="company"),
            Fact(id="f_i1", claim="2026 年新能源渗透率 58%", source_tool="ifind.industry",
                 source_query="新能源 渗透率", retrieved_at="2026-10-19",
                 confidence="medium", scope="industry"),
        ],
        script=[ScriptSegment(idx=0, section="开场钩子", text="这台车有点东西。",
                              fact_refs=["f_1"])],
        shots=[shot],
    )
    summary = {
        "id": "p_test", "title": "测试项目", "subject": "比亚迪汉 EV",
        "category": "auto_long", "theme": "buyguide", "theme_label": "单车选购深讲",
        "duration": 60, "shot_count": 10,
        "state": "FINAL_RENDERING", "est_cost": 0.4, "spent_cost": 3.2,
        "quota_budget": 40.0, "created_at": 1.0, "updated_at": 2.0,
        "has_output": False,
    }
    events = [{"id": 1, "project_id": "p_test", "created_at": 1.0,
               "src": "GATE_A", "dst": "ASSETS_READY", "reason": "参考图 5 张",
               "cost": 0.48}]
    health = {
        "ffmpeg": {"ok": True, "message": "ffmpeg ok"},
        "connector": {"ok": True, "message": "CLI ok"},
        "llm": {"active": "deepseek"},
        "plan_expires_on": "2026-10-31",
    }
    return {
        "index.html": {
            "projects": [summary], "categories": [
                {"name": "auto", "label": "汽车", "skeleton": False, "todo": [],
                 "durations": [60, 90, 120, 180, 240],
                 "default_theme": "overview",
                 "themes": [
                     {"name": "overview", "label": "通用车型概览", "intent": "标准车型介绍"},
                     {"name": "iteration", "label": "车型迭代史", "intent": "梳理演进线"},
                     {"name": "cost", "label": "优势、缺点与真实使用成本", "intent": "敢讲缺点"},
                 ]},
            ],
            # 方向下拉直接迭代这两个键 —— 缺失时 Jinja 的 Undefined
            # 在迭代时抛 UndefinedError,模板自检会立刻炸出来。
            "themes": [
                {"name": "overview", "label": "通用车型概览", "intent": "标准车型介绍"},
                {"name": "iteration", "label": "车型迭代史", "intent": "梳理演进线"},
                {"name": "cost", "label": "优势、缺点与真实使用成本", "intent": "敢讲缺点"},
            ],
            "default_theme": "overview",
            "health": health, "durations": (60, 90, 120, 180, 240),
            "plan_expires_on": "2026-10-31",
        },
        # project.html 的事实分层区有两个互斥分支:
        # 有 product_gap 警告 与 无警告。两个都要渲染,否则新增的分支是假覆盖。
        "project.html": [
            {
                "p": detail, "health": health,
                "facts_report": {
                    "sources_ok": ["connector__thsifind__profile",
                                   "connector__qichacha__search"],
                    "raw_chars": 1042,
                    "facts_extracted": 3,
                    "scope_counts": {"product": 1, "company": 1, "industry": 1},
                    "product_gap": "产品级事实仅 1 条(公司级 1 条,行业级 1 条)。",
                },
                "lint": [{"segment_idx": 1, "severity": "error", "message": "缺事实引用"}],
                "render": {"cover_path": "cover.jpg"},
                "events": events,
                "wall_clock": {"render_minutes": 6},
                "estimate": {**costs.estimate_explainer(240), "library_vlm_calls": 120,
                             "library": costs.estimate_library(120)},
                "forbidden_terms": ["最强", "第一"],
            },
            {  # 事实充足,警告分支应消失
                "p": detail, "health": health,
                "facts_report": {
                    "sources_ok": ["connector__thsifind__profile"],
                    "raw_chars": 2048,
                    "facts_extracted": 9,
                    "scope_counts": {"product": 6, "company": 2, "industry": 1},
                    "product_gap": None,
                },
                "lint": [{"segment_idx": 1, "severity": "error", "message": "缺事实引用"}],
                "render": {"cover_path": "cover.jpg"},
                "events": events,
                "wall_clock": {"render_minutes": 6},
                "estimate": {**costs.estimate_explainer(240), "library_vlm_calls": 120,
                             "library": costs.estimate_library(120)},
                "forbidden_terms": ["最强", "第一"],
            },
        ],
        "logs.html": {"project_id": "p_test", "events": events},
    }


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from app.main import templates as app_templates  # 注册自定义过滤器

    env = app_templates.env
    # 语法检查用裸 Environment(渲染检查用注册了过滤器的真实环境)。
    # 自定义过滤器必须同步注册,否则语法阶段就会误报 "No filter named"。
    bare = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    for name in ("hhmmss", "datetime", "basename"):
        bare.filters[name] = env.filters[name]
    contexts = _sample_contexts()

    failed: list[str] = []
    for name in sorted(contexts):
        # 1) 语法
        try:
            bare.get_template(name)
        except TemplateSyntaxError as exc:
            failed.append(name)
            print(f"  ✗ {name} 语法错误 第 {exc.lineno} 行:{exc.message}")
            continue
        # 2) 真实渲染(覆盖语法检查发现不了的字段缺失)
        #    同一模板可能有多套样例上下文(互斥分支),逐套渲染。
        variants = contexts[name] if isinstance(contexts[name], list) else [contexts[name]]
        label = f"{name}" if len(variants) == 1 else f"{name} x{len(variants)}"
        for ctx in variants:
            try:
                app_templates.get_template(name).render(**ctx)
            except Exception as exc:  # noqa: BLE001
                failed.append(label)
                print(f"  ✗ {label} 渲染失败:{type(exc).__name__}: {exc}")
                break
        else:
            print(f"  ✓ {label} 语法 + 渲染")

    print()
    if failed:
        print(f"✗ 模板检查未通过:{failed}")
        return 1
    rendered = sum(len(v) if isinstance(v, list) else 1 for v in contexts.values())
    print(f"✓ {len(contexts)} 个模板 / {rendered} 套样例渲染全部正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
