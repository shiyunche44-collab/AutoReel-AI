"""提示词编译器测试(计划 §6.1)。

编译器是全链路唯一"错了就会静默烧额度"的地方,所以这里对每条硬校验
都构造了**必须被拒绝**的反例,而不只是验证合法输入能通过。
"""
from __future__ import annotations

import dataclasses

import pytest

from app.models import ShotSpec
from app.pipeline.prompt_compiler import (
    ShotTranslation,
    _englishize_placeholder,
    assemble,
    compile_shot,
    format_timestamp,
    normalize_camera,
    validate,
)

VALID_TRANSLATION = ShotTranslation(
    beats_en=[
        "A metallic dark-blue sedan sits at a 45-degree front angle in an open "
        "parking lot, morning sunlight raking across the hood and catching the "
        "clear-coat reflections, the bodywork fully visible from bumper to tail"
    ],
    camera_en="Arc Shot",
    ambience_en="Light wind over the lot. Distant birds. Faint tire hum on asphalt.",
    music_en="Steady mid-tempo electronic pulse with light percussion.",
    visible_text=[],
)


def _spec(**kw) -> ShotSpec:
    base = dict(
        idx=1,
        duration=6.0,
        section="核心卖点",
        visual_zh="车辆 45 度前侧,阳光下的金属漆面反光",
        camera_zh="Arc Shot",
        narration_zh="这台车的漆面在阳光下很有质感。",
        angle="exterior_45",
    )
    base.update(kw)
    return ShotSpec(**base)


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------
def test_valid_t2va_prompt_passes_all_checks():
    prompt = assemble(_spec(), VALID_TRANSLATION, narration_mode="tts")
    assert validate(prompt, duration=6.0) == []


def test_valid_i2va_prompt_passes_with_alignment_line():
    prompt = assemble(_spec(), VALID_TRANSLATION, has_first_frame=True, narration_mode="tts")
    assert prompt.startswith("For the target video, at 0.00 seconds into the target video")
    assert validate(prompt, duration=6.0, expect_first_frame=True) == []


def test_three_fields_present_in_order():
    prompt = assemble(_spec(), VALID_TRANSLATION)
    assert "integrated_multimodal_description:" in prompt
    assert "overall_soundscape:" in prompt
    assert "non_diegetic_music:" in prompt
    assert (
        prompt.index("integrated_multimodal_description:")
        < prompt.index("overall_soundscape:")
        < prompt.index("non_diegetic_music:")
    )


def test_tts_mode_never_embeds_dialogue():
    prompt = assemble(_spec(), VALID_TRANSLATION, narration_mode="tts")
    assert "<d>" not in prompt
    assert "这台车的漆面" not in prompt


def test_native_mode_embeds_verbatim_dialogue_with_language_tag():
    tr = ShotTranslation(**{**VALID_TRANSLATION.__dict__, "dialogue_zh": "这台车的漆面很有质感。"})
    prompt = assemble(_spec(), tr, narration_mode="native")
    assert "<d>[Chinese] 这台车的漆面很有质感。</d>" in prompt
    assert validate(prompt, duration=6.0, narration_mode="native") == []


def test_visible_text_wrapped_in_english_double_quotes():
    tr = ShotTranslation(**{**VALID_TRANSLATION.__dict__, "visible_text": ["702 km"]})
    prompt = assemble(_spec(), tr)
    assert '"702 km"' in prompt


def test_multi_beat_prompt_has_increasing_timestamps():
    tr = ShotTranslation(
        **{
            **VALID_TRANSLATION.__dict__,
            "beats_en": [
                "The sedan rolls slowly into frame on a wet city street at night",
                "The camera cuts to a close view of the headlamp signature glowing amber",
            ],
        }
    )
    prompt = assemble(_spec(), tr)
    assert "[Shot 1]" in prompt and "[Shot 2]" in prompt
    assert "At 00:03.00" in prompt
    assert validate(prompt, duration=6.0) == []


