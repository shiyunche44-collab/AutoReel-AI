"""横屏解说轨:profile 继承、版式核对、节拍定时长、背景连拼、叠加层。不需要网络与额度。"""
from __future__ import annotations

import pytest
from PIL import Image

from app import config
from app.categories import load_profile
from app.models import Fact, ScriptSegment, ShotSpec, SubtitleCue
from app.pipeline import explainer, library, overlays


def _facts() -> list[Fact]:
    return [
        Fact(id="f_1", claim="小米YU7 标准版 CLTC 续航 835 公里", source_tool="t",
             confidence="high", scope="product"),
        Fact(id="f_2", claim="小米YU7 标准版售价 25.35 万元", source_tool="t",
             confidence="high", scope="product"),
        Fact(id="f_3", claim="全系标配激光雷达与英伟达 Thor 芯片", source_tool="t",
             confidence="medium", scope="product"),
    ]


class FakeLLM:
    def __init__(self, beats):
        self.beats = beats
        self.calls = 0

    def structured(self, system, user, **_):
        self.calls += 1
        return {"beats": self.beats}


# --------------------------------------------------------------------------
# profile
# --------------------------------------------------------------------------
def test_auto_long_is_landscape_and_self_contained():
    p = load_profile("auto_long")
    assert p.is_explainer
    assert p.frame_size == (1920, 1080)
    # 原先从竖屏 auto 继承的官方渠道、合规词表、BGM 现在直接写在 auto_long 里
    assert "xiaomi" in p.official_sites and p.forbidden_terms and p.music_prompt
    assert set(p.themes) == {"buyguide", "roundup", "deepdive", "qa", "pk", "debate"}
    for d in p.supported_durations():
        assert len(p.plan_for(d)) == p.shots_for(d)


def test_every_explainer_section_declares_layouts():
    p = load_profile("auto_long")
    for name in p.themes["buyguide"].blocks:
        sec = p.section(name["section"])
        assert sec.layouts, sec.name
        assert set(sec.layouts) <= set(explainer.LAYOUTS)


# --------------------------------------------------------------------------
# 版式核对
# --------------------------------------------------------------------------
def test_keywords_reject_invented_numbers_and_model_names():
    facts = _facts()
    text = "全系标配激光雷达,智驾芯片是英伟达 Thor"
    assert explainer.ground_keywords(["激光雷达", "英伟达Thor芯片"], text, facts) == \
        ["激光雷达", "英伟达Thor芯片"]
    # 8650 / Orin 两样都不在旁白和事实里
    assert explainer.ground_keywords(["高通8650", "Orin-X 芯片"], text, facts) == []
    # 数字在事实里有就放行
    assert explainer.ground_keywords(["续航835公里"], "续航很长", facts) == ["续航835公里"]


def _segments(profile) -> list[ScriptSegment]:
    plan = profile.plan_for(120)
    return [ScriptSegment(idx=i, section=s, text=f"第{i}段旁白,讲讲这台车。") for i, s in enumerate(plan)]


def test_build_beats_grounds_panel_and_downgrades_bad_data():
    p = load_profile("auto_long")
    segs = _segments(p)
    i_price = next(i for i, s in enumerate(segs) if s.section == "定位与价格")
    llm = FakeLLM([
        {"idx": i_price + 1, "layout": "panel", "view": "exterior", "panel": {
            "title": "标准版", "rows": [
                {"label": "售价", "values": ["25.35万元"], "fact_id": "f_2"},
                {"label": "续航", "values": ["835公里"], "fact_id": "f_1"},
            ]}},
        # 开场选 highlight 但关键词是编的 → 退回 broll
        {"idx": 1, "layout": "highlight", "keywords": ["零百2.9秒"]},
        # 不在可选版式里 → 用段落默认版式
        {"idx": len(segs), "layout": "nonsense"},
    ])
    log: list[dict] = []
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs,
                                  facts=_facts(), llm=llm, card_log=log)
    assert len(specs) == len(segs)
    panel = specs[i_price]
    assert panel.layout == "panel" and panel.overlay["kind"] == "panel"
    assert [r["values"][0] for r in panel.overlay["rows"]] == ["25.35万元", "835公里"]
    assert specs[0].layout == "broll"
    # 不在可选版式里 → 段落默认版式(结论段是 card);没给卡片数据,核不过再退回 broll
    assert p.section(segs[-1].section).layouts[0] == "card"
    assert specs[-1].layout == explainer.SAFE_LAYOUT
    assert not explainer.validate_beats(specs)
    assert any(e["idx"] == 0 for e in log)
    assert all(s.chapter for s in specs)


