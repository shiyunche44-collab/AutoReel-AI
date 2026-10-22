"""编辑层断言:开场黄金三秒、空洞评价、竞品段的厂商口径、重复检测、
大字与旁白复读、脚本改动同步进分镜、门禁 A 阻断。

这些是"成片好不好看"的机械判据,不需要网络与额度。
"""
from __future__ import annotations

import pytest

from app.categories import load_profile
from app.models import Fact, ScriptSegment, ShotSpec
from app.pipeline import explainer, script_gen
from app.pipeline.script_gen import (compliance_lint, depth_lint, duplicate_lint,
                                     hook_lint, vendor_attribution)


@pytest.fixture(scope="module")
def auto():
    return load_profile("auto_long")


@pytest.fixture(scope="module")
def theme(auto):
    return auto.theme_spec("buyguide")


def _f(fid, claim, **kw):
    return Fact(id=fid, claim=claim, source_tool="t", confidence=kw.pop("confidence", "high"),
                scope=kw.pop("scope", "product"), **kw)


def _seg(idx, section, text, refs=None):
    return ScriptSegment(idx=idx, section=section, text=text, fact_refs=list(refs or []))


# --------------------------------------------------------------------------
# 开场:黄金三秒
# --------------------------------------------------------------------------
def test_hook_filler_is_error_not_warning():
    """实测成片开场是「朋友们好,今天聊小米YU7」—— 套话必须拦下。"""
    segs = [_seg(0, "开场", "朋友们好,今天聊小米YU7。这台车值不值得买。")]
    issues = hook_lint(segs)
    assert issues and all(i.is_error for i in issues)
    assert any("套话" in i.message for i in issues)


@pytest.mark.parametrize("first", ["大家好", "今天来聊聊", "各位好", "欢迎收看", "这期视频"])
def test_hook_filler_variants_caught(first):
    segs = [_seg(0, "开场", f"{first},这台车有点东西。")]
    assert any(i.is_error for i in hook_lint(segs))


def test_hook_first_sentence_over_limit_is_error():
    segs = [_seg(0, "开场", "这台车的定位其实特别清楚就是中大型纯电SUV里的那一类选手。")]
    issues = hook_lint(segs)
    assert issues and all(i.is_error for i in issues)
    assert any("18" in i.message for i in issues)


def test_hook_good_opening_passes():
    segs = [_seg(0, "开场", "23.35万起,还全系标配激光雷达。")]
    assert hook_lint(segs) == []


def test_filler_term_is_error():
    """"很猛"这类空洞评价以前只是 warning,模型照写不误。"""
    facts = [_f("f_1", "零百加速2.92秒")]
    segs = [_seg(0, "三电性能", "零百加速2.92秒,这个数据很猛。", ["f_1"])]
    issues = depth_lint(segs, load_profile("auto_long"), facts)
    assert any(i.is_error and "很猛" in i.message for i in issues)


# --------------------------------------------------------------------------
# 竞品段:厂商口径不算论据
# --------------------------------------------------------------------------
def test_vendor_quote_in_comparison_section_is_error(auto, theme):
    """实测:「卢伟冰说,对比试驾后多数消费者选了YU7」被当横评论据。"""
    facts = [_f("f_1", "小米YU7标准版售价23.35万元")]
    segs = [
        _seg(0, "开场", "23.35万起,这价格有点东西。", ["f_1"]),
        _seg(1, "竞品对比",
             "卢伟冰说,对比试驾标准版和特斯拉Model Y后驱版后,多数消费者选了YU7。", ["f_1"]),
    ]
    issues = compliance_lint(segs, auto, facts, theme=theme)
    assert any(i.is_error and "厂商口径" in i.message for i in issues)


def test_vendor_quote_outside_comparison_section_not_flagged(auto, theme):
    """非对比章节里提到"官方称"是正常的引述,不拦。"""
    facts = [_f("f_1", "小米YU7标准版CLTC续航643公里")]
    segs = [_seg(0, "三电性能", "官方称CLTC续航643公里。", ["f_1"])]
    issues = compliance_lint(segs, auto, facts, theme=theme)
    assert not any("厂商口径" in i.message for i in issues)


def test_vendor_attribution_detects_person_quote_but_not_colloquial():
    assert vendor_attribution("卢伟冰说,多数消费者选了YU7")
    assert vendor_attribution("官方表示全系标配")
    assert vendor_attribution("调研显示大多数用户更认可这台")
    # "说白了"不是引述
    assert vendor_attribution("这类车说白了就是代步工具") == ""


