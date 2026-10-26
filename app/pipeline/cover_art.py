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

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .. import config

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


def bg_index(project_id: str, variant: str = "a") -> int:
    """色调下标:同一项目每次都一样;B 版取下一个,保证和 A 不同。

    项目变体里点了封面底色(`knobs` 的 cover_tone)就从那个色起;没点按项目 ID 散列。"""
    base = int(hashlib.sha1(project_id.encode("utf-8")).hexdigest(), 16) % len(TONES)
    picked = _picked_tone(project_id)
    ids = [t.name for t in TONES]
    if picked in ids:
        base = ids.index(picked)
    return (base + (1 if variant == "b" else 0)) % len(TONES)


def _picked_tone(project_id: str) -> str:
    from .. import db
    from . import variety

    try:
        return str(variety.parse((db.get_project(project_id) or {}).get("variant")).get("cover_tone") or "")
    except Exception:  # noqa: BLE001 - 读不到变体就按散列
        return ""


def tone_of(project_id: str, variant: str = "a") -> Tone:
    return TONES[bg_index(project_id, variant)]


def _luma(image: Any) -> float:
    from PIL import ImageStat

    return float(ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])


def _upper_spread(image: Any) -> float:
    from PIL import ImageStat

    g = image.convert("L").resize((64, 64))
    return float(ImageStat.Stat(g.crop((0, 0, 64, 32))).stddev[0])


def _vlm_says_clean(path: Path) -> bool:
    """问视觉模型图里有没有车 / 字。模型不可用(返回空)时放行 —— 亮度质检已经过了。"""
    from ..providers import cli, images

    try:
        answer = images.describe([cli.upload_temp_url(path)], VLM_CHECK).strip().upper()
    except Exception as exc:  # noqa: BLE001
        log.info("封面底图视觉质检不可用,跳过:%s", exc)
        return True
    return not (answer.startswith("YES") or "YES" in answer[:12] or answer.startswith(("是", "有")))


def generate(prompt: str, ratio: str, dest: Path, *, luma: tuple[int, int] = (150, 250),
             retries: int = 2) -> Path | None:
    """按提示词生一张底图并质检,合格才写到 dest(已有就直接用)。失败返回 None。"""
    from PIL import Image

    from ..providers import images

    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(retries + 1):
        ref = images.generate(prompt, output_file=dest.name, aspect_ratio=RATIOS[ratio])
        if ref is None:
            log.warning("封面生图失败(%s,第 %d 次)", dest.name, attempt + 1)
            continue
        raw = dest.with_name(f"{dest.stem}.raw")
        try:
            images.download(ref.url, raw)
            with Image.open(raw) as im:
                im = im.convert("RGB")
            if not luma[0] <= _luma(im) <= luma[1] or _upper_spread(im) > BG_MAX_SPREAD:
                log.info("封面底图亮度 / 干净度不合格,重来(%s)", dest.name)
                continue
            tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
            im.save(tmp, quality=94)
            if not _vlm_says_clean(tmp):
                tmp.unlink(missing_ok=True)
                log.info("封面底图里有车或字,重来(%s)", dest.name)
                continue
            os.replace(tmp, dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            log.warning("封面底图下载/处理失败:%s", exc)
        finally:
            raw.unlink(missing_ok=True)
    return None


def studio_bg(tone: Tone, ratio: str) -> Path | None:
    """某色调某画幅的空影棚底图:缓存有就用,没有就生成。"""
    return generate(STUDIO_PROMPT.format(tone=tone.prompt), ratio,
                    cache_dir() / "studio" / f"{tone.name}_{ratio}.jpg", luma=tone.luma)


def gradient_bg(tone: Tone, size: tuple[int, int]) -> Any:
    """生图拿不到时的垫底:同色调的上下渐变 + 下方一片淡淡的地面光。"""
    from PIL import Image, ImageDraw, ImageFilter

    w, h = size
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(tone.top, tone.bottom)))
    glow = Image.new("L", size, 0)
    ImageDraw.Draw(glow).ellipse((w * 0.1, h * 0.72, w * 0.9, h * 1.02), fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(max(w, h) // 12))
    im.paste(Image.new("RGB", size, (255, 255, 255)), (0, 0), glow)
    return im


def _fit_bg(path: Path, size: tuple[int, int]) -> Any:
    """底图按 cover 方式铺满。"""
    from PIL import Image

    out_w, out_h = size
    with Image.open(path) as im:
        im = im.convert("RGB")
    k = max(out_w / im.width, out_h / im.height)
    im = im.resize((int(im.width * k) + 1, int(im.height * k) + 1), Image.LANCZOS)
    left, top = (im.width - out_w) // 2, (im.height - out_h) // 2
    return im.crop((left, top, left + out_w, top + out_h))


def background(tone: Tone, size: tuple[int, int]) -> tuple[Any, str]:
    """(画布, "studio"|"gradient")"""
    bg = studio_bg(tone, ratio_of(size))
    if bg is not None:
        try:
            return _fit_bg(bg, size), "studio"
        except OSError as exc:
            log.warning("封面底图读不了,用渐变:%s", exc)
    return gradient_bg(tone, size), "gradient"


# --------------------------------------------------------------------------
# 车
# --------------------------------------------------------------------------
#: 封面车图的视角偏好:前 45° 最像"这台车",正尾最差
VIEW_RANK = {"exterior_45": 0, "exterior_front": 1, "exterior_side": 2, "rear": 4}


def pick_car(subject: str, exclude: Sequence[str | Path] = ()) -> tuple[Any, Path] | None:
    """挑一张能抠干净的整车官图:白底棚拍优先、官方优先、大图优先。返回 (Cutout, 原图)。"""
    from . import library, motion_assets

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
             and i.view in hero
             and i.problem not in ("cropped", "unclear", "poster") and str(Path(i.path)) not in used]
    cands.sort(key=lambda i: (not library.is_studio_shot(i.path), VIEW_RANK.get(i.view, 9),
                              not i.official, i.target != "yes", -Path(i.path).stat().st_size, i.id))
    for item in cands[:10]:
        c = motion_assets.cutout(item.path, item_id=item.id)
        if c is not None:
            return c, Path(item.path)
    return None


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
    from . import library

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
    from ..providers import cli, images

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


def _white_normalized(src: Path, dest: Path) -> Path | None:
    """把背景刷成纯白,让抠图的门能过。

    生图要的"纯白无缝背景"常常差几档(暖灰 / 米白 / 地面渐变),`motion_assets.cutout`
    的门(边缘均值 ≥228、标准差 ≤18)就卡住。这里只动**和四边连通的片背景**:按边缘亮度
    定阈值、从四边洪水填充出背景、把背景刷成纯白 —— 车身像素不在背景蒙版里,不受影响。
    背景连不上四边(占比太小)就不刷,让这一版候选直接淘汰。"""
    import numpy as np
    from PIL import Image, ImageDraw

    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
    except OSError:
        return None
    g = np.asarray(im.convert("L"), dtype=np.int32)
    h, w = g.shape
    b = max(4, min(h, w) // 40)
    ring = np.concatenate([g[:b].ravel(), g[-b:].ravel(), g[:, :b].ravel(), g[:, -b:].ravel()])
    thr = max(150, int(float(np.percentile(ring, 55)) * 0.86))
    r, gg, bb = (np.asarray(im)[..., k].astype(np.int32) for k in range(3))
    luma = (r * 299 + gg * 587 + bb * 114) // 1000
    chroma = np.maximum(np.maximum(r, gg), bb) - np.minimum(np.minimum(r, gg), bb)
    cand = (luma >= thr) & (chroma <= 30)
    m = Image.fromarray(np.where(cand, 255, 0).astype(np.uint8), "L").copy()
    seeds = [(x, 0) for x in range(0, w, 8)] + [(x, h - 1) for x in range(0, w, 8)] + \
            [(0, y) for y in range(0, h, 8)] + [(w - 1, y) for y in range(0, h, 8)]
    for s in seeds:
        if m.getpixel(s) == 255:
            ImageDraw.floodfill(m, s, 128, thresh=0)
    bg = np.asarray(m) == 128
    if bg.mean() < 0.25:  # 背景没连上四边 → 不乱刷
        return None
    out = np.asarray(im).copy()
    out[bg] = 255
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out).save(dest, quality=95)
    return dest


def _flat_on_white(cut: Any, dest: Path) -> Path | None:
    """抠好的车放到白底上,给视觉模型看(透明 PNG 直接看是黑底)。

    顺手裁到车身范围:原始抠图 PNG 是整张白底画布,车只占中间一小块,
    拼成对比图之后车太小,视觉模型看不清前脸就判不出同款。"""
    from PIL import Image

    try:
        with Image.open(cut.png) as im:
            im = im.convert("RGBA")
    except OSError:
        return None
    box = im.getchannel("A").point(lambda v: 255 if v > 24 else 0).getbbox()
    if box:
        pad = int((box[2] - box[0]) * 0.04)
        im = im.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                      min(im.width, box[2] + pad), min(im.height, box[3] + pad)))
    canvas = Image.new("RGB", im.size, (255, 255, 255))
    canvas.paste(im, (0, 0), im)
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, quality=92)
    return dest