# --------------------------------------------------------------------------
# 必须被拒绝的反例
# --------------------------------------------------------------------------
def test_rejects_text_before_shot1_token():
    prompt = (
        "integrated_multimodal_description: A moody cinematic style scene. [Shot 1] "
        "A sedan in frame, sunlight on the hood.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("[Shot 1]" in e for e in errors)


def test_rejects_non_contiguous_shot_numbering():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A\n\n"
        "[Shot 3] At 00:03.00, the camera cuts to the rear."
    )
    errors = validate(prompt, duration=6.0)
    assert any("编号不连续" in e for e in errors)


def test_rejects_non_increasing_timestamps():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame.\n\n"
        "[Shot 2] At 00:04.00, the camera cuts to the rear. "
        "[Shot 3] At 00:02.00, the camera cuts to the interior.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("未严格递增" in e for e in errors)


def test_rejects_timestamp_beyond_shot_duration():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame.\n\n"
        "[Shot 2] At 00:09.00, the camera cuts to the rear.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("超出片长" in e for e in errors)


def test_rejects_timestamp_on_first_shot():
    prompt = (
        "integrated_multimodal_description: [Shot 1] At 00:00.00, A sedan in frame.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("第一个 [Shot 1] 不应带时间戳" in e for e in errors)


def test_rejects_prompt_over_7000_codepoints():
    padding = "x" * 7200
    prompt = (
        f"integrated_multimodal_description: [Shot 1] A sedan in frame {padding}\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("超过上限" in e for e in errors)


def test_rejects_wrong_field_order():
    prompt = (
        "overall_soundscape: Light wind.\n\n"
        "integrated_multimodal_description: [Shot 1] A sedan in frame.\n\n"
        "non_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("顺序错误" in e for e in errors)


def test_rejects_missing_soundscape_field():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame.\n\n"
        "non_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("overall_soundscape" in e for e in errors)


def test_rejects_markdown_backticks():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A `sedan` in frame.\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("反引号" in e for e in errors)


def test_rejects_chinese_outside_dialogue_and_quotes():
    prompt = (
        "integrated_multimodal_description: [Shot 1] 一辆深蓝色轿车停在空旷停车场。"
        "\n\noverall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0)
    assert any("必须为英文" in e for e in errors)


def test_allows_chinese_inside_double_quotes():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame with the badge "
        '"汉" visible on the boot.\n\noverall_soundscape: Light wind.\n\nnon_diegetic_music: N/A'
    )
    assert validate(prompt, duration=6.0) == []


def test_rejects_dialogue_in_tts_mode():
    tr = ShotTranslation(**{**VALID_TRANSLATION.__dict__, "dialogue_zh": "这是一段不该出现的台词"})
    prompt = assemble(_spec(), tr, narration_mode="tts")
    # assemble 在 tts 模式下本就不会写入对白,这里直接构造违规输入验证校验器
    tampered = prompt + "\n<s> says <d>[Chinese] 不该出现</d>"
    errors = validate(tampered, duration=6.0, narration_mode="tts")
    assert any("tts 模式" in e for e in errors)


def test_rejects_too_many_soundscape_sentences():
    tr = ShotTranslation(
        **{
            **VALID_TRANSLATION.__dict__,
            "ambience_en": "One. Two. Three. Four. Five.",
        }
    )
    prompt = assemble(_spec(), tr)
    errors = validate(prompt, duration=6.0)
    assert any("1-4 句" in e for e in errors)


def test_rejects_unclosed_dialogue_tag():
    prompt = (
        "integrated_multimodal_description: [Shot 1] A sedan in frame, "
        "the narrator says <d>[Chinese] 未闭合\n\n"
        "overall_soundscape: Light wind.\n\nnon_diegetic_music: N/A"
    )
    errors = validate(prompt, duration=6.0, narration_mode="native")
    assert any("<d> 标签未闭合" in e for e in errors)


def test_i2va_requires_alignment_line():
    prompt = assemble(_spec(), VALID_TRANSLATION, has_first_frame=False)
    errors = validate(prompt, duration=6.0, expect_first_frame=True)
    assert any("前置帧对齐行" in e for e in errors)


def test_empty_prompt_rejected():
    assert validate("", duration=6.0) == ["提示词为空"]


# --------------------------------------------------------------------------
# 辅助函数
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "00:00.00"), (3.5, "00:03.50"), (65.25, "01:05.25"), (599.99, "09:59.99")],
)
def test_format_timestamp_uses_two_decimals(seconds, expected):
    assert format_timestamp(seconds) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Arc Shot", "Arc Shot"),
        ("arc shot", "Arc Shot"),
        ("环绕镜头 Arc Shot", "Arc Shot"),
        ("推近", "Static Shot"),
        ("", "Static Shot"),
    ],
)
def test_normalize_camera_maps_to_official_vocabulary(raw, expected):
    assert normalize_camera(raw) == expected