# --------------------------------------------------------------------------
# 重复检测
# --------------------------------------------------------------------------
def test_same_fact_across_sections_is_warned():
    facts = [_f("f_1", "标准版售价23.35万元")]
    segs = [
        _seg(0, "定位与价格", "入门23.35万。", ["f_1"]),
        _seg(1, "外观设计", "外观走圆润路线。"),
        _seg(2, "版本选择", "标准版还是23.35万。", ["f_1"]),
    ]
    issues = duplicate_lint(segs, facts)
    assert any("多个章节引用" in i.message for i in issues)


def test_same_fact_within_one_section_not_warned():
    """同一章节里连续几段引用同一条事实是正常的(那段参数就出自它)。"""
    facts = [_f("f_1", "全系标配激光雷达")]
    segs = [
        _seg(0, "智能配置", "激光雷达是全系标配。", ["f_1"]),
        _seg(1, "智能配置", "还有Thor芯片。", ["f_1"]),
    ]
    assert not any("多个章节引用" in i.message for i in duplicate_lint(segs, facts))


def test_number_repeated_three_times_is_warned():
    facts = [_f("f_1", "标准版售价23.35万元")]
    segs = [
        _seg(0, "定位与价格", "23.35万起。", ["f_1"]),
        _seg(1, "版本选择", "标准版23.35万。", ["f_1"]),
        _seg(2, "结论建议", "入门23.35万这个价。", ["f_1"]),
    ]
    assert any("反复出现" in i.message for i in duplicate_lint(segs, facts))


def test_single_digit_and_year_not_counted_as_duplicate():
    """3 块屏、3 米轴距、2026 款到处出现,不算重复。"""
    facts = [_f("f_1", "天际屏3块Mini LED屏")]
    segs = [
        _seg(0, "内饰空间", "3块屏。", ["f_1"]),
        _seg(1, "外观设计", "轴距3米。", ["f_1"]),
        _seg(2, "版本选择", "2026款。", ["f_1"]),
    ]
    assert not any("反复出现" in i.message for i in duplicate_lint(segs, facts))


# --------------------------------------------------------------------------
# 大字与旁白复读
# --------------------------------------------------------------------------
def test_echoes_narration_catches_verbatim_clause():
    """实测截图 #5:大字和字幕逐字重复,大字既没加信息又挡住画面。"""
    narr = "25扬声器全系标配,家里有娃的还能用晕车舒缓模式,晕车发生率降低51%。"
    assert explainer.echoes_narration("25扬声器全系标配", narr)
    assert explainer.echoes_narration("晕车发生率降低51%", narr)


def test_echoes_narration_keeps_condensed_takeaway():
    """改写过的判断句不算复读 —— 那是大字该干的事。"""
    narr = "我的判断是:家用选标准版足够,追求加速快感再考虑GT。"
    assert not explainer.echoes_narration("追求快感选GT", narr)


def test_echoes_narration_catches_verbatim_fragment():
    """大字是旁白里的一段(≥6 字)也算复读 —— 同一句话在画面上出现两次。"""
    narr = "我的判断是:家用选标准版足够,追求加速快感再考虑GT。"
    assert explainer.echoes_narration("家用选标准版", narr)
    assert explainer.echoes_narration("全系标配激光雷达", "这台车全系标配激光雷达,入门版就有。")


def test_echoes_narration_keeps_short_keyword():
    assert not explainer.echoes_narration("全系标配", "激光雷达全系标配,入门版就有。")


def test_ground_keywords_drops_echo_lines():
    facts = [_f("f_1", "25扬声器全系标配,晕车发生率降低51%")]
    narr = "25扬声器全系标配,家里有娃的还能用晕车舒缓模式,晕车发生率降低51%。"
    asked = ["25扬声器全系标配", "晕车发生率降低51%"]
    assert explainer.ground_keywords(asked, narr, facts) == asked
    assert explainer.ground_keywords(asked, narr, facts, drop_echo=True) == []


def test_hook_layout_keeps_echo():
    """冷开场刻意把钩子放大,不算复读。"""
    facts = [_f("f_1", "23.35万元")]
    narr = "23.35万起,还全系标配激光雷达。"
    asked = ["23.35万起"]
    assert explainer.ground_keywords(asked, narr, facts, drop_echo=False) == asked


