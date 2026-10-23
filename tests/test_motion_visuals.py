"""画面去重复:近重复簇、相邻多样性、运镜与转场、broll 样式、章节标牌、概念动画(HyperFrames)。

除了标了 skip 条件的真实渲染,其余不需要网络、node 与额度。
设环境变量 AIMOVICE_MOTION_TESTS=1 会把每个概念动画模板都真渲染一遍(约 2 分钟)。
"""
from __future__ import annotations

import os
import random
from pathlib import Path

import pytest
from PIL import Image

from app import config
from app.categories import load_profile
from app.models import Fact, ScriptSegment, ShotSpec
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
# 黄金三秒:开场 hook 版式
# --------------------------------------------------------------------------
def _hook_overlay(**kw):
    return {"kind": "hook", "lines": ["激光雷达", "全系标配"], **kw}


def test_hook_beat_is_grounded_and_only_first_beat_keeps_it():
    p = load_profile("auto_long")
    segs = _segments(p)
    segs[0].text = "全系标配激光雷达,这价位少见。"
    items = [{"idx": 1, "layout": "hook", "keywords": ["激光雷达", "全系标配"]},
             {"idx": 2, "layout": "hook", "keywords": ["外观"]}]
    got = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                llm=FakeLLM(items))
    assert got[0].layout == "hook" and got[0].overlay["lines"] == ["激光雷达", "全系标配"]
    assert got[1].layout != "hook"            # 开场之外的 hook 不成立
    assert explainer.validate_beats(got) == []


def test_hook_with_invented_keywords_falls_back_to_broll():
    p = load_profile("auto_long")
    segs = _segments(p)
    items = [{"idx": 1, "layout": "hook", "keywords": ["高通8650"]}]
    got = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                llm=FakeLLM(items))
    assert got[0].layout == "broll" and got[0].overlay is None


def test_hook_lines_do_not_repeat_narration_or_the_stat_number():
    p = load_profile("auto_long")
    segs = _segments(p)
    segs[0].text = "周六早上,一家人在服务区排队等充电桩。"
    items = [{"idx": 1, "layout": "hook",
              "keywords": ["服务区排队等充电桩", "835公里够用吗"],
              "stat": {"label": "CLTC 续航", "value": "835 公里", "fact_id": "f_1"}}]
    got = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                llm=FakeLLM(items))
    # 第一行照抄旁白、第二行重复大数字里的 835:都不上屏,只留大数字
    assert got[0].layout == "hook" and got[0].overlay["lines"] == []
    assert got[0].overlay["stat"]["value"] == "835 公里"


def test_hook_stat_must_come_from_facts():
    p = load_profile("auto_long")
    segs = _segments(p)
    good = {"idx": 1, "layout": "hook", "keywords": ["续航"],
            "stat": {"label": "CLTC 续航", "value": "835 公里", "fact_id": "f_1"}}
    got = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                llm=FakeLLM([good]))
    assert got[0].overlay["stat"]["value"] == "835 公里"
    bad = {**good, "stat": {"label": "续航", "value": "999 公里", "fact_id": "f_1"}}
    got = explainer.build_beats(subject="小米YU7", profile=p, segments=segs, facts=_facts(),
                                llm=FakeLLM([bad]))
    assert "stat" not in got[0].overlay       # 数字核对不过只丢数字,关键词保留


def test_hook_frames_pop_hold_then_transparent(tmp_path):
    frames = overlays.hook_frames(_hook_overlay(), "测试车", (640, 360), tmp_path, 3.0)
    assert sum(d for _, d in frames) == pytest.approx(3.0, abs=0.05)
    first = Image.open(frames[0][0])
    assert first.getchannel("A").getbbox() is not None      # 0 秒起就有字,没有淡入
    assert Image.open(frames[-1][0]).getchannel("A").getbbox() is None   # 最后一帧全透明


def test_hook_filter_holds_bar_for_three_seconds():
    g = explainer.layout_filter("hook", (1920, 1080), True)
    assert g.endswith("[out]") and "enable='lt(t,3.0)'" in g and "fade=in:st=3.0" in g


@needs_ffmpeg
def test_render_hook_beat_cuts_every_second(stills_pool, tmp_path):
    spec = ShotSpec(idx=0, duration=5.0, visual_zh="[hook] x", narration_zh="a", layout="hook",
                    overlay=_hook_overlay(), chapter="开头", view="exterior")
    dest, pieces = explainer.render_beat(spec, subject="测试车", size=(640, 360), out_dir=tmp_path,
                                         chapters=[("开头", 5.0)], chapter_idx=0,
                                         used={}, card_bg=None, recent=[], sting=None)
    assert timeline.probe_safe(dest, default=0.0) == pytest.approx(5.0, abs=0.1)
    assert len(pieces) >= 3                   # 前 3 秒至少三刀