def test_compile_shot_returns_prompt_and_errors():
    prompt, errors = compile_shot(_spec(), VALID_TRANSLATION, narration_mode="tts")
    assert errors == []
    assert prompt.startswith("integrated_multimodal_description: [Shot 1] ")


# --------------------------------------------------------------------------
# 主体锚点:提示词必须点名车型,否则视频里是一台泛化轿车
# --------------------------------------------------------------------------
def test_validate_rejects_prompt_without_subject():
    """回归:提示词只写 "a dark-colored sedan" 时必须拦下。

    文案讲比亚迪汉、画面却是一台随便什么车,根因就在这里 ——
    提示词没点名主体,视频模型就只能渲染一个泛化同类物。
    """
    from app.pipeline.prompt_compiler import validate

    generic = ("integrated_multimodal_description: [Shot 1] A dark-colored sedan "
               "drives toward the camera on a wet city road at dusk. The camera "
               "moves with Push In.\n\n"
               "overall_soundscape: Hum of an electric motor. Tires on wet asphalt.\n\n"
               "non_diegetic_music: Low synth bass pulse at a slow tempo.")

    errs = validate(generic, duration=6.0, subject_en="BYD Han EV")
    assert any("未点名主体" in e for e in errs), errs

    named = generic.replace("A dark-colored sedan", "BYD Han EV, a dark-colored sedan", 1)
    assert validate(named, duration=6.0, subject_en="BYD Han EV") == []


def test_subject_rule_skipped_when_not_requested():
    """不传 subject_en 时不应启用规则 9(保持向后兼容)。"""
    from app.pipeline.prompt_compiler import validate

    prompt = ("integrated_multimodal_description: [Shot 1] A dark-colored sedan "
              "drives toward the camera. The camera moves with Push In.\n\n"
              "overall_soundscape: Hum of an electric motor. Tires on wet asphalt.\n\n"
              "non_diegetic_music: Low synth bass pulse at a slow tempo.")
    assert validate(prompt, duration=6.0) == []


def test_normalise_subject_en_rejects_generic_and_falls_back():
    """模型把主体名退化成 "a sedan" 时必须退回真实主体名。"""
    from app.pipeline.prompt_compiler import normalise_subject_en

    assert normalise_subject_en("BYD Han EV", "比亚迪汉 EV") == "BYD Han EV"
    assert normalise_subject_en("a sedan", "比亚迪汉 EV") == "比亚迪汉 EV"
    assert normalise_subject_en("The car.", "比亚迪汉 EV") == "比亚迪汉 EV"
    assert normalise_subject_en("", "比亚迪汉 EV") == "比亚迪汉 EV"
    assert normalise_subject_en(None, "比亚迪汉 EV") == "比亚迪汉 EV"
    assert normalise_subject_en("N/A", "比亚迪汉 EV") == "比亚迪汉 EV"


def test_cjk_subject_is_quoted_so_chinese_rule_allows_it():
    """品牌名没被罗马化时必须加英文双引号,否则会被"禁裸中文"规则拦下。"""
    from app.pipeline.prompt_compiler import compile_shot, validate

    tr = ShotTranslation(
        beats_en=["A dark-colored sedan drives toward the camera on a wet road."],
        ambience_en="Hum of an electric motor.",
        music_en="N/A",
        subject_en="比亚迪汉 EV",
    )
    prompt, errors = compile_shot(_spec(), tr, narration_mode="tts")
    assert errors == [], errors
    assert '"比亚迪汉 EV"' in prompt
    assert validate(prompt, duration=6.0, subject_en="比亚迪汉 EV") == []


