"""「一次讲透」片型:细分名单、争议结尾、常驻参数清单、细分连更排期。不需要网络与额度。"""
from __future__ import annotations

import datetime as dt

import pytest
from PIL import Image

from app import config
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import overlays, variety
from app.pipeline.explainer import beat_render
from app.team import editor


@pytest.fixture(scope="module")
def auto():
    return load_profile("auto_long")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


# --------------------------------------------------------------------------
# 片型
# --------------------------------------------------------------------------
def test_theme_declares_segment_and_ledger(auto):
    theme = auto.theme_spec("walkthrough")
    assert theme.ledger and theme.segment == "offroad"
    assert "比亚迪鲨鱼" in auto.vocab.segments["offroad"]
    assert auto.theme_durations(theme, config.LANDSCAPE) == [240] or \
        max(auto.theme_durations(theme, config.LANDSCAPE)) == 240
    assert editor.segment_theme(auto) is theme
    # 其它片型不带清单、不限细分
    assert not auto.theme_spec("buyguide").ledger and not auto.theme_spec("buyguide").segment


def test_only_dispute_ending_and_no_series_outro(auto):
    theme = auto.theme_spec("walkthrough")
    assert list(variety.allowed_endings(theme)) == ["dispute"]
    text = variety.rule10({"ending": "dispute", "series": "柒哥一次讲透", "episode": 2})
    assert "柒哥一次讲透" in text and "不加下期预告" in text
    # 别的结尾照旧带栏目尾句
    other = variety.rule10({"ending": "next_episode", "series": "柒哥帮你挑", "episode": 3})
    assert "不加下期预告" not in other


# --------------------------------------------------------------------------
# 常驻参数清单
# --------------------------------------------------------------------------
def _spec(idx: int, chapter: str, rows: list[tuple[str, str]] | None = None,
          stat: tuple[str, str] | None = None) -> ShotSpec:
    overlay = None
    if rows:
        overlay = {"kind": "panel", "rows": [{"label": k, "values": [v]} for k, v in rows]}
    elif stat:
        overlay = {"kind": "stat", "label": stat[0], "value": stat[1]}
    return ShotSpec(idx=idx, duration=3.0, visual_zh="[broll] x", narration_zh="a",
                    layout="broll", chapter=chapter, overlay=overlay)


def test_ledger_accumulates_within_chapter_and_resets():
    specs = [
        _spec(0, "引言", stat=("海外销量", "4万台")),
        _spec(1, "尺寸", rows=[("车长", "5457毫米")]),
        _spec(2, "尺寸"),
        _spec(3, "尺寸", rows=[("轴距", "3260毫米"), ("车长", "5.46米")]),
        _spec(4, "动力", stat=("综合功率", "320千瓦")),
        _spec(5, "结论", stat=("起售价", "20万")),
    ]
    _, where = beat_render.chapters_of(specs)
    rows = [beat_render.ledger_rows(specs, where, n) for n in range(len(specs))]
    assert rows[0] == []                                    # 开头章不显示
    assert rows[1] == rows[2] == [("车长", "5457毫米")]      # 没新参数的拍,清单留着
    assert rows[3] == [("轴距", "3260毫米"), ("车长", "5.46米")]  # 同名留最新
    assert rows[4] == [("综合功率", "320千瓦")]               # 换章清空
    assert rows[5] == []                                    # 结论章不显示


def test_ledger_reads_numbers_the_narration_says():
    """纯实拍拍子没有数据卡:清单从旁白里念到的带单位数值来。"""
    rows = beat_render._narration_rows
    assert rows("尺寸摆在这，车长5457毫米、宽1971毫米、高1925毫米。") == [
        ("车长", "5457毫米"), ("宽", "1971毫米"), ("高", "1925毫米")]
    assert rows("有效载荷790公斤，货箱1200升。") == [("有效载荷", "790公斤"), ("货箱", "1200升")]
    assert rows("650牛·米，0-100公里/小时加速5.7秒。") == [("百公里加速", "5.7秒")]
    assert rows("加权油耗3.5升/100公里，") == [("加权油耗", "3.5升/100公里")]
    assert rows("V2L放电最高6千瓦，配2个插座") == [("V2L放电", "6千瓦")]
    # 认不出参数名的、「从 A 升到 B」、年份个数,都不上
    assert rows("动力看DMO四驱版，436马力、321千瓦、") == []
    assert rows("牵引从2500公斤升到3500公斤。") == []
    assert rows("2024年5月，它有7个安全气囊") == []


def test_ledger_rows_use_narration_when_no_card():
    specs = [
        _spec(0, "引言"),
        ShotSpec(idx=1, duration=3.0, visual_zh="[broll] x", narration_zh="轴距3260毫米，",
                 layout="broll", chapter="尺寸"),
        _spec(2, "结论"),
    ]
    _, where = beat_render.chapters_of(specs)
    assert beat_render.ledger_rows(specs, where, 1) == [("轴距", "3260毫米")]


