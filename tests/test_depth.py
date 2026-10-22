"""技术深度:推算事实、原理库、深度提醒、口径上屏。不需要网络与额度。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.categories import Principle, ProfileError, _build_principles, load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import cards, derive
from app.pipeline.script_gen import (compliance_lint, depth_lint, matched_principles,
                                     usable_facts)


@pytest.fixture(scope="module")
def auto():
    return load_profile("auto_long")


def _f(fid, claim, **kw):
    return Fact(id=fid, claim=claim, source_tool="t", confidence=kw.pop("confidence", "high"),
                scope=kw.pop("scope", "product"), **kw)


# ---- 推算 ----------------------------------------------------------------
def test_derive_rules_on_real_claims(auto):
    facts = [
        _f("f_a", "小米YU7标准版售价23.35万元，搭载73kWh磷酸铁锂电池，CLTC续航643公里。",
           basis="CLTC · 标准版"),
        _f("f_b", "小米YU7 GT最大马力1003匹，零百加速2.92秒。"),
        _f("f_c", "小米YU7标准版采用752V碳化硅高压平台，15分钟补能405公里。"),
    ]
    got = {d.topic: d for d in derive.derive(facts, auto.derive)}
    assert "11.4 度电" in got["电耗"].claim and "f_a" in got["电耗"].claim
    assert got["电耗"].basis == "CLTC · 标准版"
    assert "738 千瓦" in got["功率"].claim
    assert "每分钟约补 27 公里" in got["补能"].claim
    assert all(d.source_kind == "derived" and d.scope == "product" for d in got.values())


def test_derive_skips_low_confidence_company_and_ambiguous():
    facts = [
        _f("f_low", "旧款 1003 匹", confidence="low"),
        _f("f_co", "公司 500 匹马力的工厂", scope="company"),
        # 两个容量一个续航:不知道配哪个,不算
        _f("f_amb", "提供 73kWh 与 101.7kWh 两种电池，CLTC续航643公里"),
    ]
    assert derive.derive(facts, ["hp_to_kw", "pack_consumption", "nope"]) == []


def test_derived_fact_passes_card_grounding(auto):
    src = _f("f_c", "15分钟补能405公里")
    d = derive.derive([src], ["charge_rate"])[0]
    card = cards.normalise({"rows": [
        {"label": "每分钟补能", "values": ["27 公里"], "fact_id": d.id}]}, "spec")
    grounded, _ = cards.ground(card, [src, d])
    assert grounded and grounded["rows"][0]["tag"] == "推算"


def test_derived_dropped_when_source_disabled():
    src = _f("f_c", "15分钟补能405公里")
    d = derive.derive([src], ["charge_rate"])[0]
    src.enabled = False
    assert d not in usable_facts([src, d])


# ---- 原理库 --------------------------------------------------------------
def test_principles_have_no_digits_and_load(auto):
    assert len(auto.principles) >= 15
    with pytest.raises(ProfileError):
        _build_principles([{"id": "k_x", "terms": ["a"], "explain": "能快 30%"}])
    with pytest.raises(ProfileError):
        _build_principles([{"id": "x", "terms": ["a"], "explain": "b"}])


def test_only_principles_named_in_facts_are_offered(auto):
    facts = [_f("f_1", "采用752V碳化硅高压平台"), _f("f_2", "CLTC续航643公里")]
    ids = {p.id for p in matched_principles(auto, facts)}
    assert {"k_sic", "k_hv", "k_cltc"} <= ids
    assert "k_lidar" not in ids


def test_principle_ref_is_valid_but_not_a_source_for_numbers(auto):
    facts = [_f("f_1", "采用752V碳化硅高压平台")]
    ok = ScriptSegment(idx=0, section="三电性能", text="碳化硅损耗更低,同样的电能跑得更远。",
                       fact_refs=["k_sic"])
    bad = ScriptSegment(idx=1, section="三电性能", text="碳化硅能省 10% 的电。",
                        fact_refs=["k_sic"])
    errors = [i for i in compliance_lint([ok, bad], auto, facts) if i.is_error]
    assert [i.segment_idx for i in errors] == [1]


# ---- 深度提醒 ------------------------------------------------------------
def test_depth_lint_flags_filler_missing_basis_and_missing_principle(auto):
    facts = [_f("f_1", "标准版CLTC续航643公里,采用碳化硅高压平台")]
    segs = [
        ScriptSegment(idx=0, section="三电性能", text="续航 643 公里,这个数据很猛。",
                      fact_refs=["f_1"]),
    ]
    issues = depth_lint(segs, auto, facts)
    msgs = [i.message for i in issues]
    assert any("很猛" in m for m in msgs)
    assert any("口径" in m and "CLTC" in m for m in msgs)
    assert any("三电性能" in m and "原理" in m for m in msgs)
    # 空洞评价是 error(必须改),口径与深度是 warning(提醒)
    filler = [i for i in issues if i.message.startswith("空洞评价")]
    assert filler and all(i.is_error for i in filler)
    assert all(i.severity == "warning" for i in issues if not i.message.startswith("空洞评价"))


def test_depth_lint_quiet_on_good_segment(auto):
    facts = [_f("f_1", "标准版CLTC续航643公里,采用碳化硅高压平台")]
    segs = [ScriptSegment(idx=0, section="三电性能",
                          text="CLTC 标称 643 公里;碳化硅损耗低,所以同样的电跑得更远。",
                          fact_refs=["f_1", "k_sic"])]
    assert depth_lint(segs, auto, facts) == []


# ---- 口径上屏 ------------------------------------------------------------
def test_source_line_summarises_basis_and_kinds():
    used = [_f("f_1", "x", basis="CLTC · 标准版", source_kind="official",
               retrieved_at="2026-10-20"),
            _f("f_2", "y", source_kind="media", retrieved_at="2026-10-01"),
            _f("f_3", "z", source_kind="derived", retrieved_at="2026-10-20")]
    line = cards.source_line(used)
    assert line.startswith("数据来源:官方 1 · 媒体 1 · 推算 1")
    assert "口径:CLTC · 标准版" in line and "2026-10" in line
    assert cards.basis_tag(used[0]) == "CLTC · 标准版 | 官方"


# ---- 误报修正 ------------------------------------------------------------
def test_model_code_digits_are_not_claims(auto):
    from app.pipeline.script_gen import has_numeric_claim
    assert not has_numeric_claim("小米YU7,买哪个版本不后悔?")
    assert not has_numeric_claim("V6s 电机")
    assert has_numeric_claim("752V 平台") and has_numeric_claim("700TOPS")
    assert has_numeric_claim("CLTC续航643公里")


def test_first_impression_is_not_an_absolute_claim(auto):
    segs = [ScriptSegment(idx=0, section="内饰空间", text="坐进去,第一感觉是简洁。"),
            ScriptSegment(idx=1, section="内饰空间", text="它是同级第一。")]
    errors = [i for i in compliance_lint(segs, auto, []) if i.is_error]
    assert {i.segment_idx for i in errors} == {1}


def test_basis_warning_only_for_numbers_under_that_basis(auto):
    facts = [_f("f_1", "标准版售价23.35万元,搭载73kWh电池,CLTC续航643公里")]
    price = ScriptSegment(idx=0, section="定位与价格", text="标准版 23.35 万。", fact_refs=["f_1"])
    rng = ScriptSegment(idx=1, section="定位与价格", text="续航 643 公里。", fact_refs=["f_1"])
    warned = {i.segment_idx for i in depth_lint([price, rng], auto, facts) if "口径" in i.message}
    assert warned == {1}


def test_tech_term_absent_from_facts_is_flagged(auto):
    facts = [_f("f_1", "标配激光雷达")]
    segs = [ScriptSegment(idx=0, section="版本选择", text="Pro 和 Max 有空悬,按需选择。")]
    assert any("空悬" in i.message for i in depth_lint(segs, auto, facts))
    ok = [ScriptSegment(idx=0, section="智能配置", text="激光雷达夜里更可靠。",
                        fact_refs=["f_1", "k_lidar"])]
    assert not any("不见于" in i.message for i in depth_lint(ok, auto, facts))
