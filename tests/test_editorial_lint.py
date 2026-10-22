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
