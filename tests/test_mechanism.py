"""原理拆解片型(mechanism)与机构画面版式(mech)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.categories import load_profile
from app.models import ScriptSegment, ShotSpec
from app.pipeline import mechanism_art, motion, rhythm
from app.pipeline.explainer import mech, validate_beats


@pytest.fixture(scope="module")
def profile():
    return load_profile("auto_long")


def test_theme_is_portrait_long_and_needs_angle(profile):
    theme = profile.theme_spec("mechanism")
    assert theme.requires_angle
    assert profile.theme_durations(theme, config.PORTRAIT) == (180, 240, 300)
    assert profile.supported_durations(config.PORTRAIT) == (60, 90, 120, 180)
    assert profile.vocab.series["mechanism"] == "柒哥拆给你看"


@pytest.mark.parametrize("duration", [180, 240, 300])
def test_plan_keeps_core_sections(profile, duration):
    plan = profile.plan_for(duration, "mechanism", portrait=True)
    assert len(plan) == profile.shots_for(duration)
    assert plan[0] == "拆解开场" and plan[-1] == "拆解结论"
    for name in ("一句答案", "部件一", "部件二", "代价旧账"):
        assert name in plan
    # 机构画面是这个片型的主体:允许 mech 的拍占多数
    allowed = [s for s in plan if "mech" in profile.section(s).layouts]
    assert len(allowed) / len(plan) >= 0.6


def test_every_part_points_at_an_existing_template(profile):
    scenes = motion.available_scenes()
    for name, spec in profile.vocab.mech_parts.items():
        assert spec["scene"] in scenes, name
        assert motion.supports_portrait(motion.DEFAULT_STYLE, spec["scene"]), name
        if spec["scene"] == "cutaway":
            assert spec.get("art"), name


def test_guide_only_appears_when_a_beat_allows_mech(profile):
    other = [ScriptSegment(idx=0, section="是什么", text="x")]
    ours = [ScriptSegment(idx=0, section="部件一", text="x")]
    assert not mech.enabled(profile, other)
    assert mech.mech_guide(profile.vocab, mech.enabled(profile, other)) == ""
    guide = mech.mech_guide(profile.vocab, mech.enabled(profile, ours))
    assert "hybrid_series" in guide and "engine_turbo" in guide
    # 进 SYSTEM.format 的参数:花括号得是成对的 JSON 示例,不能留下未转义的占位
    assert "{concept_guide}" not in guide


def test_marks_must_be_spoken_and_carry_no_numbers():
    narration = "低速时发动机只发电,由驱动电机推车,热效率做到 46%。"
    got = mech.ground_marks(["发动机只发电", "驱动电机推车", "46%", "行星齿轮", "发动机只发电", "电"], narration)
    assert got == [{"text": "发动机只发电"}, {"text": "驱动电机推车"}]


def test_unknown_part_falls_back_to_keywords_or_gives_up(profile):
    ov, why = mech.ground_mech({"part": "warp_drive", "title": "串联模式", "marks": ["发电机"]},
                               "这时候是串联,发动机带着发电机发电。", [], profile.vocab)
    assert ov and ov["part"] == "hybrid_series" and ov["scene"] == "powerflow" and why
    assert ov["marks"] == [{"text": "发电机"}]
    ov, why = mech.ground_mech({"part": "warp_drive"}, "这台车卖得不错。", [], profile.vocab)
    assert ov is None and why


def test_title_with_a_number_is_dropped_and_rows_need_facts(profile):
    ov, _ = mech.ground_mech({"part": "engine_cycle", "title": "热效率46%", "rows": [
        {"label": "热效率", "values": ["46%"], "fact_id": "f_missing"}]}, "热效率 46%。", [], profile.vocab)
    assert ov["title"] == "" and ov["rows"] == []


def _beats(profile, layouts, texts):
    return [ShotSpec(idx=i, section="部件一", chapter="电混系统", visual_zh=f"[{lay}] x",
                     narration_zh=t, layout=lay) for i, (lay, t) in enumerate(zip(layouts, texts, strict=True))]


def test_promote_fills_broll_and_keeps_the_part_within_a_chapter(profile):
    specs = _beats(profile, ["broll"] * 4,
                   ["低速时是串联,发动机只发电。", "所以它一直待在最省油的转速。",
                    "到了高速,离合器结合,发动机直驱。", "这一步省掉了两道转换。"])
    out = mech.promote_mech(specs, profile)
    assert [s.layout for s in out][:3] == ["mech", "mech", "mech"]
    assert out[0].overlay["part"] == "hybrid_series"
    assert out[1].overlay["part"] == "hybrid_series"      # 没命中词:接着上一拍的部件
    assert out[2].overlay["part"] == "hybrid_parallel"
    assert not validate_beats(out)


def test_promote_leaves_other_themes_alone(profile):
    specs = [ShotSpec(idx=0, section="是什么", visual_zh="[broll] x",
                      narration_zh="低速时是串联,发动机只发电。", layout="broll")]
    assert mech.promote_mech(specs, profile)[0].layout == "broll"


def test_consecutive_mech_beats_do_not_count_as_one_static_screen(profile):
    specs = mech.promote_mech(_beats(profile, ["broll"] * 6, ["低速时是串联,发动机只发电。"] * 6), profile)
    for s in specs:
        s.duration = 8.0
    assert all(s.layout == "mech" for s in specs[:4])
    run, _ = rhythm.same_run([s for s in specs if s.layout == "mech"])
    assert run == 1
    assert rhythm.max_change_gap([s for s in specs if s.layout == "mech"]) == 0.0


def test_scene_data_carries_words_and_marks(profile):
    ov = mech.overlay_for("engine_turbo", profile.vocab, title="小排量大扭矩",
                          marks=["涡轮"], narration="废气推着涡轮转。")
    ov["marks"][0]["at"] = 1.5
    data = mech.scene_data(ov, "奥迪A6L", idx=5)
    assert data["mode"] == "turbo" and data["variant"] == 1
    assert data["marks"] == [{"text": "涡轮", "at": 1.5}]
    assert len(data["words"]["strokes"]) == 4


def test_art_prompt_forbids_text_and_cache_is_per_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    prompt = mechanism_art.prompt_for("cutaway of a gearbox")
    assert "no text" in prompt and "no numbers" in prompt and "no logos" in prompt
    a = mechanism_art.path_for("gearbox", "cutaway of a gearbox")
    b = mechanism_art.path_for("gearbox", "cutaway of another gearbox")
    assert a != b and a.parent == tmp_path / "_cache" / "mechanism"
    with pytest.raises(mechanism_art.MechArtError):
        mechanism_art.cutaway("gearbox", "cutaway of a gearbox", generate=False)
    with pytest.raises(mechanism_art.MechArtError):
        mechanism_art.cutaway("gearbox", "")
    a.parent.mkdir(parents=True)
    a.write_bytes(b"x" * 30_000)
    assert mechanism_art.cutaway("gearbox", "cutaway of a gearbox", generate=False) == a


@pytest.mark.parametrize("scene", ["piston", "powerflow", "cutaway"])
def test_templates_compose_in_both_aspects(profile, scene):
    ov = {"kind": "mech", "scene": scene, "mode": "turbo", "title": "t", "marks": [{"text": "涡轮"}], "rows": []}
    for size in (motion.size_for(config.PORTRAIT), motion.size_for(config.LANDSCAPE)):
        html = motion.compose_html(scene, mech.scene_data(ov, "奥迪A6L"), 6.0, size=size)
        assert "HF.mech(tl" in html and "涡轮" in html


@pytest.mark.parametrize("scene,mode", [("planetary", "split"), ("planetary", "reduce"),
                                        ("damper", "air"), ("pack", "cool"), ("motor", ""),
                                        ("powerflow", "assist")])
def test_new_templates_compose_in_both_aspects(profile, scene, mode):
    ov = {"kind": "mech", "scene": scene, "mode": mode, "title": "t", "marks": [], "rows": []}
    for size in (motion.size_for(config.PORTRAIT), motion.size_for(config.LANDSCAPE)):
        html = motion.compose_html(scene, mech.scene_data(ov, "奥迪A6L"), 6.0, size=size)
        assert "HF.mech(tl" in html


@pytest.mark.parametrize("name", ["planetary", "damper", "pack", "motor", "powerflow", "piston",
                                  "cutaway"])
def test_templates_stay_ascii(name):
    text = (Path(__file__).resolve().parent.parent / "motion" / "scenes" / f"{name}.html").read_bytes()
    assert all(b < 128 for b in text)


def test_part_keywords_pick_the_new_parts(profile):
    v = profile.vocab
    assert mech.part_for("行星齿轮把发动机的力分成两路。", v) in {"gear_split", "gear_reduce"}
    assert mech.part_for("定子通上三相电,磁场转起来。", v) == "motor_field"
    assert mech.part_for("空气弹簧把车身托起来。", v) == "damper_air"
    assert mech.part_for("电机只是帮一把,48V轻混。", v) == "hybrid_assist"


def test_promote_works_per_chapter_and_skips_money_beats(profile):
    first = _beats(profile, ["broll"] * 4, ["低速时是串联,发动机只发电。"] * 4)
    texts = ["这套东西也有旧账。", "指导价贵了一截。", "综合油耗6.98L。",
             "高速时离合器结合,发动机直驱。", "保养也更麻烦。"]
    cost = [ShotSpec(idx=4 + i, section="代价旧账", chapter="代价", visual_zh="[broll] x",
                     narration_zh=x, layout="broll") for i, x in enumerate(texts)]
    tail = mech.promote_mech(first + cost, profile)[4:]
    if "mech" not in (profile.section("代价旧账").layouts or ()):
        assert all(s.layout == "broll" for s in tail)
        return
    # 本章前面还没讲到部件:用本章后面最先讲到的那个,不拿别的章的部件兜底
    assert tail[0].layout == "mech" and tail[0].overlay["part"] == "hybrid_parallel"
    assert tail[1].layout == "broll" and tail[2].layout == "broll"   # 讲钱、报数字的拍不接续
    assert tail[3].overlay["part"] == "hybrid_parallel"


def test_promote_leaves_a_chapter_without_parts_alone(profile):
    first = _beats(profile, ["broll"] * 4, ["低速时是串联,发动机只发电。"] * 4)
    cost = [ShotSpec(idx=4 + i, section="代价旧账", chapter="代价", visual_zh="[broll] x",
                     narration_zh=x, layout="broll")
            for i, x in enumerate(["这套东西也有旧账。", "保养也更麻烦。"])]
    assert all(s.layout == "broll" for s in mech.promote_mech(first + cost, profile)[4:])


def test_views_rotate_when_one_cutaway_runs_long(profile):
    part = next(k for k, v in profile.vocab.mech_parts.items() if v.get("art"))
    specs = []
    for i in range(6):
        s = ShotSpec(idx=i, section="部件一", chapter="c", visual_zh="[mech] x", narration_zh="x",
                     layout="mech")
        s.overlay = mech.overlay_for(part, profile.vocab, narration="x")
        specs.append(s)
    views = [s.overlay["view"] for s in mech.assign_views(specs)]
    assert views == [0, 0, 1, 1, 2, 2]


def test_art_cache_is_per_view(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    art = "cutaway of a gearbox"
    paths = {mechanism_art.path_for("gearbox", art, v) for v in range(len(mechanism_art.VIEWS))}
    assert len(paths) == len(mechanism_art.VIEWS)
    assert mechanism_art.path_for("gearbox", art) == mechanism_art.path_for("gearbox", art, 0)
    assert "exploded" in mechanism_art.prompt_for(art, 2)


def test_art_with_text_is_redrawn_then_rejected(tmp_path, monkeypatch):
    from PIL import Image

    from app.providers import ark_image

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    src = tmp_path / "src.png"
    Image.effect_noise((640, 480), 80).convert("RGB").save(src)
    calls = []

    class _Resp:
        content = src.read_bytes()

        def raise_for_status(self):
            return None

    monkeypatch.setattr(ark_image, "generate_one", lambda *a, **k: calls.append(1) or "http://x/y.png")
    monkeypatch.setattr(mechanism_art.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(mechanism_art, "says_clean", lambda path: False)
    with pytest.raises(mechanism_art.MechArtError):
        mechanism_art.cutaway("gearbox", "cutaway of a gearbox")
    assert len(calls) == mechanism_art.RETRIES + 1
    assert not list((tmp_path / "_cache" / "mechanism").glob("*.png"))
    monkeypatch.setattr(mechanism_art, "says_clean", lambda path: True)
    assert mechanism_art.cutaway("gearbox", "cutaway of a gearbox").is_file()


def test_theme_carries_pace_cover_and_title_style(profile):
    theme = profile.theme_spec("mechanism")
    assert theme.chars_per_second > 5 and theme.cover_tone == "teardown"
    assert theme.title_style == "question" and "汽车原理" in theme.tags


def test_question_title_rules():
    from app.pipeline import packaging

    ok = "奥迪A6L,小排量凭什么带得动这台车?"
    assert not packaging.check_title(ok, set(), name="奥迪A6L", style="question")
    assert packaging.check_title("奥迪A6L,到底该不该买?", set(), name="奥迪A6L", style="question")
    assert packaging.check_title("奥迪A6L的发动机很厉害", set(), name="奥迪A6L", style="question")
    t = packaging.fallback_title(packaging.KIND_STAKE, "奥迪A6L", name="奥迪A6L", style="question",
                                 angle="小排量凭什么带得动这台车?")
    assert t == ok
    assert "titles" in packaging.system_for("question") and packaging.system_for("") == packaging.SYSTEM


def test_teardown_cover_uses_question_and_dark_tone():
    from app.pipeline import cover_art
    from app.pipeline.cover_art import layout

    tone = cover_art.THEME_TONES["teardown"]
    assert tone.dark and tone.light_text
    assert cover_art.question_part("比亚迪秦L DM-i,多一道能量转换凭什么更省油?",
                                   "比亚迪秦L DM-i") == "多一道能量转换凭什么更省油?"
    assert layout._ask_cuts("两升排量凭什么带得动一吨八的车?") == [7]


def test_theme_tags_replace_the_shopping_tag():
    from app.publish.metadata import ContentContext, build_tags

    tags = build_tags(ContentContext(subject="奥迪A6L", series="柒哥拆给你看",
                                     theme_tags=("汽车知识", "汽车原理")), 5)
    assert tags[:4] == ["奥迪A6L", "柒哥拆给你看", "汽车知识", "汽车原理"]
    assert "奥迪A6L怎么选" not in tags
    assert "奥迪A6L怎么选" in build_tags(ContentContext(subject="奥迪A6L"), 5)


def test_quantities_in_a_title_are_not_taken_for_model_names():
    from app.pipeline import packaging

    known = "奥迪A6L 坦克300"
    assert not packaging.unknown_models("奥迪A6L,2.0T凭什么拉得动1896kg的车?", known)
    assert not packaging.unknown_models("奥迪A6L,堵车20-30km/h一顿一顿,问题出在哪?", known)
    assert not packaging.unknown_models("奥迪A6L,配48V轻混图什么?", known)
    assert packaging.unknown_models("奥迪A6L,比坦克500强在哪?", known) == ["克500"]
    assert packaging.unknown_models("奥迪A6L,和钛7比怎么样?", known) == ["钛7"]


def test_question_fallback_keeps_the_spaced_name_and_ends_with_a_mark():
    from app.pipeline import packaging

    t = packaging.fallback_title(packaging.KIND_STAKE, "比亚迪秦L DM-i", name="比亚迪秦L DM-i",
                                 style="question", angle="多一道能量转换,凭什么反而更省油",
                                 allowed={"1"})   # 「一道」会被当成数字 1
    assert t == "比亚迪秦L DM-i,多一道能量转换凭什么反而更省油?"



# ---- 主编排题(plan_mechanism)与片型目标 ----

@pytest.fixture
def plan_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    monkeypatch.setattr(config, "TEAM_AUTO_APPROVE_TOPICS", False)
    return db


def _at(day, hour=18):
    import datetime as dt

    return dt.datetime(2026, 11, day, hour, 30).timestamp()


def test_topic_seeds_are_complete_and_carry_no_digits(profile):
    import re

    topics = profile.vocab.mechanism_topics
    assert len(topics) >= 20
    assert all(t["car"] and t["part"] and t["angle"] for t in topics)
    assert not [t for t in topics if re.search(r"\d", t["angle"])]
    assert len({(t["car"], t["angle"]) for t in topics}) == len(topics)
    # 种子顺序本身就不该让同一类部件连排三期
    parts = [t["part"] for t in topics]
    assert not [i for i in range(2, len(parts)) if parts[i] == parts[i - 1] == parts[i - 2]]


def test_plan_mechanism_off_by_default(plan_db, profile, monkeypatch):
    from app import config
    from app.team import editor

    monkeypatch.setattr(config, "TEAM_MECHANISM_WEEKDAYS", ())
    slots = [_at(17), _at(21)]
    assert editor.plan_mechanism("auto_long", profile, slots, _at(15)) == (0, slots)
    assert plan_db.list_plan_items("auto_long") == []


def test_plan_mechanism_fills_configured_weekdays_in_seed_order(plan_db, profile, monkeypatch):
    from app import config
    from app.team import editor

    monkeypatch.setattr(config, "TEAM_MECHANISM_WEEKDAYS", (1, 5))  # 周二、周六
    # 11-16 周一、11-17 周二(两个档)、11-21 周六
    slots = [_at(16), _at(17, 12), _at(17), _at(21)]
    made, rest = editor.plan_mechanism("auto_long", profile, slots, _at(15))
    assert made == 2 and rest == [_at(16), _at(17)]          # 一天只占一个档
    items = sorted(plan_db.list_plan_items("auto_long"), key=lambda i: i["slot_at"])
    seeds = profile.vocab.mechanism_topics
    assert [(i["subject"], i["angle"]) for i in items] == [(t["car"], t["angle"]) for t in seeds[:2]]
    assert all(i["theme"] == "mechanism" and i["aspect"] == config.PORTRAIT
               and i["duration"] == 240 and i["status"] == "proposed" for i in items)
    # 再排一次:排过的题和冷却期里的车都不重复
    made, _ = editor.plan_mechanism("auto_long", profile, [_at(24)], _at(15))
    assert made == 1
    subjects = [i["subject"] for i in plan_db.list_plan_items("auto_long")]
    assert len(subjects) == len(set(subjects)) == 3


def test_next_topic_skips_cooled_done_and_a_third_same_part():
    from app.team import editor

    topics = (
        {"car": "甲车", "part": "混动", "angle": "凭什么省油"},
        {"car": "乙车", "part": "混动", "angle": "为什么背着发动机"},
        {"car": "丙车", "part": "悬架", "angle": "凭什么托得住"},
    )
    pick = editor.next_mechanism_topic
    assert pick(topics, set(), set(), [])["car"] == "甲车"
    assert pick(topics, {"甲车"}, set(), [])["car"] == "乙车"                       # 冷却期
    assert pick(topics, set(), {editor._topic_key("甲车", "凭什么省油?")}, [])["car"] == "乙车"  # 做过
    assert pick(topics, set(), set(), ["混动", "混动"])["car"] == "丙车"            # 不连排第三期
    assert pick(topics, set(), set(), ["悬架", "混动"])["car"] == "甲车"
    assert pick(topics, set(), set(), [], {"丙车"})["car"] == "丙车"                # 观众点播优先
    assert pick(topics, {"丙车"}, set(), ["混动", "混动"]) is None


def test_plan_mechanism_counts_parts_already_on_the_calendar(plan_db, profile, monkeypatch):
    from app import config
    from app.models import new_id
    from app.team import editor

    monkeypatch.setattr(config, "TEAM_MECHANISM_WEEKDAYS", (0, 1, 2, 3, 4, 5, 6))
    seeds = profile.vocab.mechanism_topics
    same = [t for t in seeds if t["part"] == seeds[0]["part"]]
    for n, topic in enumerate(same[:2]):
        plan_db.create_plan_item({
            "id": new_id("pl_"), "category": "auto_long", "slot_at": _at(16 + n),
            "status": "approved", "kind": "single", "subject": topic["car"], "subjects": [],
            "angle": topic["angle"], "theme": "mechanism", "aspect": config.PORTRAIT,
            "duration": 240, "hook": "", "search_terms": "", "evidence": [], "rationale": "",
            "heat": 3})
    made, _ = editor.plan_mechanism("auto_long", profile, [_at(18)], _at(15))
    assert made == 1
    new = max(plan_db.list_plan_items("auto_long"), key=lambda i: i["slot_at"])
    part = next(t["part"] for t in seeds if t["car"] == new["subject"])
    assert part != seeds[0]["part"]


def _mw(views, c5, gained, *, hours=80.0, theme="mechanism"):
    return {"views": views, "completion_5s": c5, "followers_gained": gained, "bounce_2s": 0.2,
            "hours_since_publish": hours, "published_at": 0, "theme": theme}


def test_mechanism_has_its_own_goals():
    from app.team import growth

    spec = growth.THEME_GOALS["mechanism"]
    few = growth.goals([_mw(900, 0.6, 5)] * 3 + [_mw(9999, 0.9, 50, hours=30.0)], None, spec)
    assert few["works"] == 3 and few["hold"] and "3/8" in few["verdict"]      # 不满 72 小时的不算
    good = growth.goals([_mw(600, 0.55, 4)] * 7 + [_mw(600, 0.55, 0)], None, spec)
    assert good["ready"] and all(m["met"] for m in good["metrics"])            # 涨粉取均值 3.5
    bad = growth.goals([_mw(600, 0.55, 1)] * 8, None, spec)
    assert bad["verdict"].startswith("没达标") and "转化" in bad["verdict"]
    assert growth.goals([], None, spec)["verdict"].startswith("还没有发布满 72 小时")


def test_summary_keeps_mechanism_out_of_the_search_goals(plan_db, monkeypatch):
    from app.team import growth

    works = [_mw(900, 0.6, 5)] * 2 + [_mw(40, 0.4, 0, theme="buyguide")] * 3
    monkeypatch.setattr(growth, "_work", lambda pub, now: pub["w"])
    monkeypatch.setattr(growth.db, "list_publications", lambda limit=500: [
        {"platform": "douyin", "state": "published", "published_at": 1.79e9, "w": w} for w in works])
    out = growth.summary(now=1.79e9, since="")
    assert out["goals"]["works"] == 3
    assert out["goals_by_theme"]["mechanism"]["works"] == 2
    assert out["goals_by_theme"]["mechanism"]["min_works"] == 8
