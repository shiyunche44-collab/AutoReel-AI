"""统一风格封面:浅色影棚底(MiniMax 生图)+ 上方超大粗字标题 + 下方真车官图抠图。

版式参考高赞选车号:浅色柔和渐变底,第一行深色粗体大字(车名 / 价格 / 问题),第二行强调色副标,
车放在下半部,完整、干净、占画面一半。信息一眼看完。

- 风格统一靠固定的 4 个色调 `TONES`(底色 + 主字色 + 强调色)和固定版式;项目按 id 落到某个色调,
  B 版换下一个色调,A/B 一眼分得开。
- 底图只生成**空影棚**(不出车不出字),每个色调 × 画幅只生一次,缓存在 `data/_cache/cover_theme/`,
  所有项目共用。生图失败用同色调的代码渐变垫底,底图永远拿得到。
- 车用素材池里的白底官图抠出来(`motion_assets.cutout`),车型不会画错。**抠不出来**(不是白底图、
  非车品类)时,`ai_car` 用生图模型按车名生成一台纯白底的车再抠图 —— 车型可能有出入,所以过三道
  质检门(能抠图 / 干净 / 与官方图同款),并且封面会带一句 AI 披露。两条路都不成才退回
  `narration.make_cover` 的老封面。封面不能拦住成片。
"""

from __future__ import annotations

import hashlib  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Sequence  # noqa: F401
from ... import config  # noqa: F401

from .tone import (  # noqa: E402,F401
    BG_MAX_SPREAD,
    RATIOS,
    STUDIO_PROMPT,
    STYLE_VERSION,
    TONES,
    Tone,
    VLM_CHECK,
    _is_light,
    _picked_tone,
    bg_index,
    cache_dir,
    log,
    ratio_of,
    tone_of,
)
from .studio import (  # noqa: E402,F401
    _correct_bg,
    _fit_bg,
    _luma,
    _upper_spread,
    _vlm_says_clean,
    background,
    generate,
    gradient_bg,
    studio_bg,
)
from .reference import (  # noqa: E402,F401
    BODY_WORDS,
    CAR_CLEAN_PROMPT,
    CAR_MODES,
    CAR_PROMPT,
    CAR_REF_PROMPT,
    CAR_SAME_PROMPT,
    COLOR_WORDS,
    COVER_CAR_FACING,
    GATES_VERSION,
    REF_DESC_PROMPT,
    REF_DESC_VERSION,
    STUDIO_HINTS,
    _ask_ref_attributes,
    _cars_root,
    _desc_body,
    _desc_color,
    _hash,
    _read_json,
    _studio_hint,
    _write_json,
    car_prompt,
    pick_reference,
    ref_attributes,
)
from .car_ai import (  # noqa: E402,F401
    CarVisual,
    VIEW_RANK,
    _ai_car_pass,
    _car_modes,
    _car_trace,
    _compare_pair,
    _flat_on_white,
    _generate_with_reference,
    _vlm_answer,
    _white_normalized,
    ai_car,
    pick_car,
    pick_visual,
)
from .plates import (  # noqa: E402,F401
    _car_layer,
    find_plates,
    find_yellow_plates,
    scrub_body_plates,
    scrub_plates,
    scrub_watermarks,
)
from .layout import (  # noqa: E402,F401
    Frame,
    _NO_END,
    _NO_START,
    _car_fit,
    _face,
    _faces,
    _fit,
    _font,
    _ghost,
    _ink,
    _line_h,
    _pill,
    _soft_cuts,
    _text,
    _title_lines,
    model_word,
    place_car,
    render,
    short_name,
    split_title,
    strip_figure,
    usable_hook,
)
from .builder import (  # noqa: E402,F401
    _save,
    b_title,
    build,
    compose,
    pick_title,
    rebuild,
)
from .cli import (  # noqa: E402,F401
    main,
)