def test_panel_with_unsupported_number_drops_only_the_bad_row():
    p = load_profile("auto_long")
    segs = _segments(p)
    i_price = next(i for i, s in enumerate(segs) if s.section == "定位与价格")
    llm = FakeLLM([{"idx": i_price + 1, "layout": "panel", "panel": {"title": "x", "rows": [
        {"label": "售价", "values": ["23.99万元"], "fact_id": "f_2"},
        {"label": "续航", "values": ["835公里"], "fact_id": "f_1"},
    ]}}])
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs,
                                  facts=_facts(), llm=llm)
    # 编造的那行(23.99万元)必须不上屏;核过的那行要上屏 ——
    # 只剩一行也上屏(退成大数字),整拍退成 broll 等于把核对过的参数白白扔了
    got = specs[i_price]
    assert got.layout in ("stat", "panel")
    blob = str(got.overlay)
    assert "23.99" not in blob and "835" in blob
    assert not explainer.validate_beats([got])


def test_llm_failure_still_yields_valid_beats():
    class Broken:
        def structured(self, *a, **k):
            raise RuntimeError("down")

    p = load_profile("auto_long")
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=_segments(p),
                                  facts=_facts(), llm=Broken())
    assert not explainer.validate_beats(specs)


def test_enforce_rhythm_breaks_long_card_runs():
    card = {"kind": "spec", "title": "t", "columns": [], "rows": [], "source": ""}
    specs = [ShotSpec(idx=i, visual_zh="[card] x", narration_zh="a", layout="card", card=card)
             for i in range(5)]
    out = explainer.enforce_rhythm(specs)
    layouts = [s.layout for s in out]
    assert layouts == ["card", "card", "broll", "card", "card"]
    assert out[2].card is None


def test_build_beats_grounds_stat_and_rejects_invented_number():
    p = load_profile("auto_long")
    segs = _segments(p)
    perf = [i for i, s in enumerate(segs) if s.section == "三电性能"]
    good, bad = perf[0], perf[-1] if len(perf) > 1 else None
    beats = [{"idx": good + 1, "layout": "stat",
              "stat": {"label": "CLTC续航", "value": "835公里", "fact_id": "f_1"}}]
    if bad is not None and bad != good:
        beats.append({"idx": bad + 1, "layout": "stat",
                      "stat": {"label": "零百", "value": "3.2秒", "fact_id": "f_1"}})
    specs = explainer.build_beats(subject="小米YU7", profile=p, segments=segs,
                                  facts=_facts(), llm=FakeLLM(beats))
    s = specs[good]
    assert s.layout == "stat"
    assert s.overlay == {"kind": "stat", "label": "CLTC续航", "value": "835公里",
                         "fact_id": "f_1", "source": s.overlay["source"], "tag": ""}
    if bad is not None and bad != good:
        # 退回 broll 后只剩样式标记(assign_broll_styles),没有编出来的数字
        assert specs[bad].layout == "broll"
        assert specs[bad].overlay is None or specs[bad].overlay.get("kind") == "broll"
    assert not explainer.validate_beats(specs)


def test_ground_stat_needs_a_number():
    facts = [Fact(id="f_9", claim="全系标配激光雷达", source_tool="t", confidence="high",
                  scope="product")]
    got, why = explainer.ground_stat({"label": "雷达", "value": "激光雷达", "fact_id": "f_9"},
                                     facts)
    assert got is None and why