def test_translate_shot_captures_subject_en(monkeypatch):
    """LLM 返回的 subject_en 必须被保留下来(后续 assemble 靠它点名主体)。"""
    from app.models import ShotSpec
    from app.pipeline import prompt_compiler

    class _LLM:
        def structured(self, system, user, **kw):
            return {
                "beats_en": ["A dark-colored sedan drives toward the camera."],
                "camera_en": "Push In",
                "ambience_en": "Hum of an electric motor.",
                "music_en": "N/A",
                "visible_text": [],
                "dialogue_zh": "",
                "subject_en": "BYD Han EV",
            }

    spec = ShotSpec(idx=0, duration=6.0, section="开场钩子",
                    visual_zh="一台深色轿车驶来", camera_zh="Push In",
                    narration_zh="这台车有点东西。")
    tr = prompt_compiler.translate_shot(
        spec, subject="比亚迪汉 EV", section=None, llm=_LLM())
    assert tr.subject_en == "BYD Han EV"

    # 关键:即使 LLM 没在节拍里点名,拼装后的提示词也必须点名
    prompt, errors = prompt_compiler.compile_shot(spec, tr, narration_mode="tts")
    assert errors == [], errors
    assert prompt.startswith(
        "integrated_multimodal_description: [Shot 1] BYD Han EV."), prompt[:120]


