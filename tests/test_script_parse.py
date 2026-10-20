"""脚本解析与合规 lint 测试(计划 §6.1)。

lint 是防止"模型编造参数"的最后一道闸门,这里对每条规则都构造了反例。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline.script_gen import (
    compliance_lint,
    ensure_disclaimers,
    normalize_segments,
    total_chars,
)


@pytest.fixture(scope="module")
def auto_profile():
    return load_profile("auto_long")


@pytest.fixture(scope="module")
def beauty_profile(auto_profile):
    """美妆式的医疗宣称红线:品类词表里写了就拦(原 beauty.yaml 的词表节选)。"""
    return replace(auto_profile, name="beauty",
                   forbidden_terms=("治疗", "根治", "治愈", "疗效", "最强", "第一"))


def _theme(profile, name: str, **kw):
    """在 buyguide 的基础上造一个方向,测 lint / 告警对方向配置的响应。"""
    return replace(profile.theme_spec("buyguide"), name=name, label=kw.pop("label", name), **kw)


@pytest.fixture(scope="module")
def cost_theme(auto_profile):
    return _theme(auto_profile, "cost", allow_subject_critique=True,
                  extra_forbidden_terms=("智商税", "冤大头"))


@pytest.fixture(scope="module")
def brand_profile(auto_profile):
    """多一个以公司 / 行业事实为主体的品牌方向。"""
    brand = _theme(auto_profile, "brand", label="品牌历史", required_scopes=("company", "industry"))
    return replace(auto_profile, themes={**auto_profile.themes, "brand": brand})


@pytest.fixture
def facts() -> list[Fact]:
    return [
        Fact(id="f_1", claim="CLTC 续航 702 公里", source_tool="ifind.financials",
             confidence="high", scope="product"),
        Fact(id="f_2", claim="百公里加速 3.8 秒", source_tool="ifind.news",
             confidence="medium", scope="product"),
        Fact(id="f_c1", claim="注册资本 9117197565", source_tool="ifind.stock_summary",
             confidence="high", scope="company"),
    ]


# --------------------------------------------------------------------------
# 事实层级
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "claim,expected",
    [
        ("汉 EV CLTC 续航 702 公里", "product"),
        ("百公里加速 3.8 秒", "product"),
        ("官方指导价 21.98 万元", "product"),
        ("该公司注册资本为 9117197565", "company"),
        ("法定代表人为王传福", "company"),
        ("证券代码为 002594.SZ", "company"),
        ("新能源汽车行业渗透率持续提升", "industry"),
    ],
)
def test_classify_scope(claim, expected):
    from app.pipeline.intake import classify_scope

    assert classify_scope(claim) == expected


def test_company_facts_cannot_be_used_as_product_specs(auto_profile, facts):
    """用公司工商信息讲产品参数是解说视频最典型的讲错对象,必须拦截。"""
    segments = [
        ScriptSegment(
            idx=0, section="参数硬数据",
            text="这台车的注册资本是 9117197565 元。",
            fact_refs=["f_c1"],
        )
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    assert any("公司层面事实当作产品参数" in i.message for i in issues)


def test_company_facts_ok_when_talking_about_company(auto_profile, facts):
    """讲公司背景本身没问题,只是不能冒充产品参数。"""
    segments = [
        ScriptSegment(idx=0, section="开场钩子",
                      text="比亚迪这家公司的法定代表人是王传福。",
                      fact_refs=["f_c1"])
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    assert not any("公司层面事实当作产品参数" in i.message for i in issues)


def test_product_facts_pass(auto_profile, facts):
    segments = [
        ScriptSegment(idx=0, section="参数硬数据", text="续航 702 公里。",
                      fact_refs=["f_1"])
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    assert not [i for i in issues if i.is_error]


def test_product_gap_warning_triggers_when_no_product_facts(auto_profile):
    from app.pipeline.intake import product_gap_warning

    company_only = [
        Fact(id="c1", claim="注册资本 9117197565", source_tool="t", scope="company")
    ]
    # 告警文案现在按**方向**描述,不再是写死的"产品级事实仅 N 条"
    msg = product_gap_warning(company_only, auto_profile)
    assert msg and "产品级 0 条" in msg
    assert auto_profile.theme_spec(None).label in msg

    with_product = company_only + [
        Fact(id="p1", claim="续航 702 公里", source_tool="t", scope="product"),
        Fact(id="p2", claim="加速 3.8 秒", source_tool="t", scope="product"),
        Fact(id="p3", claim="售价 21.98 万", source_tool="t", scope="product"),
    ]
    assert product_gap_warning(with_product, auto_profile) is None


def test_product_gap_warning_is_theme_aware(brand_profile):
    """品牌历史方向的整片主体就是 company/industry 事实。

    product 少于 3 条对它完全正常,不该再报警 —— 否则每个品牌方向项目
    都会挂一条毫无意义的黄色横幅。
    """
    from app.pipeline.intake import product_gap_warning

    # brand 方向要求 company + industry 两层(战略解读要落到行业位置上),
    # 所以这里必须两层都给足,否则报的是 industry 缺口而不是 product。
    brand_solid = [
        Fact(id="c1", claim="注册资本 91.7 亿元", source_tool="t", scope="company"),
        Fact(id="c2", claim="成立于 1995 年", source_tool="t", scope="company"),
        Fact(id="c3", claim="技术路线为纯电", source_tool="t", scope="company"),
        Fact(id="i1", claim="新能源渗透率 58%", source_tool="t", scope="industry"),
        Fact(id="i2", claim="市场份额 12%", source_tool="t", scope="industry"),
        Fact(id="i3", claim="行业销量增长 20%", source_tool="t", scope="industry"),
    ]
    assert product_gap_warning(brand_solid, brand_profile, "brand") is None

    # 只给 company 不给 industry:brand 方向该报 industry 缺口,而不是 product 缺口
    msg = product_gap_warning(brand_solid[:3], brand_profile, "brand")
    assert msg and "行业级 0 条" in msg and "产品级" not in msg

    # 同一组事实,产品向方向就该报 product 缺口 —— 证明告警确实跟着方向走
    overview_msg = product_gap_warning(brand_solid, brand_profile, "buyguide")
    assert overview_msg and "产品级 0 条" in overview_msg


# --------------------------------------------------------------------------
# 合规 lint
# --------------------------------------------------------------------------
def test_passes_clean_script(auto_profile, facts):
    segments = [
        ScriptSegment(idx=0, section="开场钩子", text="这台车有点东西。"),
        ScriptSegment(idx=1, section="参数硬数据", text="它的续航做到了七百零二公里。",
                      fact_refs=["f_1"]),
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    assert [i for i in issues if i.is_error] == []


def test_blocks_numeric_claim_without_fact_reference(auto_profile, facts):
    """核心防线:数字没有事实引用必须被拦截。"""
    segments = [
        ScriptSegment(idx=0, section="参数硬数据", text="续航 702 公里,加速 3.8 秒。"),
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    errors = [i for i in issues if i.is_error]
    assert errors
    assert "事实引用" in errors[0].message


def test_allows_qualitative_claim_without_reference(auto_profile, facts):
    """不含数字的定性描述不需要事实引用 —— 否则会逼模型去编数字。"""
    segments = [ScriptSegment(idx=0, text="它的内饰做工相当扎实。")]
    issues = compliance_lint(segments, auto_profile, facts)
    assert not [i for i in issues if i.is_error]


@pytest.mark.parametrize(
    "text",
    [
        "今天聊比亚迪汉 EV 2026 款。",
        "这台 2024 款改款后的变化主要在内饰。",
    ],
)
def test_model_year_alone_does_not_require_fact(auto_profile, facts, text):
    """年款年份不是性能参数,不应触发 lint —— 否则告警会泛滥到被忽略。"""
    segments = [ScriptSegment(idx=0, text=text)]
    issues = compliance_lint(segments, auto_profile, facts)
    assert not [i for i in issues if i.is_error], issues


@pytest.mark.parametrize(
    "text",
    [
        "续航 702 公里。",
        "百公里加速 3.8 秒。",
        "2026 款续航 702 公里。",   # 年款 + 参数:参数仍需引用
        "售价 21.98 万元。",
    ],
)
def test_spec_numbers_still_require_fact(auto_profile, facts, text):
    segments = [ScriptSegment(idx=0, text=text)]
    issues = compliance_lint(segments, auto_profile, facts)
    assert any(i.is_error for i in issues), f"{text} 应被拦截"


def test_has_numeric_claim_unit():
    from app.pipeline.script_gen import has_numeric_claim

    assert has_numeric_claim("续航 702 公里")
    assert has_numeric_claim("加速 3.8 秒")
    assert not has_numeric_claim("内饰做工很扎实")
    assert not has_numeric_claim("汉 EV 2026 款")
    assert not has_numeric_claim("")


def test_blocks_unknown_fact_reference(auto_profile, facts):
    segments = [ScriptSegment(idx=0, text="续航 702 公里。", fact_refs=["f_nonexistent"])]
    issues = compliance_lint(segments, auto_profile, facts)
    assert any("不存在的事实 ID" in i.message for i in issues)


def test_blocks_category_forbidden_terms(beauty_profile, facts):
    """美妆品类的医疗术语红线。"""
    segments = [ScriptSegment(idx=0, text="这个成分能治疗痘痘。")]
    issues = compliance_lint(segments, beauty_profile, facts)
    assert any("禁用词" in i.message for i in issues)


def test_blocks_absolute_claims(auto_profile, facts):
    segments = [ScriptSegment(idx=0, text="这是目前最强的一台车。")]
    issues = compliance_lint(segments, auto_profile, facts)
    assert any("绝对化用语" in i.message for i in issues)


def test_blocks_medical_terms_only_for_beauty(beauty_profile, auto_profile, facts):
    text = "它能根治这个问题。"
    assert any(i.is_error for i in compliance_lint(
        [ScriptSegment(idx=0, text=text)], beauty_profile, facts))
    # 汽车品类没有这条红线,同样的词不拦
    assert not any(i.is_error for i in compliance_lint(
        [ScriptSegment(idx=0, text=text)], auto_profile, facts))


def test_lint_reports_segment_index(auto_profile, facts):
    segments = [
        ScriptSegment(idx=0, text="正常的一句话。"),
        ScriptSegment(idx=1, text="续航 702 公里。"),
    ]
    issues = compliance_lint(segments, auto_profile, facts)
    assert any(i.segment_idx == 1 for i in issues)


# --------------------------------------------------------------------------
# 段落对齐
# --------------------------------------------------------------------------
def test_normalize_merges_extra_segments(auto_profile):
    plan = ["A", "B", "C"]
    segments = [
        ScriptSegment(idx=0, section="A", text="一二三"),
        ScriptSegment(idx=1, section="B", text="四五六"),
        ScriptSegment(idx=2, section="C", text="七八九"),
        ScriptSegment(idx=3, section="D", text="九十十一"),
    ]
    out = normalize_segments(segments, plan)
    assert len(out) == 3
    # 合并时 fact_refs 去重保留
    assert out[-1].idx == 2


def test_normalize_pads_missing_segments(auto_profile):
    plan = ["A", "B", "C", "D", "E"]
    segments = [ScriptSegment(idx=0, section="A", text="只有一段。")]
    out = normalize_segments(segments, plan)
    assert len(out) == 5
    assert [s.idx for s in out] == [0, 1, 2, 3, 4]


def test_normalize_preserves_fact_refs_on_merge(auto_profile):
    """3 段合并成 2 段时,合并段的 fact_refs 要去重保留全部。"""
    plan = ["A", "B"]
    segments = [
        ScriptSegment(idx=0, section="A", text="前", fact_refs=["f_1"]),
        ScriptSegment(idx=1, section="B", text="中", fact_refs=["f_1"]),
        ScriptSegment(idx=2, section="C", text="后", fact_refs=["f_1", "f_2"]),
    ]
    out = normalize_segments(segments, plan)
    assert len(out) == 2
    assert set(out[-1].fact_refs) == {"f_1", "f_2"}


# --------------------------------------------------------------------------
# 免责声明
# --------------------------------------------------------------------------
def test_disclaimer_appended_when_missing(auto_profile):
    segments = [ScriptSegment(idx=0, section="购买建议", text="适合家用。")]
    out = ensure_disclaimers(segments, auto_profile)
    assert any("仅供参考" in out[-1].text for _ in auto_profile.required_disclaimers)


def test_disclaimer_not_duplicated(auto_profile):
    disclaimer = auto_profile.required_disclaimers[0]
    segments = [ScriptSegment(idx=0, text=f"建议这样选。({disclaimer})")]
    out = ensure_disclaimers(segments, auto_profile)
    assert out[0].text.count(disclaimer) == 1


def test_total_chars_counts_all_segments(auto_profile):
    segments = [
        ScriptSegment(idx=0, text="一二三"),
        ScriptSegment(idx=1, text="四五六七八"),
    ]
    assert total_chars(segments) == 8


# --------------------------------------------------------------------------
# 方向化 lint:允许讲本车缺点,禁止贬低竞品
# --------------------------------------------------------------------------
def test_cost_theme_allows_subject_critique(auto_profile, cost_theme):
    """允许讲缺点的方向(allow_subject_critique)存在意义就是敢讲本车缺点,
    不能因为出现负面表述就拦下来。"""
    theme = cost_theme

    segments = [
        ScriptSegment(idx=0, section="缺点",
                      text="后排坐垫偏硬,长途超过两个小时容易累。"),
        ScriptSegment(idx=1, section="缺点",
                      text="车机语音识别偶尔会听错指令,这个确实烦人。"),
    ]
    issues = compliance_lint(segments, auto_profile, [], theme=theme)
    assert not [i for i in issues if i.is_error], [i.message for i in issues]


def test_cost_theme_blocks_competitor_disparagement(auto_profile, cost_theme):
    """允许讲本车缺点 ≠ 可以拿别家当垫脚石。"""
    theme = cost_theme
    segments = [
        ScriptSegment(idx=0, section="缺点",
                      text="友商那台车就是智商税,买的人都是冤大头。"),
        ScriptSegment(idx=1, section="使用成本",
                      text="同级的竞品在保值率上不如它,这点得承认。"),
    ]
    issues = compliance_lint(segments, auto_profile, [], theme=theme)
    errors = [i for i in issues if i.is_error]
    assert errors, "贬低竞品必须被拦下"
    # 第一段既是方向禁用词又是贬低竞品;第二段只是客观差异,不该被拦
    assert {i.segment_idx for i in errors} == {0}
    assert any("贬低" in i.message for i in errors)


def test_cost_theme_allows_objective_competitor_statement(auto_profile, cost_theme):
    """客观差异陈述不该被误判为贬低 —— 否则对比方向没法写。"""
    theme = cost_theme
    segments = [
        ScriptSegment(idx=0, section="使用成本",
                      text="同级的竞品在保值率上不如它,这点得承认。"),
        ScriptSegment(idx=1, section="缺点",
                      text="和友商相比,这台的短板主要在后排舒适性上。"),
    ]
    issues = compliance_lint(segments, auto_profile, [], theme=theme)
    assert not [i for i in issues if i.is_error], [i.message for i in issues]


def test_non_cost_theme_blocks_disparagement_too(auto_profile):
    """禁止贬低竞品对所有方向生效,不只允许讲缺点的方向。"""
    theme = auto_profile.theme_spec("buyguide")
    assert theme.allow_subject_critique is False
    segments = [
        ScriptSegment(idx=0, section="竞品对比", text="竞品那台车就是智商税。"),
    ]
    issues = compliance_lint(segments, auto_profile, [], theme=theme)
    assert [i for i in issues if i.is_error]


def test_brand_theme_is_not_killed_by_scope_rule(auto_profile, brand_profile):
    """品牌历史方向的整片主体就是公司层事实。

    lint 规则 5(用公司事实讲产品参数)由**产品词**触发,品牌方向的文本
    不会命中 —— 锁定这个行为,防止以后有人"顺手加强"该规则时把品牌方向掐死。
    """
    theme = brand_profile.theme_spec("brand")
    assert theme.required_scopes == ("company", "industry")

    facts_list = [
        Fact(id="c1", claim="注册资本 91.7 亿元", source_tool="qcc", scope="company"),
        Fact(id="c2", claim="成立于 1995 年", source_tool="qcc", scope="company"),
    ]
    segments = [
        ScriptSegment(idx=0, section="品牌起点",
                      text="这家车企最早是做电池起家的,九几年就成立了。",
                      fact_refs=["c1", "c2"]),
    ]
    issues = compliance_lint(segments, auto_profile, facts_list, theme=theme)
    assert not [i for i in issues if i.is_error], [i.message for i in issues]