def test_enforce_rhythm_breaks_back_to_back_stats():
    ov = {"kind": "stat", "label": "x", "value": "1秒"}
    specs = [ShotSpec(idx=i, visual_zh="[stat] x", narration_zh="a", layout="stat",
                      overlay=dict(ov)) for i in range(3)]
    out = explainer.enforce_rhythm(specs)
    assert [s.layout for s in out] == ["stat", "broll", "stat"]
    assert out[1].overlay is None and out[1].visual_zh.startswith("[broll]")


def test_count_text_rolls_up_and_lands_exactly():
    assert overlays.count_text("3.23 秒", 0.0) == "0.00 秒"
    assert overlays.count_text("3.23 秒", 0.5) == "1.61 秒" or \
        overlays.count_text("3.23 秒", 0.5) == "1.62 秒"
    assert overlays.count_text("约 1,200 km", 0.5) == "约 600 km"
    assert overlays.count_text("约 1,200 km", 1.0) == "约 1,200 km"   # 落地与原文一字不差
    assert overlays.count_text("激光雷达", 0.3) == "激光雷达"


def test_render_stat_beat_with_ffmpeg(tmp_path, monkeypatch):
    if not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()):
        pytest.skip("没有 ffmpeg")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    spec = ShotSpec(idx=0, duration=2.0, visual_zh="[stat] x", narration_zh="a", layout="stat",
                    overlay={"kind": "stat", "label": "续航", "value": "835公里",
                             "source": "数据来源"})
    dest, _ = explainer.render_beat(spec, subject="测试车", size=(640, 360), out_dir=tmp_path,
                                    chapters=[("开头", 2.0)], chapter_idx=0, used={},
                                    card_bg=None)
    from app.pipeline import timeline
    assert timeline.probe_safe(dest, default=0.0) == pytest.approx(2.0, abs=0.1)


# --------------------------------------------------------------------------
# 时长与章节
# --------------------------------------------------------------------------
def test_beat_durations_match_narration_track():
    cues = [SubtitleCue(index=1, start=0.0, end=2.0, text="a", shot_idx=0),
            SubtitleCue(index=2, start=2.3, end=5.3, text="b", shot_idx=0),
            SubtitleCue(index=3, start=5.6, end=7.6, text="c", shot_idx=1)]
    d = explainer.beat_durations(cues, 3, gap=0.3)
    assert d == [5.6, 2.3, 0.0]
    # 旁白轨 = Σ(句长 + 停顿)
    assert sum(d) == pytest.approx(7.6 + 0.3)


def test_chapters_group_adjacent_beats():
    specs = [ShotSpec(idx=i, visual_zh="x", chapter=c, duration=d)
             for i, (c, d) in enumerate([("开头", 5), ("外观", 6), ("外观", 7), ("结论", 4)])]
    chapters, where = explainer.chapters_of(specs)
    assert chapters == [("开头", 5), ("外观", 13), ("结论", 4)]
    assert where == [0, 1, 1, 2]


def test_layout_filters_reference_expected_inputs():
    f = explainer.layout_filter("panel", (1920, 1080), True)
    assert "[2:v]" in f and "[1:v]" in f and f.endswith("[out]")
    f = explainer.layout_filter("highlight", (1920, 1080), True)
    assert "pad=" in f and "fade=in" in f
    # 叠加层缺失时退化为只叠章节条
    assert explainer.layout_filter("panel", (1920, 1080), False) == \
        "[0:v][1:v]overlay=0:0,format=yuv420p[out]"


