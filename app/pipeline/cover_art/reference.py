"""官方参照图与车的提示词:挑参照、问视觉模型车型 / 颜色。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Sequence
from ... import config

from .tone import log


# --------------------------------------------------------------------------
# 车:素材池抠不出白底官图时的兜底 —— 生图生成一台车
# --------------------------------------------------------------------------
#: 素材池里没有白底棚拍官图很常见:官方图库给的都是带背景的实拍 / 定妆照,
#: 抠图算法的前提不成立,封面就只能退回老模板 —— 于是同一个号的两条片子封面长得不一样。
#: 这里用生图模型按车名生成一台**纯白底**的车,再抠图合进同一套版式。
#:
#: 车型对不对是这套方案最大的风险,靠三道门挡:
#:   G1 能抠图 —— 纯白底、车不贴边、面积正常(直接用现有 motion_assets.cutout);
#:   G2 干净   —— 视觉模型确认图里没有文字 / 车标 / 水印 / 第二辆车;
#:   G3 同款   —— 生成图与官方参照图拼成一张,视觉模型判"同一款车"才放行。
#: 拿不到结论一律判负 —— 宁可退回老封面,也不能把一台陌生的车推上封面。
#: 用了 AI 车时封面会带一句披露(见 explainer.compose)。

#: 生图提示词。机位固定「前 45°、车头朝右」,和白底官图那条路出来的封面是同一个机位,
#: 主页一排看过去像同一条产线出来的。
#:
#: `{look}` 是从官方参照图里提取的造型特征(前脸、灯带、轮毂、比例)。单靠"BYD Tang"几个字
#: 生图模型认不准 —— 实测它会画成一台起亚 Sportage,还带 KIA 车标。**把形状说清楚**才画得
#: 像;车标必须留白 —— 封面上出现别家车标,比画得不够像严重得多。
CAR_PROMPT = (
    "Studio product photograph of {model}, {color} {body}, the exact same car model as the "
    "official manufacturer photo of this vehicle. Exterior design: {look}. "
    "Front three-quarter view with the nose pointing to the right, the whole vehicle visible in "
    "frame with generous empty margin on every side, centered on a pure white seamless studio "
    "background, soft even lighting, no ground shadow, no props, photorealistic, 8k. "
    "Shot like an unbranded design prototype: the front nose is completely smooth and blank with "
    "no emblem and no marque badge, the body carries no lettering and no model name, and the "
    "license plate is a plain blank white rectangle with no text and no numbers. "
    "It is a clean production car: no roof box, no roof rails, no roof cargo, no accessories; "
    "only a low flat sensor pod integrated flush into the roof. "
    "No text, no letters, no numbers, no logo, no badge, no watermark, no people, no second car."
)

#: 图生图提示词(有官方参照图时用这个)。
#:
#: 有参照图时**以图为准**:车型、车身线条、灯带、轮毂全由参照图决定,文字只负责约束
#: 背景和机位。只给车名的话生图模型会画成"长得像那一类"的车 —— 实测给"BYD Tang"
#: 生出来的是一台起亚 Sportage。
CAR_REF_PROMPT = (
    "Studio product photograph of the exact same car shown in the reference image: reproduce "
    "that identical vehicle, keeping its precise body shape, proportions, front fascia, "
    "connected LED light bar, grille, wheel design, roof line and roof sensor pod, in the same "
    "body colour. Front three-quarter view with the nose pointing to the right, the whole "
    "vehicle visible in frame with generous empty margin on every side, centered on a pure "
    "white seamless studio background, soft even lighting, no ground shadow, no props, "
    "photorealistic, 8k. "
    "Shot like an unbranded design prototype: the front nose is completely smooth and blank with "
    "no emblem and no marque badge, the body carries no lettering and no model name, and the "
    "license plate is a plain blank white rectangle with no text and no numbers. "
    "It is a clean production car: no roof box, no roof rails, no roof cargo, no accessories; "
    "only a low flat sensor pod integrated flush into the roof. "
    "No text, no letters, no numbers, no logo, no badge, no watermark, no people, no second car."
)

#: 生图模式的尝试顺序。
#:
#: `ref` = 以官方参照图做**图生图**(车型由图决定,最可能对);`t2i` = 纯文字生图
#: (车型靠提示词硬凑,大概率过不了同款门)。前者只有 Connector 后端支持,且
#: `mcode-tools` 只在 MiniMax Code 运行时里存在 —— 拿不到就退回后者,再不行退老封面。
CAR_MODES = ("ref", "t2i")

#: 封面版式固定的车头朝向。第一支(小米YU7)的官方官图 `img_ca4c52a242` 就是车头朝右,
#: 后面所有项目的封面都跟它一致 —— 主页一排看过去是同一条产线。
COVER_CAR_FACING = "right"

#: 中文颜色词 → 英文(参照图 desc 里写的是中文,提示词用英文更稳)
COLOR_WORDS: dict[str, str] = {
    "白": "white", "银": "silver", "灰": "grey", "黑": "black", "红": "red", "蓝": "blue",
    "绿": "green", "金": "gold", "棕": "brown", "黄": "yellow", "米": "beige",
}
#: 车身类型(desc / 视觉模型都可能写中文)
BODY_WORDS: dict[str, str] = {
    "SUV": "SUV", "suv": "SUV", "跨界": "crossover SUV", "轿车": "sedan", "旅行": "wagon",
    "MPV": "MPV", "mpv": "MPV", "两厢": "hatchback", "掀背": "hatchback", "皮卡": "pickup truck",
}
#: desc 里带这些词 = 影棚 / 定妆照,形体与配色最干净,当参照图最合适
STUDIO_HINTS = ("白底", "定妆", "影棚", "产品图", "纯色背景", "棚拍")

#: 问参照图:车型 / 颜色 / 车身类型 / 造型特征 / 车头朝向
REF_DESC_PROMPT = (
    "Look at this official car photo. Answer with exactly five lines and nothing else:\n"
    "MODEL: <make and model in English, e.g. BYD Tang>\n"
    "COLOR: <body color in English, e.g. silver white>\n"
    "BODY: <one of SUV / sedan / MPV / hatchback / wagon / pickup truck>\n"
    "LOOK: <one short English phrase (max 25 words) describing the distinctive exterior design: "
    "front face, light bar, grille, wheels, roof, body proportions>\n"
    "FACING: <left if the front of the car points to the left side of the image, "
    "right if it points to the right side>"
)

#: 参照图描述的缓存版本:改了问法就升版本,旧答案自然作废
REF_DESC_VERSION = 3

#: 质检门判据版本。改了任何一道门的问法就升版本,旧缓存里的结论自动作废重跑。
GATES_VERSION = 2

#: G2:图里有没有**不该出现**的东西(有就答 YES)
#:
#: ⚠️ 问法要精确,两头都会出事:
#: - 问得太宽("有没有 logo/badge")→ 视觉模型把**原厂**轮毂中心盖的徽章、空白车牌框
#:   都算成 logo,四张干净候选全被误杀(实测)。
#: - 问得太窄("ignore the vehicle's own badges")→ 连**别家车标**一起放过了,带着
#:   一汽标的起亚 Sportage 判成干净(实测)。
#:
#: 所以这道门只问四件硬伤,**不碰车标**:
#:   ① 可读文字 / 有字的车牌(汽车之家水印、车名字样);
#:   ② 水印 / 文案 / 第二辆车 / 道具;
#:   ③ 车顶行李箱、行李架、拖车钩这类**加装** —— 官方图库里那台车没有,加上就是另一台车。
#: 车标属于哪家公司由 G3(同款门)兜:跟官方参照图一比,别家车标自然露馅。
CAR_CLEAN_PROMPT = (
    "Look at this studio photo of a car on a plain white background. Answer YES if any of the "
    "following is visible: (1) readable words, letters or numbers anywhere, including a licence "
    "plate that has characters on it; (2) a watermark, caption or website name anywhere in the "
    "image; (3) a second vehicle, a person or any prop in the scene; (4) add-on equipment fitted "
    "to the car such as a roof box, roof rails, a roof rack, luggage on the roof or a tow bar. "
    "Answer NO if none of these is present. Answer with only YES or NO."
)

#: G3:生成的车和官方参照是不是同一款(是就答 YES)
#:
#: 这道门同时兜住"别家车标":实测带 KIA 标 / 一汽标的候选,跟官方参照图一比全部判否 ——
#: 车标不对意味着车就不对,不用再单独设一道车标门(那道门实测不可靠:问"车标是谁家的"
#: 它把原厂轮毂徽章读成别家厂牌,问"是不是别家标"它把干净候选也判成别家,两头都错)。
CAR_SAME_PROMPT = (
    "Do the two images show the same car make and model? Answer with only YES or NO."
)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cars_root() -> Path:
    return config.DATA_DIR / "_cache" / "cover_theme" / "cars"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.part{path.suffix}")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)


def _studio_hint(desc: str) -> bool:
    return any(w in (desc or "") for w in STUDIO_HINTS)


def _desc_color(desc: str) -> str:
    """从 desc 里取车身颜色。

    ⚠️ 先把「白底 / 影棚 / 定妆照」这类**背景**词去掉:「白底定妆照」里的白说的是背景,
    不是车身颜色 —— 当成车色就等于让模型画一台白车。"""
    text = desc or ""
    for hint in STUDIO_HINTS:
        text = text.replace(hint, " ")
    for zh, en in COLOR_WORDS.items():
        if zh in text:
            return en
    return ""


def _desc_body(desc: str) -> str:
    for zh, en in BODY_WORDS.items():
        if zh in (desc or ""):
            return en
    return ""


def pick_reference(subject: str, exclude: Sequence[str | Path] = ()) -> tuple[Path, str] | None:
    """挑一张**只用来比对和写提示词**、绝不上封面的官方图。

    参照图必须认得出就是这台车(`target == "yes"`),否则拿它比"同款"就是拿错车比。
    官方图库的照片会带别家的角标 / 车牌标识 —— 参照图只在本地比对,不会进封面。"""
    from .. import library

    if not subject:
        return None
    try:
        lib = library.Library.load(subject)
    except Exception:  # noqa: BLE001
        return None
    vocab = library.vocab_of(lib)
    hero = vocab.primary_views or set(vocab.views)
    used = {str(Path(p)) for p in exclude}
    cands = [i for i in lib.items
             if i.kind == "still" and i.usable and not i.small_still and Path(i.path).is_file()
             and i.problem not in ("cropped", "unclear", "poster")
             and i.view in hero and i.target == "yes"
             and str(Path(i.path)) not in used]
    if not cands:
        return None
    cands.sort(key=lambda i: (not _studio_hint(i.desc), -Path(i.path).stat().st_size, i.id))
    best = cands[0]
    return Path(best.path), str(best.desc or "")


def _ask_ref_attributes(ref: Path) -> dict[str, str]:
    """问视觉模型:参照图里是什么车、什么颜色、什么车身类型、造型上有什么特点、车标是谁家的。"""
    from ...providers import cli, images

    try:
        url = cli.upload_temp_url(ref)
        text = images.describe([url], REF_DESC_PROMPT)
    except Exception as exc:  # noqa: BLE001
        log.info("参照图描述不可用:%s", exc)
        return {}
    attrs: dict[str, str] = {}
    for key, tag in (("model", "MODEL"), ("color", "COLOR"), ("body", "BODY"),
                     ("look", "LOOK"), ("facing", "FACING")):
        m = re.search(rf"{tag}\s*[:：]\s*(.+)", text or "", re.IGNORECASE)
        if m:
            attrs[key] = m.group(1).strip().strip("。.")
    return attrs


def ref_attributes(ref: Path, desc: str = "") -> dict[str, str]:
    """参照图的车型 / 颜色 / 车身类型 / 造型特征。视觉模型优先,desc 里的中文兜底。

    结果按参照图缓存 —— 同一个车型反复重出封面不再重复问视觉模型。"""
    path = _cars_root() / "_refs" / f"{_hash(str(ref))[:16]}_v{REF_DESC_VERSION}.json"
    attrs = _read_json(path)
    if not attrs:
        attrs = _ask_ref_attributes(ref)
        _write_json(path, attrs)
    if not attrs.get("color"):
        attrs["color"] = _desc_color(desc)
    if not attrs.get("body"):
        attrs["body"] = _desc_body(desc)
    return attrs


def car_prompt(subject: str, attrs: dict[str, str]) -> str:
    """生成那台车的提示词。

    车型用视觉模型认出来的英文名(带中文原名兜底),颜色、车身形态同理 —— 官方图库里
    同一个车型有多个颜色版本,写错颜色等于换了一台车。造型特征(`look`)同样来自参照图:
    只给车型名字,生图模型会画成一台"长得像那一类"的车;把前脸、灯带、轮毂说清楚才像。"""
    model = (attrs.get("model") or "").strip() or subject
    if subject and subject not in model:
        model = f"{model} ({subject})"
    color = (attrs.get("color") or "").strip() or "silver"
    body = (attrs.get("body") or "").strip() or "SUV"
    look = (attrs.get("look") or "").strip() or (
        "a modern Chinese car with clean body surfaces, hidden door handles and large alloy wheels")
    return CAR_PROMPT.format(model=model, color=color, body=body, look=look)
