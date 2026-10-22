"""在售版本清单:解析懂车帝 car_list、车系名匹配。全离线。"""
from __future__ import annotations

from app.providers import car_lineup
from app.providers.facts import SOURCES


def _info(year, name, price, tags, car_id=1):
    return {"type": "1115", "info": {"car_id": car_id, "year": year, "series_name": "小米YU7",
                                    "car_name": name, "official_price_str": price, "tags": tags}}


PAYLOAD = {"data": {"tab_list": [
    {"tab_key": "online_all", "data": [
        {"type": "1137", "info": {"name": "2026款-小米YU7"}},  # 年款分组标题
        _info(2026, "后驱标准版", "23.35万", ["835KM", "后驱"]),
        _info(2026, "四驱GT", "38.99万", ["705KM", "四驱"]),
        _info(2025, "后驱长续航版", "25.35万", ["835KM", "后驱"]),
    ]},
    {"tab_key": "online_2025", "data": [_info(2025, "后驱长续航版", "25.35万", ["后驱"])]},
]}}


def test_parse_only_on_sale_tab_and_skip_headers():
    rows = car_lineup.parse_car_list(PAYLOAD)
    assert [(r["year"], r["name"], r["price"], r["drive"]) for r in rows] == [
        (2026, "后驱标准版", "23.35万", "后驱"),
        (2026, "四驱GT", "38.99万", "四驱"),
        (2025, "后驱长续航版", "25.35万", "后驱"),
    ]


def test_render_has_year_price_drive_but_not_range_tag():
    text = car_lineup.render("小米YU7", car_lineup.parse_car_list(PAYLOAD), "2026-10-22")
    assert "- 2025款 小米YU7 后驱长续航版:厂商指导价 25.35万,后驱" in text
    assert "目前在售 3 个版本" in text and "已停售" in text
    # 列表里的续航标签不可靠(标准版标着 835KM),不能进事实
    assert "835" not in text


def test_series_match_prefers_exact_name():
    assert car_lineup._match_score("特斯拉Model Y", "Model Y") == 2
    assert car_lineup._match_score("小米YU7", "小米YU7") == 2
    assert car_lineup._match_score("小米YU7", "小米YU7 GT") == 1
    assert car_lineup._match_score("小米YU7", "小米SU7") == 0


def test_registered_as_fact_source():
    assert "dongchedi.lineup" in SOURCES
