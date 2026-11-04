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


NESTED_LIST = [{"车型信息": {"品牌": "吉利银河", "车系": "星愿", "在售版本": [
                    {"年款": "2026款", "版本": "480km 探索版", "厂商指导价": "8.68万"}]},
                "配置参数": [
                    {"车型": "2026款 310km 向往版", "最大功率": {"value": 58, "unit": "kW"}},
                    {"车型": "2026款 480km 探索版", "最大功率": {"value": 85, "unit": "kW"}}]}]


def test_nested_list_format_one_row_per_trim():
    rows, trims = ark_datapro.parse_items(NESTED_LIST, "吉利星愿")
    assert trims == ["2026款 310km 向往版", "2026款 480km 探索版"]
    assert dict(rows)["最大功率(kW)"]["2026款 480km 探索版"] == "85"


def test_specs_tries_sub_brand_after_miss(monkeypatch):
    seen = []

    def fake(q):
        seen.append(q)
        if "吉利银河星愿" in q:
            return {"items": NESTED_LIST}
        return {"items": [], "msg": "本次query不在专业数据集的支持范围内"}

    monkeypatch.setattr(ark_datapro, "search", fake)
    doc = ark_datapro.specs("吉利星愿")
    assert doc.ok and "480km 探索版 85kW" in doc.text
    assert len(seen) == 2 and "吉利星愿" in seen[0]


def test_names_keep_original_first_and_skip_other_brands():
    assert ark_datapro._names("吉利星愿") == ["吉利星愿", "吉利银河星愿", "银河星愿"]
    assert ark_datapro._names("吉利银河星愿") == ["吉利银河星愿"]
    assert ark_datapro._names("小米SU7") == ["小米SU7"]


def _flat_trim(name):
    return {"车系": "星愿", "车型名称": f"2026款 星愿 {name}", "销售状态": "在售",
            "最大功率(kW)": "85"}


def test_specs_backfills_unreturned_trims(monkeypatch):
    def fake(q):
        if "全部在售版本" in q:
            return {"items": [_flat_trim("410km 向往版")], "has_more": True,
                    "unreturned_model_names": ["2026款 星愿 480km 探索版"]}
        assert "2026款 星愿 480km 探索版" in q
        return {"items": [_flat_trim("480km 探索版")]}

    monkeypatch.setattr(ark_datapro, "search", fake)
    doc = ark_datapro.specs("银河星愿")
    assert doc.ok and "在售 2 个版本" in doc.text


def test_specs_refuses_partial_table(monkeypatch):
    monkeypatch.setattr(ark_datapro, "search", lambda q: {
        "items": [_flat_trim("410km 向往版")], "has_more": True,
        "unreturned_model_names": [f"款{i}" for i in range(ark_datapro._MAX_BACKFILL + 1)]})
    doc = ark_datapro.specs("银河星愿")
    assert not doc.ok and "没取全" in doc.error


def test_specs_drops_other_series(monkeypatch):
    other = {"车系": "海豚", "车型名称": "2026款 海豚 420km", "销售状态": "在售", "最大功率(kW)": "70"}
    monkeypatch.setattr(ark_datapro, "search", lambda q: {"items": [other]})
    doc = ark_datapro.specs("吉利星愿")
    assert not doc.ok and "不是这个车系" in doc.error


def test_sales_nested_format_keeps_basis(monkeypatch):
    monkeypatch.setattr(ark_datapro, "search", lambda q: {"items": [{
        "查询条件": {"品牌车系": "吉利星愿"},
        "销售数据": [{"月份": "2026-08", "零售量": 39651, "统计口径": "零售量/全国上牌数据"},
                     {"月份": "2026-09", "零售量": 44275, "统计口径": "零售量/全国上牌数据"}]}]})
    doc = ark_datapro.sales("吉利星愿")
    assert doc.ok and "2026-09 44275辆(零售量/全国上牌数据)" in doc.text
    assert doc.text.index("2026-09 44275辆") < doc.text.index("2026-08 39651辆")


def test_sales_other_field_names_keep_each_month_basis(monkeypatch):
    # 同一句查询,另一次返回换了字段名,而且各月口径不一样
    monkeypatch.setattr(ark_datapro, "search", lambda q: {"items": [{
        "查询条件": {"品牌/车系": "吉利星愿"},
        "销售数据": [{"统计月份": "2026-10", "销量": 71300, "统计口径": "销量"},
                     {"统计月份": "2026-09", "销量": 44275, "统计口径": "零售量/全国上牌数据"}]}]})
    doc = ark_datapro.sales("吉利星愿")
    assert "2026-10 71300辆(销量)" in doc.text
    assert "2026-09 44275辆(零售量/全国上牌数据)" in doc.text


def test_year_kept_when_trim_name_has_none():
    items = [{"车型信息": {"车系": "星愿", "年款": "2026款", "车型": "480km 探索版"},
              "配置参数": {"CLTC纯电续航里程": "480km"}}]
    _, trims = ark_datapro.parse_items(items, "银河星愿")
    assert trims == ["2026款 480km 探索版"]


def test_backfill_falls_back_to_bare_model_name(monkeypatch):
    def fake(q):
        if "全部在售版本" in q:
            return {"items": [_flat_trim("410km 向往版")], "has_more": True,
                    "unreturned_model_names": ["2026款 星愿 480km 探索版"]}
        if q == "汽车配置:2026款 星愿 480km 探索版":
            return {"items": [_flat_trim("480km 探索版")]}
        return {"items": [], "msg": "本次query不在专业数据集的支持范围内"}

    monkeypatch.setattr(ark_datapro, "search", fake)
    assert "在售 2 个版本" in ark_datapro.specs("银河星愿").text


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


def test_both_failing_reports_dataset_error(monkeypatch):
    monkeypatch.setattr(ark_datapro, "enabled", lambda: True)
    monkeypatch.setattr(ark_datapro, "specs", lambda s, p=None: RawDoc(
        tool=ark_datapro.CONFIG_TOOL, query=s, retrieved_at="d", text="", ok=False, error="没收录"))
    monkeypatch.setattr(car_specs, "specs", lambda s, p=None: RawDoc(
        tool=car_specs.TOOL, query=s, retrieved_at="d", text="", ok=False, error="找不到"))
    assert facts.autohome_specs("吉利星愿").error == "专业数据集:没收录;找不到"


def test_specs_retries_short_query_when_field_list_is_refused(monkeypatch):
    def fake(q):
        if "整备质量" in q:
            return {"items": [], "msg": "本次query不在专业数据集的支持范围内"}
        return {"items": [_flat_trim("410km 向往版")]}

    monkeypatch.setattr(ark_datapro, "search", fake)
    assert ark_datapro.specs("吉利星愿").ok
