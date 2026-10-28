"""封面色调:Tone、画幅、按项目稳定挑色调。"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from ... import config


log = logging.getLogger(__name__)

#: 改了提示词 / 色调就升版本号,旧图自然失效(不会混用两套风格)
STYLE_VERSION = 3


@dataclass(frozen=True)
class Tone:
    name: str
    #: 写进生图提示词的底色描述
    prompt: str
    #: 生图失败时代码渐变的上 / 下两色
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    text: tuple[int, int, int]
    accent: tuple[int, int, int]
    #: 底图平均亮度的合格区间(浅底配深字要够亮,灰绿底配白字要够暗)
    luma: tuple[int, int] = (150, 250)

    @property
    def light_text(self) -> bool:
        return sum(self.text) > 600


TONES = (
    Tone("beige", "warm cream beige fading to soft sand", (240, 232, 218), (214, 202, 184),
         (32, 34, 38), (40, 98, 74)),
    Tone("sage", "muted sage grey-green, slightly deeper towards the floor", (112, 136, 130),
         (86, 108, 104), (250, 250, 247), (246, 214, 140), luma=(70, 150)),  # 再亮白字就压不住
    Tone("mist", "pale misty blue-grey", (226, 232, 238), (196, 205, 216),
         (30, 34, 42), (28, 84, 176)),
    Tone("silver", "light neutral silver grey", (232, 232, 230), (204, 204, 202),
         (30, 32, 36), (178, 56, 44)),
)

STUDIO_PROMPT = (
    "Minimalist seamless photo studio backdrop for a premium car advertisement, {tone} colour, "
    "smooth even wall with a gentle vertical gradient in the upper two thirds, light matte floor "
    "in the lower third with a soft subtle reflection and diffused light pooling in the centre, "
    "clean, airy, high-end commercial look, photorealistic, 8k. Completely empty scene: no car, "
    "no vehicle, no objects, no people, no text, no letters, no logo, no watermark."
)

RATIOS = {"16x9": "16:9", "9x16": "9:16", "3x4": "3:4"}

#: 上半部(压字的地方)亮度标准差上限:太花字就压不住
BG_MAX_SPREAD = 26
VLM_CHECK = ("Does this image contain a car or any vehicle, readable text or letters, a logo, "
             "or a watermark? Answer with only YES or NO.")



# --------------------------------------------------------------------------
# 底图:生图与缓存
# --------------------------------------------------------------------------
def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "cover_theme" / f"v{STYLE_VERSION}"


def ratio_of(size: tuple[int, int]) -> str:
    w, h = size
    if w >= h:
        return "16x9"
    return "9x16" if h / w > 1.5 else "3x4"


def _is_light(tone: Tone) -> bool:
    """浅底(深字)还是暗底(浅字)。"""
    return tone.luma[0] > 120


def bg_index(project_id: str, variant: str = "a") -> int:
    """色调下标:同一项目每次都一样;B 版取下一个,保证和 A 不同。

    项目变体里点了封面底色(`knobs` 的 cover_tone)就从那个色起;没点按项目 ID 散列。

    **B 版不能落到暗底**:四种底色里只有一种是暗的,主页一排封面如果混进一张
    暗底白字的,看着就像另一个人做的(实跑踩过:A 版米色,B 版正好轮到唯一的暗色)。
    """
    base = int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(TONES)
    picked = _picked_tone(project_id)
    ids = [t.name for t in TONES]
    if picked in ids:
        base = ids.index(picked)
    if variant != "b":
        return base
    idx = (base + 1) % len(TONES)
    if _is_light(TONES[base]) and not _is_light(TONES[idx]):
        for step in (2, 3):  # 往后找一个同样是浅底的
            cand = (base + step) % len(TONES)
            if _is_light(TONES[cand]):
                return cand
    return idx


def _picked_tone(project_id: str) -> str:
    from ... import db
    from .. import variety

    try:
        return str(variety.parse((db.get_project(project_id) or {}).get("variant")).get("cover_tone") or "")
    except Exception:  # noqa: BLE001 - 读不到变体就按散列
        return ""


def tone_of(project_id: str, variant: str = "a") -> Tone:
    return TONES[bg_index(project_id, variant)]
