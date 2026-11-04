"""画面和包装:屏上数字要说过,轻包装按实拍算节奏,转场默认硬切,素材用量随全片重渲清零。"""
from __future__ import annotations

import pytest

from app import config, db
from app.categories import load_profile
from app.models import ScriptSegment, ShotSpec
from app.pipeline import explainer, looks, render, rhythm, timeline
from app.pipeline.explainer.planning_a import sync_narration
from app.pipeline.explainer.text import said_overlay
from app.pipeline.library import select
from app.pipeline.library.model import Item
from tests.test_background_conflict import _pool, data_dir  # noqa: F401
from tests.test_explainer import FakeLLM, _facts, _segments
from tests.test_motion_visuals import needs_ffmpeg


def _spec(idx: int, layout: str = "broll", text: str = "一二三四五", **kw) -> ShotSpec:
    return ShotSpec(idx=idx, visual_zh=f"[{layout}] x", narration_zh=text, layout=layout, **kw)


def test_panel_keeps_only_rows_the_beat_says():
    panel = {"kind": "panel", "rows": [{"label": "续航", "values": ["835公里"]},
                                       {"label": "售价", "values": ["25.35万元"]},
                                       {"label": "雷达", "values": ["标配"]}]}
    got = said_overlay(panel, "这台车续航835公里。")
    assert [r["label"] for r in got["rows"]] == ["续航", "雷达"]
    number_only = {**panel, "rows": panel["rows"][:2]}
    assert said_overlay(number_only, "这钱花得值。") is None


def test_stat_needs_the_number_in_the_narration():
    raw = {"label": "续航", "value": "835公里", "fact_id": "f_1"}
    assert explainer.ground_stat(raw, _facts(), "续航835公里。")[0]["value"] == "835公里"
    got, why = explainer.ground_stat(raw, _facts(), "这台车跑得挺远。")
    assert got is None and "没说过" in why[-1]


def test_edited_narration_takes_unsaid_numbers_off_screen():
    profile = load_profile("auto_long")
    stat = {"kind": "stat", "label": "续航", "value": "835公里", "fact_id": "f_1"}
    spec = _spec(0, "stat", text="续航835公里。", overlay=stat)
    seg = ScriptSegment(idx=0, section="三电性能", text="这台车跑得挺远。")
    got = sync_narration([spec], [seg], _facts(), profile)[0]
    assert got.layout == "broll" and got.overlay is None


def _build_lite(items):
    p = load_profile("auto_long")
    segs = _segments(p)
    with looks.use("lite"):
        specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                      llm=FakeLLM(items(segs)))
    return specs


def test_lite_has_no_panels_and_spaces_out_big_text():
    def items(segs):
        out = [{"idx": n + 1, "layout": "highlight", "keywords": ["贵的是电池", "不值"]}
               for n in range(len(segs))]
        out[1] = {"idx": 2, "layout": "panel", "panel": {"rows": [
            {"label": "续航", "values": ["835公里"], "fact_id": "f_1"}]}}
        return out

    specs = _build_lite(items)
    assert "panel" not in {s.layout for s in specs}
    t, shown = 0.0, []
    for s in specs:
        if s.layout == "highlight":
            shown.append(t)
        t += max(1.0, len(s.narration_zh) / 5.0)
    gaps = [b - a for a, b in zip(shown, shown[1:], strict=False)]
    assert all(g >= explainer.planning_b.OVERLAY_GAP_SECONDS for g in gaps)
    assert {(s.overlay or {}).get("style") for s in specs if s.layout == "broll"} <= {None, "plain"}


def test_overlays_are_capped_by_seconds_and_the_hook_stays():
    specs = [_spec(0, "hook")] + [_spec(n, "card" if n % 2 else "concept") for n in range(1, 10)]
    explainer.thin_overlays(specs)
    assert specs[0].layout == "hook"
    assert sum(s.layout in rhythm.INFO_LAYOUTS for s in specs) / len(specs) <= rhythm.INFO_RATIO_MAX
    assert {s.layout for s in specs} >= {"broll"}


