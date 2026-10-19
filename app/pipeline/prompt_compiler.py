"""H3 提示词编译器(全链路的地基,计划 §2.5 / §4.2)。

职责切分刻意做成三层,目的是让**格式正确性变成确定性问题**而非概率问题:

    translate_shot()   需要 LLM:把中文分镜翻成结构化英文
           ↓
    assemble()         纯函数:按官方三字段格式拼装提示词(零外部依赖,可单测)
           ↓
    validate()         纯函数:10 条硬校验,不合规就拦下,绝不发给模型烧额度

为什么不让 LLM 直接写最终提示词:模型的自由发挥会把时间戳写成 03:5、
把 [Shot 1] 前面塞进风格描述、把英文字段里混进中文 —— 这些都会被 H3 静默劣化。
让模型只负责"翻译与描述",格式由代码保证,是这类生产管线的标准做法。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..categories import CategoryProfile, SectionSpec
from ..models import ShotSpec

#: H3 官方运镜词表。编译器只允许使用这些词,避免自造术语导致动作漂移。
CAMERA_VOCABULARY: tuple[str, ...] = (
    "Zoom In", "Zoom Out", "Push In", "Pull Out",
    "Pan Left", "Pan Right", "Truck Left", "Truck Right",
    "Tilt Up", "Tilt Down", "Pedestal Up", "Pedestal Down",
    "Arc Shot", "Tracking Shot", "Static Shot",
    "Shake Slightly", "Shake Strongly", "POV",
    "Roll Clockwise", "Roll Counterclockwise",
)

#: 整条提示词的长度上限(Unicode 码点)
MAX_PROMPT_CODEPOINTS = 7000

#: 三个必备字段
FIELD_DESCRIPTIVE = "integrated_multimodal_description"
FIELD_SOUNDSCAPE = "overall_soundscape"
FIELD_MUSIC = "non_diegetic_music"

#: I2VA 模式的前置帧对齐行模板
ALIGNMENT_TEMPLATE = (
    "For the target video, at 0.00 seconds into the target video, "
    "<Picture 1> (from [Shot 1]) is fully referenced."
)

_SHOT_TAG_RE = re.compile(r"\[Shot (\d+)\]")
#: [Shot 2] At 00:03.50, —— 小数支持 2~3 位
_TIMESTAMP_RE = re.compile(r"\[Shot (\d+)\]\s*At\s*(\d{2}):(\d{2})\.(\d{2,3})")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_DIALOGUE_RE = re.compile(r"<d>.*?</d>", re.DOTALL)
_DOUBLED_QUOTED_RE = re.compile(r"\"[^\"]*\"")
_BRACKETED_RE = re.compile(r"\[[^\[\]]*\]")

#: 中文占位符 → 英文占位符。视频模型不会渲染汉字,只会把「[年款标识]」
#: 这种它读不懂的方括号内容画成鬼画符(实测 drafts/001.mp4、004.mp4)。
#:
#: 这里放代码而不是只靠 LLM 翻译,是因为它是**确定性**的:`assemble()` 是所有
#: 路径的必经之地,再皮实的模型也总有漏翻的时候,而漏翻一次就是一条废片。
#: 词表可以慢慢补,补不到的走下面的 `[ON-SCREEN LABEL]` 兜底。
_CJK_PLACEHOLDER_MAP: dict[str, str] = {
    "年款标识": "[MODEL YEAR BADGE]",
    "续航版本标识": "[RANGE TRIM BADGE]",
    "百公里能耗": "[ENERGY CONSUMPTION PER 100KM]",
    "能耗标识": "[ENERGY CONSUMPTION PER 100KM]",
    "续航里程": "[RANGE]",
    "车机界面": "[INFOTAINION SCREEN]",
    "仪表盘": "[INSTRUMENT CLUSTER]",
    "中控屏": "[CENTER DISPLAY]",
    "充电口": "[CHARGING PORT]",
    "车标": "[BRAND BADGE]",
    "价格": "[PRICE TAG]",
    "售价": "[PRICE TAG]",
    "配置表": "[SPECIFICATION TABLE]",
    "参数": "[SPECIFICATION TABLE]",
    "车牌": "[LICENSE PLATE]",
    "车门": "[DOOR]",
    "座椅": "[SEAT]",
    "轮毂": "[WHEEL]",
    "大灯": "[HEADLIGHT]",
    "尾灯": "[TAIL LIGHT]",
    "刹车灯": "[BRAKE LIGHT]",
    "屏幕数据": "[DATA READOUT]",
    "字幕条": "[LOWER THIRD]",
    "logo": "[BRAND BADGE]",
}

#: 词表没覆盖到的中文占位符的兜底。宁可给模型一个语义为空的英文标签,
#: 也不要给汉字 —— 鬼画符是确定性失败,泛化标签只是语义损失。
_CJK_PLACEHOLDER_FALLBACK = "[ON-SCREEN LABEL]"


def _englishize_placeholder(text: str) -> str:
    """把可见文字里的中文占位符换成英文占位符。

    两种情况:
    1. **方括号内含中文** —— 逐个方括号查词表,查不到用 `[ON-SCREEN LABEL]`。
    2. **整串就是中文** —— 模型把 `on_screen_text` 原样抄了回来,连括号都没加。
       按整串查词表,查不到同样降级。

    中英混排(如 "比亚迪汉 EV")**故意原样保留**:那是品牌名,不是占位符,
    而且 `normalise_subject_en()` 在拿不到英文名时也会退回中文主体名。
    它由 `validate()` 的引号白名单放行 —— 规则 6 之所以允许引号内中文,
    就是为了容纳这种主体名。
    """
    # 情况 2:整串无 ASCII 字母数字(说明它是纯中文标签,不是品牌名)
    stripped = text.strip()
    if stripped and not re.search(r"[A-Za-z0-9]", stripped):
        return _CJK_PLACEHOLDER_MAP.get(stripped.strip("[]").strip(),
                                        _CJK_PLACEHOLDER_FALLBACK)

    def _sub(match: re.Match[str]) -> str:
        inner = match.group(0)[1:-1]
        if not _CJK_RE.search(inner):
            return match.group(0)          # 本来就是英文占位符,不动
        return _CJK_PLACEHOLDER_MAP.get(inner, _CJK_PLACEHOLDER_FALLBACK)

    return _BRACKETED_RE.sub(_sub, text)


@dataclass
class ShotTranslation:
    """LLM 产出的英文结构化描述。"""

    #: 每个内部节拍的英文画面描述。单节拍 = 连续单镜头;多节拍 = 提示词内切镜。
    beats_en: list[str] = field(default_factory=list)
    #: 环境音英文描述(1-4 句)
    ambience_en: str = ""
    #: 观众可听配乐英文描述(1-3 句);无则 "N/A"
    music_en: str = "N/A"
    #: 画面中可见的文字。**缺失时给占位符,禁止臆造品牌/型号/口号。**
    visible_text: list[str] = field(default_factory=list)
    #: 画外音台词(narration_mode=native 时才写入提示词)
    dialogue_zh: str = ""
    #: 主体在**画面里的**品牌+型号英文名(罗马化),如 "BYD Han EV"。
    #: 必须非空且不能是泛化名词 —— 提示词不点名主体,视频模型就会画出一台
    #: 泛化的车,这是"文案讲汉、画面不是汉"的直接原因。
    subject_en: str = ""
    #: 画面中第二台车的品牌+型号英文名(横向对比 / 迭代史场景)。
    #: 可选:拿不到就不写,**不参与规则 9 校验** —— 规则 9 要防的是
    #: "文案讲汉、画面是泛化轿车",主主体必须锚死;第二台车属于增强。
    subject_en_secondary: str = ""
    #: 归一化后的运镜词,必须落在 CAMERA_VOCABULARY 内
    camera_en: str = "Static Shot"
    #: 画面中是否出现人物及其唇部状态(native 模式需要)
    has_visible_speaker: bool = False


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def format_timestamp(seconds: float) -> str:
    """格式化为 MM:SS.ss(两位小数),H3 官方要求。"""
    total = max(0.0, float(seconds))
    minutes = int(total // 60)
    secs = total - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def normalize_camera(camera: str) -> str:
    """把运镜归一到官方词表。无法匹配时退回 Static Shot(最稳)。"""
    if not camera:
        return "Static Shot"
    text = camera.strip()
    for word in CAMERA_VOCABULARY:
        if text.lower() == word.lower():
            return word
    # 尝试子串匹配,容忍 "Arc Shot 环绕" 这类中英混写
    lowered = text.lower()
    for word in sorted(CAMERA_VOCABULARY, key=len, reverse=True):
        if word.lower() in lowered:
            return word
    return "Static Shot"


def count_sentences(text: str) -> int:
    """统计英文句子数。用于 overall_soundscape / non_diegetic_music 的长度约束。"""
    cleaned = text.strip()
    if not cleaned or cleaned.upper() == "N/A":
        return 0
    parts = [p for p in re.split(r"(?<=[.!?])\s+", cleaned) if p.strip()]
    return len(parts)


# --------------------------------------------------------------------------
# 拼装(纯函数)
# --------------------------------------------------------------------------
def assemble(
    spec: ShotSpec,
    translation: ShotTranslation,
    *,
    has_first_frame: bool = False,
    narration_mode: str = "tts",
    section: SectionSpec | None = None,
) -> str:
    """按 H3 官方三字段格式拼装一条提示词。"""
    beats = [b.strip() for b in translation.beats_en if b and b.strip()]
    if not beats:
        beats = ["[PENDING TRANSLATION] the subject holds a steady pose in frame"]

    # 主体锚点:必须在第一个节拍里点名。LLM 倾向于把主语写成泛化名词
    # ("a dark-colored sedan"),那样视频里就是一台随便什么车。
    # 这段放在 assemble 而不是 translate_shot,是因为 assemble 是所有路径的
    # 必经之地 —— 手工构造的 translation 也逃不掉。
    anchor = translation.subject_en.strip()
    if anchor and anchor.lower() not in beats[0].lower():
        shown = f'"{anchor}"' if _CJK_RE.search(anchor) else anchor
        beats[0] = f"{shown}. {beats[0]}"

    # 第二主体锚点(可选):横向对比与车型迭代史的画面必须出现第二台车。
    # 规则 9 仍然只校验主主体 —— 这里是增强,拿不到第二锚点就不加,
    # 不能因为缺了它就拦下整条提示词(那是把可选当必需,会让长视频全线编译失败)。
    if section is not None and getattr(section, "multi_subject", False):
        second = translation.subject_en_secondary.strip()
        if second and second.lower() not in " ".join(beats).lower():
            if len(beats) >= 2:
                shown2 = f'"{second}"' if _CJK_RE.search(second) else second
                beats[1] = f"{shown2} is also in frame. {beats[1]}"
            else:
                shown2 = f'"{second}"' if _CJK_RE.search(second) else second
                beats[0] = f"{beats[0]} {shown2} is also in frame."

    camera = normalize_camera(translation.camera_en)
    duration = spec.duration

    # --- 画面描述体 ---
    # 单节拍:风格 + 构图 + 运镜 + 动作,连续写成一段。
    # 多节拍:第一个标签无时间戳,后续标签带严格递增的时间戳。
    parts: list[str] = []
    for i, beat in enumerate(beats, start=1):
        clean = beat.lstrip()
        clean = re.sub(r"^\[Shot\s*\d+\]\s*(At\s*[\d:.]+\s*,\s*)?", "", clean)
        if i == 1:
            body = f"[Shot 1] {clean}"
        else:
            # 在片段时长内均分切点,留出最后的收尾时间
            cut_at = duration * (i - 1) / len(beats)
            body = f"[Shot {i}] At {format_timestamp(cut_at)}, the camera cuts to {clean}"
        parts.append(body)

    visual_body = " ".join(parts)
    # 运镜以自然语句嵌入,不作为孤立标签
    if camera and camera.lower() not in visual_body.lower():
        visual_body = f"{visual_body} The camera moves with {camera}."

    # --- 台词(tts 模式下绝不写入提示词) ---
    dialogue_block = ""
    if narration_mode == "native" and translation.dialogue_zh.strip():
        speaker = "The narrator (S1) says in an off-screen voiceover"
        lips = (
            " The on-screen character keeps their lips completely closed throughout."
            if translation.has_visible_speaker
            else ""
        )
        dialogue_block = (
            f"{speaker}: <d>[Chinese] {translation.dialogue_zh.strip()}</d>.{lips}"
        )
        visual_body = f"{visual_body} {dialogue_block}"

    # --- 可见文字:一律英文双引号包裹;缺失文案用占位符 ---
    # `_englishize_placeholder` 是这里的确定性兜底:LLM 已经被告知名确要翻成英文,
    # 但模型漏翻是常态(实测 drafts/001.mp4、004.mp4 就是漏翻的 "[年款标识]",
    # 被 H3 画成了鬼画符)。放在 assemble 而不是 translate_shot,是为了让
    # 手工构造的 ShotTranslation 也逃不掉这一层。
    text_bits = [
        _englishize_placeholder(t.strip())
        for t in translation.visible_text
        if t and t.strip()
    ]
    if text_bits:
        joined = " ".join(f'"{t}"' for t in text_bits)
        visual_body = f'{visual_body} Readable on-screen text includes {joined}.'

    descriptive = f"{FIELD_DESCRIPTIVE}: {visual_body}"

    ambience = translation.ambience_en.strip() or "N/A"
    music = translation.music_en.strip() or "N/A"

    body = f"{descriptive}\n\n{FIELD_SOUNDSCAPE}: {ambience}\n\n{FIELD_MUSIC}: {music}"

    if has_first_frame:
        # I2VA 前置对齐行 + 一个空行
        return f"{ALIGNMENT_TEMPLATE}\n\n{body}"
    return body


# --------------------------------------------------------------------------
# 校验(纯函数)
# --------------------------------------------------------------------------
def validate(prompt: str, *, duration: float = 6.0, expect_first_frame: bool = False,
             narration_mode: str = "tts",
             subject_en: str = "") -> list[str]:
    """执行 10 条硬校验,返回错误列表。空列表表示通过。

    `subject_en` 传入时启用规则 9:提示词必须点名主体。传空串则跳过
    (用于不关心主体的场景)。
    """
    errors: list[str] = []

    if not prompt or not prompt.strip():
        return ["提示词为空"]

    text = prompt.strip()

    # 规则 1:首 token 结构。I2VA 需前置对齐行,T2VA 需直接以字段开头。
    if expect_first_frame:
        if not text.startswith(ALIGNMENT_TEMPLATE):
            errors.append("I2VA 模式缺少前置帧对齐行")
        else:
            text = text[len(ALIGNMENT_TEMPLATE):].strip()
    if not text.startswith(f"{FIELD_DESCRIPTIVE}:"):
        errors.append(
            f"提示词必须以 '{FIELD_DESCRIPTIVE}:' 开头(当前以 '{text[:40]}' 开头)"
        )
    elif not re.match(
        rf"^{re.escape(FIELD_DESCRIPTIVE)}:\s*\[Shot 1\]\s", text
    ):
        errors.append(
            f"'{FIELD_DESCRIPTIVE}:' 之后必须紧跟 '[Shot 1] ',不得插入风格描述"
        )

    # 规则 2:三字段齐全且顺序正确
    pos_desc = text.find(f"{FIELD_DESCRIPTIVE}:")
    pos_snd = text.find(f"{FIELD_SOUNDSCAPE}:")
    pos_mus = text.find(f"{FIELD_MUSIC}:")
    if pos_snd == -1:
        errors.append(f"缺少字段 {FIELD_SOUNDSCAPE}")
    if pos_mus == -1:
        errors.append(f"缺少字段 {FIELD_MUSIC}")
    if -1 not in (pos_desc, pos_snd, pos_mus) and not (pos_desc < pos_snd < pos_mus):
        errors.append("三个字段顺序错误,必须依次为 "
                      f"{FIELD_DESCRIPTIVE} → {FIELD_SOUNDSCAPE} → {FIELD_MUSIC}")

    # 规则 3:[Shot N] 编号连续、首镜头无时间戳、后续时间戳严格递增且不超片长
    tags = [(int(m.group(1)), m.start()) for m in _SHOT_TAG_RE.finditer(text)]
    if not tags:
        errors.append("提示词中没有任何 [Shot N] 标签")
    else:
        numbers = [n for n, _ in tags]
        if numbers != list(range(1, len(numbers) + 1)):
            errors.append(f"[Shot N] 编号不连续: {numbers}")
        first_pos = tags[0][1]
        if _TIMESTAMP_RE.match(text[first_pos:]):
            errors.append("第一个 [Shot 1] 不应带时间戳")

        stamps = [
            (int(m.group(1)), int(m.group(2)) * 60 + float(m.group(3)) + float("0." + m.group(4)))
            for m in _TIMESTAMP_RE.finditer(text)
        ]
        if len(stamps) != len(numbers) - 1:
            errors.append(
                f"除首个镜头外每个 [Shot N] 都必须带 'At MM:SS.ss,' 时间戳,"
                f"实际 {len(stamps)}/{max(0, len(numbers) - 1)}"
            )
        prev = -1.0
        for n, value in stamps:
            if value <= prev:
                errors.append(f"[Shot {n}] 时间戳 {value} 未严格递增")
            if value <= 0 or value > duration + 1e-6:
                errors.append(
                    f"[Shot {n}] 时间戳 {value:.2f}s 超出片长 {duration}s"
                )
            prev = value

    # 规则 4:镜头数与切点数一致(N 个标签 = N-1 个切点)
    n_tags = len(tags)
    n_stamps = len(_TIMESTAMP_RE.findall(text))
    if n_tags and n_stamps != n_tags - 1:
        errors.append(f"切点数应为 {n_tags - 1},实际 {n_stamps}")

    # 规则 5:soundscape 1-4 句,music 1-3 句(N/A 视为 0 句且合法)
    if pos_snd != -1:
        snd = _field_value(text, pos_snd, pos_mus)
        count = count_sentences(snd)
        if count != 0 and not (1 <= count <= 4):
            errors.append(f"{FIELD_SOUNDSCAPE} 应为 1-4 句或 N/A,实际 {count} 句")
    if pos_mus != -1:
        mus = _field_value(text, pos_mus, len(text))
        count = count_sentences(mus)
        if count != 0 and not (1 <= count <= 3):
            errors.append(f"{FIELD_MUSIC} 应为 1-3 句或 N/A,实际 {count} 句")

    # 规则 6:对白完整位于 <d> 内;可见文字用英文双引号;除对白与引号内容外不得出现中文
    dialogue_blocks = _DIALOGUE_RE.findall(text)
    if text.count("<d>") != text.count("</d>"):
        errors.append("<d> 标签未闭合")
    for block in dialogue_blocks:
        if not re.fullmatch(r"<d>\[[^\]]+\][\s\S]+</d>", block):
            errors.append(f"<d> 内缺少语言标签或内容为空: {block[:60]}")
    if narration_mode == "tts" and dialogue_blocks:
        errors.append(
            "tts 模式下画面提示词不得包含 <d> 对白(旁白走独立音轨),"
            "请检查翻译结果是否误将解说词写入画面描述"
        )

    cjk_errors = _find_cjk_outside_allowed(text)
    if cjk_errors:
        errors.append(
            "除 <d> 对白与英文双引号包裹的可见文字外,其余字段必须为英文;"
            f"发现中文:{' / '.join(cjk_errors[:3])}"
        )

    # 规则 7:总长度上限
    length = len(prompt)
    if length > MAX_PROMPT_CODEPOINTS:
        errors.append(f"提示词长度 {length} 超过上限 {MAX_PROMPT_CODEPOINTS} 码点")

    # 规则 8:不含 Markdown 反引号
    if "`" in text:
        errors.append("提示词中不得包含 Markdown 反引号")

    # 规则 9:提示词必须点名主体。
    # 没有这条,LLM 会把主语退化成 "a dark-colored sedan",画面里就是一台
    # 泛化轿车 —— 文案讲比亚迪汉、视频不是汉,就是这么来的。
    if subject_en.strip():
        anchor = subject_en.strip().lower()
        if anchor not in text.lower():
            errors.append(
                f"提示词未点名主体「{subject_en}」,视频模型会渲染成泛化同类物;"
                "请在画面描述中写明品牌与型号"
            )

    # 规则 10:方括号内禁止中文。
    #
    # 这条独立于规则 6 的引号白名单。引号内允许中文是**必要的** ——
    # `normalise_subject_en()` 拿不到英文名时会退回中文主体名,`assemble()`
    # 会给它包引号,一刀切禁掉引号中文会让这类镜头全线编译失败。
    # 而方括号是占位符的唯一载体,精准命中它就够了。
    # 典型漏网:"Readable on-screen text includes "[年款标识]""
    # —— 双重引号区被规则 6 挖空,汉字就这么进了 H3。
    bracket_cjk = [
        m.group(0) for m in _BRACKETED_RE.finditer(text) if _CJK_RE.search(m.group(0))
    ]
    if bracket_cjk:
        errors.append(
            "方括号占位符内不得出现中文(视频模型会渲染成乱码),"
            f"请改写为英文占位符:{' / '.join(sorted(set(bracket_cjk))[:3])}"
        )

    return errors


def _field_value(text: str, start_pos: int, end_pos: int) -> str:
    """提取某字段的取值部分。"""
    colon = text.index(":", start_pos)
    return text[colon + 1 : end_pos].strip() if end_pos > colon else ""


def _find_cjk_outside_allowed(text: str) -> list[str]:
    """找出位于 <d> 与英文双引号之外的中文片段。"""
    # 先挖掉允许区域
    masked = _DIALOGUE_RE.sub(" ", text)
    masked = _DOUBLED_QUOTED_RE.sub(" ", masked)
    hits: list[str] = []
    for match in _CJK_RE.finditer(masked):
        snippet = masked[max(0, match.start() - 12) : match.start() + 12]
        hits.append(snippet.strip())
    return hits


# --------------------------------------------------------------------------
# LLM 翻译
# --------------------------------------------------------------------------
TRANSLATE_SYSTEM = """You are a cinematographer translating a Chinese shot description into an English prompt for the MiniMax H3 text-to-video model.

