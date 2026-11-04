"""口语二改:只换说法,动了内容就退回原句;以及逗号、短句和 AI 腔 lint。"""
from __future__ import annotations

import json

import pytest

from app import config
from app.categories import load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import script_gen, timeline
from app.pipeline.script_gen.spoken_pass import rewrite_spoken


@pytest.fixture
def profile():
    return load_profile("auto_long")


def _fact() -> Fact:
    return Fact(id="f1", claim="410km版售价6.88万元,CLTC续航410km", scope="product", basis="CLTC",
                source_kind="official", source_tool="test", confidence="high")


def _seg(i: int, text: str, section: str = "论点一") -> ScriptSegment:
    return ScriptSegment(idx=i, section=section, text=text, fact_refs=["f1"])


class _Llm:
    def __init__(self, *texts: str, error: Exception | None = None) -> None:
        self.texts, self.error, self.calls = texts, error, 0

    def structured(self, system, user, required_keys=None, temperature=0.0):
        self.calls += 1
        if self.error:
            raise self.error
        return {"segments": [{"i": n, "text": t} for n, t in enumerate(self.texts)]}


OLD = "410km版售价6.88万元,CLTC标称410km,这笔钱花得比较值。"


def _run(profile, llm, *texts):
    segs = [_seg(i, t) for i, t in enumerate(texts)]
    return rewrite_spoken(segs, subject="星愿", profile=profile, facts=[_fact()], llm=llm)


def test_rewrite_keeps_refs_and_order(profile):
    new = "如果你看410km版,6.88万元,CLTC标称410km,这钱花得值。"
    out = _run(profile, _Llm("开场不改。", new), "开场不改。", OLD)
    assert out[1].text == new and out[1].fact_refs == ["f1"] and out[1].section == "论点一"


@pytest.mark.parametrize("bad", [
    "410km版售价6.98万元,CLTC标称410km,这笔钱花得比较值。",     # 数字改了
    "410km版售价6.88万元,标称410km,这笔钱花得比较值。",          # 口径丢了
    "410km版售价6.88万元,CLTC标称410km,有车主说这笔钱花得值。",  # 新加了转述来源
    "410km版售价6.88万元,CLTC标称410km,还带NOA,钱花得比较值。",  # 新加了技术名词
    "410km版售价6.88万元,CLTC标称410km,这笔钱我没能核实。",      # 交代缺数据
    "410km版6.88万元,CLTC标称410km",                              # 没说完
    "410km版6.88万元,CLTC标称410km。" + "这笔钱花得值得你好好想一想" * 3 + "。",  # 太长
])
def test_rewrite_returns_original_when_content_changed(profile, bad):
    out = _run(profile, _Llm("开场不改。", bad), "开场不改。", OLD)
    assert out[1].text == OLD


def test_rewrite_does_not_read_data_as_attribution(profile):
    new = "410km版售价6.88万元,CLTC标称410km,看数据这笔钱花得值。"
    out = _run(profile, _Llm("开场不改。", new), "开场不改。", OLD)
    assert out[1].text == new


TWO_FACTS = [_fact(), Fact(id="f2", claim="电池容量40.16kWh", scope="product", basis="官方",
                           source_kind="official", source_tool="test", confidence="high")]
PARAMS = "410km版售价6.88万元,CLTC标称410km,电池40.16kWh,这笔钱花得比较值。"


def _run_params(profile, new, **kw):
    segs = [_seg(0, "开场不改。"), _seg(1, PARAMS).model_copy(update={"fact_refs": ["f1", "f2"]})]
    return rewrite_spoken(segs, subject="星愿", profile=profile, facts=TWO_FACTS,
                          llm=_Llm("开场不改。", new), **kw)[1]


def test_rewrite_may_drop_minor_number_and_its_ref(profile):
    new = "410km版售价6.88万元,CLTC标称410km,电池也大了,这笔钱花得比较值。"
    got = _run_params(profile, new)
    assert got.text == new and got.fact_refs == ["f1"]


def test_rewrite_may_not_drop_most_numbers(profile):
    assert _run_params(profile, "这台车CLTC标称410km,电池也大了,钱花得比较值。").text == PARAMS


def test_rewrite_may_not_drop_thesis_number(profile):
    new = "410km版售价6.88万元,CLTC标称410km,电池也大了,这笔钱花得比较值。"
    assert _run_params(profile, new, thesis="40.16kWh才是这台车的分水岭").text == PARAMS


def test_rewrite_gives_crowded_segment_a_number_target(profile):
    seen = {}

    class Spy(_Llm):
        def structured(self, system, user, required_keys=None, temperature=0.0):
            seen["draft"] = json.loads(user)
            return super().structured(system, user, required_keys, temperature)

    crowded = "价格是" + "、".join(f"{n}元" for n in range(10, 20)) + "。"
    rewrite_spoken([_seg(0, "开场不改。"), _seg(1, crowded)], subject="星愿", profile=profile,
                   facts=[_fact()], llm=Spy("开场不改。", crowded))
    assert "max_numbers" not in seen["draft"][0] and seen["draft"][1]["max_numbers"] == 7


def test_rewrite_hedge_follows_the_number(profile):
    old = "这款多了约37%,电池40.16kWh,价格6.88万元,CLTC标称410km。"
    gone = "这款多了不少,电池40.16kWh,价格6.88万元,CLTC标称410km。"
    bare = "这款多了37%,电池40.16kWh,价格6.88万元,CLTC标称410km。"
    assert _run(profile, _Llm("开场不改。", gone), "开场不改。", old)[1].text == gone
    assert _run(profile, _Llm("开场不改。", bare), "开场不改。", old)[1].text == old


