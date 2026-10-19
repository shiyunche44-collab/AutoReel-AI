"""真实素材通路端到端自检(要联网、要调 connector)。

单元测试里所有外部调用都是打桩的,所以"检索到底能不能拿到真车照"、
"画面上的中文占位符是不是真的没了"、"真实片段能不能被渲染层拼进成片"
这三件事只能靠真跑确认。本脚本一次跑完这三项。

用法:
    python -m tests.verify_real_assets
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SUBJECT = "BYD Han EV"
SELFTEST_PROJECT = "_selftest_real_assets"


def _specs():
    from app.models import ShotSpec

    angles = ["exterior_45", "interior", "rear", "detail", "exterior_45",
              "screen_spec", "industry", "exterior_45", "interior", "rear"]
    return [
        ShotSpec(idx=i, duration=6.0, section="核心卖点",
                 visual_zh=f"比亚迪汉 {angles[i]}", angle=angles[i])
        for i in range(10)
    ]


def check_refs() -> tuple[bool, dict]:
    """素材层:真实车照能不能拿到,以及筛没筛掉电商图。"""
    from app import categories
    from app.llm.deepseek import DeepSeekProvider
    from app.pipeline import asset_resolver

    profile = categories.load_profile("auto")
    refs, report = asset_resolver.build_reference_set(
        subject=SUBJECT, profile=profile, specs=_specs(),
        project_dir=ROOT / "data" / "projects" / SELFTEST_PROJECT,
        limit=5, verify=True, llm=DeepSeekProvider(),
    )
    print(f"  检索命中 {report['searched']} 张 -> 采用 {len(refs)} 张")
    print(f"  来源构成 {report.get('origin_counts')}")
    print(f"  has_real_photo = {report.get('has_real_photo')}")
    print(f"  硬过滤 {report.get('rejected_by_filter')} 张;"
          f"校验弃用 {report.get('verified_rejected')} 张;"
          f"verify_parsed = {report.get('verify_parsed')}")
    for r in report.get("rejected_reasons", [])[:4]:
        print(f"    剔除:{r[:100]}")
    for e in report.get("errors", [])[:3]:
        print(f"    记录:{e[:140]}")

    junk = [r.url for r in refs
            if any(h in r.url.lower() for h in ("alibaba", "ebay", "dhgate", "shutterstock"))]
    ok = (
        report["searched"] > 0
        and bool(report.get("has_real_photo"))
        and report.get("origin_counts", {}).get("search", 0) >= 3
        and not junk
    )
    if not ok:
        print(f"  ✗ 判定失败 searched={report['searched']} "
              f"real={report.get('has_real_photo')} "
              f"counts={report.get('origin_counts')} 电商残留={junk}")
    return ok, report


def check_prompt_clean() -> bool:
    """编译出来的提示词里,不能再有含中文的方括号。"""
    import json

    from app.categories import load_profile
    from app.models import ShotSpec
    from app.pipeline import prompt_compiler

    profile = load_profile("auto")
    dirty = 0
    for i in range(10):
        spec = ShotSpec(
            idx=i, duration=6.0, section="核心卖点", visual_zh="整车 45 度前侧",
            angle="exterior_45",
            # 分镜里就是中文占位符 —— 这正是被渲染成鬼画符的那些
            on_screen_text=["[年款标识]", "[续航版本标识]", "[百公里能耗]"],
        )
        translation = prompt_compiler.ShotTranslation(
            beats_en=["BYD Han EV. A metallic sedan parked in an open lot, "
                      "morning light raking across the hood."],
            camera_en="Arc Shot",
            ambience_en="Light wind over the lot.",
            music_en="N/A",
            # 模拟 LLM 漏翻的情况:中文原样抄了回来
            visible_text=["[年款标识]", "[续航版本标识]", "[百公里能耗]"],
            subject_en="BYD Han EV",
        )
        prompt, errors = prompt_compiler.compile_shot(
            spec, translation, narration_mode="tts",
            section=profile.section("核心卖点"),
        )
        brackets = [m.group(0) for m in prompt_compiler._BRACKETED_RE.finditer(prompt)
                    if prompt_compiler._CJK_RE.search(m.group(0))]
        if brackets or errors:
            dirty += 1
            print(f"  ✗ 镜头 {i}: 残留={brackets} errors={errors}")
    print(f"  10 个镜头全部通过编译且零中文方括号" if not dirty else f"  ✗ {dirty} 个镜头不合格")
    return dirty == 0


def check_clip_render() -> bool:
    """真实片段能不能被渲染层拼进成片(真跑 ffmpeg)。"""
    from app import config, db
    from app.pipeline import clip_resolver, generate, render
    from app.providers import clips as clips_mod

    pid = SELFTEST_PROJECT
    pdir = config.project_dir(pid)
    specs = _specs()

    # shots 表有指向 projects 的外键,自检项目必须先落库
    if not db.get_project(pid):
        db.create_project({
            "id": pid, "title": "真实素材自检", "subject": SUBJECT, "category": "auto",
            "theme": "overview", "duration": 60, "shot_count": len(specs),
            "narration_mode": "tts", "state": "FINAL_RENDERING", "state_reason": None,
            "quota_budget": 100.0, "spent_cost": 0.0, "est_cost": 11.4,
            "created_at": 1.0, "updated_at": 2.0,
        })
    generate.initialise_shots(pid, specs, stage="final", ref_limit=0)

    candidates = clips_mod.commons_search_videos("electric car charging", limit=1)
    if not candidates:
        print("  ! 没有候选片段,跳过(维基车型视频存量本来就少)")
        return True
    dest = pdir / "clips" / "selftest.mp4"
    cand = candidates[0]
    cand.local_path = str(clips_mod.fetch_clip(cand.url, dest, seconds=6))

    applied = clip_resolver.apply_clips(pid, {0: cand})
    row = next(r for r in db.get_shots(pid, "final") if r["idx"] == 0)
    print(f"  注入 {applied} 个镜头;status={row['status']} "
          f"consistency={row['consistency']} est_cost={row['est_cost']}")

    # 用同一个真实片段当全部 10 个镜头的素材,只验证渲染层能吃下它
    shot_paths = [Path(row["final_path"])] + [dest] * 9
    result = render.compose(pid, shot_paths, output_name="clip_selftest.mp4")
    print(f"  合成 ok={result.ok} 时长={result.duration:.1f}s")
    for w in (result.warnings or []):
        print(f"    警告:{w[:120]}")
    if result.ok:
        print(f"  输出 {result.output_path.stat().st_size // 1024} KB")

    clip_resolver.persist(pid, {
        "enabled": True, "applied": applied,
        "selected": {"0": {"path": cand.local_path, "origin": cand.origin,
                           "title": cand.title, "license": cand.license,
                           "author": cand.author, "page_url": cand.page_url}},
        "candidates": 1, "fetched": 1, "qc_rejected": 0,
        "errors": [], "sources_tried": ["selftest"],
        "savings_estimate": 0.48,
        "summary": "自检:1 个镜头用真实素材",
    }, {0: cand})
    return result.ok and row["status"] == "succeeded" and row["est_cost"] == 0.0


def main() -> int:
    print("[1/3] 素材层:真实车照检索与筛选")
    ok_refs, _ = check_refs()
    print("\n[2/3] 提示词:中文占位符是否已被英文替身覆盖")
    ok_prompt = check_prompt_clean()
    print("\n[3/3] 真实片段:注入 + 渲染层能否拼进成片")
    ok_clip = check_clip_render()

    print("\n" + "=" * 56)
    for name, ok in (("真实参考图", ok_refs), ("中文占位符", ok_prompt),
                     ("真实片段成片", ok_clip)):
        print(f"{'✓' if ok else '✗'} {name}")
    return 0 if all((ok_refs, ok_prompt, ok_clip)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