# --------------------------------------------------------------------------
# 脚本改动要同步进分镜
# --------------------------------------------------------------------------
def _spec(idx, narration, layout="broll", overlay=None):
    return ShotSpec(idx=idx, duration=5.0, section="开场", visual_zh=f"[{layout}] 宣传画面",
                    narration_zh=narration, layout=layout, overlay=overlay)


def test_sync_narration_updates_storyboard():
    """门禁 A 改的稿必须进成片:TTS 读的是分镜里的 narration_zh。"""
    specs = [_spec(0, "朋友们好,今天聊小米YU7。")]
    segs = [_seg(0, "开场", "23.35万起,这价格不像电车。")]
    out = explainer.sync_narration(specs, segs, [])
    assert out[0].narration_zh == "23.35万起,这价格不像电车。"


def test_sync_narration_regrounds_highlight():
    """大字是照抄旁白时,旁白一改就该重新核对。"""
    facts = [_f("f_1", "全系标配激光雷达")]
    specs = [_spec(0, "旧旁白", layout="highlight",
                   overlay={"kind": "highlight", "lines": ["全系标配"]})]
    segs = [_seg(0, "开场", "全系标配激光雷达,入门版就有。")]
    out = explainer.sync_narration(specs, segs, facts)
    assert out[0].overlay["lines"] == ["全系标配"]


def test_sync_narration_drops_dead_highlight_to_broll():
    """大字里的数字既不在新旁白、也没有事实兜底(那条事实被取消勾选)→ 退回 broll,
    别在画面上留一个没有出处的数字。"""
    specs = [_spec(0, "零百加速2.92秒,很带劲。", layout="highlight",
                   overlay={"kind": "highlight", "lines": ["2.92秒"]})]
    segs = [_seg(0, "开场", "这台车动力确实不错。")]
    out = explainer.sync_narration(specs, segs, [])
    assert out[0].layout == "broll"
    assert out[0].overlay is None


def test_sync_narration_keeps_highlight_backed_by_fact():
    """数字有事实兜底就留着 —— 叠加层本来就是信息层,不必跟着旁白走。"""
    facts = [_f("f_1", "零百加速2.92秒")]
    specs = [_spec(0, "零百加速2.92秒。", layout="highlight",
                   overlay={"kind": "highlight", "lines": ["2.92秒"]})]
    segs = [_seg(0, "开场", "动力是它的强项。")]
    out = explainer.sync_narration(specs, segs, facts)
    assert out[0].layout == "highlight"
    assert out[0].overlay["lines"] == ["2.92秒"]


def test_sync_narration_drops_highlight_that_echoes_new_narration():
    """改稿后大字变成旁白的复读 → 丢掉,退回 broll。"""
    facts = [_f("f_1", "全系标配激光雷达")]
    specs = [_spec(0, "旧旁白。", layout="highlight",
                   overlay={"kind": "highlight", "lines": ["全系标配激光雷达"]})]
    segs = [_seg(0, "开场", "这台车全系标配激光雷达,入门版就有。")]
    out = explainer.sync_narration(specs, segs, facts)
    assert out[0].layout == "broll"
    assert out[0].overlay is None


def test_sync_narration_rejects_count_mismatch():
    with pytest.raises(ValueError):
        explainer.sync_narration([_spec(0, "a")], [_seg(0, "开场", "a"), _seg(1, "开场", "b")], [])


# --------------------------------------------------------------------------
# 门禁 A:lint 有 error 就不放行
# --------------------------------------------------------------------------
def _gate_project(state="GATE_A"):
    return {"id": "p_test", "state": state, "subject": "测试", "category": "auto_long",
            "theme": "buyguide", "duration": 60, "narration_mode": "tts",
            "shot_count": 1, "quota_budget": 40.0, "spent_cost": 0.0}


def _patch_gate(monkeypatch, blockers):
    from app.pipeline import orchestrator

    monkeypatch.setattr(orchestrator.db, "get_project", lambda *a, **k: _gate_project())
    monkeypatch.setattr(orchestrator, "_gate_a_blockers",
                        lambda pid, profile, theme: blockers)
    monkeypatch.setattr(orchestrator, "_script",
                        lambda pid: [_seg(0, "开场", "23.35万起,这价格不像电车。")])
    monkeypatch.setattr(orchestrator, "_sync_storyboard",
                        lambda pid, segments: [_spec(0, segments[0].text)])
    # 门禁 A 放行时要合成旁白,那一步会先**预留配额额度**。这里测的是
    # 门禁本身的放行逻辑,不是账本 —— 把额度这一层放行掉。
    monkeypatch.setattr(orchestrator.db, "reserve_spend", lambda *a, **k: True)
    monkeypatch.setattr(orchestrator.db, "commit_spend", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.db, "release_spend", lambda *a, **k: None)
    return orchestrator