Hard requirements:
1. Output ONE JSON object only. No markdown fences, no commentary.
2. `beats_en` describes what is VISIBLE. Each entry is one continuous beat with a concrete subject, action, setting and lighting. Do NOT describe the voiceover.
3. `camera_en` MUST be one of: Zoom In, Zoom Out, Push In, Pull Out, Pan Left, Pan Right, Truck Left, Truck Right, Tilt Up, Tilt Down, Pedestal Up, Pedestal Down, Arc Shot, Tracking Shot, Static Shot, Shake Slightly, Shake Strongly, POV, Roll Clockwise, Roll Counterclockwise.
4. `ambience_en`: 1-4 English sentences. Ambient sound, physical sounds, non-verbal human sounds only. Never dialogue or score.
5. `music_en`: 1-3 English sentences of audience-only score (instrumentation, tempo, rhythm, dynamics). Do NOT describe mood or narrative function. Use "N/A" if none.
6. `visible_text`: readable on-screen text, ALWAYS in English. The input `on_screen_text`
   field may contain CHINESE placeholders such as "[年款标识]" — you MUST rewrite every one of
   them into an English placeholder. A video model cannot render Chinese characters inside
   brackets: it paints them as illegible pseudo-glyphs, so "[年款标识]" comes out as garbage
   on screen. Use this mapping:
     [年款标识]        -> [MODEL YEAR BADGE]
     [续航版本标识]    -> [RANGE TRIM BADGE]
     [百公里能耗]      -> [ENERGY CONSUMPTION PER 100KM]
     [车机界面]        -> [INFOTAINION SCREEN]
     [仪表盘]          -> [INSTRUMENT CLUSTER]
     [价格] / [售价]   -> [PRICE TAG]
   For any Chinese placeholder not listed here, output "[ON-SCREEN LABEL]" instead of Chinese.
   NEVER put Chinese characters inside `visible_text`, and NEVER invent a brand, model or slogan.
