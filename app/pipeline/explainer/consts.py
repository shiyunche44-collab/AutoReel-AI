"""常量与提示词模板。"""
from __future__ import annotations

import logging
from ... import config
from .. import aspect
from .. import render as render_mod


_log = logging.getLogger(__name__)

LAYOUTS: tuple[str, ...] = ("broll", "panel", "highlight", "card", "stat", "concept", "hook")
#: 不需要任何数据就能成立的版式 —— 其它版式核对失败都退回它
SAFE_LAYOUT = "broll"
#: 黄金三秒:开场拍前几秒是快切 + 大字的冷开场,每刀多长、大字停多久
HOOK_SECONDS = 3.0
HOOK_CUT = 1.0
#: 开场一刀实测发黑时最多重取几次
HOOK_CUT_RETRIES = 3
#: 连续几拍整屏卡片就太像 PPT 了
MAX_CARD_RUN = 2
#: 概念动画全片最多几拍(每拍要渲染半分钟左右,也不该喧宾夺主)
MAX_CONCEPT = 6
#: 连着最多几拍 broll(都是底图换镜头,没有任何叠加层)。再长就是整段静默
MAX_BROLL_RUN = 3
#: broll 拍的样式,按出现顺序轮换(见 assign_broll_styles)
BROLL_STYLES: tuple[str, ...] = ("plain", "lower_third", "split")
#: 分屏至少要这么长,太短的拍几栏还没滑完就切走了
SPLIT_MIN_SECONDS = 2.5
#: 拍内相邻两段之间的转场时长与样式(按段号轮换)
XFADE = 0.3
XFADE_KINDS: tuple[str, ...] = ("fade", "smoothleft", "slideleft", "circleopen", "zoomin",
                                "fadewhite")
#: 关键词每行最多几个字
KEYWORD_CHARS = 12
#: 横屏字幕单行字数。竖屏是 15 —— 两者都定义在 `aspect.LINE_CHARS`,按项目取用 `aspect.line_chars`
SUBTITLE_LINE_CHARS = aspect.LINE_CHARS[config.LANDSCAPE]
#: 图片背景的运镜(按片段散开挑,见 motion_for)
STILL_MOTIONS = render_mod.STILL_MOTIONS
#: 中间产物的编码参数:拼接用 -c copy,所以所有片段必须完全一致
_ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(render_mod.FPS)]

SYSTEM = """你是信息型{domain}解说视频(横屏 16:9,对标「三驱兄弟」)的剪辑导演。
画面是{subject_noun}的官方宣传片做背景,信息靠叠加层传达。你的任务:给每一拍选**版式**并填叠加数据。

版式:
- broll:全屏宣传片,无叠加。旁白在讲感受、观点、过渡时用。
- panel:宣传片 + 左侧参数面板。旁白提到 2 个以上参数时用。给 `panel`:
  {{"title": "面板标题(10 字内)", "rows": [{{"label": "参数名", "values": ["取值含单位"], "fact_id": "f_xxx"}}]}}
  rows 2–5 行。
- highlight:宣传片缩成小窗 + 两行大字。旁白在强调一个卖点 / 关键配置 / 结论时用。给 `keywords`:
  1–2 行,每行 {kw} 字以内,是这一拍最想让观众记住的词(如 {keyword_examples})。
- hook:**只有第 1 拍可选**(黄金三秒冷开场):前 3 秒快切 + 屏幕中央大字弹出。给 `keywords`
  (1–2 行,每行 {kw} 字以内,是旁白第一句里最抓人的词),旁白第一句若围绕一个核心数字,
  再给 `stat`(同下)。段落「可选版式」里有 hook 就优先用它。
- card:整屏数据卡。段落标了「卡片」时优先用。给 `card`:
  {{"title": "卡片标题(12 字内)", "columns": [两个对比对象名,仅对比卡需要],
    "rows": [{{"label": "...", "values": ["..."], "fact_id": "f_xxx"}}]}}
- 对比卡(compare):每行引用**一条同时写着两台车取值的事实**(事实清单里带 [对比:车名] 的那些),
  `values` 按 `columns` 的顺序给两台车的取值,只填这一条 fact_id。两台车没写在同一条事实里
  就别做对比卡 —— 核不过会自动作废。口径不一致的数字不放在一起比。
- stat:宣传片压暗 + 中央一个大数字滚动。旁白只围绕**一个**核心数字时用({stat_examples})。给 `stat`:{{"label": "参数名(8 字内)", "value": "取值含单位", "fact_id": "f_xxx",
  "note": "这个数字对观众意味着什么({note_chars} 字内,从这一拍旁白里取,如「吃顿饭的工夫」「同价位最低一档」)"}}{concept_guide}

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或解释文字。
2. `beats` 数组,每项 {{"idx": 序号, "layout": 版式, "view": "{view_enum}",
   "visual": "背景画面一句话(15 字内)", "panel": ..., "keywords": [...], "card": ...,
   "stat": ...}};
   用不到的键可以省略。数量与给定拍数一致,idx 从 1 开始连续。
3. layout 只能从该拍「可选版式」里选。
4. panel / card 的每一行和 stat 的数字**只能**取自「可用事实」,fact_id 填那条事实的 id;values 里的数字
   必须与事实原文一字不差(含单位),不得换算、取整或估算。找不到就换版式,不要编。
5. keywords 里的型号、芯片名、英文和数字必须在这一拍的旁白或可用事实里出现过,不得自创。
6. 节奏:不要连续 3 拍以上用 card;panel、highlight、stat 穿插着用,不要连续两拍 stat,
   **全片 broll 不超过三成**(共 {count} 拍,最多 {broll_cap} 拍)。旁白里讲到了参数、数字,
   这一拍就用 panel / stat / concept 把它放上屏 —— broll 只留给讲感受、讲取舍、做过渡的拍。
7. view:{view_guide}

必需键: beats"""

USER = """{subject_label}:{subject}
总拍数:{count}

逐拍清单(版式要服务于这一拍的旁白):
{beats}

可用事实:
{facts}

输出 JSON。"""
