"""成片验收(edit.style_check):和对标基线比一遍,偏离只出提示。style_metrics 全部打桩。"""
from __future__ import annotations

import pytest

from app.pipeline import teardown
from app.pipeline.edit import style_check, targets

#: 和讲解 4 条同一个量级的成片
GOOD = {"hook_median": 2.7, "body_p10": 0.8, "body_median": 1.7, "body_p90": 3.4,
        "cuts_per_minute": 28.0, "dynamic_ratio": 0.8, "still_ratio": 0.15, "lufs": -14.1,
        "true_peak": -1.5, "quiet_gap_db": 25.0, "cps": 6.0}


def _metrics(**over):
    """style_metrics 的返回形状(见 teardown.style_metrics)。"""
    m = {"duration": 60.0, "width": 1920, "height": 1080, "fps": 30.0,
         "cuts": [round(k * 2.1, 2) for k in range(1, 29)], "cuts_per_minute": 28.0,
         "hook": {"shots": 2, "p10": 2.5, "median": 2.7, "p90": 2.9},
         "body": {"shots": 26, "p10": 0.8, "median": 1.7, "p90": 3.4},
         "motion": [], "dynamic_ratio": 0.8, "still_ratio": 0.15,
         "lufs": -14.1, "lra": 6.0, "true_peak": -1.5, "music_bed": True,
         "music_bed_detail": {"verdict": True, "quiet_gap_db": 25.0}}
    m.update(over)
    return m


CUES = [{"start": 1.0, "end": 4.0, "text": "这台车 起售价10.98万"},   # 去空白 12 字
        {"start": 4.0, "end": 6.0, "text": "续航530公里。"}]          # 8 字


def test_benchmark_like_film_passes_every_rule():
    checks = style_check.compare(GOOD)
    assert [c["key"] for c in checks] == [r.key for r in style_check.RULES]
    assert all(c["ok"] and not c["hint"] for c in checks)


def test_old_flat_film_gets_hints():
    """老片子:匀速 1.3 秒一刀、停顿是干的、语速 7.2。"""
    old = GOOD | {"body_p10": 1.26, "body_p90": 3.0, "quiet_gap_db": 61.4, "cps": 7.22}
    off = {c["key"]: c for c in style_check.compare(old) if not c["ok"]}
    assert set(off) == {"body_p10", "quiet_gap_db", "cps"}
    assert off["body_p10"]["hint"] == "缺快切,节奏是匀速的"
    assert style_check.warning(off["cps"]) == "语速 7.22 字/秒(对标 5.7~6.5 字/秒):语速偏快"
    assert style_check.warning(off["quiet_gap_db"]).startswith(
        "停顿处比旁白低 61.4 dB(对标 ≤40 dB):")


def test_one_sided_rules_and_rounding():
    checks = {c["key"]: c for c in style_check.compare(
        GOOD | {"dynamic_ratio": 0.4321, "true_peak": -0.2})}
    assert checks["dynamic_ratio"]["value"] == 0.43
    assert checks["dynamic_ratio"]["range"] == [targets.DYNAMIC_MIN, None]
    assert style_check.warning(checks["dynamic_ratio"]) == "动态画面占比 0.43(对标 ≥0.6):画面动得少"
    assert style_check.warning(checks["true_peak"]) == "真峰值 -0.2 dBTP(对标 ≤-1 dBTP):可能削波"


def test_portrait_checks_generic_rules_only_and_no_bgm_skips_music_bed():
    keys = {c["key"] for c in style_check.compare(GOOD, portrait=True)}
    assert keys == {"dynamic_ratio", "still_ratio", "lufs", "true_peak", "quiet_gap_db"}
    keys = {c["key"] for c in style_check.compare(GOOD | {"quiet_gap_db": 61.0}, bgm=False)}
    assert "quiet_gap_db" not in keys
    # 量不出来的(None)跳过,不算偏离
    assert "hook_median" not in {c["key"] for c in style_check.compare(GOOD | {"hook_median": None})}


