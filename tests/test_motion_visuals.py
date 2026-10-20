"""画面去重复:近重复簇、相邻多样性、运镜与转场、broll 样式、章节标牌、概念动画(HyperFrames)。

除了标了 skip 条件的真实渲染,其余不需要网络、node 与额度。
设环境变量 AIMOVICE_MOTION_TESTS=1 会把每个概念动画模板都真渲染一遍(约 2 分钟)。
"""
from __future__ import annotations

import os
import random

import pytest
from PIL import Image

from app import config
from app.categories import load_profile
from app.models import Fact, ShotSpec
from app.pipeline import explainer, library, motion, overlays, render, timeline
from tests.test_explainer import FakeLLM, _facts, _segments

HAS_FFMPEG = bool(config.FFMPEG_PATH and config.FFMPEG_PATH.is_file())
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="没有 ffmpeg")


def _image(path, seed: int, noise: int = 0):
    """渐变 + 色块的测试图;同 seed、加一点噪声 = "几乎同一张"。"""
    rnd = random.Random(seed)
    im = Image.new("RGB", (320, 180))
    px = im.load()
    blocks = [(rnd.randrange(320), rnd.randrange(180), rnd.randrange(40, 120)) for _ in range(6)]
    jitter = random.Random(seed * 31 + noise)
    for x in range(320):
        for y in range(180):
            v = (x * 255 // 320 + y * 40 // 180) % 256
            for bx, by, r in blocks:
                if abs(x - bx) < r // 2 and abs(y - by) < r // 3:
                    v = 255 - v
            if noise:
                v = max(0, min(255, v + jitter.randint(-noise, noise)))
            px[x, y] = (v, v, v)
    im.save(path)
    return path


@pytest.fixture()
def stills_pool(tmp_path, monkeypatch):
    """三张官图:a1 / a2 几乎同一张(同角度的棚拍),b 是另一张。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    a1 = _image(tmp_path / "a1.png", 1)
    a2 = _image(tmp_path / "a2.png", 1, noise=6)
    b = _image(tmp_path / "b.png", 2)
    lib = library.Library(subject="测试车")
    lib.items = [library.Item(id=i, kind="still", path=str(p), tagged=True, view="exterior_45",
                              target="yes", official=True)
                 for i, p in (("a1", a1), ("a2", a2), ("b", b))]
    lib.save()
    return lib


# --------------------------------------------------------------------------
# 去重与相邻多样性
# --------------------------------------------------------------------------
def test_near_dup_groups_cluster_lookalike_stills(stills_pool):
    groups = library.near_dup_groups(stills_pool.items)
    assert groups["a1"] == groups["a2"]
    assert groups["b"] != groups["a1"]


def test_fill_counts_usage_per_cluster(stills_pool):
    used: dict[str, int] = {}
    first = library.fill("测试车", 3.0, used=used, recent=[])
    second = library.fill("测试车", 3.0, used=used, recent=[])
    ids = {first[0].item_id, second[0].item_id}
    # 用掉 a1(或 a2)之后,下一拍先上 b,而不是那张"几乎一样"的
    assert "b" in ids and len(ids & {"a1", "a2"}) == 1


def test_fill_still_covers_duration_when_pool_is_tiny(stills_pool):
    used = {"a1": 5, "a2": 5, "b": 5}
    pieces = library.fill("测试车", 20.0, used=used, recent=["a1", "b"])
    assert sum(p.length for p in pieces) == pytest.approx(20.0)


def test_fill_avoids_same_view_as_previous_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    lib = library.Library(subject="测试车")

    def item(id_, start, view):
        return library.Item(id=id_, kind="video", path=str(vid), tagged=True, view=view,
                            target="yes", start=start, end=start + 3, source="s")

    lib.items = [item("front1", 0, "exterior_front"), item("front2", 3, "exterior_front"),
                 item("rear1", 6, "rear")]
    lib.save()
    recent = ["front1"]
    pieces = library.fill("测试车", 2.0, used={"front1": 1}, recent=recent)
    assert pieces[0].item_id == "rear1"           # 上一刀是车头,这一刀换个视角
    assert recent[-1] == "rear1"                  # 就地追加,跨拍共享
    assert len(recent) <= library.RECENT_PIECES


# --------------------------------------------------------------------------
# 运镜与转场
# --------------------------------------------------------------------------
def test_motion_for_never_repeats_previous_cut():
    prev = ""
    for n in range(40):
        m = explainer.motion_for("still_x", n, prev)
        assert m in render.STILL_MOTIONS and m != prev
        prev = m
    assert len({explainer.motion_for(f"id{k}", 0) for k in range(30)}) >= 5


def test_xfade_offsets_keep_total_length():
    lengths = [3.0, 2.5, 4.0]
    offsets = explainer.xfade_offsets(lengths)
    assert offsets == [3.0, 5.5]
    extended = [x + explainer.XFADE for x in lengths[:-1]] + [lengths[-1]]
    # 每次转场重叠 XFADE:拼完 = Σ加长后的段 − 转场次数 × XFADE = Σ原长
    assert sum(extended) - explainer.XFADE * len(offsets) == pytest.approx(sum(lengths))


@needs_ffmpeg
@pytest.mark.parametrize("motion_name", render.STILL_MOTIONS)
def test_every_still_motion_renders(tmp_path, motion_name):
    src = _image(tmp_path / "s.png", 3)
    dest = render.still_to_clip(src, tmp_path / "m.mp4", seconds=1.0, motion=motion_name,
                                size=(320, 180))
    assert timeline.probe_safe(dest, default=0.0) == pytest.approx(1.0, abs=0.1)


@needs_ffmpeg
def test_background_clip_with_xfade_has_exact_duration(stills_pool, tmp_path):
    pieces = [library.Piece("a1", "still", stills_pool.items[0].path, 0.0, 1.5),
              library.Piece("b", "still", stills_pool.items[2].path, 0.0, 1.7),
              library.Piece("a2", "still", stills_pool.items[1].path, 0.0, 1.3)]
    out = explainer.background_clip(pieces, 4.5, (320, 180), tmp_path / "w", motion_seed=2)
    assert timeline.probe_safe(out, default=0.0) == pytest.approx(4.5, abs=1 / render.FPS + 0.01)


# --------------------------------------------------------------------------
# broll 样式与章节标牌
# --------------------------------------------------------------------------
def _brolls(chapters):
    return [ShotSpec(idx=i, visual_zh="[broll] x", narration_zh="a", layout="broll", chapter=c)
            for i, c in enumerate(chapters)]


def test_assign_broll_styles_rotates_without_back_to_back_splits():
    specs = explainer.assign_broll_styles(_brolls(["开头"] + ["外观"] * 8))
    styles = [explainer.broll_style(s) for s in specs]
    assert {"plain", "lower_third", "split"} <= set(styles)
    assert not any(a == b == "split" for a, b in zip(styles, styles[1:]))
    # 确定性:同样的分镜两次分配结果一样
    again = [explainer.broll_style(s) for s in explainer.assign_broll_styles(_brolls(["开头"] + ["外观"] * 8))]
    assert again == styles


def test_no_lower_third_on_first_beat_of_chapter():
    specs = explainer.assign_broll_styles(_brolls(["开头", "外观", "外观", "内饰", "内饰"]))
    firsts = [0, 1, 3]
    assert all(explainer.broll_style(specs[i]) != "lower_third" for i in firsts)


def test_sting_only_on_first_beat_of_later_chapters():
    specs = [ShotSpec(idx=i, visual_zh="x", chapter=c, duration=3)
             for i, c in enumerate(["开头", "开头", "外观", "外观", "结论"])]
    chapters, where = explainer.chapters_of(specs)
    got = [explainer.sting_for(specs, where, chapters, s.idx) for s in specs]
    assert got == [None, None, (2, "外观"), None, (3, "结论")]


def test_sting_frames_wipe_in_then_fade_to_transparent(tmp_path):
    frames = overlays.chapter_sting_frames(2, "外观内饰", (1920, 1080), tmp_path)
    total = sum(d for _, d in frames)
    assert 1.0 <= total <= 1.4
    last = Image.open(frames[-1][0])
    assert last.getchannel("A").getextrema() == (0, 0)
    peak = Image.open(frames[overlays.STING_WIPE_STEPS - 1][0])
    assert peak.getchannel("A").getextrema()[1] > 0


def test_with_sting_appends_overlay_on_out():
    g = explainer.with_sting(explainer.layout_filter("broll", (1920, 1080), False), 2)
    assert g.endswith("[out]") and "[2:v]format=rgba[st]" in g and "eof_action=pass" in g
    assert g.count("[out]") == 1


@needs_ffmpeg
@pytest.mark.parametrize("style", ["split", "lower_third"])
def test_render_broll_styles_with_sting(stills_pool, tmp_path, style):
    spec = ShotSpec(idx=1, duration=3.0, visual_zh="[broll] x", narration_zh="a", layout="broll",
                    overlay={"kind": "broll", "style": style}, chapter="外观", view="exterior")
    dest, pieces = explainer.render_beat(spec, subject="测试车", size=(640, 360), out_dir=tmp_path,
                                         chapters=[("开头", 2.0), ("外观", 3.0)], chapter_idx=1,
                                         used={}, card_bg=None, recent=[],
                                         sting=(2, "外观") if style == "split" else None)
    assert timeline.probe_safe(dest, default=0.0) == pytest.approx(3.0, abs=0.1)
    if style == "split":
        assert len(pieces) >= 2                   # 分屏的几栏是不同的背景


# --------------------------------------------------------------------------
# 概念动画:分镜核对与降级
# --------------------------------------------------------------------------
def _concept_beats(scene, rows, columns=None):
    p = load_profile("auto_long")
    segs = _segments(p)
    i = next(n for n, s in enumerate(segs) if s.section == "三电性能")
    item = {"idx": i + 1, "layout": "concept",
            "concept": {"scene": scene, "title": "电池", "rows": rows, "columns": columns or []}}
    return explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                 llm=FakeLLM([item]))[i]


def test_concept_beat_is_grounded():
    s = _concept_beats("battery", [{"label": "CLTC续航", "values": ["835公里"], "fact_id": "f_1"}])
    assert s.layout == "concept"
    assert s.overlay["kind"] == "concept" and s.overlay["scene"] == "battery"
    assert s.overlay["rows"][0]["values"] == ["835公里"]
    assert not explainer.validate_beats([s])


def test_concept_with_invented_number_or_unknown_scene_falls_back():
    bad_num = _concept_beats("battery", [{"label": "续航", "values": ["900公里"], "fact_id": "f_1"}])
    assert bad_num.layout == "broll"
    bad_scene = _concept_beats("rocket", [{"label": "CLTC续航", "values": ["835公里"], "fact_id": "f_1"}])
    assert bad_scene.layout == "broll"


def test_enforce_rhythm_spaces_and_caps_concepts():
    ov = {"kind": "concept", "scene": "battery", "rows": []}
    specs = [ShotSpec(idx=i, visual_zh="[concept] x", narration_zh="a", layout="concept",
                      overlay=dict(ov)) for i in range(3)]
    assert [s.layout for s in explainer.enforce_rhythm(specs)] == ["concept", "broll", "concept"]
    many = []
    for i in range(30):
        layout = "concept" if i % 2 == 0 else "panel"
        many.append(ShotSpec(idx=i, visual_zh=f"[{layout}] x", narration_zh="a", layout=layout,
                             overlay=dict(ov)))
    out = explainer.enforce_rhythm(many)
    assert sum(s.layout == "concept" for s in out) == explainer.MAX_CONCEPT


def test_concept_guide_only_for_categories_with_scenes():
    assert "concept" in explainer.concept_guide(load_profile("auto_long").vocab)
    assert explainer.concept_guide(load_profile("baby_long").vocab) == ""


def test_every_declared_scene_has_a_template():
    declared = set(load_profile("auto_long").vocab.concept_scenes)
    assert declared <= motion.available_scenes()
    for name in declared:
        html = (motion.scenes_dir() / f"{name}.html").read_text(encoding="utf-8")
        assert motion.SECONDS_TOKEN in html and motion.DATA_TOKEN in html, name
        assert motion.BACKGROUND_TOKEN in html, name


# --------------------------------------------------------------------------
# 概念动画:渲染模块
# --------------------------------------------------------------------------
def test_compose_html_injects_data_and_escapes_script_end():
    html = motion.compose_html("battery", {"title": "</script><b>x", "rows": []}, 6.5)
    assert 'data-duration="6.500"' in html
    assert "</script><b>" not in html and "<\\/script>" in html


def test_compose_html_background_switches_video_and_fallback():
    with_bg = motion.compose_html("battery", {"rows": []}, 4.0, background=True)
    assert 'src="bg.mp4"' in with_bg and 'data-duration="4.000"' in with_bg
    without = motion.compose_html("battery", {"rows": []}, 4.0)
    assert "bg.mp4" not in without and "nobg" in without
    assert motion.BACKGROUND_TOKEN not in with_bg + without


def test_cache_key_changes_with_background_bytes(tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")
    data = {"rows": []}
    ka = motion.cache_key("battery", data, 4.0, (1920, 1080), a)
    assert ka != motion.cache_key("battery", data, 4.0, (1920, 1080), b)
    assert ka != motion.cache_key("battery", data, 4.0, (1920, 1080))


def test_render_uses_cache_without_running_node(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "motion_status", lambda: (True, "ok"))
    data = {"subject": "车", "title": "t", "rows": []}
    key = motion.cache_key("battery", data, 4.0, (1920, 1080))
    cached = motion.cache_dir() / f"battery_{key}.mp4"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached-mp4")
    monkeypatch.setattr(motion.subprocess, "run", lambda *a, **k: pytest.fail("不该渲染"))
    out = motion.render("battery", data, 4.0, (1920, 1080), tmp_path / "o.mp4")
    assert out.read_bytes() == b"cached-mp4"


def test_render_refuses_when_tooling_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "motion_status", lambda: (False, "未安装 HyperFrames"))
    with pytest.raises(motion.MotionError, match="HyperFrames"):
        motion.render("battery", {}, 4.0, (1920, 1080), tmp_path / "o.mp4")


@needs_ffmpeg
def test_concept_beat_falls_back_to_card_when_motion_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def broken(*a, **k):
        raise motion.MotionError("没有 node")

    monkeypatch.setattr(motion, "render", broken)
    spec = ShotSpec(idx=0, duration=2.0, visual_zh="[concept] x", narration_zh="a",
                    layout="concept",
                    overlay={"kind": "concept", "scene": "battery", "title": "电池",
                             "rows": [{"label": "续航", "values": ["835公里"], "fact_id": "f_1"}],
                             "columns": [], "source": "数据来源"})
    dest, _ = explainer.render_beat(spec, subject="测试车", size=(640, 360), out_dir=tmp_path,
                                    chapters=[("开头", 2.0)], chapter_idx=0, used={},
                                    card_bg=None)
    assert timeline.probe_safe(dest, default=0.0) == pytest.approx(2.0, abs=0.1)


MOTION_OK = config.motion_status()[0] and HAS_FFMPEG
ALL_SCENES = os.getenv("AIMOVICE_MOTION_TESTS") == "1"


@pytest.mark.skipif(not MOTION_OK, reason="没有 node / HyperFrames / Chrome")
@pytest.mark.parametrize("scene", sorted(load_profile("auto_long").vocab.concept_scenes))
def test_scene_renders_with_hyperframes(scene, tmp_path, monkeypatch):
    if scene != "battery" and not ALL_SCENES:
        pytest.skip("只默认真渲染一个模板;AIMOVICE_MOTION_TESTS=1 全渲染")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    rows = [{"label": "续航", "values": ["835公里", "700公里"]},
            {"label": "售价", "values": ["25.35万元", "26万元"]}]
    data = motion.scene_data({"title": "测试", "rows": rows, "columns": ["A", "B"]}, "测试车")
    out = motion.render(scene, data, 2.0, (1920, 1080), tmp_path / f"{scene}.mp4")
    assert timeline.probe_safe(out, default=0.0) == pytest.approx(2.0, abs=0.1)
