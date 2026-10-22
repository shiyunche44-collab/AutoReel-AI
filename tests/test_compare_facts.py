"""竞品对比:对比事实从抽取到上屏的整条链。

对比事实 = 一条 claim 里同时写清本车与对比车同一维度的取值。
它只能出现在竞品对比段 —— 混进本车的参数面板就是把别家的数字安到本车头上。
"""
from __future__ import annotations

import pytest

from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import explainer, intake, script_gen


@pytest.fixture(scope="module")
def auto():
    return load_profile("auto_long")


@pytest.fixture(scope="module")
def theme(auto):
    return auto.theme_spec("buyguide")


def _f(fid, claim, **kw):
    return Fact(id=fid, claim=claim, source_tool="t", confidence=kw.pop("confidence", "high"),
                scope=kw.pop("scope", "product"), **kw)


class _LLM:
    """假 LLM:按脚本返回事实。"""

    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    def structured(self, system, user, **_):
        self.calls.append((system, user))
        return {"facts": self.facts}


# --------------------------------------------------------------------------
# 抽取层
# --------------------------------------------------------------------------
def test_paired_comparison_fact_keeps_compare_with(auto):
    facts, dropped = intake.extract_facts(
        "材料", subject="小米YU7", category=auto.label, llm=_LLM([
            {"claim": "小米YU7标准版CLTC续航835公里,特斯拉Model Y后驱版CLTC续航545公里",
             "source_tool": "ifind.search_news", "source_query": "q",
             "confidence": "high", "topic": "对比", "compare_with": "特斯拉Model Y"},
        ]), profile=auto)
    assert dropped == 0
    assert len(facts) == 1
    assert facts[0].compare_with == "特斯拉Model Y"
    assert facts[0].scope == "product"


def test_solo_competitor_number_is_dropped(auto):
    """只讲别家车、句中没有本车的数字 —— 留着就是张冠李戴的种子。"""
    facts, dropped = intake.extract_facts(
        "材料", subject="小米YU7", category=auto.label, llm=_LLM([
            {"claim": "特斯拉Model Y后驱版CLTC续航545公里",
             "source_tool": "ifind.search_news", "source_query": "q",
             "confidence": "high", "topic": "续航", "compare_with": "特斯拉Model Y"},
        ]), profile=auto)
    assert facts == []
    assert dropped == 1


def test_solo_competitor_number_dropped_even_with_brand_stripped(auto):
    """材料里只写型号不写品牌("YU7 标准版")也算提到了本车。"""
    facts, dropped = intake.extract_facts(
        "材料", subject="小米YU7", category=auto.label, llm=_LLM([
            {"claim": "YU7标准版CLTC续航835公里,Model Y后驱版CLTC续航545公里",
             "source_tool": "ifind.search_news", "source_query": "q",
             "confidence": "high", "topic": "对比", "compare_with": "Model Y"},
        ]), profile=auto)
    assert dropped == 0
    assert facts[0].compare_with == "Model Y"


def test_own_fact_is_not_marked_as_comparison(auto):
    facts, _ = intake.extract_facts(
        "材料", subject="小米YU7", category=auto.label, llm=_LLM([
            {"claim": "小米YU7标准版售价23.35万元",
             "source_tool": "ifind.search_news", "source_query": "q",
             "confidence": "high", "topic": "售价", "compare_with": "小米YU7"},
        ]), profile=auto)
    assert facts[0].compare_with == ""


def test_extract_prompt_asks_for_paired_facts(auto):
    llm = _LLM([])
    intake.extract_facts("材料", subject="小米YU7", category=auto.label, llm=llm, profile=auto)
    system = llm.calls[-1][0]
    assert "compare_with" in system
    assert "ONE fact that names BOTH" in system


# --------------------------------------------------------------------------
# lint:对比事实只能出现在竞品对比段
# --------------------------------------------------------------------------
def test_comparison_fact_in_normal_section_is_error(auto, theme):
    facts = [
        _f("f_1", "小米YU7标准版CLTC续航835公里,特斯拉Model Y后驱版CLTC续航545公里",
           compare_with="特斯拉Model Y"),
    ]
    segs = [ScriptSegment(idx=0, section="三电性能",
                          text="续航 835 公里。", fact_refs=["f_1"])]
    issues = script_gen.compliance_lint(segs, auto, facts, theme=theme)
    assert any(i.is_error and "对比事实" in i.message for i in issues)


def test_comparison_fact_in_comparison_section_is_ok(auto, theme):
    facts = [
        _f("f_1", "小米YU7标准版CLTC续航835公里,特斯拉Model Y后驱版CLTC续航545公里",
           compare_with="特斯拉Model Y"),
    ]
    segs = [ScriptSegment(idx=0, section="竞品对比",
                          text="YU7 CLTC 续航 835 公里,Model Y 是 545 公里。",
                          fact_refs=["f_1"])]
    issues = script_gen.compliance_lint(segs, auto, facts, theme=theme)
    assert not any(i.is_error and "对比事实" in i.message for i in issues)


# --------------------------------------------------------------------------
# 分镜层:非对比拍不认对比事实
# --------------------------------------------------------------------------
def test_facts_for_drops_comparison_fact_outside_comparison_section():
    facts = [
        _f("f_1", "小米YU7标准版售价23.35万元"),
        _f("f_2", "小米YU7续航835公里,Model Y 545公里", compare_with="Model Y"),
    ]
    out = explainer.facts_for(facts, "", "三电性能")
    assert [f.id for f in out] == ["f_1"]
    out = explainer.facts_for(facts, "", "竞品对比")
    assert [f.id for f in out] == ["f_1", "f_2"]


# --------------------------------------------------------------------------
# 对比卡:一行引用一条同时含两台车取值的事实
# --------------------------------------------------------------------------
def test_compare_card_grounds_on_paired_fact():
    from app.pipeline import cards

    fact = _f("f_1", "小米YU7标准版售价23.35万元,特斯拉Model Y后驱版售价26.39万元")
    card = cards.normalise({
        "title": "入门价对比",
        "columns": ["小米YU7标准版", "特斯拉Model Y后驱版"],
        "rows": [{"label": "起售价", "values": ["23.35万元", "26.39万元"], "fact_id": "f_1"}],
    }, "compare")
    grounded, dropped = cards.ground(card, [fact])
    assert grounded, dropped
    assert grounded["rows"][0]["values"] == ["23.35万元", "26.39万元"]


def test_compare_card_rejected_when_fact_only_has_one_side():
    """一条事实只写本车的数字时,对比卡核不过 —— 宁可不放卡,不放半张空卡。"""
    from app.pipeline import cards

    fact = _f("f_1", "小米YU7标准版售价23.35万元")
    card = cards.normalise({
        "title": "入门价对比",
        "columns": ["小米YU7标准版", "特斯拉Model Y后驱版"],
        "rows": [{"label": "起售价", "values": ["23.35万元", "26.39万元"], "fact_id": "f_1"}],
    }, "compare")
    grounded, dropped = cards.ground(card, [fact])
    assert grounded is None
    assert any("26.39" in d for d in dropped)
