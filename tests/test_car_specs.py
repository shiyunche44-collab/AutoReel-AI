"""参数配置表:解析汽车之家参数接口、按取值合并版本、找品牌。全离线。"""
from __future__ import annotations

from app.providers import car_specs
from app.providers.facts import SOURCES

NAMES = {1: "2026款 后驱标准版", 2: "2026款 四驱GT"}


def _cell(specid, value="", sub=()):
    return {"specid": specid, "value": value,
            "sublist": [{"subname": "", "subvalue": s} for s in sub]}


RESULT = {"paramtypeitems": [
    {"name": "基本参数", "paramitems": [
        {"name": "厂商指导价(元)", "valueitems": [_cell(1, "23.35万"), _cell(2, "38.99万")]},
        {"name": "CLTC纯电续航里程(km)", "valueitems": [_cell(1, "643"), _cell(2, "705"), _cell(9, "835")]},
        {"name": "整备质量(kg)", "valueitems": [_cell(1, "2200"), _cell(2, "2460")]},
        {"name": "官方100-0km/h制动(m)", "valueitems": [_cell(1, "-"), _cell(2, "32.9")]},
    ]},
    {"name": "底盘转向", "paramitems": [
        {"name": "前悬架类型", "valueitems": [_cell(1, sub=["双叉臂式独立悬架"]),
                                            _cell(2, sub=["双叉臂式独立悬架"])]},
        {"name": "前轮胎规格", "valueitems": [_cell(1, sub=["245/55 R19", "245/50 R20"]),
                                            _cell(2, sub=["265/40 R21"])]},
    ]},
    {"name": "电池/充电", "paramitems": [
        # 同名参数在两组各出现一次,只留第一次
        {"name": "CLTC纯电续航里程(km)", "valueitems": [_cell(1, "643"), _cell(2, "705")]},
        {"name": "电池快充电量范围(%)", "valueitems": [_cell(1, "10-80"), _cell(2, "10-80")]},
    ]},
]}


def test_parse_skips_price_dashes_dupes_and_unknown_trims():
    rows = car_specs.parse_params(RESULT, NAMES)
    names = [n for n, _ in rows]
    assert "厂商指导价(元)" not in names  # 价格归在售清单
    assert names.count("CLTC纯电续航里程(km)") == 1
    assert dict(rows)["CLTC纯电续航里程(km)"] == {"2026款 后驱标准版": "643", "2026款 四驱GT": "705"}
    assert dict(rows)["官方100-0km/h制动(m)"] == {"2026款 四驱GT": "32.9"}
    assert dict(rows)["前轮胎规格"]["2026款 后驱标准版"] == "245/55 R19、245/50 R20"


def test_render_units_whole_range_and_merge():
    text = car_specs.render("小米YU7", car_specs.parse_params(RESULT, NAMES), list(NAMES.values()),
                            "2026-10-22")
    assert "在售 2 个版本:2026款 后驱标准版、2026款 四驱GT" in text
    assert "- 整备质量:2026款 后驱标准版 2200kg;2026款 四驱GT 2460kg" in text
    assert "- 官方100-0km/h制动:2026款 四驱GT 32.9米" in text
    assert "- 前悬架类型:全系 双叉臂式独立悬架" in text
    assert "- 电池快充电量范围:全系 10-80%" in text


def test_brand_from_subject():
    brands = [{"id": 1, "name": "小米汽车"}, {"id": 2, "name": "特斯拉"}, {"id": 3, "name": "小鹏"}]
    assert car_specs._brand_ids("小米YU7", brands) == [1]
    assert car_specs._brand_ids("特斯拉Model Y", brands) == [2]
    assert car_specs._brand_ids("Model Y", brands) == []


def test_registered_as_fact_source():
    assert "autohome.specs" in SOURCES