def test_gate_a_blocks_on_lint_error(monkeypatch):
    """机器查得出的错不该靠眼睛找 —— 门禁 A 拦住,别带着已知缺陷去烧渲染的钱。"""
    from app.pipeline import orchestrator

    orch = _patch_gate(monkeypatch, [{"segment_idx": 0, "severity": "error",
                                       "message": "开场第一句是套话「朋友们好」"}])
    called = []
    monkeypatch.setattr(orch.explainer, "time_beats",
                        lambda *a, **k: called.append(1) or ([], None))
    with pytest.raises(orchestrator.PipelineError) as err:
        orch.advance("p_test", confirmed=True)
    assert "门禁 A 未放行" in str(err.value)
    assert "套话" in str(err.value)
    assert not called, "被拦下时不该开始合成旁白"


def test_gate_a_force_overrides(monkeypatch):
    """明知有问题仍然继续:必须显式给 force,不能默认放行。"""
    from app.pipeline import orchestrator

    orch = _patch_gate(monkeypatch, [{"segment_idx": 0, "severity": "error",
                                       "message": "开场第一句是套话「朋友们好」"}])
    monkeypatch.setattr(orch.explainer, "time_beats",
                        lambda *a, **k: ([_spec(0, "旁白")], _FakeNarration()))
    monkeypatch.setattr(orch.db, "update_project", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_set_state", lambda *a, **k: None)
    monkeypatch.setattr(orch.explainer, "init_beats", lambda *a, **k: None)
    monkeypatch.setattr(orch.costs, "explainer_spent", lambda *a, **k: 0.0)
    out = orch.advance("p_test", confirmed=True, force=True)
    assert out["state"] == "DRAFT_RENDERING"


class _FakeNarration:
    cached_sentences = 0
    bgm_reused = False
    warnings: list[str] = []
    sentences: list[str] = []

    def to_dict(self):
        return {}


def test_confirm_gate_passes_force_flag(monkeypatch):
    """force 必须真的传到 advance —— 否则"仍然继续"这个出口是假的。"""
    from app.pipeline import orchestrator

    seen: dict = {}

    def fake_advance(project_id, *, confirmed=False, force=False):
        seen["confirmed"] = confirmed
        seen["force"] = force
        return {"state": "ASSETS_READY", "waiting": False}

    monkeypatch.setattr(orchestrator.db, "get_project", lambda *a, **k: _gate_project())
    monkeypatch.setattr(orchestrator, "advance", fake_advance)
    orchestrator.confirm_gate("p_test", orchestrator.GateRequest(gate="A", force=True))
    assert seen["confirmed"] is True and seen["force"] is True
    seen.clear()
    orchestrator.confirm_gate("p_test", orchestrator.GateRequest(gate="A"))
    assert seen["confirmed"] is True and seen["force"] is False


# --------------------------------------------------------------------------
# 主论点:全片那句判断
# --------------------------------------------------------------------------
def test_script_generate_asks_for_a_thesis(auto):
    """主论点要和脚本一起出来:先定判断,再照着它写。"""
    class _LLM:
        def structured(self, system, user, **_):
            self.system = system
            return {"segments": [{"section": "开场", "text": "23.35万起。"}],
                    "title": "标题", "thesis": "适合既要又要的人"}

    llm = _LLM()
    segments, title, thesis = script_gen.generate(
        subject="小米YU7", profile=auto, facts=[], plan=["开场"], duration=120, llm=llm)
    assert thesis == "适合既要又要的人"
    assert title == "标题"
    assert "thesis" in llm.system
    assert "主论点" in llm.system


def test_thesis_must_land_in_the_conclusion(auto):
    """结论段不回扣主论点 = 全片散成并列的参数章。"""
    segs = [
        _seg(0, "开场", "23.35万起,这价格不像电车。"),
        _seg(1, "结论建议", "总结一下,值得买。"),
    ]
    issues = script_gen.thesis_lint(segs, "适合既要又要的人")
    assert issues and not issues[0].is_error
    assert "主论点" in issues[0].message


def test_thesis_landed_is_quiet(auto):
    segs = [
        _seg(0, "开场", "23.35万起,这价格不像电车。"),
        _seg(1, "结论建议", "总结一下,适合既要又要的人。"),
    ]
    assert script_gen.thesis_lint(segs, "适合既要又要的人") == []


def test_thesis_lint_quiet_without_conclusion_section(auto):
    segs = [_seg(0, "开场", "23.35万起。")]
    assert script_gen.thesis_lint(segs, "适合又要又要的人") == []


def test_thesis_lint_quiet_when_no_thesis(auto):
    segs = [_seg(0, "结论建议", "总结一下。")]
    assert script_gen.thesis_lint(segs, "") == []


# ------------------------------------------------------------------ 立场 / 所以呢
def test_stance_lint_flags_topic_style_hook():
    topic = [_seg(0, "开场", "别急着订车，先搞清楚YU7买哪版。")]
    assert [i.severity for i in script_gen.stance_lint(topic)] == ["warning"]
    stance = [_seg(0, "开场", "多花3万,买的不是续航是快充。")]
    assert script_gen.stance_lint(stance) == []
    assert script_gen.stance_lint([_seg(0, "使用成本", "先搞清楚电费。")]) == []


def test_meaning_lint_flags_spec_dump_once():
    dump = [_seg(i, "外观设计", f"车长{4999 + i}毫米。") for i in range(5)]
    issues = script_gen.meaning_lint(dump)
    assert len(issues) == 1 and issues[0].severity == "warning"
    assert "5/5" in issues[0].message
    judged = [_seg(i, "外观设计", f"车宽{1996 + i}毫米,老小区地库拐弯得先量一量。")
              for i in range(5)]
    assert script_gen.meaning_lint(judged) == []
    assert script_gen.meaning_lint([_seg(0, "开场", "这台车很有意思。")]) == []


# ------------------------------------------------------------------ 车迷视角:说对、说全、说到点子上
def test_term_lint_catches_merged_part_name(auto):
    """真片竖版写成「碳陶卡钳」,事实原文是「卡钳 + 碳陶瓷制动盘」。"""
    bad = [_seg(0, "三电性能", "碳陶卡钳制动32.9米。")]
    issues = script_gen.term_lint(bad, auto)
    assert [i.severity for i in issues] == ["error"] and "制动盘" in issues[0].message
    assert script_gen.term_lint([_seg(0, "三电性能", "碳陶制动盘,制动32.9米。")], auto) == []


def test_derived_lint_wants_hedge_on_computed_number_only():
    src = _f("f_src", "标准版73kWh磷酸铁锂电池,CLTC续航643公里。", source_kind="official")
    der = _f("f_der", "按 73kWh 电池、CLTC 续航 643 公里粗算,每百公里约用 11.4 度电(推算,源自 f_src)",
             source_kind="derived", source_query="f_src")
    facts = [src, der]
    bare = [_seg(0, "三电性能", "百公里11.4度电。", ["f_src", "f_der"])]
    assert [i.severity for i in script_gen.derived_lint(bare, facts)] == ["warning"]
    hedged = [_seg(0, "三电性能", "粗算百公里11.4度电。", ["f_der"])]
    assert script_gen.derived_lint(hedged, facts) == []
    # 只说源头的官方数(643 公里)不算推算数字
    official = [_seg(0, "三电性能", "CLTC 643公里。", ["f_src", "f_der"])]
    assert script_gen.derived_lint(official, facts) == []


def test_hero_lint_flags_unused_hard_facts(auto):
    hero = _f("f_ring", "YU7 GT在纽博格林北环跑出7分22秒755的圈速。")
    plain = _f("f_len", "车长4999mm。")
    segs = [_seg(0, "外观设计", "车长4999毫米。", ["f_len"])]
    assert [i.severity for i in script_gen.hero_lint(segs, auto, [hero, plain])] == ["warning"]
    used = segs + [_seg(1, "三电性能", "纽北7分22秒755。", ["f_ring"])]
    assert script_gen.hero_lint(used, auto, [hero, plain]) == []
    assert script_gen.hero_lint(segs, auto, [plain]) == []


def test_rival_lint_wants_a_name_in_comparison_section():
    cmp = _f("f_cmp", "YU7四驱版零百2.98秒,岚图追光S四驱版4.5秒。", compare_with="岚图追光S")
    vague = [_seg(0, "竞品对比", "同价位比,它的牌面是激光雷达。", [])]
    assert [i.severity for i in script_gen.rival_lint(vague, [cmp])] == ["warning"]
    named = [_seg(0, "竞品对比", "比岚图追光S快1.5秒。", ["f_cmp"])]
    assert script_gen.rival_lint(named, [cmp]) == []
    # 没有对比段(竖屏精简掉了)就不提
    assert script_gen.rival_lint([_seg(0, "结论建议", "买标准版。")], [cmp]) == []


def test_disabled_facts_stay_out_of_script():
    off = _f("f_old", "2025款标准版CLTC续航835公里。", enabled=False)
    on = _f("f_new", "2026款标准版CLTC续航643公里。")
    assert [f.id for f in script_gen.usable_facts([off, on])] == ["f_new"]


def test_conflict_lint_wants_basis_when_two_facts_disagree():
    off = _f("f_off", "YU7 GT零百加速2.92秒。", basis="GT版 · 2026款", source_kind="official")
    med = _f("f_med", "YU7 GT零百加速2.98秒。", basis="GT版 · 不含起步时间", source_kind="media")
    seg = [_seg(0, "三电性能", "零百2.92秒。", ["f_off"])]
    issues = script_gen.conflict_lint(seg, [off, med])
    assert [i.severity for i in issues] == ["warning"] and "f_med" in issues[0].message
    # 版本不同(标准版 vs GT 版)不是冲突
    std = _f("f_std", "YU7标准版零百加速5.9秒。", basis="标准版 · 2026款")
    assert script_gen.conflict_lint(seg, [off, std]) == []


def test_range_lint_wants_soc_window():
    f = _f("f_ch", "全系5.2C超充,10%-80%最快充电12分钟,15分钟补能620公里。")
    bare = [_seg(0, "三电性能", "12分钟就能充好。", ["f_ch"])]
    assert [i.severity for i in script_gen.range_lint(bare, [f])] == ["warning"]
    ok = [_seg(0, "三电性能", "10%到80%只要12分钟。", ["f_ch"])]
    assert script_gen.range_lint(ok, [f]) == []
    # 用的是别的分句里的数(620 公里),不要求区间
    other = [_seg(0, "三电性能", "15分钟补620公里。", ["f_ch"])]
    assert script_gen.range_lint(other, [f]) == []


def test_numbers_in_does_not_read_lingbai_as_hundred():
    """「零百」是 0-100km/h 加速,不是 100:以前读成 100292,和事实的 2.92 对不上。"""
    from app.pipeline.grounding import numbers_in
    assert numbers_in("零百2.92秒") == {"292"}
    assert numbers_in("三十一辆") == {"31"}


def test_new_lints_regressions_from_live_run():
    """p_a4b14e3ff3a7 实跑暴露的三处误报 / 漏报。"""
    # 竞品叫法:「特斯拉Model Y」只说「Model Y」也算点名
    cmp = _f("f_cmp", "YU7标准版CLTC续航643公里,比Model Y入门版多50公里。", compare_with="特斯拉Model Y")
    assert script_gen.rival_lint([_seg(0, "竞品对比", "和Model Y比。", [])], [cmp]) == []
    # 推算出处里的事实 ID 数字不算推算数
    src = _f("f_63d4b", "标准版73kWh电池,CLTC续航643公里。")
    der = _f("f_der", "按 73kWh 电池、CLTC 续航 643 公里粗算,每百公里约用 11.4 度电(推算,源自 f_63d4b)",
             source_kind="derived", source_query="f_63d4b")
    assert script_gen.derived_lint([_seg(0, "用车成本", "一公里不到6分钱。", ["f_der"])], [src, der]) == []
    # 「搭载 800V」和「采用 752V」是同一参数;单位写法不同(km/公里)也算同一维度
    v800 = _f("f_800", "小米YU7全系搭载800V碳化硅高压平台。", basis="全系 · 2025款")
    v752 = _f("f_752", "小米YU7标准版采用752V碳化硅平台。", basis="标准版 · 2026款")
    seg = [_seg(0, "三电性能", "全系800V平台。", ["f_800"])]
    assert [i.severity for i in script_gen.conflict_lint(seg, [v800, v752])] == ["warning"]
    old = _f("f_o", "标准版CLTC工况续航835km。", basis="CLTC · 标准版 · 2025款")
    new = _f("f_n", "标准版CLTC续航643公里。", basis="CLTC · 标准版 · 2026款")
    bare = [_seg(0, "版本选择", "标准版835公里续航。", ["f_o"])]
    assert [i.severity for i in script_gen.conflict_lint(bare, [old, new])] == ["warning"]
    # 说了年款就清楚了
    named = [_seg(0, "版本选择", "2025款标准版835公里续航。", ["f_o"])]
    assert script_gen.conflict_lint(named, [old, new]) == []
