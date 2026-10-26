"""生产旋钮:没挂手册时全取默认(golden 不动)、挂了按权重偏、并行实验轮流照做、实测特征分档。"""
from __future__ import annotations

import json
import time

import pytest

from tests.test_coach import _project, fresh_db, no_playbook  # noqa: F401 - fixtures


def test_pick_is_empty_without_playbook():
    from app.pipeline import knobs

    assert knobs.pick("p1") == {}
    assert knobs.pick("p1", chosen={"pace": "fast"}) == {"pace": "fast"}


def test_defaults_match_old_constants():
    from app.pipeline import knobs, library

    with knobs.use({}):
        assert knobs.piece_seconds() == library.FILL_MAX_PIECE
        assert knobs.concept_share() == 0.13 and knobs.speed_factor() == 1.0
        assert knobs.value("hook_visual") == "" and knobs.voice() == ""
    with knobs.use({"pace": "fast", "speed": "nonsense"}):
        assert knobs.piece_seconds() == 3.0
        assert knobs.speed_factor() == 1.0  # 不认识的值 → 默认


def test_concept_min_follows_knob():
    from app.pipeline import explainer, knobs

    with knobs.use({}):
        base = explainer.concept_min(40)
    with knobs.use({"concept_level": "high"}):
        assert explainer.concept_min(40) >= base
    with knobs.use({"concept_level": "low"}):
        assert explainer.concept_min(40) <= base


def test_pick_leans_on_weights(fresh_db):
    from app.pipeline import knobs, variety

    variety.learned = lambda dim: {"fast": 2.0, "mid": 0.5, "slow": 0.5} if dim == "pace" else {}
    try:
        got = [knobs.pick(f"p{i}")["pace"] for i in range(200)]
    finally:
        variety.learned = None
    assert got.count("fast") > got.count("mid") * 2
    assert got.count("mid") > 0  # 探索还在


def test_voice_knob_only_with_candidates():
    from app.pipeline import knobs

    class P:
        voices = ("a", "b")

    assert knobs.VOICE in knobs.universe(P())
    assert knobs.VOICE not in knobs.universe(None)


def test_looks_follow_plan_and_weights(fresh_db):
    from app.pipeline import looks, variety

    before = [looks.pick(f"p{i}") for i in range(10)]
    variety.plan = lambda dim=None: {"dimension": "look", "value": "blueprint"} if dim == "look" else None
    try:
        assert looks.pick("p1") == "blueprint"
    finally:
        variety.plan = None
    assert [looks.pick(f"p{i}") for i in range(10)] == before


# --------------------------------------------------------------------------
# 并行实验
# --------------------------------------------------------------------------
def _publish(db, pid, *, opening, t0):
    _project(db, f"p_{pid}", opening=opening)
    db.create_publication({"id": pid, "project_id": f"p_{pid}", "account_id": "acc1",
                           "platform": "douyin", "title": "t", "state": "published",
                           "remote_id": f"w_{pid}", "published_at": t0,
                           "variant": json.dumps({"title_kind": "number", "cover": "a"})})


def test_parallel_plans_take_turns(fresh_db):
    from app.team import playbook

    t0 = time.time()
    fresh_db.set_team_setting(playbook.PLAN_KEY, [
        {"dimension": "opening", "value": "conflict", "size": 3, "since": t0},
        {"dimension": "pace", "value": "fast", "size": 3, "since": t0},
        {"dimension": "opening", "value": "scene", "size": 3, "since": t0},  # 同维度第二个:丢掉
    ])
    assert [p["dimension"] for p in playbook.stored_plans()] == ["opening", "pace"]
    assert playbook.plan() == {"dimension": "opening", "value": "conflict"}
    assert playbook.plan("pace") is None  # 这条片轮到开头,旋钮照常挑
    _publish(fresh_db, "a", opening="conflict", t0=t0 + 1)
    assert playbook.plan("pace") == {"dimension": "pace", "value": "fast"}
    assert playbook.plan("opening") is None


def test_old_single_plan_still_works(fresh_db):
    from app.team import playbook

    fresh_db.set_team_setting(playbook.PLAN_KEY, {"dimension": "opening", "value": "conflict",
                                                  "size": 3, "since": time.time()})
    assert playbook.plan("opening") == {"dimension": "opening", "value": "conflict"}


def test_knob_pick_honours_plan(fresh_db):
    from app.pipeline import knobs
    from app.team import playbook

    fresh_db.set_team_setting(playbook.PLAN_KEY, [
        {"dimension": "hook_visual", "value": "broll", "size": 3, "since": time.time()}])
    playbook.install()
    assert all(knobs.pick(f"p{i}")["hook_visual"] == "broll" for i in range(5))


# --------------------------------------------------------------------------
# 实测特征
# --------------------------------------------------------------------------
def test_measured_features_and_extract(fresh_db):
    from app.models import ShotSpec
    from app.pipeline import explainer
    from app.team import features

    _project(fresh_db, "p1")
    explainer.beats_dir("p1")
    specs = [ShotSpec(idx=0, duration=6, visual_zh="x", layout="hook"),
             ShotSpec(idx=1, duration=6, visual_zh="x", layout="concept"),
             ShotSpec(idx=2, duration=8, visual_zh="x", layout="broll")]
    fresh_db.write_json("p1", "beats/000.json", [{"length": 3, "kind": "video"},
                                                {"length": 3, "kind": "video"}])
    fresh_db.write_json("p1", "beats/002.json", [{"length": 4, "kind": "still"},
                                                {"length": 4, "kind": "video", "third_party": True}])
    m = explainer.measured_features("p1", specs, {"total_chars": 100, "bgm_path": "b.mp3"})
    assert m["duration"] == 20 and m["first_layout"] == "hook"
    assert m["cuts_per_minute"] == pytest.approx(5 * 60 / 20)
    assert m["concept_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["official_video_ratio"] == pytest.approx(6 / 14, abs=1e-3)
    assert m["chars_per_second"] == 5.0 and m["bgm"] is True

    fresh_db.write_json("p1", "features.json", m)
    f = features.extract({"variant": "{}"}, fresh_db.get_project("p1"))
    assert f["m_cuts"] == "常规" and f["m_first"] == "hook" and f["m_concept"] == "多"
    assert f["m_official"] == "中" and f["m_cps"] == "中"
    assert f["pace"] == ""  # 老项目没旋钮


def test_knob_dimension_labels():
    from app.team import features

    assert features.value_label("pace", "fast").startswith("快切")
    assert features.DIMENSION_LABEL["m_cuts"] == "实测切换密度"


def test_duration_weights():
    from app.categories import load_profile
    from app.team import editor
    from app.team.features import duration_bucket

    profile = load_profile("auto_long")
    theme = next(t for t in profile.themes if not profile.theme_spec(t).multi)
    plain = editor.choose_format(profile, theme)
    assert editor.choose_format(profile, theme, weights={}, seed="x") == plain
    durations = profile.theme_durations(profile.theme_spec(theme), plain[0])
    if len({duration_bucket(d) for d in durations}) < 2:
        pytest.skip("这个片型的时长都在同一档")
    best = next(d for d in durations if duration_bucket(d) != duration_bucket(plain[1]))
    w = {duration_bucket(d): (2.0 if duration_bucket(d) == duration_bucket(best) else 0.5)
         for d in durations}
    got = [editor.choose_format(profile, theme, weights=w, seed=str(i))[1] for i in range(100)]
    assert sum(duration_bucket(g) == duration_bucket(best) for g in got) > 25  # 不加权时一条都不会落在那档