def _compare_pair(flat: Path, ref: Path, dest: Path, *, ref_facing: str = "right") -> Path | None:
    """生成图(白底) + 官方参照图拼成一张,让视觉模型一次看完两张。

    **两张摆成同朝向**才比:一张车头朝左、一张朝右,视觉模型会顺着"角度不一样"判否,
    比的其实不是车型。这里按参照图自己的朝向决定要不要镜像它,而不是写死翻转 ——
    官方定妆照有朝左的也有朝右的,写死翻会把本来同向的两张弄成反的。"""
    from PIL import Image

    try:
        with Image.open(flat) as im:
            a = im.convert("RGB")
        with Image.open(ref) as im:
            b = im.convert("RGB")
    except OSError:
        return None
    # 参照图朝左 → 镜像成朝右,和生成图对齐
    if str(ref_facing).strip().lower().startswith("l"):
        b = b.transpose(Image.FLIP_LEFT_RIGHT)
    height = 600

    def fit(x: Any) -> Any:
        k = height / x.height
        return x.resize((max(1, int(x.width * k)), height), Image.LANCZOS)

    a, b = fit(a), fit(b)
    canvas = Image.new("RGB", (a.width + b.width + 12, height), (255, 255, 255))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + 12, 0))
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(dest, quality=90)
    return dest


def _vlm_answer(path: Path | None, prompt: str) -> str:
    """问视觉模型一个是非题。返回 "YES" / "NO" / ""(拿不到答案)。

    这个视觉模型是中文的,回答可能是「是」「否」「没有」而不是 YES/NO —— 只认英文会把
    一张干净的图误杀成 "unknown"。两种都认。

    拿不到答案**不放行**:封面只有一帧,推错一台车的代价远大于退回老模板。"""
    from ..providers import cli, images

    if path is None:
        return ""
    try:
        url = cli.upload_temp_url(path)
        answer = images.describe([url], prompt)
    except Exception as exc:  # noqa: BLE001
        log.info("封面质检(视觉模型)不可用:%s", exc)
        return ""
    text = (answer or "").strip()
    if not text:
        return ""
    head = text.upper()
    if head.startswith("YES"):
        return "YES"
    if head.startswith("NO"):
        return "NO"
    if text.startswith(("是", "有", "确认")):
        return "YES"
    if text.startswith(("否", "没有", "不")):
        return "NO"
    log.info("封面质检:看不懂的回答 %r", text[:60])
    return ""


def _car_trace(meta: dict[str, Any]) -> dict[str, Any]:
    """写进 render.json 的溯源信息:这台车是生出来的,参照的是哪张官方图、出了几个候选。

    `car_seed` 只有单张生成时才有值 —— 多候选时种子交给服务端随机(传了反而 n 张长得一样)。
    `car_mode` 记的是走的哪条生图路:`ref` = 以官方参照图做图生图,`t2i` = 纯文字生图。"""
    return {"car_source": "ai_generated",
            "car_mode": str(meta.get("mode") or ""),
            "car_reference": str(meta.get("reference") or ""),
            "car_seed": int(meta.get("seed") or 0),
            "car_candidates": int(meta.get("candidates_requested") or 0),
            "car_prompt_hash": _hash(str(meta.get("prompt") or ""))[:12]}


