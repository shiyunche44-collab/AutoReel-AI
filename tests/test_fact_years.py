"""旧年款事实屏蔽:新闻检索把上一年款的参数一起捞回来,不能进脚本。"""
from __future__ import annotations

from app.models import Fact
from app.pipeline.intake import fact_year, mark_stale_years


def _f(claim, basis="", **kw):
    return Fact(claim=claim, basis=basis, source_tool=kw.pop("source_tool", "t"),
                scope=kw.pop("scope", "product"), **kw)


def test_fact_year_reads_basis_and_claim():
    assert fact_year(_f("x", "CLTC · 标准版(2026款)")) == 2026
    assert fact_year(_f("2025 款标准版续航835公里")) == 2025
    assert fact_year(_f("全系800V")) is None


def _current():
    return [_f("小米YU7标准版搭载73kWh磷酸铁锂电池,CLTC续航643公里。", "CLTC · 标准版(2026款)"),
            _f("小米YU7标准版采用752V碳化硅高压平台,15分钟补能405公里。", "标准版(2026款)"),
            _f("小米YU7标准版售价23.35万元。", "标准版(2026款)"),
            _f("小米YU7 GT百公里制动距离32.9米。", "GT版(2026款)")]


def test_changed_numbers_of_older_year_disabled():
    """p_c13886ee229b / p_3de7193d9669 的真实情况:新款改了电池、续航、电压平台。"""
    range_old = _f("2025年6月上市的YU7标准版CLTC续航835公里。", "CLTC · 2025款标准版")
    volt_old = _f("小米YU7全系采用800V碳化硅高压平台。", "全系 · 2025款")
    stale = mark_stale_years(_current() + [range_old, volt_old])
    assert stale == [range_old, volt_old]
    assert not range_old.enabled and not volt_old.enabled


def test_carry_over_specs_of_older_year_kept():
    """车身尺寸、悬架这些沿用的参数,旧年款写着也照样对。"""
    size = _f("小米YU7车身尺寸为长4999mm、宽1996mm,轴距3000mm。", "2025款")
    turning = _f("小米YU7转弯半径5.7米。", "全系 · 2025款")  # 和制动距离同是"米",不是一回事
    same = _f("小米YU7标准版售价23.35万元。", "标准版 · 2025款")  # 数没变
    pro = _f("YU7 Pro版CLTC续航770公里。", "CLTC · 2025款Pro版")  # 新款没有 Pro 版的数可比
    undated = _f("全系800V碳化硅平台。")
    company = _f("2025款累计交付10万辆", "2025款", scope="company")
    extra = [size, turning, same, pro, undated, company]
    assert mark_stale_years(_current() + extra) == []
    assert all(f.enabled for f in extra)


def test_nothing_disabled_when_newest_year_is_thin():
    """新款只有一两条时,旧款可能才是在售主力,不动。"""
    facts = [_f("x", "2026款"), _f("y", "2025款"), _f("z", "2025款")]
    assert mark_stale_years(facts) == []
    assert all(f.enabled for f in facts)


def test_on_sale_lineup_facts_never_disabled():
    """p_b3dc0365cc69 实跑:「四驱GT」没有「版」字被当成全系,在售的 2025 款三个版本价格全被屏蔽了。"""
    cur = [_f("2026款小米YU7后驱标准版厂商指导价23.35万元。", "2026款 · 后驱标准版", source_tool="dongchedi.lineup"),
           _f("2026款小米YU7四驱GT厂商指导价38.99万元。", "2026款 · 四驱GT", source_tool="dongchedi.lineup"),
           _f("标准版CLTC续航643公里。", "CLTC · 标准版 · 2026款")]
    pro = _f("2025款小米YU7四驱Pro版厂商指导价27.99万元。", "2025款 · 四驱Pro版", source_tool="dongchedi.lineup")
    assert mark_stale_years(cur + [pro]) == [] and pro.enabled
    # 不在清单里的、同版本被改了数的旧事实照样屏蔽
    old = _f("2025款标准版CLTC续航835公里。", "CLTC · 2025款标准版")
    assert mark_stale_years(cur + [pro, old]) == [old]


def test_versions_and_rate_units():
    from app.pipeline.grounding import differing_measures, measures, versions_of
    assert versions_of("2026款 · 四驱GT") == {"GT"}
    assert versions_of("CLTC · 标准版 · 2026款") == {"标准版"}
    assert versions_of("全系") == set()
    # 「标准版」和「后驱标准版」是同一个版本;GT 和标准版不是
    assert differing_measures("续航643公里", "标准版", "续航835公里", "后驱标准版")
    assert not differing_measures("导价23.35万元", "后驱标准版", "导价38.99万元", "四驱GT")
    # 电耗 12.9kWh/100km 和电池 73kWh 不是一个维度
    assert not differing_measures("搭载73kWh电池", "", "能耗12.9kWh/100km", "")
    assert measures("能耗12.9kWh/100km") == {("能耗", "kWh/"): {"12.9"}}


def test_on_sale_spec_sheet_facts_never_disabled():
    """配置表只列在售版本:2025 款后驱长续航版 835 公里还在卖,不能当成被 2026 款改掉的旧数。"""
    cur = [_f("2026款标准版CLTC续航643公里。", "CLTC · 2026款 · 后驱标准版"),
           _f("2026款四驱GT CLTC续航705公里。", "CLTC · 2026款 · 四驱GT"),
           _f("2026款后驱标准版整备质量2200kg。", "2026款 · 后驱标准版")]
    long = _f("2025款后驱长续航版整备质量2315kg。", "2025款 · 后驱长续航版 · 标准版",
              source_tool="autohome.specs")
    assert mark_stale_years(cur + [long]) == [] and long.enabled