def test_hook_lint_flags_long_or_filler_first_sentence():
    from app.pipeline import script_gen
    long = [ScriptSegment(idx=0, section="开场", text="这是一句非常非常非常非常长的开场白根本读不完。")]
    assert script_gen.hook_lint(long)
    filler = [ScriptSegment(idx=0, section="开场", text="大家好,今天聊YU7。")]
    assert script_gen.hook_lint(filler)
    ok = [ScriptSegment(idx=0, section="开场", text="这价位竟然标配激光雷达。后面详细说。")]
    assert script_gen.hook_lint(ok) == []


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


def test_concept_with_invented_number_or_unknown_scene_never_renders_as_concept():
    bad_num = _concept_beats("battery", [{"label": "续航", "values": ["900公里"], "fact_id": "f_1"}])
    # 编造的数字:整拍没有任何可上屏的数据,只能退回 broll
    assert bad_num.layout == "broll"
    bad_scene = _concept_beats("rocket", [{"label": "CLTC续航", "values": ["835公里"], "fact_id": "f_1"}])
    # 模板不存在:这一拍不再是 rocket(那行核对过的数据也没跟着一起扔掉)。
    # 它可能被兜底成另一张**存在的**模板,或者退回面板 —— 但绝不能是 rocket。
    assert bad_scene.overlay.get("scene") != "rocket"
    if bad_scene.layout == "concept":
        assert bad_scene.overlay["scene"] in load_profile("auto_long").vocab.concept_scenes
    assert "835" in str(bad_scene.overlay)


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
        # 占位符在骨架里,片段拼装后必须齐全
        html = motion.compose_html(name, {"rows": []}, 6.0)
        assert 'data-duration="6.000"' in html, name
        assert 'data-width="1920"' in html and 'data-height="1080"' in html, name
        assert '<script src="_lib.js"></script>' in html, name


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


def _filter_pool(tmp_path, monkeypatch, clean_len):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    lib = library.Library(subject="测试车")

    def item(id_, start, length, **kw):
        return library.Item(id=id_, kind="video", path=str(vid), tagged=True, view="driving",
                            target="yes", start=start, end=start + length, source="s_" + id_, **kw)

    lib.items = [item("clean", 0, clean_len), item("host", 100, 5, desc="博主出镜讲解车内空间"),
                 item("wm", 200, 5, problem="text_overlay")]
    lib.save()


def test_fill_excludes_host_and_third_party_watermark_when_clean_pool_is_enough(tmp_path, monkeypatch):
    _filter_pool(tmp_path, monkeypatch, clean_len=library.HARD_FILTER_MIN_SECONDS + 5)
    ids = {p.item_id for p in library.fill("测试车", 20.0)}
    assert ids == {"clean"}


def test_fill_keeps_dirty_clips_as_fallback_when_clean_pool_is_small(tmp_path, monkeypatch):
    _filter_pool(tmp_path, monkeypatch, clean_len=3)
    ids = {p.item_id for p in library.fill("测试车", 12.0)}
    assert ids & {"host", "wm"}


def test_audio_graph_ducks_bgm_under_narration_without_amix_normalize():
    g, label = render._build_audio_graph(True, True, 0.0)
    assert "asplit=2[narr][narrsc]" in g and "sidechaincompress" in g
    assert "normalize=0" in g and label == "[aout]"


