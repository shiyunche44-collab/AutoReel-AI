"""专业车评片型:写稿前的分析、提示词口吻、新 lint、推算规则。"""
from __future__ import annotations

import pytest

from app.categories import Slot, load_profile
from app.models import Fact, ScriptSegment
from app.pipeline import derive, script_gen, variety


@pytest.fixture(scope="module")
def profile():
    return load_profile("auto_long")


def _fact(fid, claim, topic="", basis="", compare_with=""):
    return Fact(id=fid, claim=claim, source_tool="t", confidence="high", scope="product",
                retrieved_at="2026-01-01", topic=topic, basis=basis, compare_with=compare_with)


def _seg(idx, text, refs=(), section="论点一"):
    return ScriptSegment(idx=idx, section=section, text=text, fact_refs=list(refs))


def _msgs(issues):
    return [i.message for i in issues]


class _LLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error
        self.calls = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        if self.error:
            raise self.error
        return self.reply


FACTS = [_fact("f_1", "零百加速 3.9 秒"), _fact("f_2", "续航 700 公里", basis="CLTC"),
         _fact("f_3", "对手售价 30 万", topic="对比", compare_with="对手"),
         _fact("f_4", "有媒体实测冬季续航打折", topic="体验")]


def _reply(n=4):
    args = [{"claim": f"判断{i}", "heading": "一个特别特别特别特别长的章名" + str(i),
             "fact_refs": ["f_1", "f_3", "不存在"], "reasoning": "推理", "counter": "反方",
             "move": "算账"} for i in range(n)]
    return {"tension": "快和省在打架", "maker_bet": "赌家用", "thesis": "这台车赌错了人",
            "arguments": args, "weakness": {"claim": "短", "heading": "短板", "fact_refs": ["f_2"]},
            "rival": {"claim": "比", "heading": "对手", "fact_refs": ["f_3", "f_1"]}}


# ---- 分析 ----------------------------------------------------------------
def test_brief_keeps_only_known_refs_and_short_headings(profile):
    brief = script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=_LLM(_reply()))
    point = brief["arguments"][0]
    assert point["fact_refs"] == ["f_1"]  # 不存在的、对比事实都去掉
    assert len(point["heading"]) <= script_gen.HEADING_MAX_CHARS
    assert brief["rival"]["fact_refs"] == ["f_3"]
    assert brief["thesis"] == "这台车赌错了人"


@pytest.mark.parametrize("llm", [_LLM(error=RuntimeError("boom")), _LLM(_reply(1)),
                                 _LLM({"tension": "", "thesis": "", "arguments": []})])
def test_brief_degrades_to_none(profile, llm):
    assert script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=llm) is None


def test_brief_prompt_carries_lens_why_and_history(profile):
    llm = _LLM(_reply())
    script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=llm, lens="rivalry",
                           why="刚降价", history=[{"thesis": "老论点", "headings": ["老章名"]}])
    system = llm.calls[0][0]
    assert profile.vocab.lenses["rivalry"] in system
    assert "刚降价" in system and "老论点" in system and "老章名" in system


def test_align_slots_folds_extra_argument_sections(profile):
    slots = [Slot("车评开场"), Slot("论点一"), Slot("论点一"), Slot("论点二"), Slot("论点三"),
             Slot("短板")]
    got = script_gen.align_slots(slots, {"arguments": [{}, {}]}, profile)
    assert [s.section for s in got] == ["车评开场", "论点一", "论点一", "论点二", "论点二", "短板"]
    assert script_gen.align_slots(slots, None, profile) == slots


def test_outline_maps_arguments_in_plan_order(profile):
    brief = script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=_LLM(_reply()))
    plan = ["车评开场", "论点三", "论点一", "论点一", "短板", "车评结论"]
    block = script_gen.outline_block(brief, plan, profile)
    assert block.index("- 论点三:") < block.index("- 论点一:")
    assert "判断0" in block[block.index("- 论点三:"):block.index("- 论点一:")]
    assert "主论点(thesis 字段原样用这句):这台车赌错了人" in block
    assert "每段要做的事" in block