# --------------------------------------------------------------------------
# 背景连拼
# --------------------------------------------------------------------------
@pytest.fixture()
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    lib = library.Library(subject="测试车")
    vid = tmp_path / "a.mp4"
    vid.write_bytes(b"x")
    still = tmp_path / "s.jpg"
    still.write_bytes(b"x")

    def item(id_, start, end, view="exterior_45", target="yes", problem="", kind="video",
             path=vid):
        return library.Item(id=id_, kind=kind, path=str(path), tagged=True, view=view,
                            target=target, problem=problem, start=start, end=end)

    lib.items = [
        item("clean1", 0, 3), item("clean2", 3, 5.5), item("int1", 6, 9, view="interior"),
        item("sub1", 10, 20, problem="subtitle"),
        item("unknown1", 20, 25, target="unknown"),
        item("tiny", 25, 25.6),
        item("still1", 0, 0, kind="still", path=still),
    ]
    lib.save()
    return lib


def test_fill_prefers_clean_confirmed_footage_and_covers_duration(pool):
    used: dict[str, int] = {}
    pieces = library.fill("测试车", 8.0, hint="exterior", used=used)
    assert sum(p.length for p in pieces) == pytest.approx(8.0)
    ids = [p.item_id for p in pieces]
    assert ids[0] == "clean1"
    assert "tiny" not in ids and "sub1" not in ids
    assert all(p.length <= library.FILL_MAX_PIECE + library.FILL_MIN_PIECE for p in pieces)
    assert all(used[i] == 1 for i in ids)


def test_fill_hint_interior_reorders_but_does_not_filter(pool):
    pieces = library.fill("测试车", 3.0, hint="interior", used={})
    assert pieces[0].item_id == "int1"


def test_fill_rotates_through_unused_material(pool):
    used: dict[str, int] = {}
    first = [p.item_id for p in library.fill("测试车", 5.0, used=used)]
    second = [p.item_id for p in library.fill("测试车", 5.0, used=used)]
    assert not set(first) & set(second[:1])


def test_fill_uses_subtitle_footage_cropped_before_reused_clean(pool):
    used = {"clean1": 1, "clean2": 1, "int1": 1, "unknown1": 1}
    pieces = library.fill("测试车", 8.0, used=used)
    ids = [p.item_id for p in pieces]
    assert ids[0] == "still1" and ids[1] == "sub1"     # 官图 → 裁字幕段 → 用过的干净段
    sub = pieces[1]
    assert sub.crop_bottom == library.SUBTITLE_CROP
    assert pieces[0].crop_bottom == 0.0


def test_fill_ranks_presenter_footage_after_cropped_subtitles(pool):
    lib = library.Library.load("测试车")
    host = library.Item(id="host1", kind="video", path=lib.items[0].path, tagged=True,
                        view="exterior_45", target="yes", problem="subtitle",
                        desc="博主讲解车头", start=30, end=40)
    lib.items.append(host)
    lib.save()
    used = {"clean1": 1, "clean2": 1, "int1": 1, "unknown1": 1, "still1": 1}
    pieces = library.fill("测试车", 4.0, used=used)
    assert pieces[0].item_id == "sub1"
    host_piece = next(p for p in library.fill("测试车", 30.0, used={}) if p.item_id == "host1")
    assert host_piece.crop_bottom == 0.0               # 主持人裁不掉,不裁


def test_fit_filter_crops_subtitle_band_and_fills_frame():
    plain = explainer._fit_filter(1920, 1080)
    assert "boxblur" in plain and "crop=iw" not in plain
    cropped = explainer._fit_filter(1920, 1080, 0.22)
    assert cropped.startswith("[0:v]crop=iw:trunc(ih*0.780/2)*2:0:0,")
    assert "boxblur" not in cropped and "crop=1920:1080" in cropped


def test_fill_on_empty_pool_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert library.fill("没有这台车", 6.0) == []