def test_ledger_draws_top_left_onto_existing_bar(tmp_path):
    size = (1920, 1080)
    bar = overlays.chapter_bar([("开头", 3), ("尺寸", 30), ("结论", 5)], 1, size,
                               tmp_path / "bar.png")
    before = Image.open(bar).convert("RGBA")
    overlays.ledger([("车长", "5457毫米"), ("轴距", "3260毫米")], "尺寸", size, bar)
    after = Image.open(bar).convert("RGBA")
    assert after.size == size
    box = (0, 0, size[0] // 3, size[1] // 2)
    assert after.crop(box).getbbox() is not None            # 左上角画了东西
    right = (size[0] // 2, 0, size[0], size[1] // 2)
    assert after.crop(right).tobytes() == before.crop(right).tobytes()  # 别处没动

    # 没有行、竖屏:原样不动
    plain = overlays.chapter_bar([("开头", 3), ("结论", 5)], 0, size, tmp_path / "plain.png")
    raw = Image.open(plain).tobytes()
    overlays.ledger([], "尺寸", size, plain)
    assert Image.open(plain).tobytes() == raw


# --------------------------------------------------------------------------
# 服务重启后接着建素材池
# --------------------------------------------------------------------------
def test_orphan_pool_resumes_and_resets_wait(fresh_db, monkeypatch):
    from app.pipeline import library, orchestrator

    started: list[str] = []
    building: set[str] = set()
    monkeypatch.setattr(library, "pool_status",
                        lambda n: "building" if n in building else "tagging")
    monkeypatch.setattr(library, "is_building", lambda n: n in building)

    def fake_start(pid, name, profile, llm=None, **kw):
        started.append(name)
        building.add(name)

    monkeypatch.setattr(orchestrator, "_start_library", fake_start)
    monkeypatch.setattr(orchestrator, "_llm", lambda: None)
    pid = "p_test"
    fresh_db.write_json(pid, "beats_waiting.json", {"8": 1.0})
    orchestrator._resume_orphan_pools(pid, ["比亚迪鲨鱼"], None)
    assert started == ["比亚迪鲨鱼"]
    assert fresh_db.read_json(pid, "beats_waiting.json", None) == {}
    # 已经在建:不重复起
    orchestrator._resume_orphan_pools(pid, ["比亚迪鲨鱼"], None)
    assert started == ["比亚迪鲨鱼"]
    # 建完了还是没素材(empty):同一个项目不再接手第二次
    building.clear()
    monkeypatch.setattr(library, "pool_status", lambda n: "empty")
    orchestrator._resume_orphan_pools(pid, ["比亚迪鲨鱼"], None)
    assert started == ["比亚迪鲨鱼"]
    orchestrator._resume_orphan_pools("p_other", ["坦克300"], None)
    assert started == ["比亚迪鲨鱼", "坦克300"]


# --------------------------------------------------------------------------
# 细分连更
# --------------------------------------------------------------------------
def test_next_segment_car_skips_cooled_then_done():
    names = ("坦克300", "比亚迪鲨鱼", "长城炮")
    pick = editor.next_segment_car
    assert pick(names, set(), set()) == "坦克300"
    assert pick(names, {editor._norm("坦克300")}, set()) == "比亚迪鲨鱼"
    assert pick(names, set(), {editor._norm("坦克300")}) == "比亚迪鲨鱼"
    # 都做过:从头轮;全在冷却:不排
    done = {editor._norm(n) for n in names}
    assert pick(names, set(), done) == "坦克300"
    assert pick(names, done, set()) == ""


def _slot(day: int, hour: int = 18) -> float:
    return dt.datetime(2026, 11, day, hour, 30).timestamp()


def test_plan_segment_fills_configured_weekdays_only(fresh_db, auto, monkeypatch):
    monkeypatch.setattr(config, "TEAM_WALKTHROUGH_WEEKDAYS", (1, 5))  # 周二、周六
    monkeypatch.setattr(config, "TEAM_AUTO_APPROVE_TOPICS", False)
    # 11-16 周一、11-17 周二(两个档)、11-21 周六
    slots = [_slot(16), _slot(17, 12), _slot(17), _slot(21)]
    made, rest = editor.plan_segment("auto_long", auto, slots, _slot(15))
    assert made == 2 and rest == [_slot(16), _slot(17)]     # 一天只占一个档
    items = sorted(fresh_db.list_plan_items("auto_long"), key=lambda i: i["slot_at"])
    assert [i["theme"] for i in items] == ["walkthrough", "walkthrough"]
    assert all(i["aspect"] == config.LANDSCAPE and i["duration"] == 240 for i in items)
    assert all(i["status"] == "proposed" for i in items)
    names = auto.vocab.segments["offroad"]
    assert [i["subject"] for i in items] == list(names[:2])  # 按名单顺序,不重复


def test_plan_segment_off_by_default(fresh_db, auto, monkeypatch):
    monkeypatch.setattr(config, "TEAM_WALKTHROUGH_WEEKDAYS", ())
    slots = [_slot(17), _slot(21)]
    assert editor.plan_segment("auto_long", auto, slots, _slot(15)) == (0, slots)
    assert fresh_db.list_plan_items("auto_long") == []