# --------------------------------------------------------------------------
# compile_with_llm:覆盖真实调用路径
# --------------------------------------------------------------------------
class _StubLLM:
    """记录收到的载荷,并返回一份合法的翻译结果。"""

    def __init__(self):
        self.calls: list[dict] = []

    def structured(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        d = dataclasses.asdict(VALID_TRANSLATION)
        d.setdefault("subject_en", "BYD Han EV")
        return d


def test_compile_with_llm_uses_section_beat_as_ambience():
    """回归:assemble 曾读 SectionSpec.ambience_zh,而该字段实际叫 beat。

    这个错只有走完整 LLM 路径才会暴露 —— 纯校验函数 compile_shot
    根本不碰 SectionSpec,所以单测全绿也照样在真实流水线里 10/10 编译失败。
    """
    from app import categories
    from app.pipeline.prompt_compiler import compile_with_llm

    profile = categories.load_profile("auto")
    section = next(iter(profile.sections.values()))
    assert not hasattr(section, "ambience_zh")
    assert section.beat, "auto profile 的段落应有 beat 文本"

    llm = _StubLLM()
    spec = _spec()
    spec.section = section.name
    prompt, errors = compile_with_llm(
        spec, subject="比亚迪汉 EV", profile=profile, llm=llm, narration_mode="tts",
    )
    assert errors == [], f"编译不该报错:{errors}"
    assert prompt
    assert llm.calls, "LLM 应被调用"


def test_compile_with_llm_survives_every_auto_section():
    """auto profile 的每个段落都必须能编译 —— 逐个跑一遍,防止新增段落再踩字段名。"""
    from app import categories
    from app.pipeline.prompt_compiler import compile_with_llm

    profile = categories.load_profile("auto")
    for section in profile.sections.values():
        llm = _StubLLM()
        spec = _spec()
        spec.section = section.name
        prompt, errors = compile_with_llm(
            spec, subject="比亚迪汉 EV", profile=profile, llm=llm, narration_mode="tts",
        )
        assert not errors, f"段落「{section.name}」编译失败:{errors}"
        assert prompt.startswith("integrated_multimodal_description:")


# --------------------------------------------------------------------------
# 中文占位符 → 英文占位符
#
# 起因:实测 drafts/001.mp4、drafts/004.mp4 画面上出现鬼画符。链路是
# 分镜的中文占位符("[年款标识]")被 translate_shot 原样写进 visible_text,
# 再被 assemble() 用英文双引号包起来 —— 而规则 6 的引号白名单正好把它放行。
# --------------------------------------------------------------------------
def _with_visible(items: list[str]) -> ShotTranslation:
    t = dataclasses.replace(VALID_TRANSLATION, visible_text=items)
    t.subject_en = "BYD Han EV"
    return t


def test_placeholders_never_reach_the_video_prompt():
    """占位符是"内容未知"的标记,不是画面文字。视频模型会照字面画出来 ——
    小米 YU7 草稿里 "[BODY LENGTH]"、"[BLOCKING RATE]" 全成了画面字幕。"""
    prompt = assemble(_spec(), _with_visible(["[年款标识]", "[PRODUCT TITLE]", "[A] [B]"]),
                      narration_mode="tts")
    assert "Readable on-screen text" not in prompt
    assert "[MODEL YEAR BADGE]" not in prompt and "年款标识" not in prompt
    assert "[PRODUCT TITLE]" not in prompt
    assert validate(prompt, duration=6.0, narration_mode="tts",
                    subject_en="BYD Han EV") == []


def test_concrete_text_is_kept_next_to_dropped_placeholders():
    prompt = assemble(_spec(), _with_visible(["702 km", "[续航读数]"]), narration_mode="tts")
    assert '"702 km"' in prompt
    assert "[" not in prompt.split("Readable on-screen text")[1].split(".")[0]


@pytest.mark.parametrize("zh,en", [
    ("[年款标识]", "[MODEL YEAR BADGE]"),
    ("[续航版本标识]", "[RANGE TRIM BADGE]"),
    ("[百公里能耗]", "[ENERGY CONSUMPTION PER 100KM]"),
    ("[车机界面]", "[INFOTAINION SCREEN]"),
    ("[价格]", "[PRICE TAG]"),
    ("车机界面", "[INFOTAINION SCREEN]"),
    ("[某个没收录的标识]", "[ON-SCREEN LABEL]"),
])
def test_placeholder_map_covers_common_auto_labels(zh: str, en: str):
    """映射仍然保留:它决定一段文字算不算占位符(中文标签 → 英文占位符 → 丢弃)。"""
    assert _englishize_placeholder(zh) == en


def test_chinese_subject_name_still_compiles():
    """回归:`normalise_subject_en()` 拿不到英文名时会退回中文主体名,
    `assemble()` 会给它包引号 —— 这条路径不能被新增的方括号规则误杀。"""
    t = dataclasses.replace(VALID_TRANSLATION, visible_text=["[MODEL YEAR BADGE]"])
    t.subject_en = "比亚迪汉 EV"
    prompt = assemble(_spec(), t, narration_mode="tts")
    # 主体锚点被引号包裹,规则 6 的引号白名单放行
    assert validate(prompt, duration=6.0, narration_mode="tts",
                    subject_en="比亚迪汉 EV") == []


def test_rule10_rejects_chinese_inside_brackets():
    """绕过 assemble() 直接送一条脏提示词(比如手工编辑的),必须拦下。"""
    dirty = (
        'integrated_multimodal_description: [Shot 1] "BYD Han EV" rolls slowly. '
        'Readable on-screen text includes "[年款标识]". '
        "The camera moves with Arc Shot.\n\n"
        "overall_soundscape: Light wind.\n\n"
        "non_diegetic_music: N/A"
    )
    errors = validate(dirty, duration=6.0, narration_mode="tts")
    assert any("方括号" in e and "中文" in e for e in errors), errors


def test_assembled_prompt_never_contains_cjk_in_brackets():
    """兜底断言:逐个遍历词表,assemble 输出里不允许再有含 CJK 的方括号。"""
    from app.pipeline.prompt_compiler import _CJK_PLACEHOLDER_MAP, _BRACKETED_RE, _CJK_RE

    for zh in _CJK_PLACEHOLDER_MAP:
        prompt = assemble(_spec(), _with_visible([f"[{zh}]"]), narration_mode="tts")
        assert not any(
            _CJK_RE.search(m.group(0)) for m in _BRACKETED_RE.finditer(prompt)
        ), f"[{zh}] 处理后仍残留中文方括号"
