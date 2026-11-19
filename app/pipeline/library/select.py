"""取材:给一拍挑干净片段连拼成背景(fill / long_take)。"""
from __future__ import annotations

import contextvars
import datetime as dt
import functools
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ... import config
from ...categories import Vocab
from .. import knobs, looks, media_check
from .licensing import BACKGROUND, LicenseBook
from .model import (
    FILL_MIN_PIECE,
    STILL_SECONDS,
    Item,
    Library,
    _confirmed,
    _hard_bad,
    _has_host,
    _host_sources,
    _third_party,
    _third_party_ok,
    on_target,
    titled_sources,
    vocab_of,
)
from .tagging import _dhash

log = logging.getLogger(__name__)

#: 当前这次渲染不许用的素材(源地址 / 素材 id)。打回重做的项目由 `banned_materials` 设置
_BANNED: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "banned_materials", default=frozenset())
_YEAR_RE = re.compile(r"(?<!\d)(20[0-3]\d)(?!\d)")


@contextmanager
def banned_materials(keys):
    """这段代码里的取材(fill / long_take)一律跳过这些素材:源地址或素材 id 命中就不取。"""
    token = _BANNED.set(frozenset(str(k) for k in keys or () if k))
    try:
        yield
    finally:
        _BANNED.reset(token)


def base_item_id(item_id: str) -> str:
    """连拼切出来的子段 id(`xxx~2`)还原成池子里的素材 id。"""
    return item_id.split("~", 1)[0]


def stale_title(title: str, *, now_year: int | None = None,
                max_age: int | None = None) -> bool:
    """源标题里写着年份、且最新的那个年份比今年早超过 `MATERIAL_MAX_AGE_YEARS` 年 → 老款。
    标题没年份的认不出来,放行(由人工打回 + 排除兜底)。"""
    max_age = config.MATERIAL_MAX_AGE_YEARS if max_age is None else max_age
    years = [int(y) for y in _YEAR_RE.findall(title or "")]
    if not max_age or not years:
        return False
    return (now_year or dt.date.today().year) - max(years) > max_age


def screen(lib: Library, pool: list[Item]) -> list[Item]:
    """取材前的相关性过滤:被打回项目点名的素材、标题年份太老的源,整源剔除。"""
    banned = _BANNED.get()
    urls = {s.id: s.url for s in lib.sources}
    stale = {s.id for s in lib.sources if stale_title(s.title)}
    out = []
    for i in pool:
        if i.source in stale:
            continue
        if banned and (i.id in banned or base_item_id(i.id) in banned or i.url in banned
                       or urls.get(i.source, "") in banned
                       # 重拉高清换下来的段:点名的是老段,新段同一个镜头,一起不要
                       or (i.replaces and (i.replaces in banned
                                           or base_item_id(i.replaces) in banned))):
            continue
        out.append(i)
    return out


# --------------------------------------------------------------------------
# 挑:素材干净程度
# --------------------------------------------------------------------------
def _dirt(item: Item) -> int:
    """画面干净程度:干净 0、角标水印 1、烧录字幕 2。字幕会和成片自己的旁白、
    字幕打架,比角落台标严重得多,所以排最后但不拦。"""
    dirt = {"": 0, "text_overlay": 1, "subtitle": 2}.get(item.problem, 1)
    # 主持人出镜同样抢戏:描述里点名有人在讲解 / 介绍的,按字幕同级处理
    if _has_host(item):
        dirt = max(dirt, 2)
    return dirt




# --------------------------------------------------------------------------
# 铺:横屏解说轨的背景画面
# --------------------------------------------------------------------------
@dataclass
class Piece:
    """背景里的一小段:视频按 [start, start+length) 截,图片做推拉。"""
    item_id: str
    kind: str
    path: str
    start: float
    length: float
    #: 截取时裁掉画面底部的比例(烧录字幕段裁掉字幕区再放大),0 = 不裁
    crop_bottom: float = 0.0
    #: 截取时四边一起往里收的比例(第三方视频的角标安全裁切),0 = 不裁
    crop_edge: float = 0.0
    #: 别家账号的视频(`_third_party`):记进每拍的 beats/NNN.json,统计兜底用了多少
    third_party: bool = False
    #: 使用记录(随 beats/NNN.json 落盘,版权投诉 / 下架时按出处反查成片):
    #: 原始来源地址、平台、取材当时的授权状态(licensing.STATUSES)
    source_url: str = ""
    platform: str = ""
    license_status: str = ""


def _trace(book: LicenseBook, item: Item) -> dict[str, str]:
    """Piece 的使用记录字段:这段素材的原始来源和取材时的授权状态。"""
    return {"source_url": book.origin_url(item), "platform": item.platform,
            "license_status": book.license(item).status}


#: 一段最长用几秒就换下一段 —— 信息解说的背景是 2–4 秒一刀的节奏,
#: 一个镜头拖太久反而像卡住了
FILL_MAX_PIECE = 4.5


#: 从源片第 0 秒开始的段,跳过开头这么多秒(黑场淡入)
SOURCE_FADE_IN = 0.8
#: 发布会回放每个镜头开头常是黑场 / 白场淡入,取用时跳过这么多秒
LAUNCH_FADE_IN = 0.5
#: 回放里前景有观众头 / 手机的段,取用时裁掉底部这么多(放大后头就出画了)
AUDIENCE_CROP = 0.2
#: 帧平均亮度低于它就当黑场(宣传片开头结尾的黑底片名卡,打标签时常被放过)
DARK_LUMA = 28