def test_fill_drops_whole_source_when_it_is_a_host_vlog(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    lib = library.Library(subject="测试车")

    def item(id_, src, start, length, desc=""):
        return library.Item(id=id_, kind="video", path=str(vid), tagged=True, view="driving",
                            target="yes", start=start, end=start + length, source=src, desc=desc)

    lib.items = [item("c1", "clean", 0, 70)] + \
        [item("vlog_host", "vlog", 0, 5, "博主出镜讲解")] + \
        [item(f"vlog_{n}", "vlog", 10 + n * 10, 5, "红色车前45度") for n in range(4)]
    lib.save()
    assert {p.item_id for p in library.fill("测试车", 20.0)} == {"c1"}


# --------------------------------------------------------------------------
# 概念动画 v3:车图抠图、模板元数据、长镜头底、数字拆分
# --------------------------------------------------------------------------
def _studio_car(path, facing="right", bg=(250, 250, 250)):
    """白底上的"车":车身 + 驾驶舱(靠车尾一侧高)+ 两个深色轮子。"""
    from PIL import ImageDraw

    im = Image.new("RGB", (800, 450), bg)
    d = ImageDraw.Draw(im)
    d.rectangle((120, 220, 680, 320), fill=(150, 20, 40))           # 车身
    d.rectangle((260, 150, 540, 220), fill=(120, 20, 35))           # 驾驶舱
    # 车头那头削低一截(引擎盖)
    d.rectangle((600, 220, 680, 262) if facing == "right" else (120, 220, 200, 262), fill=bg)
    for cx in (240, 560):
        d.ellipse((cx - 50, 270, cx + 50, 370), fill=(20, 20, 20))  # 轮子伸到最底下
    im.save(path)
    return path


def test_cutout_finds_wheels_floor_and_caches(tmp_path, monkeypatch):
    from app.pipeline import motion_assets

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    c = motion_assets.cutout(_studio_car(tmp_path / "car.png"), item_id="x")
    assert c is not None and Path(c.png).is_file() and Path(c.edges).is_file()
    # 轮心:原图 240 / 560,车身外接框 120–680,左右各留 2% 白边
    left = 120 - int(560 * motion_assets.MARGIN)
    assert c.wheels[0] == pytest.approx((240 - left) / c.width, abs=0.03)
    assert c.wheels[1] == pytest.approx((560 - left) / c.width, abs=0.03)
    assert c.floor > 0.9 and c.roof < 0.1
    with Image.open(c.png) as im:
        assert im.mode == "RGBA" and im.getpixel((2, 2))[3] == 0      # 背景透明
    again = motion_assets.cutout(tmp_path / "car.png", item_id="x")
    assert again == c                                                   # 走缓存


def test_cutout_facing(tmp_path, monkeypatch):
    from app.pipeline import motion_assets

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert motion_assets.cutout(_studio_car(tmp_path / "r.png", "right")).facing == "right"
    assert motion_assets.cutout(_studio_car(tmp_path / "l.png", "left")).facing == "left"


def test_cutout_rejects_non_studio_and_cropped(tmp_path, monkeypatch):
    from app.pipeline import motion_assets

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert motion_assets.cutout(_image(tmp_path / "street.png", 3)) is None   # 不是白底
    im = Image.new("RGB", (400, 300), (250, 250, 250))
    from PIL import ImageDraw
    ImageDraw.Draw(im).rectangle((0, 100, 300, 200), fill=(30, 30, 30))       # 车贴着左边
    im.save(tmp_path / "cut.png")
    assert motion_assets.cutout(tmp_path / "cut.png") is None


def test_hero_still_picks_studio_view(tmp_path, monkeypatch):
    from app.pipeline import motion_assets

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    lib = library.Library(subject="测试车")
    lib.items = [
        library.Item(id="street", kind="still", path=str(_image(tmp_path / "s.png", 4)),
                     tagged=True, view="exterior_side", target="yes", official=True),
        library.Item(id="studio", kind="still", path=str(_studio_car(tmp_path / "c.png")),
                     tagged=True, view="exterior_side", target="yes"),
    ]
    lib.save()
    c = motion_assets.hero_still("测试车", ["exterior_45", "exterior_side"])
    assert c is not None and c.item_id == "studio"
    assert motion_assets.hero_still("测试车", ["interior"]) is None


def test_scene_meta_declares_background_and_photo(tmp_path, monkeypatch):
    assert motion.scene_background("battery") == "studio"
    assert motion.scene_background("accel") == "footage"
    for scene in ("dims", "lidar", "chip", "charging", "compare"):
        assert motion.scene_background(scene) == "studio"
    assert motion.scene_photo_views("battery") == ["exterior_side"]
    assert motion.scene_photo_views("lidar") == ["exterior_side"]
    assert motion.scene_photo_views("compare") == []             # 对比镜不用车图
    # 模板没声明 = 实拍底、不要车图
    (tmp_path / "plain.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(motion, "scenes_dir", lambda: tmp_path)
    assert motion.scene_background("plain") == "footage"
    assert motion.scene_photo_views("plain") == []


def test_compose_html_inlines_base_css_and_photo_geometry(tmp_path, monkeypatch):
    from app.pipeline import motion_assets

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    photo = motion_assets.cutout(_studio_car(tmp_path / "car.png"))
    html = motion.compose_html("battery", {"rows": []}, 5.0, photo=photo)
    # 字体:Montserrat 的声明必须在页面里,HyperFrames 才会内嵌(样式一律内联,不留外链)
    assert '<link rel="stylesheet"' not in html and '"Montserrat"' in html
    assert '"src": "car.png"' in html and '"edges": "car_edges.png"' in html
    assert str(tmp_path) not in html                                   # 不泄漏本机路径
    data = {"rows": []}
    assert motion.cache_key("battery", data, 5.0, (1920, 1080), photo=photo) != \
        motion.cache_key("battery", data, 5.0, (1920, 1080))


def test_long_take_prefers_long_confirmed_moving_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"x")
    lib = library.Library(subject="测试车")

    def item(id_, start, length, view, target="yes"):
        return library.Item(id=id_, kind="video", path=str(vid), tagged=True, view=view,
                            target=target, start=start, end=start + length, source="s_" + id_)

    lib.items = [item("short", 10, 2.0, "driving"), item("static", 20, 9.0, "exterior_45"),
                 item("drive", 40, 6.0, "driving"), item("other_car", 60, 12.0, "driving", "unknown")]
    lib.save()
    used: dict[str, int] = {}
    recent: list[str] = []
    take = library.long_take("测试车", 8.0, hint="exterior", used=used, recent=recent)
    assert take.item_id == "drive" and take.length == pytest.approx(6.0)  # 放慢 1.33 倍铺满
    assert used == {"drive": 1} and recent == ["drive"]
    nxt = library.long_take("测试车", 8.0, hint="exterior", used=used, recent=recent)
    assert nxt.item_id == "static"                                       # 用过的往后排
    assert nxt.length == pytest.approx(8.0) and nxt.start == pytest.approx(20.5)  # 够长就从中间截
    assert library.long_take("测试车", 30.0, hint="exterior") is None   # 放慢也铺不满


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="没有 node")
def test_lib_parts_splits_number_unit_and_tag(tmp_path):
    import json
    import subprocess

    script = tmp_path / "t.js"
    script.write_text(
        "global.window = {}; global.document = {};\n"
        + (motion.config.MOTION_DIR / "_lib.js").read_text(encoding="utf-8")
        + "\nconst H = window.HF; console.log(JSON.stringify(["
          "H.parts('752V碳化硅'), H.parts('3.23 秒'), H.parts('4999 mm'), H.parts('全系标配'),"
          "H.parts('CLTC 835公里')]));\n", encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True,
                                    encoding="utf-8", check=True).stdout)
    v, s, mm, none, km = out
    assert (v["num"], v["unit"], v["tag"]) == (752, "V", "碳化硅")
    assert (s["num"], s["dec"], s["unit"], s["tag"]) == (3.23, 2, "秒", "")
    assert (mm["num"], mm["unit"]) == (4999, "mm")
    assert none is None
    assert (km["head"], km["num"], km["unit"]) == ("CLTC", 835, "公里")