# ---- 提示词 ----------------------------------------------------------------
class _Capture:
    def __init__(self):
        self.calls = []

    def structured(self, system, user, required_keys=(), **_):
        self.calls.append((system, user))
        return {k: [] if k != "title" else "t" for k in required_keys}


def _render(profile, theme_name, **kw):
    llm = _Capture()
    theme = profile.theme_spec(theme_name)
    duration = profile.supported_durations()[0]
    plan = list(profile.plan_for(duration, theme_name))
    script_gen.generate(subject="某车", profile=profile, facts=FACTS, plan=plan, duration=duration,
                        llm=llm, theme=theme, **kw)
    return llm.calls[-1]


def test_review_prompt_uses_reviewer_rules(profile):
    system, user = _render(profile, "review", brief=script_gen.write_brief(
        subject="某车", profile=profile, facts=FACTS, llm=_LLM(_reply())))
    assert "三种话分开说" in system and "严禁假装亲身使用" in system
    assert "本片论证大纲" in system and "这台车赌错了人" in system
    assert '每个数字后面跟一句"所以呢"' not in system
    assert "体验转述" in system
    assert "{chars}" not in system + user and "{per_shot}" not in system + user
    assert "必需关键词" not in system


def test_buyguide_prompt_keeps_old_rules(profile):
    system, _ = _render(profile, "buyguide")
    assert '每个数字后面跟一句"所以呢"' in system
    assert "三种话分开说" not in system and "本片论证大纲" not in system
    assert "必需关键词" in system


def test_hook_prompt_mentions_tension_only_when_given(profile):
    with_tension = _Capture()
    script_gen.choose_hook(subject="某车", facts=FACTS, profile=profile, llm=with_tension,
                           tension="快和省在打架")
    plain = _Capture()
    script_gen.choose_hook(subject="某车", facts=FACTS, profile=profile, llm=plain)
    assert "快和省在打架" in with_tension.calls[0][0] + with_tension.calls[0][1]
    assert "核心矛盾" not in plain.calls[0][0] + plain.calls[0][1]


# ---- 片型 ----------------------------------------------------------------
def test_review_is_default_and_buyguide_remains(profile):
    assert profile.theme_spec().name == "review"
    assert profile.theme_spec("buyguide").name == "buyguide"


def test_short_review_has_fewer_chapters_and_keeps_the_ending(profile):
    short, long = (list(dict.fromkeys(profile.plan_for(d, "review"))) for d in (120, 240))
    assert len(short) == 7 and len(long) == 9
    assert short[0] == "车评开场" and short[-1] == "车评结论" and "短板" in short


def test_prompt_example_does_not_name_a_body_part(profile):
    assert "老板" not in script_gen.BRIEF_SYSTEM and "老板" not in profile.vocab.judgment_example


def test_lens_only_for_themes_that_declare_it(profile):
    review = variety.pick(profile.theme_spec("review"), "p_1")
    assert review["lens"] in profile.theme_spec("review").lenses
    assert "lens" not in variety.pick(profile.theme_spec("buyguide"), "p_1")


def test_lens_avoids_recent(profile):
    theme = profile.theme_spec("review")
    recent = [{"lens": x} for x in theme.lenses[:-1]]
    assert variety.pick(theme, "p_1", recent)["lens"] == theme.lenses[-1]


# ---- lint ----------------------------------------------------------------
@pytest.mark.parametrize("text", ["我试驾了一圈", "亲测好开", "我开了三天"])
def test_firsthand_lint_is_error(profile, text):
    issues = script_gen.firsthand_lint([_seg(0, text)], profile)
    assert [i.severity for i in issues] == ["error"]


@pytest.mark.parametrize("text", ["有媒体试驾后说底盘偏软", "我判断它是给家用准备的",
                                  "有车主说只有四百公里,这不是我亲测"])
def test_firsthand_lint_allows_reported_and_inferred(profile, text):
    assert script_gen.firsthand_lint([_seg(0, text)], profile) == []