@functools.lru_cache(maxsize=4096)
def _luma(path: str) -> float:
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as im:
            return float(ImageStat.Stat(im.convert("L").resize((64, 36))).mean[0])
    except (OSError, ValueError):
        return 255.0  # 读不了就不拦


#: 观众头 / 手机的剪影:画面底部一条比上面暗得多(舞台灯打亮,观众背光成黑块)
AUDIENCE_STRIP = 0.2
AUDIENCE_DARK_RATIO = 0.35


@functools.lru_cache(maxsize=4096)
def _dark_strip(path: str) -> bool:
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as im:
            g = im.convert("L").resize((64, 36))
        cut = int(36 * (1 - AUDIENCE_STRIP))
        top = ImageStat.Stat(g.crop((0, 0, 64, cut))).mean[0]
        bottom = ImageStat.Stat(g.crop((0, cut, 64, 36))).mean[0]
        return top >= 90 and bottom < min(60.0, top * AUDIENCE_DARK_RATIO)
    except (OSError, ValueError):
        return False


#: 回放里整屏是大标题 / 大数字的 PPT 页:它自己带着一个数,和这一拍旁白讲的数对不上
#: (旁白讲充电、屏上写 776万销量),观众会以为说的是一件事。当背景一律不要
_TEXT_SLIDE = re.compile(r"含大字|大字|销量|数据|数字|参数|榜|图表|价格|PPT")


#: 回放段里三成以上的帧是黑的(换场黑屏、只剩一个台标):整段不要
SPAN_DARK_RATIO = 0.3


@functools.lru_cache(maxsize=4096)
def _span_dark(path: str, start: float, length: float, launch: bool = True) -> bool:
    stats = media_check.frame_stats(path, start, start + length)
    if not stats:
        return False
    dark = sum(1 for luma, sharp in stats if media_check.is_dark_frame(luma, sharp, launch=launch))
    return dark / len(stats) >= SPAN_DARK_RATIO


def _blackout(item: Item) -> bool:
    return item.launch and item.kind == "video" and _span_dark(item.path, item.start, item.length)


def _text_slide(item: Item) -> bool:
    return item.launch and bool(_TEXT_SLIDE.search(item.desc or ""))


def _audience(item: Item) -> bool:
    """回放里前景挡着观众头 / 手机:打标签时写明了,或底部一条背光剪影明显比上面暗。"""
    if not item.launch:
        return False
    if "前景观众" in (item.desc or ""):
        return True
    return bool(item.frame) and Path(item.frame).is_file() and _dark_strip(item.frame)


def _head_skip(item: Item) -> float:
    """段开头要跳过的秒数:源片开头的黑场淡入;回放每个镜头的头(虚拟块只有第一块带淡入)。"""
    if item.launch:
        first = "~" not in item.id or item.id.endswith("~0")
        return LAUNCH_FADE_IN if first and item.length - LAUNCH_FADE_IN >= FILL_MIN_PIECE else 0.0
    return SOURCE_FADE_IN if item.start < 0.5 else 0.0


def is_dark(item: Item) -> bool:
    """背景用不了的黑场:大面积黑底 + 一行小字的片名卡。叠上面板后整屏发黑。"""
    probe = item.frame or (item.path if item.kind == "still" else "")
    if not (probe and Path(probe).is_file()) or _luma(probe) >= DARK_LUMA:
        return False
    if item.launch:  # 回放里深色主题的 PPT 页:亮度低但内容多,不是黑场
        stats = media_check.image_stats(probe)
        return stats is None or media_check.is_dark_frame(stats[0], stats[1], launch=True)
    return True


#: 烧录字幕段当背景时裁掉画面底部的比例。字幕几乎都在下方五分之一,
#: 成片自己的字幕和章节条也盖在这一带,裁掉不损失要看的东西
SUBTITLE_CROP = 0.22

#: 非官方视频段当背景时四边往里收的比例(各收一半)。第三方账号的台标几乎都贴在
#: 四个角上,VLM 常漏标(Soda 那支源 25 段判成干净)—— 角标检测没下结论时,一律收掉这一圈
EDGE_CROP = 0.14
#: 第三方视频检测干净时仍留的一小圈保险边(动态水印、检测漏掉的小字)
THIRD_PARTY_SAFE_CROP = 0.04


def _crop_edge(item: Item) -> float:
    """当背景时四边往里收的比例。按所在源的角标检测结果裁(B 站会把"上传者名 +
    bilibili"烧进画面右上角,官方号的片子也有);没检测过的第三方段按 EDGE_CROP。"""
    if item.kind != "video":
        return 0.0
    if item.logo_crop >= 0:
        return item.logo_crop if item.official else max(item.logo_crop, THIRD_PARTY_SAFE_CROP)
    return 0.0 if item.official else EDGE_CROP


#: 会在图上打自家标识的图库:汽车之家把「汽车之家 AUTOHOME」印在车牌上(位置跟着车走,
#: 按固定位置裁不掉;封面那边用 cover_art.scrub_plates 抹,背景图上检测太慢也不稳)
BRANDED_GALLERIES = {"autohome"}