7. Everything must be in English except `dialogue_zh`, which stays in the original Chinese and must be copied VERBATIM.
8. `subject_en`: the subject's brand + model name in English, romanized (e.g. "BYD Han EV" for 比亚迪汉 EV). This is REQUIRED and must not be generic.
9. `subject_en_secondary`: ONLY for shots whose section needs a second vehicle on screen (side-by-side
   comparison, or the previous model year): the OTHER vehicle's brand + model in English, romanized.
   Return "" when this shot shows a single vehicle. Do NOT invent a competitor or model name that was
   not supplied — if the section names no second vehicle, return "".

IDENTIFYING THE SUBJECT (this is what makes the video show the right product):
10. The first thing `beats_en` must do is NAME the subject using `subject_en`. Never describe
   it generically as "a sedan" / "a laptop" / "a bottle" — a generic noun produces a generic
   product, which is exactly the failure this rule exists to prevent.
11. Add the subject's most recognisable, verifiable visual traits (silhouette, signature
    design elements, proportions). These must come from the subject name and general
    knowledge of that model — do NOT invent specifications.
12. Keep the naming in `beats_en` only. `visible_text` still uses placeholders (rule 6).

Required keys: beats_en, camera_en, ambience_en, music_en, visible_text, dialogue_zh, subject_en"""

TRANSLATE_USER = """Subject: {subject}
Shot section: {section}
Section beat direction: {beat}
Chinese visual description: {visual}
Chinese camera direction: {camera}
Chinese ambience direction: {ambience}
Chinese narration (verbatim, do not translate, do not include in beats): {narration}
Requested on-screen text: {on_screen_text}
Shot duration in seconds: {duration}
Aspect ratio: {ratio}

