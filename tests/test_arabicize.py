"""中文数量词 -> 阿拉伯数字:字幕可读,lint 的"数字必须有引用"不被中文数字绕过。"""
from __future__ import annotations

import pytest

from app.models import ScriptSegment
from app.pipeline import script_gen
from app.pipeline.grounding import arabicize, cn_number


@pytest.mark.parametrize("src,want", [
    ("官方价二十六万三千五", "官方价26.35万"),
    ("车长四千七百九十七毫米", "车长4797毫米"),
    ("百公里加速四点三秒", "百公里加速4.3秒"),
    ("每分钟约补十六点七公里", "每分钟约补16.7公里"),
    ("二〇二六年全球销量超六十五万辆", "2026年全球销量超65万辆"),
    ("百分之三十", "30%"),
    ("两万五", "2.5万"),
    ("一百零五", "105"),
    ("十六英寸屏", "16英寸屏"),
    # 不是数据的写法一律不动
    ("重回第一", "重回第一"), ("千万别买", "千万别买"), ("万一出问题", "万一出问题"),
    ("三电是底牌", "三电是底牌"), ("一台车", "一台车"), ("两档模式", "两档模式"),
    ("十分重要", "十分重要"), ("一一对应", "一一对应"), ("四五千块", "四五千块"),
    ("一下子", "一下子"), ("六座布局", "六座布局"),
])
def test_arabicize(src, want):
    assert arabicize(src) == want


def test_cn_number_rejects_ambiguous():
    assert cn_number("三千五") == 3500
    assert cn_number("一千零五") == 1005
    assert cn_number("七八") is None


def test_lint_catches_chinese_numbers_without_refs():
    assert script_gen.has_numeric_claim("后驱版 CLTC 五百九十三公里")
    assert not script_gen.has_numeric_claim("重回第一,一台车两档模式")


def test_normalize_segments_arabicizes():
    segs = [ScriptSegment(idx=0, section="开场", text="月销五万,售价二十六万三千五")]
    out = script_gen.normalize_segments(segs, ["开场"])
    assert out[0].text == "月销5万,售价26.35万"


def test_enumeration_first_is_not_ad_absolute():
    from app.categories import load_profile

    profile = load_profile("auto_long")
    assert script_gen.forbidden_in("第一类人,预算卡在 26 万", profile) == []
    assert script_gen.forbidden_in("销量第一", profile) == ["第一"]


def test_bounded_context_gives_every_doc_a_share():
    from app.providers.facts import CollectionResult, RawDoc

    docs = [RawDoc(tool=f"t{i}", query="q", retrieved_at="d", text=c * n)
            for i, (c, n) in enumerate([("a", 30000), ("b", 20000), ("c", 200)])]
    ctx = CollectionResult(docs=docs).bounded_context(10000)
    assert ctx.count("c") >= 200                    # 小份全文保留
    assert 4800 <= ctx.count("a") <= 4900 and 4800 <= ctx.count("b") <= 4900
    assert CollectionResult(docs=docs).context.count("a") == 30000
