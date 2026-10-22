"""背景冲突过滤:讲 A 就别配 B 的特写。

背景:复盘里有一条"讲碳陶刹车配车门把手特写"。特写是很抢戏的画面 ——
观众会以为屏幕上那个部件就是正在讲的东西,于是注意力被带跑。

做法:这一拍旁白里点名了某个部件时,背景就不能是**别的**部件的特写;
反过来,讲车门时,门把手的特写要能被优先选中。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.categories import load_profile
from app.pipeline import library as lib_mod
from app.pipeline.library import Item, Library


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _still(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 480), (230, 230, 230)).save(path, "JPEG")
    return path


def _pool(data_dir, subject="测试车", items=()):
    lib = Library(subject=subject, category="auto_long", items=list(items))
    lib.save()
    return lib


def _closeup(item_id: str, path: Path, desc: str, view: str = "detail") -> Item:
    return Item(id=item_id, kind="still", path=str(path), frame=str(path), view=view,
                target="yes", tagged=True, desc=desc, official=True)


# --------------------------------------------------------------------------
# 词表:两个品类各自有各自的细节词
# --------------------------------------------------------------------------
def test_detail_types_are_category_specific():
    auto = load_profile("auto_long").vocab
    baby = load_profile("baby_long").vocab
    assert "刹车" in auto.detail_types and "车门" in auto.detail_types
    assert "刹车" in baby.detail_types and "篷" in baby.detail_types
    # 汽车专属的细节类型不能出现在母婴品类里
    assert not ({"车门", "车漆", "大灯", "车灯", "充电"} & set(baby.detail_types))


def test_detail_types_in_reads_text():
    vocab = load_profile("auto_long").vocab
    assert vocab.detail_types_in("这套碳陶刹车是它的卖点") == {"刹车"}
    assert vocab.detail_types_in("车门与后备箱的实用性") == {"车门"}
    assert vocab.detail_types_in("一段普通的话") == set()


def test_non_car_profile_has_no_car_detail_words():
    """母婴品类的细节词表里不能有汽车词。"""
    from app.categories import load_profile as load

    baby = load("baby_long").vocab
    for words in baby.detail_types.values():
        for w in words:
            assert w not in ("车门", "车漆", "大灯", "中控屏", "车机", "后备箱", "车灯")


# --------------------------------------------------------------------------
# 冲突判定
# --------------------------------------------------------------------------
def test_detail_conflict_flags_wrong_closeup():
    vocab = load_profile("auto_long").vocab
    brake = vocab.detail_types_in("碳陶刹车是它的优势")
    door = vocab.detail_types_in("车门与后备箱")

    handle = _closeup("a", Path("x.jpg"), "车门把手与门板内饰局部特写")
    assert lib_mod._detail_conflict(handle, brake, vocab) is True    # 讲刹车配门把手 → 冲突
    assert lib_mod._detail_conflict(handle, door, vocab) is False     # 讲车门配门把手 → 不冲突

    wheel = _closeup("b", Path("y.jpg"), "轮毂与轮胎细节特写")
    assert lib_mod._detail_conflict(wheel, brake, vocab) is True
    assert lib_mod._detail_conflict(wheel, door, vocab) is True


def test_detail_conflict_ignores_wide_shots():
    """整车 / 全景不是特写,不构成冲突。"""
    vocab = load_profile("auto_long").vocab
    brake = vocab.detail_types_in("碳陶刹车是它的优势")
    wide = _closeup("c", Path("z.jpg"), "车门把手与整车前45度展示", view="exterior_45")
    assert lib_mod._detail_conflict(wide, brake, vocab) is False


def test_detail_conflict_ignores_unknown_desc():
    """描述里认不出细节就不拦 —— 不能靠猜。"""
    vocab = load_profile("auto_long").vocab
    brake = vocab.detail_types_in("碳陶刹车是它的优势")
    vague = _closeup("d", Path("w.jpg"), "白色车身局部特写")
    assert lib_mod._detail_conflict(vague, brake, vocab) is False


def test_detail_fit_ranks_matching_closeup_first():
    vocab = load_profile("auto_long").vocab
    door = vocab.detail_types_in("车门与开门按键")
    handle = _closeup("a", Path("x.jpg"), "车门内饰板与开门按键特写")
    wide = _closeup("b", Path("y.jpg"), "整车前45度展示", view="exterior_45")
    assert lib_mod._detail_fit(handle, door, vocab) == 0
    assert lib_mod._detail_fit(wide, door, vocab) == 2


# --------------------------------------------------------------------------
# 端到端:fill() 真的会避开配错的特写
# --------------------------------------------------------------------------
def test_fill_drops_conflicting_closeup(data_dir, tmp_path):
    img = _still(tmp_path / "a.jpg")
    _pool(data_dir, items=[
        _closeup("handle", img, "车门把手与门板内饰局部特写"),
        _closeup("wide", img, "整车前45度展示", view="exterior_45"),
    ])
    got = lib_mod.fill("测试车", 3.0, hint="exterior", detail="这套碳陶刹车是它的优势")
    assert got
    assert "handle" not in {p.item_id for p in got}


def test_fill_prefers_matching_closeup(data_dir, tmp_path):
    img = _still(tmp_path / "a.jpg")
    _pool(data_dir, items=[
        _closeup("handle", img, "车门内饰板与开门按键特写"),
        _closeup("wide", img, "整车前45度展示", view="exterior_45"),
    ])
    got = lib_mod.fill("测试车", 3.0, hint="exterior", detail="车门与开门按键的实用性")
    assert got
    assert got[0].item_id == "handle"


def test_fill_keeps_pool_when_filtering_would_starve_it(data_dir, tmp_path):
    """池子剔完不够铺满时,宁可配得不对,也不要这一拍没有背景。"""
    img = _still(tmp_path / "a.jpg")
    _pool(data_dir, items=[
        _closeup("handle", img, "车门把手与门板内饰局部特写"),
    ])
    got = lib_mod.fill("测试车", 12.0, hint="exterior", detail="碳陶刹车")
    assert got      # 还是铺上了