def test_attribution_lint(profile):
    bad = [_seg(0, "冬季续航会打折", ["f_4"])]
    good = [_seg(0, "有媒体实测,冬季续航会打折", ["f_4"])]
    assert [i.severity for i in script_gen.attribution_lint(bad, profile, FACTS)] == ["warning"]
    assert script_gen.attribution_lint(good, profile, FACTS) == []
    # 同一段拆成几拍,来源说一次就够
    split = [_seg(0, "有媒体实测", ["f_4"]), _seg(1, "冬季续航会打折", ["f_4"])]
    assert script_gen.attribution_lint(split, profile, FACTS) == []


def test_number_source_lint_flags_numbers_not_in_cited_facts():
    issues = script_gen.number_source_lint([_seg(0, "每天通勤 50 公里", ["f_2"])], FACTS)
    assert len(issues) == 1 and "50" in issues[0].message


@pytest.mark.parametrize("text,refs", [
    ("续航 700 公里", ["f_2"]),            # 原文
    ("差了 400 公里", ["f_5"]),            # 差值
    ("多了 57% 的续航", ["f_5"]),          # 百分比
    ("差不多是 0.70 千公里", ["f_2"]),     # 换算
    ("3 个版本", ["f_2"]),                 # 个位数计数不查
    ("2026 款 M9 很贵", ["f_2"]),          # 年份和型号不算
    ("没有引用的 88 公里", []),            # 没引用归规则 1 管
])
def test_number_source_lint_accepts_derivable(text, refs):
    facts = [*FACTS, _fact("f_5", "长续航 700 公里,标准 300 公里")]
    assert script_gen.number_source_lint([_seg(0, text, refs)], facts) == []


def test_stock_phrase_lint(profile):
    phrase = profile.vocab.stock_phrases[0]
    assert script_gen.stock_phrase_lint([_seg(0, phrase)], profile) == []
    assert len(script_gen.stock_phrase_lint([_seg(0, phrase), _seg(1, phrase)], profile)) == 1


def test_scaffold_word_flagged_once_is_enough(profile):
    word = profile.vocab.scaffold_words[0]
    assert len(script_gen.stock_phrase_lint([_seg(0, word)], profile)) == 1


def test_meaning_lint_skipped_for_reviewer(profile):
    segs = [_seg(i, f"电池容量是 {60 + i} 度", ["f_1"], section="论点一") for i in range(6)]
    def has_meaning(theme):
        return any("所以呢" in m or "念参数" in m for m in _msgs(
            script_gen.compliance_lint(segs, profile, FACTS, theme=profile.theme_spec(theme))))
    assert has_meaning("buyguide") and not has_meaning("review")


# ---- 推算 ----------------------------------------------------------------
@pytest.mark.parametrize("rule,claim,want", [
    ("wheelbase_ratio", "车长 5000 毫米,轴距 3000 毫米", "60"),
    ("charge_c_rate", "电池 100 kWh,快充峰值功率 500 kW", "5"),
    ("trim_gap", "两个版本售价 30 万和 35 万", "5"),
])
def test_new_derive_rules(rule, claim, want):
    got = derive.derive([_fact("f_1", claim)], [rule])
    assert len(got) == 1 and want in got[0].claim


@pytest.mark.parametrize("rule,claim", [
    ("wheelbase_ratio", "轴距 3000 毫米"),
    ("charge_c_rate", "电池 100 kWh,电机功率 500 kW"),
    ("trim_gap", "三个版本售价 30 万、35 万、40 万"),
])
def test_new_derive_rules_need_both_numbers(rule, claim):
    assert derive.derive([_fact("f_1", claim)], [rule]) == []


def test_compare_section_dropped_without_compare_facts(profile):
    plan = list(profile.plan_for(240, "review"))
    assert "竞品对照" in plan
    assert "竞品对照" not in profile.drop_unbacked(plan, [])
    assert "竞品对照" in profile.drop_unbacked(plan, ["对比"])


def test_first_sentence_stops_at_fullwidth_marks():
    assert script_gen.first_sentence("它值吗\uff1f值。") == "它值吗"
    assert script_gen.first_sentence("真贵\uff01但值。") == "真贵"