def _car_modes() -> tuple[str, ...]:
    """生图模式的尝试顺序。图生图优先,文字生图兜底。

    图生图只有 Connector 后端有(直连的 `subject_reference` 只认人像),而
    `mcode-tools` 只在 MiniMax Code 运行时里存在 —— CLI 不在就只剩文字生图那条路。"""
    from ..providers import cli

    if cli.connector_available():
        return CAR_MODES
    return ("t2i",)


def _generate_with_reference(prompt: str, ref: Path, *, n: int,
                             aspect_ratio: str = "16:9") -> list[Any]:
    """以官方参照图做图生图,返回候选图 URL 列表。

    参照图必须先上传成**公网可达**的临时地址 —— 生图服务在远端,拿不到本机文件。
    直连后端的 `upload_temp_url` 给的是 data URI,只对同一次直连请求里的图片理解有效,
    所以这里强制走 Connector 的上传。
    """
    from ..providers import cli, images

    if not cli.connector_available():
        log.info("封面:图生图要 mcode-tools,当前不可用")
        return []
    try:
        ref_url = cli.upload_temp_url(ref, backend="mcode")
    except Exception as exc:  # noqa: BLE001
        log.info("封面:参照图上传失败:%s", exc)
        return []
    return images.generate_many(prompt, input_urls=[ref_url], aspect_ratio=aspect_ratio,
                                n=n, output_file="car.png", backend="mcode")