def test_lite_does_not_turn_broll_into_panels():
    p = load_profile("auto_long")
    segs = _segments(p)
    for s in segs:
        s.text, s.fact_refs = "续航835公里。", ["f_1"]
    with looks.use("lite"):
        specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                      llm=FakeLLM([]))
    assert "panel" not in {s.layout for s in specs}


def test_rhythm_counts_cuts_as_change_under_lite():
    specs = [_spec(n, duration=8.0) for n in range(10)]
    assert rhythm.blockers(specs)  # 全 broll,hud 拦
    with looks.use("lite"):
        assert rhythm.blockers(specs) == []
        assert rhythm.problems(specs) == []
        assert rhythm.same_run(specs)[0] == 1


def test_rhythm_warns_when_overlays_crowd_lite():
    specs = [_spec(0, "highlight", duration=4.0), _spec(1, duration=4.0)]
    with looks.use("lite"):
        assert rhythm.info_seconds_ratio(specs) == 0.5
        assert "信息叠加" in rhythm.problems(specs)[0]
        assert rhythm.report(specs)["info_second_ratio"] == 0.5


def test_lite_panels_do_not_count_as_overlay():
    specs = [_spec(0, "panel", duration=4.0), _spec(1, duration=4.0)]
    with looks.use("lite"):
        assert rhythm.info_seconds_ratio(specs) == 0.0
    with looks.use("hud"):
        assert rhythm.info_seconds_ratio(specs) == 0.5


@pytest.mark.parametrize("ratio,warns", [(0.5, False), (0.51, True), (None, False)])
def test_still_warning_threshold(ratio, warns):
    assert bool(explainer.still_warning({"still_ratio": ratio})) is warns


def test_full_rerender_resets_footage_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    db.init_db()
    db.write_json("p", "beats_used.json", {"clip": 44})
    db.write_json("p", "beats_recent.json", ["clip"])
    monkeypatch.setattr(db, "delete_shots", lambda *a, **k: None)
    monkeypatch.setattr(db, "upsert_shots", lambda *a, **k: None)
    explainer.init_beats("p", [_spec(0)])
    assert db.read_json("p", "beats_used.json", None) == {}
    assert db.read_json("p", "beats_recent.json", None) == []


def test_lite_skips_dark_spans_of_any_video(data_dir, tmp_path, monkeypatch):
    src = tmp_path / "teaser.mp4"
    src.write_bytes(b"x")
    items = [Item(id=n, kind="video", path=str(src), start=s, end=s + 4.0, tagged=True, target="yes",
                  official=True, view="exterior_45", desc="整车")
             for n, s in (("title", 10.0), ("car", 30.0))]
    _pool(data_dir, items=items)
    monkeypatch.setattr(select, "_span_dark", lambda path, start, length, launch=True: start < 20.0)
    with looks.use("lite"):
        got = select.fill("测试车", 3.0, hint="exterior")
    assert got and all(p.start >= 30.0 for p in got)


def test_chapter_starts_skip_the_opening_chapter():
    specs = [_spec(n, chapter=c, duration=2.0) for n, c in enumerate("AABBC")]
    assert explainer.chapter_starts(specs) == [2, 4]


def _clip(path, color: str) -> str:
    proc = render._run(["-y", "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=320x180:r=24:d=2",
                        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)])
    assert proc.returncode == 0, proc.stderr
    return path


@needs_ffmpeg
def test_dissolve_keeps_length_and_audio(tmp_path):
    a, b = _clip(tmp_path / "a.mp4", "red"), _clip(tmp_path / "b.mp4", "blue")
    out = render._dissolve_heads([a, b], [1], tmp_path)
    assert out[0] == a and out[1] != b
    assert timeline.probe_safe(out[1], default=0.0) == pytest.approx(2.0, abs=0.1)
    decoded = render._run(["-v", "error", "-i", str(out[1]), "-f", "null", "-"])
    assert decoded.returncode == 0 and not decoded.stderr


@needs_ffmpeg
def test_dissolve_failure_leaves_the_hard_cut(tmp_path):
    a = _clip(tmp_path / "a.mp4", "red")
    missing = tmp_path / "missing.mp4"
    assert render._dissolve_heads([a, missing], [1], tmp_path) == [a, missing]