def test_compose_html_follows_aspect():
    data = {"subject": "车", "title": "t", "rows": []}
    land = motion.compose_html("battery", data, 4.0)
    port = motion.compose_html("battery", data, 4.0, size=motion.size_for(motion.PORTRAIT))
    assert 'data-width="1920" data-height="1080" data-aspect="landscape"' in land
    assert 'data-width="1080" data-height="1920" data-aspect="portrait"' in port
    assert "width: 1080px; height: 1920px" in port
    # 画幅进缓存 key:横竖两版不会互相命中
    assert motion.cache_key("battery", data, 4.0, (1920, 1080)) != \
        motion.cache_key("battery", data, 4.0, (1080, 1920))


def test_motion_accepts_both_frame_sizes_only(monkeypatch):
    monkeypatch.setattr(config, "motion_status", lambda: (True, "ok"))
    motion._check((1920, 1080))
    motion._check((1080, 1920))
    with pytest.raises(motion.MotionError, match="640×360"):
        motion._check((640, 360))


def test_supports_portrait_reads_header_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOTION_DIR", tmp_path)
    (tmp_path / "templates").mkdir()
    (tmp_path / "scenes").mkdir()
    (tmp_path / "templates" / "tall.html").write_text(
        "<!-- @name 竖\n     @portrait yes -->\n<style></style><script></script>", encoding="utf-8")
    (tmp_path / "templates" / "wide.html").write_text(
        "<!-- @name 横 -->\n<style></style><script></script>", encoding="utf-8")
    (tmp_path / "scenes" / "battery.html").write_text(
        "<!-- 电池\n     @portrait yes -->\n<style></style><script></script>", encoding="utf-8")
    (tmp_path / "scenes" / "accel.html").write_text(
        "<!-- 加速 -->\n<style></style><script></script>", encoding="utf-8")
    assert motion.supports_portrait("tall")
    assert not motion.supports_portrait("wide")
    assert motion.supports_portrait(None, "battery")
    assert not motion.supports_portrait(None, "accel")
    assert not motion.supports_portrait(None)
