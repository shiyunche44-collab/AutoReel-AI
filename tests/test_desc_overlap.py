
from app.pipeline.library import select
from app.pipeline.library.model import Item


def _item(desc: str) -> Item:
    return Item(id="x", kind="still", path="", desc=desc)


def test_overlap_prefers_matching_description():
    text = "后排空间很大,腿部空间也够"
    assert select._desc_overlap(_item("后排座椅与腿部空间"), text) > \
        select._desc_overlap(_item("车辆外观侧面"), text)


def test_overlap_is_capped_and_safe_on_empty():
    assert select._desc_overlap(_item(""), "后排空间") == 0
    assert select._desc_overlap(_item("后排空间"), "") == 0
    long = "后排空间腿部空间头部空间座椅宽度"
    assert select._desc_overlap(_item(long), long) == select.OVERLAP_CAP