# --------------------------------------------------------------------------
# 叠加层
# --------------------------------------------------------------------------
def test_overlays_render_transparent_canvases(tmp_path):
    size = (1920, 1080)
    p = overlays.panel({"title": "标准版", "rows": [{"label": "售价", "values": ["25.35万元"]}],
                        "source": "数据来源"}, "小米YU7", size, tmp_path / "p.png")
    h = overlays.highlight(["激光雷达", "英伟达Thor"], size, tmp_path / "h.png")
    b = overlays.chapter_bar([("开头", 3), ("定位价格", 30), ("结论", 5)], 1, size,
                             tmp_path / "b.png")
    for path in (p, h, b):
        im = Image.open(path)
        assert im.size == size and im.mode == "RGBA"
        # 画面中央必须是透明的(背景画面要透出来)
        if path is not b:
            assert im.getpixel((size[0] - 40, 40))[3] == 0
    bar = Image.open(b)
    assert bar.getpixel((10, 10))[3] == 0
    assert bar.getpixel((10, size[1] - 5))[3] > 0


def test_fill_skips_black_title_cards(pool, tmp_path):
    dark = tmp_path / "dark.png"
    Image.new("RGB", (64, 36), (5, 5, 5)).save(dark)
    lib = library.Library.load("测试车")
    for i in lib.items:
        if i.id == "clean1":
            i.frame = str(dark)
    lib.save()
    ids = [p.item_id for p in library.fill("测试车", 6.0, used={})]
    assert "clean1" not in ids and ids


def test_fill_confirmed_only_takes_official_or_recognised_footage(pool):
    lib = library.Library.load("测试车")
    lib.items.append(library.Item(id="logo1", kind="video", path=lib.items[0].path, tagged=True,
                                  view="exterior_45", target="yes", problem="text_overlay",
                                  start=40, end=50))
    lib.items.append(library.Item(id="off1", kind="video", path=lib.items[0].path, tagged=True,
                                  view="exterior_45", target="unknown", official=True,
                                  start=50, end=60))
    lib.save()
    ids = {p.item_id for p in library.fill("测试车", 60.0, used={}, confirmed=True)}
    assert "unknown1" not in ids                       # 没认出是这台车:开场不用
    assert "logo1" not in ids                          # 第三方台标:池子再小也不用
    assert "off1" in ids                               # 官方来源:认不认出都能用
    loose = {p.item_id for p in library.fill("测试车", 60.0, used={})}
    assert "unknown1" in loose                         # 普通拍照旧只是降权


def test_fill_edge_crops_third_party_video_only(pool):
    lib = library.Library.load("测试车")
    lib.items.append(library.Item(id="off1", kind="video", path=lib.items[0].path, tagged=True,
                                  view="exterior_45", target="yes", official=True,
                                  start=50, end=60))
    lib.save()
    pieces = {p.item_id: p for p in library.fill("测试车", 60.0, used={})}
    assert pieces["clean1"].crop_edge == library.EDGE_CROP
    assert pieces["off1"].crop_edge == 0.0
    assert pieces["still1"].crop_edge == 0.0


def test_fit_filter_edge_crop_runs_before_fit():
    out = explainer._fit_filter(1920, 1080, 0.0, 0.14)
    assert out.startswith("[0:v]crop=trunc(iw*0.860/2)*2:trunc(ih*0.860/2)*2,split")
    both = explainer._fit_filter(1920, 1080, 0.22, 0.14)
    assert both.startswith("[0:v]crop=iw:trunc(ih*0.780/2)*2:0:0,crop=trunc(iw*0.860")
    assert explainer._fit_filter(1920, 1080) == explainer._fit_filter(1920, 1080, 0.0, 0.0)


def test_stat_note_only_uses_numbers_from_narration():
    narration = "15分钟补能405公里,吃顿饭的工夫够从上海到南京。"
    assert explainer.ground_note("吃顿饭的工夫", narration) == "吃顿饭的工夫"
    assert explainer.ground_note("够跑300公里", narration) == ""          # 旁白没说过 300
    assert explainer.ground_note("这是一个特别长的注解超过了字数上限的", narration) == ""
    assert explainer.ground_note(None, narration) == ""


