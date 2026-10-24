"""品类词表:引擎里的品类文案全部来自 profile。

- auto_long 的提示词与去车化之前逐字一致(快照在 tests/golden/)。
- 非汽车品类渲染出来的提示词里不能混进汽车词。
不需要网络与额度。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# 直接跑这个文件(`python tests/test_profile_vocab.py --regen`)时,
# 仓库根不在 sys.path 上,`app` 导不进来
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import explainer, intake, library, script_gen
from app.providers import facts as facts_provider

GOLDEN = Path(__file__).resolve().parent / "golden"

SUBJECTS = {"auto_long": "小米YU7"}


class _Capture:
    """假 LLM:记下 system / user,返回各调用方要的空结构。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        return {k: [] if k != "title" else "t" for k in required_keys}


def _facts() -> list[Fact]:
    return [
        Fact(id="f_1", claim="示例参数 835", source_tool="t", confidence="high",
             scope="product", retrieved_at="2026-01-01"),
        Fact(id="f_2", claim="示例公司成立于 2010 年", source_tool="t", confidence="high",
             scope="company", retrieved_at="2026-01-01"),
    ]


def render_prompts(category: str, subject: str, monkeypatch) -> dict[str, object]:
    profile = load_profile(category)
    theme = profile.theme_spec()
    plan = list(profile.plan_for(profile.supported_durations()[0]))
    out: dict[str, object] = {}

    llm = _Capture()
    script_gen.generate(subject=subject, profile=profile, facts=_facts(), plan=plan,
                        duration=profile.supported_durations()[0], llm=llm, theme=theme)
    out["script.system"], out["script.user"] = llm.calls[-1]

    segs = [ScriptSegment(idx=i, section=s, text="示例旁白") for i, s in enumerate(plan[:3])]
    explainer.build_beats(subject=subject, profile=profile, segments=segs,
                          facts=_facts(), llm=llm)
    out["beats.system"], out["beats.user"] = llm.calls[-1]

    monkeypatch.setattr(intake.time, "strftime", lambda fmt, *a: "2026-10-20")
    intake.extract_facts("材料", subject=subject, category=profile.label, llm=llm,
                         profile=profile)
    out["extract.system"], out["extract.user"] = llm.calls[-1]

    vocab = profile.vocab
    out["tag.prompt"] = library.tag_prompt(subject, vocab)
    out["tag.system"] = library.tag_system(subject, vocab)
    out["discovery"] = list(library.discovery_queries(subject, "Xiaomi YU7", vocab))
    out["reference_prompt"] = profile.reference_prompt(subject, profile.section(plan[0]).angle)

    queries: list[str] = []
    monkeypatch.setattr(facts_provider, "invoke",
                        lambda tool, args, *, query_label, **_: queries.append(query_label))
    for name in ("ifind.news", "ifind.product_news", "ifind.iteration_news",
                 "ifind.compare_news", "ifind.cost_news", "ifind.tech_news"):
        facts_provider.collect([name], subject, queries=profile.fact_queries)
    out["fact_queries"] = queries
    out["industry_topic"] = intake.industry_topic(profile)
    return out


def _norm(value):
    """快照只比文字,不比空白:模板化时有些长句换了折行位置。"""
    if isinstance(value, str):
        return re.sub(r"\s+", "", value)
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    return value


def _golden_path(category: str) -> Path:
    return GOLDEN / f"{category}_prompts.json"


