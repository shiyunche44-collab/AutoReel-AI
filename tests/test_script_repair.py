"""写稿自修:数字补引用 / 改写 / 删句,换说法的重复去掉;以及几处源头收紧。"""
from __future__ import annotations

import importlib

from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import host, script_gen

# 包里导出了同名函数 `repair`,模块本身要这样取
repair = importlib.import_module("app.pipeline.script_gen.repair")


def _facts():
    return [
        Fact(id="f_price", claim="银河E5 起售价 10.98 万元", source_tool="t", scope="product"),
        Fact(id="f_range", claim="CLTC 续航 530 公里", source_tool="t", scope="product"),
    ]


def _seg(idx, text, refs=(), section="核心卖点"):
    return ScriptSegment(idx=idx, section=section, text=text, fact_refs=list(refs))


def test_missing_ref_is_matched_by_number():
    profile = load_profile("auto_long")
    segs = [_seg(0, "这台车到底值不值?"), _seg(1, "它的续航有530公里,通勤一周一充。"),
            _seg(2, "想清楚再下单。")]
    out, notes = repair.repair_grounding(segs, _facts(), profile)
    assert out[1].fact_refs == ["f_range"]
    assert notes and "补上事实引用" in notes[0]


def test_unknown_ref_is_removed_and_ungrounded_sentence_dropped():
    profile = load_profile("auto_long")
    segs = [_seg(0, "这台车到底值不值?"),
            _seg(1, "空间表现不错。零百加速只要4.9秒,很快。", refs=["f_ghost"]),
            _seg(2, "想清楚再下单。")]
    out, notes = repair.repair_grounding(segs, _facts(), profile)
    assert out[1].fact_refs == []
    assert "4.9" not in out[1].text and "空间表现不错" in out[1].text
    assert any("不存在的事实引用" in n for n in notes)
    # 修完之后这两类 error 不该再出现
    issues = script_gen.compliance_lint(out, profile, _facts())
    assert not [i for i in issues if i.is_error
                and ("没有任何事实引用" in i.message or "不存在的事实" in i.message)]


def test_llm_rewrite_must_be_grounded():
    profile = load_profile("auto_long")

    class LLM:
        def __init__(self, reply):
            self.reply, self.calls = reply, 0

        def structured(self, system, user, **kw):
            self.calls += 1
            return self.reply

    segs = [_seg(0, "开场。"), _seg(1, "百公里电耗才12.8度,一公里几分钱。"), _seg(2, "结尾。")]
    good = LLM({"text": "续航做到530公里,一周一充够用。", "fact_refs": ["f_range"]})
    out, _ = repair.repair_grounding(segs, _facts(), profile, llm=good)
    assert out[1].fact_refs == ["f_range"] and "530" in out[1].text
    # 改写稿里的数字还是没出处:不收,退回删句
    bad = LLM({"text": "百公里电耗13.5度。", "fact_refs": ["f_range"]})
    out, _ = repair.repair_grounding(segs, _facts(), profile, llm=bad)
    assert bad.calls == repair.REPAIR_PASSES
    assert len(out) == 2 or "13.5" not in out[1].text


def test_near_repeat_dropped_but_parallel_spec_rows_kept():
    segs = [_seg(0, "这台车最大的问题是后排坐垫太短,长途腿没有支撑。"),
            _seg(1, "550 Plus 版卖 12.38 万,配置够用。"),
            _seg(2, "650 Plus 版卖 13.38 万,配置够用。"),
            _seg(3, "说到底,这台车最大的问题就是后排坐垫太短,长途腿没支撑。预算够就上高配。",
                 section="结论")]
    out, notes = repair.drop_near_repeats(segs)
    assert len(out) == 4  # 并列的配置行数字不同,不算重复
    assert "坐垫" not in out[3].text and "预算够就上高配" in out[3].text
    assert notes
    lint = script_gen.near_repeat_lint(segs)
    assert lint and lint[0].segment_idx == 3


def test_portrait_hides_host_on_highlight():
    fps = 30
    land = host.hide_spans(["broll", "highlight", "broll"], [1.0, 1.0, 1.0], fps, 3 * fps)
    port = host.hide_spans(["broll", "highlight", "broll"], [1.0, 1.0, 1.0], fps, 3 * fps,
                           portrait=True)
    assert not land.any() and port[fps:2 * fps].all()


def test_still_with_text_overlay_is_not_usable():
    from app.pipeline.library.model import Item

    base = {"id": "i1", "path": "x.jpg", "tagged": True, "view": "exterior", "target": "yes",
            "width": 1920, "height": 1080}
    still = Item(kind="still", problem="text_overlay", **base)
    clean = Item(kind="still", problem="", **base)
    assert not still.usable and clean.usable