def test_brief_forbids_arguing_from_missing_facts():
    assert "清单里没写到不等于没有" in script_gen.BRIEF_SYSTEM


def test_review_never_opens_with_an_invented_scene(profile):
    assert "scene" not in profile.theme_spec("review").openings


def test_split_packs_clauses_instead_of_cutting_at_every_comma():
    sent = "若完整配置表有Ultra专属性能、内饰或座椅，你又正好需要，这6万元就不算盲花。"
    parts = script_gen._split_text(sent, 28)
    assert len(parts) == 2 and all(len(p) <= 28 for p in parts)
    assert "你又正好需要" in parts[0] or "你又正好需要" in parts[1]


def test_split_avoids_cutting_at_a_list_mark_when_a_comma_fits():
    parts = script_gen._split_text("这版把音响、座椅和电池规格都做了分级，顶配才给满，低配只留下基础的那一套配置。", 28)
    assert "音响、座椅和电池规格都做了分级" in parts[0]


def test_split_keeps_hook_first_sentence_short():
    seg = _seg(0, "它贵得有道理，可是这笔钱大多数家庭根本用不上，也不该为它买单。")
    first = script_gen.split_segments([seg])[0].text
    assert len(first) <= script_gen.HOOK_MAX_CHARS


def test_review_keeps_reasoning_sentence_whole_up_to_its_limit(profile):
    keep = profile.theme_spec("review").sentence_keep_chars
    assert keep == 36 and profile.theme_spec("buyguide").sentence_keep_chars == 0
    sent = "加长4座若对准高频商务接待，就有独立客群，不只是价格锚点。"
    assert 28 < len(sent) <= keep
    assert script_gen._split_text(sent, 28, keep_chars=keep) == [sent]
    assert len(script_gen._split_text(sent, 28)) == 2
    long = "这台车的版本策略很清楚，入门款负责走量，中配负责抬价，顶配负责撑起品牌的价格天花板。"
    assert len(long) > keep and len(script_gen._split_text(long, 28, keep_chars=keep)) > 1


def test_keep_chars_does_not_lengthen_the_hook_sentence():
    seg = _seg(0, "它贵得有道理，可是这笔钱大多数家庭根本用不上，也不该为它买单。")
    first = script_gen.split_segments([seg], keep_chars=36)[0].text
    assert len(first) <= script_gen.HOOK_MAX_CHARS


def test_split_treats_fullwidth_question_as_sentence_end():
    q = chr(0xff1f)
    parts = script_gen._split_text(f"宋Ultra选EV，还是DM-i{q}它把800V给EV，又用DM-i接家用，这是补能题。", 28, 18)
    assert parts[0] == f"宋Ultra选EV，还是DM-i{q}"


def test_brief_skips_llm_when_no_usable_facts(profile):
    def llm(*a, **k):
        raise AssertionError("没有事实不该调用 LLM")
    assert script_gen.write_brief(subject="某车", profile=profile, facts=[], llm=llm) is None


# ---- 第二阶段:问答 / 槽点 / 深挖 / 对决 ----------------------------------------
REVIEWER_THEMES = ("deepdive", "qa", "pk", "debate")


@pytest.mark.parametrize("name", REVIEWER_THEMES)
def test_theme_uses_reviewer_voice(profile, name):
    theme = profile.theme_spec(name)
    assert theme.reviewer and theme.allow_subject_critique and theme.sentence_keep_chars
    assert theme.brief_rule and set(theme.lenses) <= set(profile.vocab.lenses)
    assert "ifind.review_news" in theme.fact_sources
    rules = "\n".join(theme.script_rules)
    assert "所以呢" not in rules and "不许自己推测" not in rules
    # scene 要编用车画面,conflict 要念「每个数字都能查到出处」,action 又把片子收在评论区
    assert not {"scene", "conflict"} & set(theme.openings) and "action" not in theme.endings
    assert "你是" + profile.vocab.reviewer_voice[:6] in _render(profile, name)[0]


def test_buyguide_stays_as_fallback(profile):
    theme = profile.theme_spec("buyguide")
    assert not theme.reviewer and not theme.brief_rule
    system = _render(profile, "buyguide")[0]
    assert "三种话分开说" not in system and "门禁会直接拦下" not in system