#: 这些图库在车牌上印着自己的 logo(懂车帝的黄牌 / 汽车之家的白牌)。
#: 当背景铺满全屏时,那个黄方块明晃晃挂在车头正中,不能带上屏。
BRANDED_PLATFORMS = ("dongchedi", "autohome")

#: 抹牌算法的版本。改了检测 / 涂抹逻辑就加 1,旧缓存(`clean/` 下别的目录)自然作废。
CLEAN_VERSION = 2


def clean_still(item: Item) -> str:
    """当背景用的官图:先把车牌上别家的 logo 抹掉,返回该用的那张图的路径。

    抹不动的就还是原图 —— 宁可留个小 logo,也不要糊一片说不清是什么的色块。
    抹好的版本缓存在池子的 `clean/` 目录里,同一个池子只抹一次。
    """
    if item.kind != "still" or item.platform not in BRANDED_PLATFORMS:
        return item.path
    src = Path(item.path)
    if not src.is_file():
        return item.path
    dest = src.parent.parent / "clean" / f"v{CLEAN_VERSION}" / src.name
    if dest.is_file():
        return str(dest)
    try:
        from PIL import Image

        from .. import cover_art

        with Image.open(src) as im:
            fixed = cover_art.scrub_watermarks(im.convert("RGB"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        fixed.save(dest, quality=94)
        return str(dest)
    except Exception as exc:  # noqa: BLE001 - 抹不掉就用原图,不该拦住渲染
        log.warning("背景图抹水印失败(%s):%s", src.name, exc)
        return item.path


def _branded_still(item: Item) -> bool:
    """同档里排在别的图后面:懂车帝 / 官网的图够用就不用带别家车牌的。"""
    return item.kind == "still" and item.platform in BRANDED_GALLERIES


#: 一拍在片子里的位置:开场 / 正文 / 结论。开场和结论只用确认是这台车的一方素材,
#: 第三方视频只在正文里、一方素材铺不满时兜底
ROLES = ("hook", "body", "ending")
#: `fill(choose=...)` 时交给挑选的候选数
CHOOSE_TOP = 6


def _croppable(item: Item) -> bool:
    """只有烧录字幕、没有主持人的视频段:裁掉底部就是干净画面。"""
    return item.kind == "video" and item.problem == "subtitle" and not _has_host(item)


#: 贴旁白的素材优先,但同一簇用到这么多次就不再插队,免得一个特写反复上屏
FIT_REUSE_MAX = 2
#: 描述和旁白至少重合这么多处才算贴旁白
FIT_OVERLAP_MIN = 2
#: 车型没确认的素材整体后移的档数:要大过任何实际的使用次数罚档,重复再多也不换成别的车
UNCONFIRMED_TIER = 1000

#: 画面质量进排序时,没量过的按这个算(中性:不因为没量过而靠前或靠后)。
#: 动感取本地 1080p 可用段的中位数附近(见 motion.py 的标定)
NEUTRAL_HERO = 1
NEUTRAL_MOTION = 0.25
#: 本地体检的边缘方差低于它算"偏软",同档里往后排(可用段 p5≈500,见 media_check.FLAT_SHARPNESS)
SOFT_SHARP = 500.0


def _hero_of(item: Item) -> int:
    return item.hero if item.hero >= 0 else NEUTRAL_HERO


def _motion_of(item: Item) -> float:
    if item.kind != "video":
        return 0.0
    return item.motion if item.motion >= 0 else NEUTRAL_MOTION


def _bucket(value: float, step: float = 0.25) -> float:
    """连续分数分档:太细的话它一个键就决定了全部先后,后面的键(官方、时长)形同虚设。"""
    return round(value / step) * step


def _big(item: Item) -> float:
    """开场挑大镜头:hero × motion(没量过的按中性值),静图 0。"""
    if item.kind != "video":
        return 0.0
    return _bucket(_hero_of(item) * _motion_of(item))


def _soft(item: Item) -> bool:
    return 0 <= item.sharp < SOFT_SHARP


def _premium(item: Item, used: int, titled: set[str]) -> bool:
    """第一档素材:确认本车、1080p、动态的视频,没用过。量不到(老池子没测动感)就不算。"""
    return (used == 0 and item.kind == "video" and item.hd and item.dynamic
            and on_target(item, titled) and item.view != "people" and not _audience(item))


def _reuse_penalty(used: int) -> int:
    """用过一次以上,每多用一次降一档。池子用尽后所有候选都在"用过"档,光看档位的话
    用过的视频永远排在用过的静图前面,次数高的段会反复上屏(重拍的 +3 也压不下去)。
    视频比静图高两档:多用两次才和用过一次的静图同档,视频优先还在。"""
    return max(0, used - 1)


def _fill_tier(item: Item, used: int) -> int:
    """非官方来源、车型没确认(target 不是 yes)的素材整体排到确认过的后面,
    池子里确认过的够用时就不会选到它们(YU7 池里混的 SU7 片段多是这类)。"""
    base = _fill_tier_base(item, used) + _reuse_penalty(used)
    if not item.official and item.target != "yes":
        return base + UNCONFIRMED_TIER
    return base


def _fill_tier_base(item: Item, used: int) -> int:
    """取用的先后档:干净视频 → 带角标水印的视频(与官图同档) → 官图 → 裁掉字幕的视频 → 用过的干净视频 →
    主持人出镜的视频 → 其余。主持人裁不掉,会和成片自己的旁白打架,排最后。"""
    if item.kind == "video":
        if _dirt(item) == 0:
            return 0 if not used else 3
        if _dirt(item) == 1:  # 角落台标 / 第三方水印:干净视频不够才用
            return 1 if not used else 3
        if _croppable(item):
            return 2 if not used else 5
        return 4 if not used else 6
    return 1 if not used else 5


#: 铺背景时的"长得差不多":比打标签去重(DUP_BITS)宽 —— 同角度的几张棚拍官图、
#: 同一镜头切出来的前后段都算一簇,用掉一张就当整簇都用过
FILL_DUP_BITS = 10
#: 相邻多样性看最近几刀(跨节拍):这几刀里出现过的簇往后排
RECENT_PIECES = 6


def _probe(item: Item) -> str:
    return item.frame or (item.path if item.kind == "still" else "")


def near_dup_groups(items: Sequence[Item], bits: int = FILL_DUP_BITS) -> dict[str, str]:
    """item_id → 所在簇的代表 id。跨来源比代表帧的 dHash,贪心入簇;
    没有代表帧(读不了)的条目自成一簇。"""
    heads: list[tuple[str, int]] = []
    out: dict[str, str] = {}
    for i in items:
        probe = _probe(i)
        h = _dhash(probe) if probe and Path(probe).is_file() else None
        if h is None:
            out[i.id] = i.id
            continue
        head = next((hid for hid, hh in heads if bin(h ^ hh).count("1") <= bits), None)
        if head is None:
            heads.append((i.id, h))
            head = i.id
        out[i.id] = head
    return out


def _pool_seconds(pool: Sequence[Item]) -> float:
    """这批素材大概能铺多少秒(静图按 STILL_SECONDS 算)。"""
    total = 0.0
    for i in pool:
        if i.kind == "video":
            total += i.length if i.length >= FILL_MIN_PIECE else 0.0
        else:
            total += STILL_SECONDS
    return total


def _detail_conflict(item: Item, want_detail: set[str], vocab: Vocab) -> bool:
    """这条素材是不是"讲 A 却配了 B 的特写"。

    特写(局部近景)是很抢戏的画面:观众会以为屏幕上这个部件就是正在讲的东西。
    旁白讲碳陶刹车,背景却是一个车门把手特写 —— 观众会以为在讲门把手。
    所以:这一拍讲的是某个细节时,背景就不能是**别的**细节的特写。

    判据:
    - 素材本身得是特写视角(局部近景),整车 / 内饰全景不算冲突;
    - 素材描述里得能认出是哪个细节(描述只说"白色车身特写"认不出,就不拦);
    - 认出的细节和这一拍讲的细节不是同一个 → 冲突。
    """
    if not want_detail or not vocab.detail_types:
        return False
    if item.view not in vocab.closeup_views:
        return False
    shown = vocab.detail_types_in(item.desc or "")
    if not shown:
        return False
    return not (shown & want_detail)


_NOT_WORD = re.compile(r"[^一-鿿A-Za-z0-9]+")
_CLAUSE_SPLIT = re.compile(r"[，,。！？；;：:!?\n]+")
#: 重合度封顶:再多的字面重合也不该压过视角、水印、用过没有这些硬指标
OVERLAP_CAP = 4


def _bigrams(text: str) -> set[str]:
    """文本去掉标点后的相邻两字(中文)/ 两字符(英文数字)集合。"""
    chars = _NOT_WORD.sub("", text or "")
    return {chars[k:k + 2] for k in range(len(chars) - 1)}


def _trigrams(text: str) -> set[str]:
    """三字切词集合:命中具体部件名(如「激光雷达」「方向盘」「仪表盘」「充电口」)额外加权。"""
    chars = _NOT_WORD.sub("", text or "")
    return {chars[k:k + 3] for k in range(len(chars) - 2)}


def _desc_overlap(item: Item, text: str) -> int:
    """素材描述和这一拍旁白(或画面提示)有几处字面重合,0–OVERLAP_CAP。

    只做排序里的软偏好:旁白说"后排空间",描述写着"后排座椅"的片段就排前面。
    `_detail_fit` 只认词表里的部件,这里补上词表之外的说法,三字部件词额外给权重。"""
    if not text or not item.desc:
        return 0
    bi = len(_bigrams(item.desc) & _bigrams(text))
    tri = len(_trigrams(item.desc) & _trigrams(text))
    return min(bi + tri, OVERLAP_CAP)


def _clause_targets(detail: str, seconds: float, max_piece: float) -> list[float]:
    """按旁白的气口(逗号/句号分句)把一拍时长拆成若干刀的目标秒数。
    只有一拍够长且旁白有多个分句时才按气口切,让画面切点落在换气停顿处。"""
    if seconds < 2 * FILL_MIN_PIECE or not detail:
        return []
    clauses = [len(_NOT_WORD.sub("", c)) for c in _CLAUSE_SPLIT.split(detail) if _NOT_WORD.sub("", c)]
    if len(clauses) < 2:
        return []
    # 合并太短的碎句,保证每刀至少够 FILL_MIN_PIECE
    total_chars = sum(clauses) or 1
    raw = [seconds * c / total_chars for c in clauses]
    merged: list[float] = []
    acc = 0.0
    for d in raw:
        acc += d
        if acc >= FILL_MIN_PIECE:
            merged.append(min(acc, max_piece))
            acc = 0.0
    if acc > 0 and merged:
        merged[-1] = min(max_piece, merged[-1] + acc)
    return merged if len(merged) >= 2 else []


def _detail_fit(item: Item, want_detail: set[str], vocab: Vocab) -> int:
    """这条素材和这一拍讲的细节有多合。数字越小越合。

    0 = 正是讲的这个细节的特写(讲车门 → 门把手特写);
    1 = 特写,但看不出是哪个细节(只能算"不冲突",不算合);
    2 = 不是特写(整车 / 全景)—— 不冲突,但也没对上。

    旁白点名了某个部件时,把那个部件的特写排前面,画面和口播才是一件事。
    """
    if not want_detail or not vocab.detail_types:
        return 2
    if item.view not in vocab.closeup_views:
        return 2
    shown = vocab.detail_types_in(item.desc or "")
    if not shown:
        return 1
    return 0 if (shown & want_detail) else 1


def _chunk_videos(pool: list[Item], group: dict[str, str], members: dict[str, list[str]],
                  by_id: dict[str, Item], piece: float) -> list[Item]:
    """长视频段按一刀的长度切成虚拟块(id 加 `~k`),每块单独计"用过"。

    不切的话一段 40 秒的回放只取 4.5 秒就整段作废,视频优先的池子实际能用的秒数少一大截。
    块各自成簇:同一段的几块共用一帧,按近重复算会一块用过全算用过。"""
    out: list[Item] = []
    for i in pool:
        if i.kind != "video" or i.length < 2 * piece:
            out.append(i)
            continue
        n = max(2, int(i.length // piece))
        step = i.length / n
        for k in range(n):
            c = replace(i, id=f"{i.id}~{k}", start=round(i.start + step * k, 3),
                        end=round(i.start + step * (k + 1), 3))
            group[c.id] = c.id
            members[c.id] = [c.id]
            by_id[c.id] = c
            out.append(c)
    return out


def fill(subject: str, seconds: float, *, hint: str = "", used: dict[str, int] | None = None,
         recent: list[str] | None = None, detail: str = "", role: str = "body",
         views: Iterable[str] | None = None,
         choose: Callable[[str, list[Item]], int | None] | None = None,
         prefer_video: bool | None = None) -> list[Piece]:
    """给一个节拍铺背景:从这台车的池子里挑干净片段**连拼**到 `seconds`。

    这里不要求画面对上旁白,也不要求单段撑满时长 ——
    背景只需要"是这台车、画面干净"。快剪广告里 1.3–3 秒的短段正好拿来连拼,
    不再因为凑不满 6 秒窗口而被浪费。

    `hint`(profile 的背景偏好,汽车是 exterior / interior / driving)只参与排序。`used` 是全片共享的
    已用计数(item_id → 次数),调用方跨节拍传同一个字典,尽量不重复。
    计数按**近重复簇**算(`near_dup_groups`):同角度的几张官图用掉一张,其余也算用过。
    `recent` 是全片最近用过的 item_id(调用方跨节拍传同一个列表,这里就地追加):
    和最近几刀同簇的、和上一刀同视角的往后排 —— 只是排序,池子不够时照样铺满。
    `detail` 是这一拍在讲的细节(旁白原文),用来避开配错的特写(见 `_detail_conflict`)。
    `role`(`ROLES`)是这一拍在片子里的位置。主持人出镜、第三方台标水印的段一律不要,池子再小也不放宽。
    - hook / ending:只取一方素材(官方视频、图库静图)里官方或认出是这台车的,第三方视频一律不要
      —— 开头观众只确认"是不是我要看的那台车",结尾是片子的落点,都不能挂别人的画面;
    - body:第三方视频只在**没用过的**一方素材铺不满这一拍时才进候选,且源必须已测过台标、
      能裁掉(`_third_party_ok`);进了也排在没用过的一方素材后面、用过的前面。
    `views`(剪辑 v2):这一刀念到的半句点到的视角(`vocab.views_in`)。给了就代替 `hint` 的视角组,
    且视角命中的素材和"讲到的部件"一样算对题、排在前面。
    `choose`(剪辑 v2):第一刀从排序前 `CHOOSE_TOP` 名里再挑一次(`choose(detail, 候选)` 返回下标,
    None 或越界就用排第一的)。用来让文字模型按素材描述挑最贴这半句的画面。
    `prefer_video`:静图只在没用过的视频铺不满时兜底(贴旁白也不插到视频前面)。
    None = 跟着 look 的 `video_first` 走;False = 老排法(静图和带角标的视频同档)。

    排序的大头(`rank`):静图闸门 → 贴旁白 → 用过没有 → 第一档素材(确认本车的 1080p 动态
    视频,`_premium`)→ 档位 → …… → 大镜头分 / 动感 / 清晰度 → 其余细项。
    `role="hook"` 改成按 `hero × motion`(`_big`)挑大镜头,用过的排后。
    返回空列表表示池子里什么都没有。
    """
    if role not in ROLES:
        raise ValueError(f"未知的 role:{role}")
    seconds = float(seconds)
    used = used if used is not None else {}
    recent = recent if recent is not None else []
    lib = Library.load(subject)
    vocab = vocab_of(lib)
    book = LicenseBook(lib)
    titled = titled_sources(lib)
    if prefer_video is None:
        prefer_video = looks.current().video_first
    text_views = set(views or ())
    want = text_views or vocab.hint_views(hint)
    want_detail = vocab.detail_types_in(detail) if detail else set()
    pool = [i for i in lib.items if i.usable and Path(i.path).is_file() and (
        (i.kind == "video" and i.length >= FILL_MIN_PIECE)
        or (i.kind == "still" and i.view in vocab.angle_of_view and not i.small_still))
        and not is_dark(i) and not _text_slide(i) and not _blackout(i)]
    pool = screen(lib, book.keep(pool, BACKGROUND))
    hosts = _host_sources(lib.items)
    pool = [i for i in pool if not _hard_bad(i, hosts)]
    strict = role != "body"
    if strict:
        pool = [i for i in pool if _confirmed(i) and not _third_party(i)]
    else:
        pool = [i for i in pool if not _third_party(i) or _third_party_ok(i)]
    # 讲某个部件时,把**别的**部件的特写整类剔掉:特写太抢戏,配错了观众就
    # 以为在讲那个部件。池子剔完不够铺满这一拍时,才退回"降权排序"
    # (宁可配得不对,也不要这一拍没有背景)。
    if want_detail and vocab.detail_types:
        keep = [i for i in pool if not _detail_conflict(i, want_detail, vocab)]
        if _pool_seconds(keep) >= seconds:
            pool = keep

    group = near_dup_groups(pool)
    by_id = {i.id: i for i in lib.items}
    members: dict[str, list[str]] = {}
    for iid, g in group.items():
        members.setdefault(g, []).append(iid)

    def group_used(i: Item) -> int:
        return sum(used.get(m, 0) for m in members.get(group[i.id], [i.id]))

    # 第三方兜底:没用过的一方素材够铺这一拍,第三方就不进候选
    video_first = looks.current().video_first and not strict
    # 一刀最长几秒跟着项目的剪辑节奏旋钮走;没设 = FILL_MAX_PIECE
    max_piece = FILL_MAX_PIECE * knobs.piece_seconds() / knobs.PIECE_SECONDS["mid"]
    if video_first:
        pool = _chunk_videos(pool, group, members, by_id, max_piece)
    fallback = video_first or _pool_seconds(
        [i for i in pool if not _third_party(i) and not group_used(i)]) < seconds
    if not fallback:
        pool = [i for i in pool if not _third_party(i)]

    def stage(i: Item, n: int) -> int:
        """没用过的一方素材 → 没用过的第三方 → 用过的(再按档位)。不兜底时全是 0,只看档位。"""
        if not fallback:
            return 0
        if n:
            return 2
        if video_first:  # 视频(含可裁台标的第三方)在前,静图在后
            return 0 if i.kind == "video" else 1
        return 1 if _third_party(i) else 0

    def rank(i: Item, recent_groups: set[str], last_view: str, videos_left: bool) -> tuple:
        # 认出是这台车的排在没认出的前面:没认出的常是同品牌别的车型(YU7 池子里
        # 混着 SU7 的赛道活动),背景里出现别的车比画面不对口严重得多
        n = group_used(i)
        fit = _detail_fit(i, want_detail, vocab)
        tier = _fill_tier(i, n)
        if fit == 0 and i.kind == "still":
            # 这一拍讲的正是这个部件,那张特写就是最贴的素材 —— 提档到和干净
            # 视频同档。否则永远选不到它:静图本来比视频低一档。
            tier = _reuse_penalty(n)
        # 这一拍讲到的部件排到使用次数前面:画面和口播对得上,比少用一次更要紧
        on_topic = ((fit == 0 or _desc_overlap(i, detail) >= FIT_OVERLAP_MIN
                     or i.view in text_views)
                    and n < FIT_REUSE_MAX and _confirmed(i) and i.view != "people"
                    and not _audience(i) and group[i.id] not in recent_groups)
        # 视频优先:还有没用过的视频时,静图一律排在视频后面(贴旁白也不插队)
        gate = bool(prefer_video) and i.kind == "still" and videos_left
        # 开场:大镜头(hero × motion)排第一,用过的排后(三刀不能是同一个镜头)
        head = ((gate, stage(i, n), n > 0, -_big(i), not on_topic) if role == "hook"
                else (gate, not on_topic, stage(i, n)))
        # 码率不够的视频同档里往后排(不降档:AV1 官方片普遍码率低,降档就全让给静图了)
        return head + (
            not _premium(i, n, titled), tier, i.view == "people", _audience(i),
            not on_target(i, titled), i.low_bitrate, _branded_still(i),
            group[i.id] in recent_groups,
            bool(want) and i.view not in want, bool(last_view) and i.view == last_view, n,
            -_hero_of(i), -_bucket(_motion_of(i)), _soft(i),
            _dirt(i), fit, -_desc_overlap(i, detail), _detail_conflict(i, want_detail, vocab),
            not i.official, -i.length)

    out: list[Piece] = []
    covered = 0.0
    left = list(pool)
    clause_plan = _clause_targets(detail, seconds, max_piece)
    while left and covered < seconds - 0.05:
        window = recent[-RECENT_PIECES:]
        recent_groups = {group.get(r, r) for r in window}
        last = by_id.get(window[-1]) if window else None
        videos_left = bool(prefer_video) and any(
            x.kind == "video" and not group_used(x) for x in left)
        def key(x: Item, g: set[str] = recent_groups,
                v: str = last.view if last else "", vl: bool = videos_left) -> tuple:
            return rank(x, g, v, vl)
        if choose is not None and not out and len(left) > 1:
            top = sorted(left, key=key)[:CHOOSE_TOP]
            try:
                k = choose(detail, top)
            except Exception:  # noqa: BLE001 —— 挑不出来就按排序,不拦取材
                log.warning("取材二次挑选失败,按排序取", exc_info=True)
                k = None
            i = top[k] if isinstance(k, int) and 0 <= k < len(top) else top[0]
        else:
            i = min(left, key=key)
        left.remove(i)
        remaining = seconds - covered
        slot_cap = clause_plan[len(out)] if len(out) < len(clause_plan) else max_piece
        slot_cap = max(FILL_MIN_PIECE, min(max_piece, slot_cap))
        if i.kind == "still":
            take = min(remaining, slot_cap)
            start = 0.0
        else:
            # 源片开头的那一段几乎都是黑场淡入(打标签抽的是段中间的帧,看不出来)
            skip = _head_skip(i)
            length = i.length - skip
            take = min(length, remaining, slot_cap)
            # 别留一截过短的尾巴:够长就把它吃进这一段
            if remaining - take < FILL_MIN_PIECE:
                take = min(length, remaining)
            start = i.start + skip
        if take < min(FILL_MIN_PIECE, remaining):
            continue
        # 视频优先时官方预告的黑底片名、暗场过渡也会被挑中,这截一样不要
        if i.kind == "video" and (i.launch or looks.current().video_first) and _span_dark(
                i.path, start, take, launch=i.launch):
            continue
        out.append(Piece(item_id=i.id, kind=i.kind, path=clean_still(i), start=start, length=round(take, 3),
                         crop_bottom=SUBTITLE_CROP if _croppable(i) else AUDIENCE_CROP if _audience(i) else 0.0,
                         crop_edge=_crop_edge(i), third_party=_third_party(i), **_trace(book, i)))
        used[i.id] = used.get(i.id, 0) + 1
        recent.append(i.id)
        del recent[:-RECENT_PIECES]
        covered += take
    if out and covered < seconds - 0.05:
        # 池子太小铺不满:把剩下的时间**再切几刀**补上,而不是把最后一帧冻到底。
        #
        # 冻住 = 这一拍后半程画面一动不动(实测内饰这类素材少的视角会这样),
        # 复盘要的「每 5–6 秒一次画面变化」就断在这里。优先拿还没用过的干净官图
        # 补:同一批图配不同运镜,读起来像换了镜头,比冻帧强得多。
        rest = seconds - covered
        spare = [i for i in lib.items if i.kind == "still" and i.usable and not i.small_still
                 and Path(i.path).is_file() and not is_dark(i)
                 and i.problem not in ("cropped", "unclear", "poster")
                 and (not strict or _confirmed(i))
                 and not (want_detail and _detail_conflict(i, want_detail, vocab))]
        spare = book.keep(spare, BACKGROUND)
        spare.sort(key=lambda i: (bool(want) and i.view not in want, _branded_still(i),
                                   used.get(i.id, 0), i.target != "yes", i.id))
        k = 0
        while rest > 0.05 and spare:
            i = spare[k % len(spare)]
            k += 1
            take = min(rest, FILL_MAX_PIECE)
            out.append(Piece(item_id=i.id, kind="still", path=clean_still(i), start=0.0,
                             length=round(take, 3), **_trace(book, i)))
            used[i.id] = used.get(i.id, 0) + 1
            recent.append(i.id)
            del recent[:-RECENT_PIECES]
            rest -= take
        if rest > 0.05:
            # 连一张能用的官图都没有:只能把最后一段拉长(总比这一拍没有背景强)
            last = out[-1]
            last.length = round(last.length + rest, 3)
    return out


def piece_for(subject: str, picks: Sequence[Mapping[str, Any]], seconds: float, *,
              role: str = "body", used: dict[str, int] | None = None,
              recent: list[str] | None = None) -> list[Piece]:
    """按指定的素材铺一刀(Claude 审片时钉选的画面),裁切 / 抹牌 / 使用记录和 `fill` 一样。

    `picks` 是依次拼接的素材 `[{"item_id", "start"?, "end"?}]`:`start` / `end` 给了就截这一段
    (长视频切出来的 `~k` 块就是这样带过来的),视频截到用完为止,最后一段补满到 `seconds`。
    挑的素材得过和 `fill` 同样的硬门槛:可用、文件在、授权允许当背景、没有主持人;
    hook / ending 还得是确认本车的一方素材。过不了或铺不满抛 ValueError。"""
    if role not in ROLES:
        raise ValueError(f"未知的 role:{role}")
    lib = Library.load(subject)
    book = LicenseBook(lib)
    by_id = {i.id: i for i in lib.items}
    hosts = _host_sources(lib.items)
    seconds = float(seconds)
    used = used if used is not None else {}
    recent = recent if recent is not None else []
    out: list[Piece] = []
    covered = 0.0
    for n, pick in enumerate(picks):
        iid = str(pick.get("item_id") or "")
        base = by_id.get(base_item_id(iid))
        if base is None:
            raise ValueError(f"{subject} 的素材池里没有 {iid}")
        item = base
        if pick.get("start") is not None and pick.get("end") is not None and base.kind == "video":
            item = replace(base, id=iid, start=float(pick["start"]), end=float(pick["end"]))
        if not (item.usable and Path(item.path).is_file()):
            raise ValueError(f"{iid} 不可用(没打标签 / 标了问题 / 文件不在)")
        if not book.keep([item], BACKGROUND) or not screen(lib, [item]):
            raise ValueError(f"{iid} 的授权不允许当背景")
        if _hard_bad(item, hosts):
            raise ValueError(f"{iid} 有主持人出镜或硬伤,不能当背景")
        if role != "body" and (not _confirmed(item) or _third_party(item)):
            raise ValueError(f"{iid}:开头 / 结尾只能用确认是这台车的一方素材")
        remaining = seconds - covered
        if remaining <= 0.05:
            break
        last = n == len(picks) - 1
        if item.kind == "still":
            take, start = remaining if last else min(remaining, FILL_MAX_PIECE), 0.0
        else:
            skip = _head_skip(item)
            take = min(item.length - skip, remaining)
            start = item.start + skip
            if take < min(FILL_MIN_PIECE, remaining):
                raise ValueError(f"{iid} 只有 {item.length:.1f} 秒,太短")
        out.append(Piece(item_id=item.id, kind=item.kind, path=clean_still(item), start=start,
                         length=round(take, 3),
                         crop_bottom=SUBTITLE_CROP if _croppable(item)
                         else AUDIENCE_CROP if _audience(item) else 0.0,
                         crop_edge=_crop_edge(item), third_party=_third_party(item),
                         **_trace(book, item)))
        used[item.id] = used.get(item.id, 0) + 1
        recent.append(item.id)
        del recent[:-RECENT_PIECES]
        covered += take
    if covered < seconds - 0.05:
        raise ValueError(f"选的素材一共只有 {covered:.1f} 秒,这一刀要 {seconds:.1f} 秒:再接一段")
    return out


#: 一个长镜头最多放慢到原速的几分之一(概念动画的底)
LONG_TAKE_MAX_SLOWDOWN = 2.2
#: 长镜头优先的视角:动起来的车(行驶、航拍),数据压在上面最有"在跑"的感觉
LONG_TAKE_VIEWS = ("driving", "aerial")


def long_take(subject: str, seconds: float, *, hint: str = "",
              used: dict[str, int] | None = None, recent: list[str] | None = None,
              role: str = "body") -> Piece | None:
    """给概念动画挑**一个**能撑满(或放慢后撑满)整拍的干净长镜头。

    和 `fill` 的区别:fill 追求 2–4 秒一刀的节奏,概念动画上面压着大量信息,底在跳切会显得乱。
    排序:是这台车 > 没用过(按近重复簇算)> 不在最近几刀里 > 行驶/航拍 > 干净 > 官方 > 长。
    够长的段从中间截;找不到返回 None,调用方退回 fill 的多段拼接。
    `used` / `recent` 和 fill 共用,就地更新。`role` 同 fill:开场 / 结论只用官方或认出的一方视频,
    正文的第三方视频要测过台标,且排在一方视频后面。
    """
    used = used if used is not None else {}
    recent = recent if recent is not None else []
    lib = Library.load(subject)
    vocab = vocab_of(lib)
    book = LicenseBook(lib)
    want = vocab.hint_views(hint)
    need = seconds / LONG_TAKE_MAX_SLOWDOWN
    pool = [i for i in lib.items if i.kind == "video" and i.usable and Path(i.path).is_file()
            and not is_dark(i) and not _text_slide(i) and not _blackout(i) and (not want or i.view in want)]
    pool = screen(lib, book.keep(pool, BACKGROUND))
    hosts = _host_sources(lib.items)
    pool = [i for i in pool if not _hard_bad(i, hosts)]
    if role != "body":
        pool = [i for i in pool if _confirmed(i) and not _third_party(i)]
    else:
        pool = [i for i in pool if not _third_party(i) or _third_party_ok(i)]

    def usable_len(i: Item) -> float:
        return i.length - _head_skip(i)

    pool = [i for i in pool if usable_len(i) >= need]
    if not pool:
        return None
    group = near_dup_groups(pool)
    recent_groups = {group.get(r, r) for r in recent[-RECENT_PIECES:]}
    titled = titled_sources(lib)
    i = min(pool, key=lambda x: (_third_party(x), not on_target(x, titled), not x.hd, x.low_bitrate,
                                 used.get(group[x.id], 0) + used.get(x.id, 0),
                                 group[x.id] in recent_groups, x.view not in LONG_TAKE_VIEWS,
                                 _dirt(x), not x.official, -x.length))
    skip = _head_skip(i)
    length = usable_len(i)
    take = min(length, seconds)
    start = i.start + skip + (length - take) / 2
    used[i.id] = used.get(i.id, 0) + 1
    recent.append(i.id)
    del recent[:-RECENT_PIECES]
    return Piece(item_id=i.id, kind="video", path=i.path, start=round(start, 3), length=round(take, 3),
                 crop_bottom=SUBTITLE_CROP if _croppable(i) else 0.0, crop_edge=_crop_edge(i),
                 third_party=_third_party(i), **_trace(book, i))
