"""专业数据集:两种返回形态解析、销量解析、主备降级。全离线。"""
from __future__ import annotations

from app.providers import ark_datapro, car_specs, facts
from app.providers.facts import RawDoc

FLAT = [
    {"车型编码": "70504", "品牌": "小米", "车系": "小米YU7", "车型名称": "2025款 小米YU7 四驱Max版",
     "销售状态": "在售", "整备质量(kg)": "2405", "前轮胎规格": "245/55 R19●/245/45 R21○/245/50 R20○",
     "驱动方式": "双电机四驱●", "指导价": "329900"},
    {"车型编码": "76185", "品牌": "小米", "车系": "小米YU7", "车型名称": "2026款 小米YU7 后驱标准版",
     "销售状态": "在售", "整备质量(kg)": "2200", "前轮胎规格": "245/55 R19●/245/50 R20○",
     "驱动方式": "后置后驱●", "指导价": "233500"},
    {"车型编码": "1", "品牌": "小米", "车系": "小米YU7", "车型名称": "2024款 小米YU7 停售版",
     "销售状态": "停售", "整备质量(kg)": "1"},
]
NESTED = [{"车型信息": {"品牌": "理想汽车", "车系": "理想L9", "车型": "2026款 Ultra"},
           "配置参数": {"指导价": "45.98万", "车重": {"value": 2755, "unit": "kg"},
                        "轮胎": {"前轮胎规格": "285/45 R21"}}}]


def test_flat_format_strips_markers_series_and_stopped_trims():
    rows, trims = ark_datapro.parse_items(FLAT, "小米YU7")
    assert trims == ["2025款 四驱Max版", "2026款 后驱标准版"]
    d = dict(rows)
    assert d["前轮胎规格"]["2025款 四驱Max版"] == "245/55 R19、245/45 R21、245/50 R20"
    assert d["驱动方式"]["2026款 后驱标准版"] == "后置后驱"
    assert "指导价" not in d and "车型编码" not in d


def test_nested_format_units():
    rows, trims = ark_datapro.parse_items(NESTED, "理想L9")
    assert trims == ["2026款 Ultra"]
    assert dict(rows)["车重(kg)"] == {"2026款 Ultra": "2755"}
    text = car_specs.render("理想L9", rows, trims, "2026-11-02", source="x")
    assert "- 车重:全系 2755kg" in text


def test_parse_sales_sorted_newest_first():
    items = [{"年份": "2026", "月份": "8", "车系": "小米SU7", "销量": 16472},
             {"年份": "2026", "月份": "9", "车系": "小米SU7", "销量": 15723}]
    assert ark_datapro.parse_sales(items) == {"小米SU7": [("2026-09", 15723), ("2026-08", 16472)]}


def test_sales_reports_missing_entities(monkeypatch):
    monkeypatch.setattr(ark_datapro, "search", lambda q: {
        "items": [{"年份": "2026", "月份": "9", "车系": "特斯拉Model Y", "销量": 50009}],
        "missing_entities": ["五菱缤果Pro"]})
    doc = ark_datapro.sales("特斯拉Model Y 五菱缤果Pro")
    assert doc.ok and "2026-09 50009辆" in doc.text and "五菱缤果Pro:数据集无此车系销量" in doc.text


def test_failure_is_not_raised(monkeypatch):
    def boom(q):
        raise ark_datapro.DataproError("本次query不在专业数据集的支持范围内")

    monkeypatch.setattr(ark_datapro, "search", boom)
    doc = ark_datapro.specs("小米YU9")
    assert not doc.ok and "支持范围" in doc.error


def test_autohome_specs_falls_back_when_dataset_fails(monkeypatch):
    monkeypatch.setattr(ark_datapro, "enabled", lambda: True)
    monkeypatch.setattr(ark_datapro, "specs", lambda s, p=None: RawDoc(
        tool=ark_datapro.CONFIG_TOOL, query=s, retrieved_at="d", text="", ok=False, error="x"))
    monkeypatch.setattr(car_specs, "specs", lambda s, p=None: RawDoc(
        tool=car_specs.TOOL, query=s, retrieved_at="d", text="ah"))
    assert facts.autohome_specs("小米YU7").tool == "autohome.specs"
    monkeypatch.setattr(ark_datapro, "specs", lambda s, p=None: RawDoc(
        tool=ark_datapro.CONFIG_TOOL, query=s, retrieved_at="d", text="dp"))
    assert facts.autohome_specs("小米YU7").tool == "ark.datapro.specs"