def test_pk_still_forbids_disparaging(profile):
    assert "不贬低" in "\n".join(profile.theme_spec("pk").script_rules)


def test_prompt_lists_gate_terms(profile):
    system = _render(profile, "review")[0]
    for term in ("最强", "第一", "顶级", "天花板", "拉满"):
        assert f"「{term}」" in system


def test_brief_prompt_lists_gate_terms(profile):
    llm = _LLM(_reply())
    script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=llm)
    assert "「天花板」" in llm.calls[0][0]


@pytest.mark.parametrize(("name", "expected"), [
    ("qa", ["问题一", "问题二", "问题三", "问题四", "问题五"]),
    ("debate", ["争议一", "争议二", "争议三"]),
    ("deepdive", ["为什么", "关键数字"]),
    ("pk", ["对决价格", "对决空间", "对决三电", "对决智能"]),
])
def test_argument_slots_per_theme(profile, name, expected):
    plan = [b["section"] for b in profile.theme_spec(name).blocks]
    assert script_gen.argument_slots(plan, profile) == expected


def test_brief_prompt_carries_theme_rule_and_sections(profile):
    llm = _LLM(_reply(2))
    script_gen.write_brief(subject="A 对 B", profile=profile, facts=FACTS, llm=llm, n_args=2,
                           rule="每条判一个维度", sections=["对决价格", "对决空间"])
    system = llm.calls[0][0]
    assert "本片型的要求:每条判一个维度" in system
    assert "- 对决价格:" + profile.section("对决价格").beat in system
    llm = _LLM(_reply())
    script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=llm)
    assert "本片型的要求" not in llm.calls[0][0] and "依次对应" not in llm.calls[0][0]


def test_brief_facts_show_subject_and_controversy(profile):
    llm = _LLM(_reply())
    facts = [_fact("f_1", "续航 700 公里"), _fact("f_2", "有车主投诉异响", topic="争议")]
    facts[0].subject = "甲车"
    script_gen.write_brief(subject="某车", profile=profile, facts=facts, llm=llm)
    system = llm.calls[0][0]
    assert "f_1 [甲车]" in system and "f_2 [争议]" in system


def test_outline_skips_heading_for_plain_sections(profile):
    brief = script_gen.write_brief(subject="某车", profile=profile, facts=FACTS, llm=_LLM(_reply(2)),
                                   n_args=2)
    outline = script_gen.outline_block(brief, ["为什么", "关键数字", "代价边界"], profile)
    assert "- 为什么:判断:判断0" in outline and "- 关键数字:判断:判断1" in outline
    assert "- 代价边界:判断:短" in outline  # 短板落到深挖的「代价边界」段
    assert "章名「" not in outline
    outline = script_gen.outline_block(brief, ["问题一", "问题二"], profile)
    assert "- 问题一:章名「" in outline


def test_debate_merges_slots_when_controversies_run_out(profile):
    brief = {"arguments": [{"claim": "a"}, {"claim": "b"}]}
    slots = [Slot(s) for s in ("争议开场", "争议一", "争议二", "争议三", "争议结论")]
    out = script_gen.align_slots(slots, brief, profile)
    assert [s.section for s in out] == ["争议开场", "争议一", "争议二", "争议二", "争议结论"]


def test_prompt_keeps_prompt_words_out_of_narration(profile):
    system = _render(profile, "pk")[0]
    assert "「清单里没有」" in system and "同一个数字全片只念一次" in system
    assert "清单里" in profile.vocab.scaffold_words


@pytest.mark.parametrize("text", ["有人会说,硬件多不等于好用。", "也有人说,这钱花得冤。",
                                  "不少车主说,冬天续航打折。", "高速、低温会低于标称,这很正常。"])
def test_counterpoints_are_not_vendor_claims(text):
    assert script_gen.vendor_attribution(text) == ""


def test_vendor_quote_is_still_caught():
    assert script_gen.vendor_attribution("卢伟冰说,多数消费者选了它。")