def test_fit_filter_crops_source_bars_first():
    """源自带的黑边最先裁,后面的比例都按裁完的画面算。"""
    bars = "crop=1920:752:0:130"
    out = explainer._fit_filter(1920, 1080, 0.0, 0.0, bars)
    assert out.startswith(f"[0:v]{bars},")
    assert explainer._fit_filter(1920, 1080, 0.22, 0.14, bars).startswith(f"[0:v]{bars},crop=")
    assert explainer._fit_filter(1920, 1080) == explainer._fit_filter(1920, 1080, 0.0, 0.0, "")


def test_letterbox_crop_detects_bars_and_ignores_full_frame(tmp_path):
    """宽银幕片(上下黑边)被检出;满画幅片不裁。"""
    import shutil
    import subprocess

    from app import config

    ff = str(config.FFMPEG_PATH or shutil.which("ffmpeg") or "")
    if not ff:
        pytest.skip("没有 ffmpeg")
    full = tmp_path / "full.mp4"
    boxed = tmp_path / "boxed.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=3:r=10",
                    "-pix_fmt", "yuv420p", str(full)], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:d=3:r=10",
                    "-vf", "pad=640:360:0:0,drawbox=0:0:640:60:black:fill,drawbox=0:300:640:60:black:fill",
                    "-pix_fmt", "yuv420p", str(boxed)], check=True)
    explainer.letterbox_crop.cache_clear()
    assert explainer.letterbox_crop(str(full)) == ""
    got = explainer.letterbox_crop(str(boxed))
    assert got.startswith("crop=640:") and ":0:" in got


def _cap_world(n=10, section="三电性能"):
    """n 拍全是 broll,每拍引用 f_1 / f_2;偶数拍旁白带数字,奇数拍是观点。"""
    segs = [ScriptSegment(idx=i, section=section,
                          text="差了 228 公里。" if i % 2 == 0 else "这钱花得值。",
                          fact_refs=["f_1", "f_2"]) for i in range(n)]
    specs = [ShotSpec(idx=i, section=section, visual_zh="[broll] x", narration_zh=segs[i].text,
                      layout="broll", duration=3.0) for i in range(n)]
    return specs, segs


def test_enforce_broll_cap_brings_broll_down_to_cap_with_grounded_panels():
    from app.pipeline import rhythm

    profile = load_profile("auto_long")
    specs, segs = _cap_world()
    out = explainer.enforce_broll_cap(specs, segs, _facts(), profile)
    assert sum(1 for s in out if s.layout == "broll") <= max(1, int(10 * rhythm.BROLL_RATIO_MAX))
    panels = [s for s in out if s.layout == "panel"]
    assert panels
    cited = {"f_1", "f_2"}
    for s in panels:  # 上屏的行只来自这一拍引用过的事实
        assert {r["fact_id"] for r in s.overlay["rows"]} <= cited
    # 相邻两拍的画面指纹不一样,不会同一屏连着杵
    sigs = [rhythm._overlay_signature(s) for s in out]
    assert all(a != b for a, b in zip(sigs, sigs[1:]) if a[0] == "panel")


def test_enforce_broll_cap_leaves_beats_without_refs_alone():
    profile = load_profile("auto_long")
    specs, segs = _cap_world()
    for seg in segs:
        seg.fact_refs = []
    out = explainer.enforce_broll_cap(specs, segs, _facts(), profile)
    assert all(s.layout == "broll" for s in out)  # 没有引用就没有依据,不编


def test_enforce_broll_cap_skips_foreign_comparison_facts_outside_comparison():
    profile = load_profile("auto_long")
    specs, segs = _cap_world(section="三电性能")
    rival = Fact(id="f_r", claim="极氪 7X 10 分钟补能 800V", source_tool="t",
                 confidence="high", compare_with="极氪 7X")
    for seg in segs:
        seg.fact_refs = ["f_r"]
    out = explainer.enforce_broll_cap(specs, segs, [rival], profile)
    assert all(s.layout == "broll" for s in out)