def test_rewrite_may_drop_rhetorical_someone(profile):
    old = "有人会说410km够了,410km版售价6.88万元,CLTC标称410km,这笔钱花得比较值。"
    new = "410km够不够看你,410km版售价6.88万元,CLTC标称410km,这笔钱花得比较值。"
    assert _run(profile, _Llm("开场不改。", new), "开场不改。", old)[1].text == new


def test_rewrite_keeps_first_sentence_of_film(profile):
    out = _run(profile, _Llm("开场改了。后面也改了。", OLD), "开场不改。后面的话。", OLD)
    assert out[0].text == "开场不改。后面的话。"


def test_rewrite_reverts_segment_that_adds_lint_error(profile):
    bad = "410km版售价6.88万元,CLTC标称410km,这钱花得第一值。"
    out = _run(profile, _Llm("开场不改。", bad), "开场不改。", OLD)
    assert out[1].text == OLD


def test_rewrite_falls_back_when_llm_fails(profile):
    out = _run(profile, _Llm(error=RuntimeError("boom")), "开场不改。", OLD)
    assert [s.text for s in out] == ["开场不改。", OLD]


def test_generate_runs_pass_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(script_gen, "rewrite_spoken", lambda segs, **kw: calls.append(1) or segs)

    class Llm:
        def structured(self, system, user, required_keys=None, temperature=0.0):
            return {"title": "标题", "thesis": "判断", "segments": [
                {"section": "开场", "text": "一句话。", "fact_refs": []}]}

    profile = load_profile("auto_long")
    kw = {"subject": "星愿", "profile": profile, "facts": [], "plan": ["开场"], "duration": 60,
          "llm": Llm()}
    script_gen.generate(**kw)
    assert not calls
    monkeypatch.setattr(config, "SPOKEN_PASS", True)
    script_gen.generate(**kw)
    assert calls == [1]


# --------------------------------------------------------------------------
# 逗号和短句
# --------------------------------------------------------------------------
def test_split_keeps_comma_and_subtitle_hides_it():
    parts = script_gen._split_text("全系标配700TOPS Thor芯片加激光雷达，HAD终身免费。", 22)
    assert parts == ["全系标配700TOPS Thor芯片加激光雷达，", "HAD终身免费。"]
    assert timeline.wrap_subtitle(parts[0], 40) == "全系标配700TOPS Thor芯片加激光雷达"
    assert timeline.gap_after(parts[0]) < timeline.gap_after(parts[1])


def test_clean_spoken_keeps_period_of_complete_short_sentence():
    segs = [_seg(0, "开场这一句话不能动。"), _seg(1, "这笔钱花得值不值得再想想。"), _seg(2, "未必。"),
            _seg(3, "结论这一句话也不能动。")]
    out = script_gen.clean_spoken(segs)
    assert out[1].text == "这笔钱花得值不值得再想想。未必。"


def test_clean_spoken_joins_half_sentence_with_comma():
    segs = [_seg(0, "开场这一句话不能动。"), _seg(1, "能确定的是。"), _seg(2, "它的价格是阶梯式的那种。"),
            _seg(3, "结论这一句话也不能动。")]
    out = script_gen.clean_spoken(segs)
    assert out[1].text == "能确定的是，它的价格是阶梯式的那种。"


# --------------------------------------------------------------------------
# 资料名和 AI 腔
# --------------------------------------------------------------------------
def test_clean_spoken_strips_source_labels():
    assert script_gen.strip_source_labels("专业数据集里，全系轴距都是2650mm。") == "全系轴距都是2650mm。"
    assert script_gen.strip_source_labels("老款资料里车长4135mm。") == "老款车长4135mm。"


def test_dangling_ignores_words_that_only_look_unfinished():
    assert not script_gen.ends_dangling("入门版你还得逐项核对。")
    assert script_gen.ends_dangling("现代的四驱版和。")


def test_self_exposure_covers_not_verified():
    assert script_gen.strip_self_exposure("这一条我没能逐版本核实。下单前看配置表。") == "下单前看配置表。"


def _messages(texts, section=None):
    segs = [_seg(i, t, section[i] if section else "论点一") for i, t in enumerate(texts)]
    return [(i.severity, i.message) for i in script_gen.spoken_lint(segs)]


def test_lint_source_label_is_error():
    got = _messages(["这段话说的是专业数据集里的东西。"])
    assert any(s == "error" and "专业数据集" in m for s, m in got)


def test_lint_bookish_word_is_warning():
    got = _messages(["这台车值得一提的地方在后排。"])
    assert ("warning", "书面腔「值得一提」,换成说话的说法") in got


def test_lint_ordinal_chapter_openings():
    got = _messages(["先说价格这件事情。", "再说续航这件事情。"], ["论点一", "论点二"])
    assert any("顺序词" in m for _, m in got)
    got = _messages(["价格这件事情很重要。", "再说续航这件事情。"], ["论点一", "论点二"])
    assert not any("顺序词" in m for _, m in got)


def test_lint_film_without_you_and_uniform_length():
    same = ["这台车的价格确实比较高。"] * 12
    got = _messages(same)
    assert any("「你」" in m for _, m in got) and any("句子长度" in m for _, m in got)
    varied = ["如果你常跑高速,这台车的续航就得按打折之后的数字来算才行。", "未必。"] * 6
    got = _messages(varied)
    assert not any("「你」" in m or "句子长度" in m for _, m in got)


def test_thesis_echo_does_not_need_exact_words():
    segs = [_seg(0, "多数人该买的,其实就是中间那台410km。", "车评结论")]
    assert not script_gen.thesis_lint(segs, "多数人该买中间那台410km")
    assert script_gen.thesis_lint(segs, "智驾才是这一代最大的卖点")