def test_cps_counts_cue_chars_over_spoken_span():
    assert style_check.cps(CUES) == pytest.approx(20 / 5.0)
    assert style_check.cps([]) is None
    assert style_check.cps([{"start": 2.0, "end": 2.0, "text": "字"}]) is None


def test_flat_maps_metrics_and_drops_empty_hook():
    values = style_check.flat(_metrics(hook={"shots": 0, "median": 0.0}), CUES)
    assert values["hook_median"] is None
    assert values["body_p90"] == 3.4 and values["quiet_gap_db"] == 25.0
    assert values["cps"] == pytest.approx(4.0)


def test_measure_writes_block_without_cut_list(monkeypatch, tmp_path):
    monkeypatch.setattr(teardown, "style_metrics", lambda p: _metrics(
        body={"shots": 26, "p10": 0.8, "median": 1.6, "p90": 2.27}, cuts_per_minute=36.8))
    video = tmp_path / "output.mp4"
    block = style_check.measure(video, [{"start": 0.0, "end": 10.0, "text": "字" * 60}])
    assert block["video"] == str(video) and block["scope"] == "landscape"
    assert "cuts" not in block["metrics"] and block["metrics"]["cut_count"] == 28
    assert block["metrics"]["cps"] == 6.0
    assert [w.split(" ")[0] for w in block["warnings"]] == ["正片长镜头(p90)", "每分钟切点"]
    assert "静图一刀封顶 2.5 秒" in block["warnings"][0]


def test_measure_never_raises(monkeypatch, tmp_path):
    def boom(p):
        raise RuntimeError("ffprobe 读不出时长")

    monkeypatch.setattr(teardown, "style_metrics", boom)
    block = style_check.measure(tmp_path / "x.mp4", portrait=True)
    assert block["error"] == "ffprobe 读不出时长" and block["scope"] == "portrait"
    assert "checks" not in block


def test_explainer_style_hook_follows_switch_and_look(monkeypatch, tmp_path):
    from app import config
    from app.pipeline import explainer, looks

    seen = {}

    def fake(video, cues, *, portrait, bgm):
        seen.update(cues=cues, portrait=portrait, bgm=bgm)
        return {"warnings": []}

    monkeypatch.setattr(style_check, "measure", fake)
    monkeypatch.setattr(config, "STYLE_CHECK", True)
    monkeypatch.setattr(config, "BGM_SWITCH", None)
    nar = {"cues": CUES}
    with looks.use("commercial"):
        out = explainer._style(tmp_path / "o.mp4", nar, {"aspect": "16:9"})
    assert out == {"style": {"warnings": []}}
    assert seen == {"cues": CUES, "portrait": False, "bgm": bool(looks.get("commercial").bgm)}
    monkeypatch.setattr(config, "BGM_SWITCH", False)
    with looks.use("commercial"):
        explainer._style(tmp_path / "o.mp4", nar, {"aspect": "16:9"})
    assert seen["bgm"] is False
    # 关掉:写 None,重合成时把旧片的结果盖掉
    monkeypatch.setattr(config, "STYLE_CHECK", False)
    assert explainer._style(tmp_path / "o.mp4", nar, {"aspect": "16:9"}) == {"style": None}


def test_output_panel_shows_checks_and_hints():
    from app.templating import templates

    tpl = templates.env.get_template("_output_style.html")
    checks = style_check.compare(GOOD | {"body_p90": 2.27, "cuts_per_minute": 36.8})
    block = {"scope": "landscape", "checks": checks,
             "warnings": [style_check.warning(c) for c in checks if not c["ok"]]}
    html = tpl.render(render={"style": block})
    assert "2 项偏离" in html and html.count('class="is-off"') == 2
    assert "-15~-13" in html and "≥0.6" in html
    assert "每分钟切点:切得太碎" in html
    assert "没量出来" in tpl.render(render={"style": {"error": "ffprobe 读不出时长"}})
    assert tpl.render(render={"style": None}).strip() == ""
    assert tpl.render(render={}).strip() == ""