def regen_golden(category: str) -> Path:
    """重新生成某个品类的提示词快照。改了提示词就跑这个。"""
    from _pytest.monkeypatch import MonkeyPatch

    mp = MonkeyPatch()
    try:
        got = render_prompts(category, SUBJECTS[category], mp)
    finally:
        mp.undo()
    path = _golden_path(category)
    path.write_text(json.dumps(got, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    # 改了任何提示词之后:python tests/test_profile_vocab.py --regen
    if "--regen" not in sys.argv:
        print(__doc__)
        print("用法:python tests/test_profile_vocab.py --regen")
        raise SystemExit(0)
    for cat in sorted(SUBJECTS):
        print("重生成", regen_golden(cat))


@pytest.mark.parametrize("category", sorted(SUBJECTS))
def test_prompts_match_golden(category, monkeypatch):
    got = render_prompts(category, SUBJECTS[category], monkeypatch)
    want = json.loads(_golden_path(category).read_text(encoding="utf-8"))
    for key in want:
        assert _norm(got.get(key)) == _norm(want[key]), key


# --------------------------------------------------------------------------
# 非汽车品类
# --------------------------------------------------------------------------
#: 出现就说明汽车的词漏进了别的品类
CAR_WORDS = ("车型", "这台车", "续航", "加速", "智驾", "汽车之家", "车标", "座舱", "车企",
             "exterior", "interior", "driving")
NON_CAR = {"baby_long": "好孩子 口袋车 GB-POCKIT"}


@pytest.mark.parametrize("category", sorted(NON_CAR))
def test_non_car_prompts_have_no_car_words(category, monkeypatch):
    got = render_prompts(category, NON_CAR[category], monkeypatch)
    for key, value in got.items():
        text = json.dumps(value, ensure_ascii=False)
        hit = [w for w in CAR_WORDS if w in text]
        assert not hit, f"{category} 的 {key} 里混进了汽车词 {hit}"


@pytest.mark.parametrize("category", sorted(NON_CAR))
def test_non_car_profile_is_self_consistent(category):
    from app.providers import facts as facts_mod
    from app.providers import official

    p = load_profile(category)
    v = p.vocab
    assert p.is_explainer and p.frame_size == (1920, 1080)
    for sec in p.sections.values():
        # 每个段落的 angle 都能推出一个背景偏好,且该偏好下有能当背景的视角
        hint = v.hint_of_angle.get(sec.angle, v.default_hint)
        assert v.hint_views(hint) & set(v.angle_of_view), (sec.name, hint)
    for src in (*p.fact_sources, *(s for t in p.themes.values() for s in t.fact_sources)):
        assert src in facts_mod.SOURCES, src
    for g in p.galleries:
        assert g in official.GALLERIES, g
    assert p.voice_id


def test_baby_quality_section_is_skipped_without_records():
    p = load_profile("baby_long")
    plan = list(p.plan_for(240))
    assert "质量记录" in plan
    dropped = p.drop_unbacked(plan, ["参数"])
    assert "质量记录" not in dropped and len(dropped) == len(plan)
    # 被替掉的位置让给前一段
    i = plan.index("质量记录")
    assert dropped[i] == plan[i - 1]
    assert p.drop_unbacked(plan, ["quality"]) == plan


def test_auto_plan_unaffected_by_fact_topics():
    p = load_profile("auto_long")
    plan = list(p.plan_for(240))
    assert p.drop_unbacked(plan, []) == plan


def test_quality_facts_are_brand_level_and_scripted_carefully():
    """召回 / 抽检记录一律标成公司层;脚本提示词里带上"只能说成该品牌曾…"。"""
    p = load_profile("baby_long")
    tool = facts_provider.QCC["product_recall"]

    class LLM(_Capture):
        def structured(self, system, user, required_keys=(), **_):
            self.calls.append((system, user))
            return {"facts": [{"claim": "2024 年该品牌一款推车被召回", "source_tool": tool,
                               "source_query": "x", "confidence": "high", "topic": "召回"}]}

    llm = LLM()
    got, _ = intake.extract_facts("材料", subject="X", category=p.label, llm=llm,
                               source_docs=[{"tool": tool, "query": "x"}], profile=p)
    assert got[0].scope == "company" and got[0].topic == "quality"
    assert "recall" in llm.calls[-1][0]

    cap = _Capture()
    script_gen.generate(subject="X", profile=p, facts=got, plan=["开场"], duration=120,
                        llm=cap, theme=p.theme_spec())
    assert "该品牌曾" in cap.calls[-1][0]
    cap2 = _Capture()
    script_gen.generate(subject="X", profile=p, facts=_facts(), plan=["开场"], duration=120,
                        llm=cap2, theme=p.theme_spec())
    assert "该品牌曾" not in cap2.calls[-1][0]


def test_company_name_comes_from_brand_config():
    p = load_profile("baby_long")
    assert intake.company_of(p, "贝亲 宽口径玻璃奶瓶") == "贝亲管理(上海)有限公司"
    assert intake.company_of(p, "某杂牌推车") == "某杂牌推车"


def test_category_listing_has_ui_text():
    from app.categories import available_categories

    cats = {c["name"]: c for c in available_categories()}
    assert cats["baby_long"]["ui"]["subject_label"] == "选题 / 产品"
    assert cats["auto_long"]["ui"]["placeholder"].startswith("例:比亚迪")


def test_portrait_script_prompt_adds_short_video_rules(monkeypatch):
    """竖屏项目多一组短视频规则、按实测语速算字数;横屏 prompt 不含这些(golden 快照逐字不变)。"""
    profile = load_profile("auto_long")
    theme = profile.theme_spec()
    plan = list(profile.plan_for(60))
    llm = _Capture()
    script_gen.generate(subject="小米YU7", profile=profile, facts=_facts(), plan=plan,
                        duration=60, llm=llm, theme=theme, portrait=True)
    assert f"全片约 {int(60 * script_gen.PORTRAIT_CHARS_PER_SECOND)} 字" in llm.calls[-1][0]
    # 语速对齐后,两边只差竖屏规则这一段
    monkeypatch.setattr(script_gen, "PORTRAIT_CHARS_PER_SECOND",
                        script_gen.LANDSCAPE_CHARS_PER_SECOND)
    script_gen.generate(subject="小米YU7", profile=profile, facts=_facts(), plan=plan,
                        duration=60, llm=llm, theme=theme, portrait=True)
    portrait_system = llm.calls[-1][0]
    script_gen.generate(subject="小米YU7", profile=profile, facts=_facts(), plan=plan,
                        duration=60, llm=llm, theme=theme)
    landscape_system = llm.calls[-1][0]
    assert "竖屏短视频" in portrait_system
    assert "竖屏短视频" not in landscape_system
    assert portrait_system.replace(script_gen._portrait_policy(True), "") == landscape_system
