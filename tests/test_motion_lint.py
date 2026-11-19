"""项目内现写的概念动画片段:motion_lint 硬校验 + motion 读项目片段 / 缓存键 + 逐拍渲染优先用片段。

不调用 node:渲染用 monkeypatch 替掉 `_run_cli`。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import config, db
from app.models import Fact, ShotSpec
from app.pipeline import motion, motion_lint
from app.pipeline.explainer import beat_render

GOOD = """<!-- Project scene: range bar grows to the fact value. @portrait yes -->
<meta name="hf-bg" content="studio" />
<style>
  .rb { position: absolute; left: 10%; top: 45%; height: 40px; background: #4cf; }
</style>
<script>
  const { tl, D, S } = HF.boot();
  const bar = document.createElement("div");
  bar.className = "rb";
  HF.add(bar);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  tl.fromTo(bar, { width: 0 }, { width: "60%", duration: 1.2, ease: "expo.out" }, 0.3);
  tl.to(bar, { opacity: 0, duration: 0.4 }, S - 0.4);
  HF.register(tl);
</script>
"""


def _facts() -> list[Fact]:
    return [Fact(id="f_1", claim="CLTC 续航 835 公里", source_tool="t", scope="product"),
            Fact(id="f_2", claim="电池容量 96.3kWh", source_tool="t", scope="product")]


def _data() -> dict:
    return {"subject": "小米 SU7", "title": "续航",
            "rows": [{"label": "续航", "value": "835公里", "num": 835, "ratio": 0.83, "at": 1.2},
                     {"label": "电池", "value": "96.3kWh", "num": 96.3}]}


def _errors(issues):
    return [i.message for i in issues if i.is_error]


# --------------------------------------------------------------------------
# lint
# --------------------------------------------------------------------------
def test_good_fragment_has_no_errors():
    issues = motion_lint.lint(GOOD, _data(), _facts(), 4.0, (1920, 1080))
    assert _errors(issues) == []


def test_non_ascii_is_error_even_in_comments():
    frag = GOOD.replace("Project scene", "项目场景")
    errs = _errors(motion_lint.lint(frag, _data(), _facts(), 4.0))
    assert any("非 ASCII" in e for e in errs)


@pytest.mark.parametrize("ref", [
    '<script src="https://cdn.jsdelivr.net/npm/gsap@3/dist/gsap.min.js"></script>',
    '<script src="//cdn.example.com/x.js"></script>',
    '<style>@import url("http://fonts.example.com/a.css");</style>',
])
def test_external_resources_are_errors(ref):
    errs = _errors(motion_lint.lint(ref + "\n" + GOOD, _data(), _facts(), 4.0))
    assert any("外部网络资源" in e for e in errs)


def test_runtime_fetch_is_error():
    frag = GOOD.replace("HF.register(tl);", "fetch('data.json'); HF.register(tl);")
    assert any("运行时拉资源" in e for e in _errors(motion_lint.lint(frag, {}, [], 4.0)))


def test_only_shell_files_and_registry_components_may_be_referenced():
    ok = '<meta name="x" content="y" /><script src="three.min.js"></script>\n' + GOOD
    assert _errors(motion_lint.lint(ok, {}, [], 4.0)) == []
    comp = sorted(motion_lint.registry_files())
    if comp:
        with_comp = GOOD.replace('<meta name="hf-bg" content="studio" />',
                                 f'<div data-composition-src="compositions/{comp[0]}"></div>')
        assert _errors(motion_lint.lint(with_comp, {}, [], 4.0)) == []
    bad = '<script src="my_lib.js"></script>\n' + GOOD
    assert any("shell 不提供" in e for e in _errors(motion_lint.lint(bad, {}, [], 4.0)))


def test_svg_namespace_and_local_ids_are_not_network():
    frag = GOOD.replace("HF.register(tl);",
                        'bar.style.fill = "url(#grad)"; HF.register(tl);')
    assert _errors(motion_lint.lint(frag, {}, [], 4.0)) == []


def test_timeline_much_longer_than_beat_is_error():
    frag = GOOD.replace("duration: 1.2", "duration: 9")
    assert motion_lint.timeline_end(frag, 4.0) == pytest.approx(9.3)
    errs = _errors(motion_lint.lint(frag, {}, [], 4.0))
    assert any("明显超过拍长" in e for e in errs)
    # 按拍长 S 写的收尾刚好落在拍尾,不报
    assert motion_lint.timeline_end(GOOD, 4.0) == pytest.approx(4.0)


def test_appended_tweens_without_position_stack_up():
    frag = GOOD.replace(", 0.3);", ");").replace(", S - 0.4);", ");")
    frag = frag.replace("HF.register(tl);",
                        "tl.to(bar, { x: 10, duration: 3 }); HF.register(tl);")
    assert motion_lint.timeline_end(frag, 4.0) == pytest.approx(1.2 + 0.4 + 3)


def test_data_number_not_in_facts_is_error():
    data = _data()
    data["rows"][0]["value"] = "900公里"
    errs = _errors(motion_lint.lint(GOOD, data, _facts(), 4.0))
    assert any("900" in e and "找不到" in e for e in errs)


def test_numeric_field_not_in_facts_is_error_but_layout_fields_are_not():
    data = {"title": "续航", "big": 700, "at": 3.7, "ratio": 0.42, "rows": []}
    errs = _errors(motion_lint.lint(GOOD, data, _facts(), 4.0))
    assert len(errs) == 1 and "700" in errs[0]


def test_low_confidence_fact_only_warns_and_disabled_fact_does_not_count():
    facts = [Fact(id="f_9", claim="零百 3.2 秒", source_tool="t", confidence="low"),
             Fact(id="f_8", claim="续航 700 公里", source_tool="t", enabled=False)]
    issues = motion_lint.lint(GOOD, {"title": "3.2秒"}, facts, 4.0)
    assert _errors(issues) == [] and any("低置信度" in i.message for i in issues)
    assert _errors(motion_lint.lint(GOOD, {"title": "700公里"}, facts, 4.0))


def test_model_codes_and_years_are_names_not_numbers():
    data = {"subject": "问界M9", "title": "2026款 SU7 续航", "rows": []}
    assert _errors(motion_lint.lint(GOOD, data, [], 4.0)) == []


def test_bad_size_and_missing_blocks_are_errors():
    assert _errors(motion_lint.lint(GOOD, {}, [], 4.0, (640, 360)))
    assert any("合法片段" in e for e in _errors(motion_lint.lint("<div></div>", {}, [], 4.0)))


# --------------------------------------------------------------------------
# motion:读项目片段、缓存键
# --------------------------------------------------------------------------
def _write_scene(pid: str, beat, text: str = GOOD) -> Path:
    path = motion.project_scene_path(pid, beat)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_project_scene_path_follows_project_dir():
    path = motion.project_scene_path("p_demo", 3)
    assert path == config.project_dir("p_demo") / "motion" / "003.html"
    assert motion.project_scene_path("p_demo", "3") == path
    assert motion.project_scene("p_demo", 3) is None
    _write_scene("p_demo", 3)
    assert motion.project_scene("p_demo", "003") == path
    with pytest.raises(ValueError):
        motion.beat_key("../")


def test_compose_html_reads_project_fragment_and_ignores_style():
    path = _write_scene("p_demo", 1)
    html = motion.compose_html(path, {"title": "续航"}, 4.0, style="cinema")
    assert 'bar.className = "rb"' in html and "<!--__JS__-->" not in html
    assert '"title": "续航"' in html and 'data-duration="4.000"' in html
    assert motion.scene_background(path, "cinema") == "studio"
    assert motion.supports_portrait("cinema", path)


def test_cache_key_changes_when_fragment_changes():
    path = _write_scene("p_demo", 2)
    data = {"title": "续航"}
    k1 = motion.cache_key(path, data, 4.0, (1920, 1080))
    assert k1 == motion.cache_key(path, data, 4.0, (1920, 1080))
    path.write_text(GOOD.replace("#4cf", "#f80"), encoding="utf-8")
    k2 = motion.cache_key(path, data, 4.0, (1920, 1080))
    assert k2 != k1
    names = motion.snapshot_names(path, data, 4.0, [1.0], (1920, 1080), Path("."))
    assert names[0].name.startswith("002_studio_" + k2)


def _fake_render(monkeypatch, tmp_path, calls: list):
    gsap = tmp_path / "gsap.min.js"
    gsap.write_text("/* gsap */", encoding="utf-8")
    monkeypatch.setattr(motion, "_gsap", lambda: gsap)
    monkeypatch.setattr(config, "motion_status", lambda: (True, "ok"))

    def run_cli(scene, work, out, seconds):
        calls.append((scene, (work / "index.html").read_text(encoding="utf-8")))
        out.write_bytes(b"mp4-" + str(len(calls)).encode())
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(motion, "_run_cli", run_cli)


def test_render_project_scene_uses_cache_until_fragment_changes(tmp_path, monkeypatch):
    calls: list = []
    _fake_render(monkeypatch, tmp_path, calls)
    path = _write_scene("p_demo", 4)
    data = {"title": "续航"}
    out = motion.render(path, data, 4.0, (1920, 1080), tmp_path / "a.mp4")
    assert out.read_bytes() == b"mp4-1" and len(calls) == 1
    assert calls[0][0] == path and 'bar.className = "rb"' in calls[0][1]
    motion.render(path, data, 4.0, (1920, 1080), tmp_path / "b.mp4")
    assert len(calls) == 1                                  # 命中缓存
    path.write_text(GOOD.replace("1.2", "1.5"), encoding="utf-8")
    again = motion.render(path, data, 4.0, (1920, 1080), tmp_path / "c.mp4")
    assert len(calls) == 2 and again.read_bytes() == b"mp4-2"
    assert any(p.name.startswith("004_") for p in motion.cache_dir().glob("*.mp4"))


def test_project_scene_data_sidecar(tmp_path):
    path = _write_scene("p_demo", 5)
    assert motion.project_scene_data(path) == {}
    path.with_suffix(".json").write_text(json.dumps({"words": {"rotor": "转子"}}), encoding="utf-8")
    assert motion.project_scene_data(path) == {"words": {"rotor": "转子"}}
    path.with_suffix(".json").write_text("{broken", encoding="utf-8")
    assert motion.project_scene_data(path) == {}


# --------------------------------------------------------------------------
# 逐拍渲染:有项目片段就优先用,失败照样退回数据卡
# --------------------------------------------------------------------------
class _Stop(Exception):
    pass


def _concept_spec(idx: int) -> ShotSpec:
    return ShotSpec(idx=idx, duration=2.0, visual_zh="[concept] x", narration_zh="a",
                    layout="concept",
                    overlay={"kind": "concept", "scene": "battery", "title": "电池",
                             "rows": [{"label": "续航", "values": ["835公里"], "fact_id": "f_1"}],
                             "columns": [], "source": "数据来源"})


def _patch_beat(monkeypatch, seen: dict, fail: bool):
    def fake_inputs(*a, **k):
        seen["inputs_scene"] = k.get("scene")
        return None, None, []

    monkeypatch.setattr(beat_render, "concept_inputs", fake_inputs)

    def fake_render(scene, data, *a, **k):
        seen["scene"], seen["data"], seen["style"] = scene, data, k.get("style")
        if fail:
            raise motion.MotionError("渲染炸了")
        raise _Stop

    def fake_card(*a, **k):
        seen["card"] = True
        raise _Stop

    notes: list = []
    monkeypatch.setattr(beat_render.motion, "render", fake_render)
    monkeypatch.setattr(beat_render.cards, "render", fake_card)
    monkeypatch.setattr(beat_render.degrade, "note", lambda kind, detail: notes.append((kind, detail)))
    return notes


def _run_beat(spec, tmp_path, project_id):
    with pytest.raises(_Stop):
        beat_render.render_beat(spec, subject="测试车", size=(1920, 1080), out_dir=tmp_path / "out",
                                chapters=[("开头", 2.0)], chapter_idx=0, used={}, card_bg=None,
                                style="cinema", project_id=project_id)


def test_concept_beat_prefers_project_fragment(tmp_path, monkeypatch):
    # 渲染时片段要过 lint:上屏的 835 得有事实撑着
    db.write_json("p_beat", "facts.json", [Fact(id="f_1", claim="续航 835公里", source_tool="t",
                                               scope="product", basis="CLTC",
                                               source_kind="official").model_dump()])
    path = _write_scene("p_beat", 7)
    path.with_suffix(".json").write_text(json.dumps({"words": {"a": "转子"}}), encoding="utf-8")
    seen: dict = {}
    _patch_beat(monkeypatch, seen, fail=False)
    _run_beat(_concept_spec(7), tmp_path, "p_beat")
    assert seen["scene"] == path and seen["inputs_scene"] == path
    assert seen["style"] == motion.DEFAULT_STYLE
    assert seen["data"]["words"] == {"a": "转子"} and seen["data"]["title"] == "电池"
    assert "card" not in seen


def test_concept_beat_without_fragment_uses_library_scene(tmp_path, monkeypatch):
    seen: dict = {}
    _patch_beat(monkeypatch, seen, fail=False)
    _run_beat(_concept_spec(8), tmp_path, "p_beat")
    assert seen["scene"] == "battery" and seen["style"] == "cinema"


def test_project_fragment_failure_falls_back_to_card_and_notes(tmp_path, monkeypatch):
    _write_scene("p_beat", 9)
    seen: dict = {}
    notes = _patch_beat(monkeypatch, seen, fail=True)
    _run_beat(_concept_spec(9), tmp_path, "p_beat")
    assert seen.get("card") is True
    assert notes and notes[0][0] == "concept.to_card" and "009.html" in notes[0][1]