Return the JSON object now."""


def translate_shot(
    spec: ShotSpec,
    *,
    subject: str,
    section: SectionSpec | None,
    llm,
    narration_mode: str = "tts",
    ratio: str = "9:16",
) -> ShotTranslation:
    """调用 LLM 把中文分镜翻成英文结构化描述。"""
    sec = section or SectionSpec(name=spec.section or "")
    payload = {
        "subject": subject,
        "section": sec.name,
        "beat": sec.beat or spec.visual_zh,
        "visual": spec.visual_zh,
        "camera": sec.camera or spec.camera_zh,
        "ambience": sec.beat or spec.ambience_zh,
        "narration": spec.narration_zh if narration_mode == "native" else "(none - narration is on a separate audio track)",
        "on_screen_text": ", ".join(spec.on_screen_text) or "(none specified)",
        "duration": spec.duration,
        "ratio": ratio,
    }
    data = llm.structured(
        TRANSLATE_SYSTEM,
        TRANSLATE_USER.format(**payload),
        required_keys=["beats_en", "camera_en", "ambience_en", "music_en", "subject_en"],
        temperature=0.4,
    )

    beats_raw = data.get("beats_en") or []
    if isinstance(beats_raw, str):
        beats_raw = [beats_raw]
    beats = [str(b).strip() for b in beats_raw if str(b).strip()]

    visible = data.get("visible_text") or []
    if isinstance(visible, str):
        visible = [visible]
    # 提示词里可见文字允许中文(品牌名常为中文),但必须用引号包裹,
    # 因此这里原样保留,由 validate() 的引号规则兜底。
    visible_text = [str(v).strip() for v in visible if str(v).strip()]

    dialogue = str(data.get("dialogue_zh") or "").strip()
    if narration_mode != "native":
        dialogue = ""
    elif spec.narration_zh and not dialogue:
        dialogue = spec.narration_zh.strip()

    subject_en = normalise_subject_en(data.get("subject_en"), subject)
    # 第二主体走 normalise 之外的宽松路径:它本来就是可选增强,
    # 拿不到就留空,绝不 fallback 成主主体名(那会在画面里点名两遍同一台车)。
    secondary = str(data.get("subject_en_secondary") or "").strip().strip("\"'")
    if secondary.lower() in _GENERIC_SUBJECTS or secondary.lower() == subject_en.lower():
        secondary = ""

    return ShotTranslation(
        beats_en=beats,
        ambience_en=str(data.get("ambience_en") or "").strip(),
        music_en=str(data.get("music_en") or "").strip() or "N/A",
        visible_text=visible_text,
        dialogue_zh=dialogue,
        subject_en=subject_en,
        subject_en_secondary=secondary,
        camera_en=str(data.get("camera_en") or "").strip(),
        has_visible_speaker=bool(data.get("has_visible_speaker", False)),
    )


#: 泛化词:模型把主体名退化成这些词时,等于没点名
_GENERIC_SUBJECTS = {
    "a sedan", "sedan", "a car", "car", "a vehicle", "vehicle", "the product",
    "product", "a laptop", "laptop", "a phone", "phone", "a bottle", "bottle",
    "a model", "model", "it", "n/a", "none", "unknown",
}


def normalise_subject_en(value: object, fallback: str) -> str:
    """把 LLM 给的主体名清洗成可用的英文锚点。

    拿不到就退回中文原文 —— 宁可提示词里出现带引号的中文品牌名,
    也不要一个泛化名词。后者会被 validate() 的规则 9 拦下。
    """
    text = str(value or "").strip().strip("\"'")
    text = text.rstrip(".,;:").strip()
    if not text:
        return fallback.strip()
    # 去掉冠词再比泛化词:"The car" / "A sedan" 都等于没点名
    probe = re.sub(r"^(the|a|an)\s+", "", text, flags=re.IGNORECASE).strip().lower()
    if probe in _GENERIC_SUBJECTS:
        return fallback.strip()
    # 含中文说明模型没翻译,原样保留(validate 会用引号规则放行)
    return text


# --------------------------------------------------------------------------
# 一站式入口
# --------------------------------------------------------------------------
def compile_shot(
    spec: ShotSpec,
    translation: ShotTranslation,
    *,
    has_first_frame: bool = False,
    narration_mode: str = "tts",
    section: SectionSpec | None = None,
    max_rebuild: int = 2,
) -> tuple[str, list[str]]:
    """拼装 + 校验。返回 (提示词, 错误列表)。

    `max_rebuild` 保留给上层做"结构化微调后再编译"的钩子;
    本函数是纯函数,不做自动重试,重试逻辑放在调用方以便记录与观测。
    """
    prompt = assemble(
        spec,
        translation,
        has_first_frame=has_first_frame,
        narration_mode=narration_mode,
        section=section,
    )
    errors = validate(
        prompt,
        duration=spec.duration,
        expect_first_frame=has_first_frame,
        narration_mode=narration_mode,
        subject_en=translation.subject_en,
    )
    return prompt, errors


def compile_with_llm(
    spec: ShotSpec,
    *,
    subject: str,
    profile: CategoryProfile,
    llm,
    narration_mode: str = "tts",
    has_first_frame: bool = False,
) -> tuple[str, list[str]]:
    """翻译 + 拼装 + 校验的一站式版本(需要可用的 LLM)。"""
    section = profile.section(spec.section)
    translation = translate_shot(
        spec,
        subject=subject,
        section=section,
        llm=llm,
        narration_mode=narration_mode,
        ratio=profile.aspect_ratio,
    )
    return compile_shot(
        spec,
        translation,
        has_first_frame=has_first_frame,
        narration_mode=narration_mode,
        section=section,
    )