def _ai_car_pass(subject: str, ref: Path, attrs: dict[str, str], prompt: str,
                  mode: str) -> tuple[Any, Path, dict[str, Any]] | None:
    """按一种生图模式出候选、过三道质检门。返回 (Cutout, 图路径, 溯源) 或 None。

    每种模式各有一份缓存:图生图被判否不该连累文字生图,反过来也一样。"""
    from ..providers import images
    from . import library, motion_assets

    n_cand = config.COVER_AI_CAR_CANDIDATES
    seed = int(_hash(f"{subject}|{prompt}")[:8], 16) % (2 ** 31 - 1)
    h = _hash(prompt)[:12]
    # 文字生图沿用旧的缓存目录名(老缓存继续有效),图生图单独一个后缀
    dirname = f"{h}_{n_cand}" if mode == "t2i" else f"{h}_ref_{n_cand}"
    d = _cars_root() / library.slug(subject) / dirname
    meta_path = d / "meta.json"
    meta = _read_json(meta_path)
    # 老缓存没有 mode 字段(那时只有文字生图一条路),当作 t2i
    if meta.get("prompt") != prompt or (meta.get("mode") or "t2i") != mode:
        meta = {}
    # 质检门改了判据,旧结论作废 —— 否则"上次全否"的缓存会把新判据永远挡住。
    if meta and meta.get("gates_version") != GATES_VERSION:
        meta = {}
    chosen = str(meta.get("chosen") or "")
    if chosen:
        cand = d / chosen
        cut = motion_assets.cutout(cand, item_id="ai")
        if cut is not None:
            log.info("封面:%s 用缓存的生图车(%s %s)", subject, mode, cand.name)
            return cut, cand, _car_trace(meta)
        meta = {}
    if meta.get("status") == "rejected":
        log.info("封面:%s %s 生图的车没通过质检", subject, mode)
        return None

    d.mkdir(parents=True, exist_ok=True)
    if mode == "ref":
        refs = _generate_with_reference(prompt, ref, n=n_cand)
    else:
        # ⚠️ **多候选时不要传 seed**:接口文档写明"传 seed 则 n 张内容相近, 不传则逐张随机"。
        # 传了 seed,n=4 会生成四张几乎一样的车,多候选就白费了 —— 实测踩过这个坑。
        # 幂等不靠 seed,靠上面按提示词哈希建的缓存目录。
        refs = images.generate_many(prompt, aspect_ratio="16:9", n=n_cand,
                                    seed=seed if n_cand <= 1 else None, output_file="car.png")
    if not refs:
        log.warning("封面:%s %s 生图失败", subject, mode)
        _write_json(meta_path, {"status": "error", "prompt": prompt, "mode": mode,
                                "gates_version": GATES_VERSION,
                                "seed": 0, "candidates_requested": n_cand})
        return None

    records: list[dict[str, Any]] = []
    picked_meta: dict[str, Any] = {}
    for i, ref_img in enumerate(refs):
        raw = d / f"cand{i}.jpg"
        try:
            images.download(ref_img.url, raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("封面:%s 生图候选下载失败:%s", subject, exc)
            continue
        cut = motion_assets.cutout(raw, item_id="ai")
        used = raw
        if cut is None:
            # 底色差几档就刷成纯白再抠一次
            norm = _white_normalized(raw, d / f"cand{i}_norm.jpg")
            if norm is not None:
                cut = motion_assets.cutout(norm, item_id="ai")
                used = norm
        rec: dict[str, Any] = {"file": used.name, "gates": {}}
        if cut is None:
            rec["gates"]["G1_cutout"] = "fail"
            records.append(rec)
            continue
        rec["gates"]["G1_cutout"] = "pass"
        # G4 朝向:封面版式固定"车头朝右"(第一支的官方官图就是朝右的)。
        # 生图模型不一定听"nose pointing to the right"——实测同一提示词两张里就有一张朝左。
        # 朝左的车放上封面,和第一支并排一眼就看出不是一条产线出来的,直接淘汰。
        facing = cut.facing
        rec["gates"]["G4_facing"] = facing
        if facing != COVER_CAR_FACING:
            records.append(rec)
            continue
        flat = _flat_on_white(cut, d / f"cand{i}_flat.jpg")
        clean = _vlm_answer(flat, CAR_CLEAN_PROMPT)
        rec["gates"]["G2_clean"] = clean or "unknown"
        pair = _compare_pair(flat, ref, d / f"cand{i}_cmp.jpg",
                             ref_facing=str(attrs.get("facing") or "right")) if flat else None
        same = _vlm_answer(pair, CAR_SAME_PROMPT)
        rec["gates"]["G3_same_model"] = same or "unknown"
        records.append(rec)
        if clean == "NO" and same == "YES":
            picked_meta = {"status": "ok", "prompt": prompt, "mode": mode,
                           "gates_version": GATES_VERSION,
                           "seed": seed if n_cand <= 1 else 0,
                           "chosen": used.name, "reference": str(ref), "attrs": attrs,
                           "candidates_requested": n_cand, "candidates": records}
            break
    if not picked_meta:
        picked_meta = {"status": "rejected", "prompt": prompt, "mode": mode,
                       "gates_version": GATES_VERSION,
                       "seed": seed if n_cand <= 1 else 0,
                       "reference": str(ref), "attrs": attrs, "candidates_requested": n_cand,
                       "candidates": records}
        log.info("封面:%s %s 生图的 %d 个候选都没通过质检", subject, mode, len(records))
    _write_json(meta_path, picked_meta)
    if picked_meta.get("status") != "ok":
        return None
    cand = d / str(picked_meta["chosen"])
    cut = motion_assets.cutout(cand, item_id="ai")
    if cut is None:
        return None
    return cut, cand, _car_trace(picked_meta)


def ai_car(subject: str, exclude: Sequence[str | Path] = ()) -> tuple[Any, Path, dict[str, Any]] | None:
    """素材池里没有白底官图时:生成一台车,过三道质检门后返回 (Cutout, 原图, 溯源)。

    两种生图路依次试:**图生图**(拿官方参照图去生,车型最可能对)→ **文字生图**。
    都拿不到能过门的车 → 返回 None,调用方退回老封面。封面不拦住成片。"""
    if not config.COVER_ART or not config.COVER_AI_CAR or not subject:
        return None
    from . import library

    picked = pick_reference(subject, exclude)
    if picked is None:
        log.info("封面:%s 没有认得出是这台车的参照官图,不用生图兜底", subject)
        return None
    ref, desc = picked
    attrs = ref_attributes(ref, desc)
    for mode in _car_modes():
        prompt = CAR_REF_PROMPT if mode == "ref" else car_prompt(subject, attrs)
        got = _ai_car_pass(subject, ref, attrs, prompt, mode)
        if got is not None:
            return got
    return None


@dataclass(frozen=True)
class CarVisual:
    """封面里那台车从哪来。"""
    cut: Any                  # motion_assets.Cutout
    still: Path               # 原图(官方白底官图 / 生图候选)
    source: str               # "official_cutout" | "ai_generated"
    trace: dict[str, Any] = field(default_factory=dict)


def pick_visual(subject: str, exclude: Sequence[str | Path] = ()) -> CarVisual | None:
    """封面里那台车:先要素材池的白底官图(和第一支完全一致),抠不出来就生图生成一台。"""
    car = (pick_car(subject, exclude) if exclude else None) or pick_car(subject)
    if car is not None:
        cut, still = car
        if cut is not None:
            return CarVisual(cut=cut, still=Path(still), source="official_cutout",
                             trace={"car_source": "official_cutout"})
    ai = ai_car(subject, exclude)
    if ai is None:
        return None
    cut, still, trace = ai
    return CarVisual(cut=cut, still=still, source="ai_generated", trace=trace)


def find_plates(im: Any) -> list[tuple[int, int, int, int]]:
    """找车身上印着别家标识的白色车牌(官图库常在车牌上打「汽车之家 AUTOHOME」)。

    车牌 = 车身下半部一块又白又匀的横长方块(宽占车宽 5%–25%,宽高比 1.8–5.5,近乎填满外框),
    中间有一些不白的字。空白车牌(没有字)不算。返回 [(x0, y0, x1, y1)]。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    a = np.asarray(im.convert("RGBA")).astype(np.int32)
    r, g, b, alpha = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    luma = (r * 299 + g * 587 + b * 114) // 1000
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    white = (luma >= 200) & (chroma <= 40) & (alpha >= 250)
    # 闭运算把字的笔画补上,整块车牌连成一片
    m = Image.fromarray(np.where(white, 255, 0).astype(np.uint8), "L")
    m = m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5)).copy()
    H, W = white.shape
    found = []
    ys, xs = np.nonzero(np.asarray(m)[::3, ::3] == 255)
    for y, x in zip(ys * 3, xs * 3):
        if m.getpixel((int(x), int(y))) != 255:
            continue
        ImageDraw.floodfill(m, (int(x), int(y)), 128, thresh=0)
        comp = np.asarray(m) == 128
        m.paste(0, mask=Image.fromarray((comp * 255).astype(np.uint8)))
        yy, xx = np.nonzero(comp)
        x0, x1, y0, y1 = int(xx.min()), int(xx.max()) + 1, int(yy.min()), int(yy.max()) + 1
        w, h = x1 - x0, y1 - y0
        if not (W * 0.05 <= w <= W * 0.25 and 1.8 <= w / max(1, h) <= 5.5):
            continue
        fill = comp[y0:y1, x0:x1].mean()
        ink = 1 - white[y0:y1, x0:x1].mean()
        if fill >= 0.8 and 0.03 <= ink <= 0.5 and (y0 + y1) / 2 >= H * 0.4:
            # 字贴着牌边时闭运算会把那一边啃掉几像素:往外扩到亮色(牌底 + 字)结束的地方
            while y0 > 0 and luma[y0 - 1, x0:x1].mean() > 150:
                y0 -= 1
            while y1 < H and luma[y1, x0:x1].mean() > 150:
                y1 += 1
            while x0 > 0 and luma[y0:y1, x0 - 1].mean() > 150:
                x0 -= 1
            while x1 < W and luma[y0:y1, x1].mean() > 150:
                x1 += 1
            found.append((x0, y0, x1, y1))
    return found


def scrub_plates(im: Any) -> Any:
    """把车牌上的字抹成空白车牌:留一圈细牌框,里面填车牌底色(按纵向拟合的渐变,和原来的打光一致)。
    返回新图。"""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    im = im.convert("RGBA")
    boxes = find_plates(im)
    if not boxes:
        return im
    im = im.copy()
    a = np.asarray(im)
    k = np.array([0.299, 0.587, 0.114])
    for x0, y0, x1, y1 in boxes:
        p = max(2, int((y1 - y0) * 0.07))
        ix0, iy0, ix1, iy1 = x0 + p, y0 + p, x1 - p, y1 - p
        rgb = a[iy0:iy1, ix0:ix1, :3].astype(np.float64)
        luma = rgb @ k
        # 只拿车牌底(最亮那批、去掉字)的像素,按行号拟合一条直线,得到上下的明暗渐变
        yy, _ = np.nonzero(luma >= np.percentile(luma, 75) - 10)
        cols = rgb[luma >= np.percentile(luma, 75) - 10]
        ramp = np.arange(iy1 - iy0)
        grad = np.stack([np.polyval(np.polyfit(yy, cols[:, c], 1), ramp) for c in range(3)], axis=1)
        patch = np.repeat(np.clip(grad, 0, 255)[:, None, :], ix1 - ix0, axis=1).astype(np.uint8)
        mask = Image.new("L", (ix1 - ix0, iy1 - iy0), 0)
        ImageDraw.Draw(mask).rectangle((1, 1, ix1 - ix0 - 2, iy1 - iy0 - 2), fill=255)
        im.paste(Image.fromarray(patch, "RGB"), (ix0, iy0), mask.filter(ImageFilter.GaussianBlur(1)))
    log.info("封面车图抹掉 %d 块带标识的车牌", len(boxes))
    return im


def _car_layer(cut: Any) -> Any:
    """抠图 PNG 裁到车身,抹掉车牌上的别家标识,边缘收一圈去掉白边。"""
    from PIL import Image, ImageFilter

    with Image.open(cut.png) as im:
        im = scrub_plates(im)
    a = im.getchannel("A").filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
    im.putalpha(a)
    box = a.point(lambda v: 255 if v > 24 else 0).getbbox()
    return im.crop(box) if box else im


@dataclass(frozen=True)
class Frame:
    """版式网格。竖屏的内容全放在**中间 3:4 安全区**里:抖音主页宫格把 9:16 封面裁成中间的 3:4,
    出了安全区的字和车在主页上直接看不到。"""
    w: int
    h: int
    landscape: bool
    top: int      # 安全区上沿
    height: int   # 安全区高度
    margin: int

    @classmethod
    def of(cls, size: tuple[int, int]) -> "Frame":
        w, h = size
        if w >= h:
            return cls(w, h, True, 0, h, max(48, int(w * 0.05)))
        sh = min(h, w * 4 // 3)
        return cls(w, h, False, (h - sh) // 2, sh, max(40, int(w * 0.07)))

    def y(self, k: float) -> int:
        """安全区内的纵向位置(0 = 安全区上沿,1 = 下沿)。"""
        return int(self.top + self.height * k)


def _car_fit(car: Any, f: Frame) -> tuple[Any, int, int]:
    """车的尺寸和位置:竖屏占满宽度落在安全区下部,横屏放右半边。返回 (缩放后的车, x, 地面 y)。"""
    from PIL import Image

    if f.landscape:
        max_w, max_h, floor = f.w * 0.60, f.h * 0.60, f.y(0.90)
    else:
        max_w, max_h, floor = f.w * 1.0, f.height * 0.42, f.y(0.90)
    k = min(max_w / car.width, max_h / car.height)
    car = car.resize((max(1, int(car.width * k)), max(1, int(car.height * k))), Image.LANCZOS)
    x = int(f.w - f.margin * 0.5 - car.width) if f.landscape else (f.w - car.width) // 2
    return car, x, int(floor)


def _ghost(canvas: Any, f: Frame, tone: Tone, word: str, car_box: tuple[int, int, int, int]) -> None:
    """车身背后一行超大的型号字(YU7、i9),只比底色深一点点:杂志封面的层次感,也让型号一眼认得出。"""
    from PIL import Image, ImageDraw

    if not word:
        return
    x0, y0, x1, y1 = car_box
    target = (x1 - x0) * (0.98 if f.landscape else 0.92)
    font = _font(HEAVY, 100)
    font = _font(HEAVY, max(40, int(100 * target / max(1.0, font.getlength(word)))))
    left, top, right, bottom = font.getbbox(word)
    if bottom - top > (y1 - y0) * 0.9:  # 字太高会顶到标题区
        font = _font(HEAVY, int(font.size * (y1 - y0) * 0.9 / (bottom - top)))
        left, top, right, bottom = font.getbbox(word)
    # 字脚压到车身一半:上半截从车顶露出来,下半截藏在车后
    # 横屏右对齐车尾,别和左边的标题叠在一起
    gx = int(x1 - right) if f.landscape else int((x0 + x1) / 2 - (right + left) / 2)
    gy = int(y0 + (y1 - y0) * 0.45 - bottom)
    layer = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(layer).text((gx, gy), word, font=font, fill=44 if tone.light_text else 38)
    ink = (255, 255, 255) if tone.light_text else tone.text
    canvas.paste(Image.new("RGB", canvas.size, ink), (0, 0), layer)


def place_car(canvas: Any, cut: Any, f: Frame, *, tone: Tone | None = None, ghost: str = "") -> None:
    """车落在地面线上:背后型号大字 → 淡倒影 → 接触阴影 → 车。直接画在 canvas 上。"""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    car, x, floor = _car_fit(_car_layer(cut), f)
    y = floor - car.height
    if tone is not None:
        _ghost(canvas, f, tone, ghost, (x, y, x + car.width, floor))

    # 倒影:翻转后从 22% 渐隐到 0,只露车高的 30%
    ref_h = int(car.height * 0.30)
    ref = car.transpose(Image.FLIP_TOP_BOTTOM).crop((0, 0, car.width, ref_h))
    fade = Image.new("L", ref.size)
    fd = ImageDraw.Draw(fade)
    for yy in range(ref_h):
        fd.line([(0, yy), (ref.width, yy)], fill=int(56 * (1 - yy / max(1, ref_h)) ** 1.6))
    ref.putalpha(ImageChops.multiply(ref.getchannel("A"), fade))
    canvas.paste(ref, (x, floor - 2), ref)

    # 接触阴影:车底一条压扁的模糊椭圆
    cx = x + car.width / 2
    sh = Image.new("L", canvas.size, 0)
    sw, shh = car.width * 0.90, max(8, car.height * 0.10)
    ImageDraw.Draw(sh).ellipse((cx - sw / 2, floor - shh * 0.55, cx + sw / 2, floor + shh * 0.45),
                               fill=130)
    sh = sh.filter(ImageFilter.GaussianBlur(max(6, int(shh * 0.6))))
    canvas.paste(Image.new("RGB", canvas.size, (20, 20, 22)), (0, 0), sh)
    canvas.paste(car, (x, y), car)


# --------------------------------------------------------------------------
# 文字
# --------------------------------------------------------------------------
def _text(canvas: Any, xy: tuple[int, int], text: str, font: Any, fill: tuple[int, int, int],
          shadow: int = 0) -> None:
    """字;shadow > 0 时垫一层很淡的柔和投影(只有浅色字用)。"""
    from PIL import Image, ImageDraw, ImageFilter

    if not text:
        return
    if shadow:
        size = getattr(font, "size", 40)
        layer = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(layer).text((xy[0], xy[1] + max(2, size // 24)), text, font=font, fill=shadow)
        layer = layer.filter(ImageFilter.GaussianBlur(max(3, size // 14)))
        canvas.paste(Image.new("RGB", canvas.size, (0, 0, 0)), (0, 0), layer)
    ImageDraw.Draw(canvas).text(xy, text, font=font, fill=fill)


def _face(weight: int) -> tuple[str | None, int]:
    """封面字体:思源黑体可变字重(Windows 自带 NotoSansSC-VF)能调到 Black,比雅黑粗体更有冲击力;
    没有就用雅黑粗体。返回 (路径, 字重)。"""
    from .narration import _font_file

    vf = Path(config.FONTS_DIR) / "NotoSansSC-VF.ttf"
    return (str(vf), weight) if vf.is_file() else (_font_file(bold=True), 0)


HEAVY: Any = None  # 主标题 / 型号大字,首次用到时定下来
BOLD: Any = None   # 色带副标 / 角标


def _faces() -> None:
    global HEAVY, BOLD
    if HEAVY is None:
        HEAVY, BOLD = _face(900), _face(760)


def _font(spec: Any, size: int) -> Any:
    """spec:字体路径,或 (路径, 可变字重)。"""
    from PIL import ImageFont

    path, weight = spec if isinstance(spec, tuple) else (spec, 0)
    try:
        font = ImageFont.truetype(path, size) if path else ImageFont.load_default()
        if weight:
            font.set_variation_by_axes([weight])
        return font
    except (OSError, AttributeError):
        return ImageFont.load_default()


def _line_h(font: Any, k: float = 1.2) -> int:
    return int(getattr(font, "size", 40) * k)


def _ink(rgb: tuple[int, int, int]) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


#: 没有标点时的断行禁忌:量词 / 助词不能起行,指示词不能收行(「这几 / 个数字」)
_NO_START = set("个的了吗呢吧啊们着过得地么")
_NO_END = set("这那几每第")


def _soft_cuts(title: str) -> list[int]:
    """没有标点的标题能断的位置:不拆英文数字串(YU7、23.35),不违反上面的禁忌。
    只取中间一段,免得断成一长一短。"""
    n = len(title)
    out = []
    for k in range(max(2, n // 3), min(n - 1, n - n // 3) + 1):
        a, b = title[k - 1], title[k]
        if (a.isascii() and a.isalnum() or a == ".") and (b.isascii() and b.isalnum() or b == "."):
            continue
        if b in _NO_START or a in _NO_END:
            continue
        out.append(k)
    return out


def _title_lines(title: str, bold: str | None, max_width: int, biggest: int,
                 smallest: int = 56) -> tuple[list[str], Any]:
    """一段字最多两行:放得下就一行;否则断在标点 / 安全位置上,在 biggest..smallest 里取最大字号。"""
    from .narration import _COVER_BREAK, _cover_wrap

    font = _font(bold, biggest)
    if font.getlength(title) <= max_width:
        return [title], font
    cuts = [k + 1 for k, ch in enumerate(title[:-1]) if ch in _COVER_BREAK] or _soft_cuts(title)
    if cuts:
        k = min(cuts, key=lambda c: (abs(c - len(title) / 2), -c))  # 一样近取靠后的,上行长一点
        halves = [title[:k].strip(), title[k:].strip()]
        for size in range(biggest, smallest - 1, -6):
            font = _font(bold, size)
            if font.getlength(title) <= max_width:
                return [title], font
            if all(font.getlength(x) <= max_width for x in halves):
                return halves, font
    size = biggest
    while True:
        font = _font(bold, size)
        if font.getlength(title) <= max_width:
            return [title], font
        lines = _cover_wrap(title, font, max_width, 2)
        fits = all(font.getlength(x) <= max_width for x in lines) and not lines[-1].endswith("…")
        if fits or size <= smallest:
            return lines, font
        size -= 6


def _fit(text: str, bold: str | None, max_width: int, biggest: int, smallest: int,
         one_line_min: int) -> tuple[list[str], Any]:
    """先缩字号保一行(不小于 one_line_min),放不下一行再按 `_title_lines` 断两行。
    「23.35万起」「小米YU7」拆成两行比字小一号难看得多。"""
    for size in range(biggest, one_line_min - 1, -4):
        font = _font(bold, size)
        if font.getlength(text) <= max_width:
            return [text], font
    return _title_lines(text, bold, max_width, one_line_min, smallest)


def short_name(kicker: str) -> str:
    """kicker「小米YU7 · 25.35万元起」→「小米YU7」"""
    return (kicker or "").split("·")[0].strip()


def split_title(title: str, name: str = "") -> tuple[str, str]:
    """标题拆成(主行大字, 强调色副标):断在第一处标点;没有标点就车名当主行、其余当副标。"""
    from .narration import _COVER_BREAK

    title = title.strip()
    for k, ch in enumerate(title[:-1]):
        if ch in _COVER_BREAK and k >= 2:
            main, sub = title[:k].strip(), title[k + 1:].strip()
            if ch in "?？!！":
                main += ch
            return main, sub
    if name and name != title:
        rest = title[len(name):].strip(" :：") if title.startswith(name) else title
        return name, rest or title
    return title, ""


def usable_hook(hook: str) -> str:
    """B 版大字只用带数字的钩子(价格、时长、续航)。主论点硬截出来的半句(「小米YU7版本跨」)
    放大成主视觉只会更难看,这种情况 B 版改排标题。"""
    from .packaging import figures_in

    # figures_in 会剥掉型号代号(YU7 的 7 不算数字)
    return hook if hook and len(hook) <= 8 and figures_in(hook) else ""


def model_word(name: str) -> str:
    """车名里的型号(「小米YU7」→「YU7」「理想i9」→「i9」),当车身背后的大字;没有就不画。"""
    import re

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", name or "")
    word = max(words, key=len) if words else ""
    return word if 2 <= len(word) <= 6 else ""


def _pill(canvas: Any, xy: tuple[int, int], text: str, font: Any, fill: tuple[int, int, int],
          color: tuple[int, int, int], pad: tuple[float, float] = (0.5, 0.22),
          radius: float = 0.28) -> tuple[int, int]:
    """字压在圆角色块上(副标色带、车名角标)。xy 是色块左上角,返回色块 (宽, 高)。"""
    from PIL import ImageDraw

    size = getattr(font, "size", 40)
    left, top, right, bottom = font.getbbox(text)
    px, py = int(size * pad[0]), int(size * pad[1])
    bw, bh = int(right - left + px * 2), int(bottom - top + py * 2)
    d = ImageDraw.Draw(canvas)
    d.rounded_rectangle((xy[0], xy[1], xy[0] + bw, xy[1] + bh), radius=int(size * radius), fill=fill)
    d.text((xy[0] + px - left, xy[1] + py - top), text, font=font, fill=color)
    return bw, bh


def render(canvas: Any, tone: Tone, *, main: str, sub: str = "", chip: str = "",
           brand: str = "") -> Any:
    """排字(原地画,返回 canvas)。纯本地,单测直接调。

    信息层级(从上到下,一屏只讲一件事):
    1. 角标 —— 车名小胶囊,告诉划过的人「这条讲哪台车」(主标题已经是车名就不重复);
    2. 主标题 —— 最粗最大的一行:价格 / 车名 / 结论,缩字号也不拆行;
    3. 色带副标 —— 强调色实底 + 反白字:问题 / 冲突,缩略图里也最先被看到;
    4. 署名 —— 安全区右下角的账号名,主页一排封面靠它和固定版式认出同一个号。"""
    from PIL import Image, ImageDraw

    _faces()
    f = Frame.of(canvas.size)
    shadow = 90 if tone.light_text else 0
    band_text = tone.text if _ink(tone.accent) > 150 else (255, 255, 255)
    chip_fill, chip_text = tone.text, tone.top
    if f.landscape:
        col_w = int(f.w * 0.48)
        x, y = f.margin, f.y(0.12)
        mains, mf = _fit(main, HEAVY, col_w, int(f.h * 0.16), int(f.h * 0.07), int(f.h * 0.10))
        if chip:
            cf = _font(BOLD, int(f.h * 0.05))
            y += _pill(canvas, (x, y), chip, cf, chip_fill, chip_text)[1] + int(cf.size * 0.55)
        for ln in mains:
            top = mf.getbbox(ln)[1]
            _text(canvas, (x, y - top), ln, mf, tone.text, shadow)
            y += _line_h(mf, 1.08)
        y += int(mf.size * 0.10)
        if sub:
            subs, sf = _fit(sub, BOLD, col_w - int(f.h * 0.05), max(40, int(mf.size * 0.42)),
                            int(f.h * 0.04), int(f.h * 0.05))
            for ln in subs:
                y += _pill(canvas, (x, y), ln, sf, tone.accent, band_text)[1] + int(sf.size * 0.18)
    else:
        max_w = f.w - f.margin * 2
        y = f.y(0.07)
        mains, mf = _fit(main, HEAVY, max_w, int(f.w * 0.21), int(f.w * 0.09), int(f.w * 0.12))
        if chip:
            cf = _font(BOLD, int(f.w * 0.042))
            bw = cf.getbbox(chip)[2] - cf.getbbox(chip)[0] + int(cf.size * 1.0)
            y += _pill(canvas, ((f.w - bw) // 2, y), chip, cf, chip_fill, chip_text)[1] \
                + int(cf.size * 0.5)
        for ln in mains:
            left, top, right, _ = mf.getbbox(ln)
            _text(canvas, (int((f.w - (right - left)) / 2 - left), y - top), ln, mf, tone.text, shadow)
            y += _line_h(mf, 1.06)
        y += int(mf.size * 0.12)
        if sub:
            subs, sf = _fit(sub, BOLD, max_w - int(f.w * 0.08), max(56, int(mf.size * 0.46)),
                            int(f.w * 0.05), int(f.w * 0.065))
            for ln in subs:
                l, _, r, _ = sf.getbbox(ln)
                bw = r - l + int(sf.size * 1.0)
                y += _pill(canvas, ((f.w - bw) // 2, y), ln, sf, tone.accent, band_text)[1] \
                    + int(sf.size * 0.18)
    if brand:
        bf = _font(BOLD, int(min(f.w, f.h) * 0.032))
        l, t, r, b = bf.getbbox(brand)
        bx, by = f.w - f.margin - (r - l), f.y(0.965) - (b - t)
        layer = Image.new("L", canvas.size, 0)
        ImageDraw.Draw(layer).text((bx - l, by - t), brand, font=bf, fill=150)
        canvas.paste(Image.new("RGB", canvas.size, tone.text), (0, 0), layer)
    return canvas


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def _save(image: Any, dest: Path) -> None:
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    try:
        image.save(tmp, quality=93)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def compose(cut: Any, tone: Tone, size: tuple[int, int], *, main: str, sub: str = "",
            name: str = "", brand: str = "") -> tuple[Any, str]:
    """底图 → 型号大字 + 车 → 字。返回 (图, 底图来源)。"""
    canvas, source = background(tone, size)
    place_car(canvas, cut, Frame.of(size), tone=tone, ghost=model_word(name))
    chip = name if name and name not in main and name not in sub else ""
    return render(canvas, tone, main=main, sub=sub, chip=chip, brand=brand), source


def build(project_id: str, dest: str | Path, *, size: tuple[int, int], title: str, subject: str,
          kicker: str = "", variant: str = "a", hook: str = "",
          exclude: Sequence[str | Path] = ()) -> dict[str, Any] | None:
    """出一版封面,同时按 3:4 和 4:3 各重排一张(小红书 3:4;抖音网页版要横 4:3 + 竖 3:4)。
    成功返回 {"path", "tone", "source", "still"},否则 None。

    车从哪来:素材池的白底官图(抠图)→ 抠不出来就生图生成一台(`ai_car`,过三道质检门)
    → 都没有才退回老封面。两种来源走的是同一套版式,主页一排看过去是同一种封面。

    B 版:有带数字的钩子就钩子当大字、标题当副标;`exclude` 传 A 版用过的官图,尽量换一张车图。"""
    from .narration import cover_3x4_path, cover_4x3_path

    if not config.COVER_ART or not title:
        return None
    try:
        visual = pick_visual(subject, exclude)
        if visual is None:
            log.info("封面:%s 没有能抠的白底官图,退回老封面", subject)
            return None
        cut, still = visual.cut, visual.still
        hook = usable_hook(hook) if variant == "b" else ""
        main, sub = (hook, title) if hook else split_title(title, short_name(kicker))
        tone = tone_of(project_id, variant)
        short = min(size)
        jobs = [(Path(dest), size),
                (cover_3x4_path(dest), (short, short * 4 // 3)),
                (cover_4x3_path(dest), (short * 4 // 3, short))]
        name = short_name(kicker)
        rendered = [(path, compose(cut, tone, sz, main=main, sub=sub, name=name, brand=config.COVER_BRAND))
                    for path, sz in jobs]
        for path, (im, _) in rendered:
            _save(im, path)
        return {"path": str(dest), "tone": tone.name, "source": rendered[0][1][1],
                "still": str(still), "main": main, "sub": sub, **visual.trace}
    except Exception as exc:  # noqa: BLE001
        log.warning("统一风格封面失败,退回老封面:%s", exc)
        return None


def pick_title(pkg: Any, fallback: str) -> str:
    """封面大标题:包装里选中的那条(≤18 字、数字已核对);没有包装用片名。"""
    if pkg is not None and getattr(pkg, "titles", None):
        idx = pkg.picked.get("title", 0)
        idx = idx if isinstance(idx, int) and 0 <= idx < len(pkg.titles) else 0
        return pkg.titles[idx].text
    return fallback


def b_title(pkg: Any, title_a: str, hook: str) -> str:
    """B 版的标题:大字出自哪条就用哪条;没有能用的大字就换一条**不同写法**的标题,
    让 A/B 的差别落在文案上,而不是同一句话换个底色。"""
    from . import packaging

    if pkg is None or not getattr(pkg, "titles", None):
        return title_a
    if usable_hook(hook):
        return packaging.hook_title(pkg.titles, hook) or title_a
    return next((t.text for t in pkg.titles if t.text != title_a), title_a)


# --------------------------------------------------------------------------
# 命令行:给已完成的项目重出封面
# --------------------------------------------------------------------------
def rebuild(project_id: str) -> dict[str, Any]:
    """按现有 render.json / packaging.json 重画 A、B 封面,不重新合成视频。"""
    from .. import db
    from . import aspect, explainer, packaging

    project = db.get_project(project_id) or {}
    rendered = db.read_json(project_id, "render.json", {}) or {}
    if not project or not rendered:
        return {"ok": False, "reason": "项目不存在或还没成片"}
    meta = rendered.get("cover_meta") or {}
    size = aspect.frame_size(project)
    subject = str(project.get("subject") or "")
    pkg = packaging.load(project_id)
    title_a = pick_title(pkg, str(project.get("title") or ""))
    out: dict[str, Any] = {"ok": True}
    a = build(project_id, config.project_dir(project_id) / aspect.cover_name(project),
              size=size, title=title_a, subject=subject, kicker=meta.get("kicker", ""))
    out["a"] = a
    price, _ = explainer._price_of(db.read_json(project_id, "facts.json", []) or [])
    hook = packaging.cover_hook(pkg.titles if pkg else [], price=price,
                                thesis=str(project.get("thesis") or ""))
    if hook and a:
        # B 版换一张参照图(AI 车用的是官方参照图,真车用的是官图本身)
        out["b"] = build(project_id, config.project_dir(project_id) / aspect.cover_name(project, "b"),
                         size=size, title=b_title(pkg, title_a, hook), subject=subject,
                         kicker=meta.get("kicker", ""), variant="b", hook=hook,
                         exclude=[a.get("car_reference") or a["still"]])
    if a:
        rendered["cover_style"] = "art"
        rendered["cover_art"] = {k: v for k, v in (("a", a), ("b", out.get("b"))) if v}
        rendered["cover_car"] = a.get("car_source", "official_cutout")
        note = str(rendered.get("source_note") or "")
        if a.get("car_source") == "ai_generated" and "AI 生成" not in note:
            # 封面那台车是生图出来的:说清楚,别让人以为是官方实拍
            rendered["source_note"] = (note + ";封面车型图为 AI 生成示意,以官方图为准").strip(";")
        db.write_json(project_id, "render.json", rendered)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json

    from .. import db

    ap = argparse.ArgumentParser(description="统一风格封面:给已成片的项目重出封面")
    ap.add_argument("--project", action="append", default=[], help="重出这个项目的封面(可多次)")
    ap.add_argument("--all", action="store_true", help="重出所有已成片项目的封面")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ids = list(args.project)
    if args.all:
        ids += [p["id"] for p in db.list_projects(limit=1000)
                if db.read_json(p["id"], "render.json", None)]
    for pid in dict.fromkeys(ids):
        print(pid, json.dumps(rebuild(pid), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
