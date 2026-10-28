"""取材:给一拍挑干净片段连拼成背景(fill / long_take)。"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
from ...categories import Vocab
from .. import knobs

from .model import FILL_MIN_PIECE, Item, Library, STILL_SECONDS, _confirmed, _hard_bad, _has_host, _host_sources, _third_party, _third_party_ok, vocab_of
from .tagging import _dhash

log = logging.getLogger(__name__)


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


#: 一段最长用几秒就换下一段 —— 信息解说的背景是 2–4 秒一刀的节奏,
#: 一个镜头拖太久反而像卡住了
FILL_MAX_PIECE = 4.5


#: 从源片第 0 秒开始的段,跳过开头这么多秒(黑场淡入)
SOURCE_FADE_IN = 0.8
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


def is_dark(item: Item) -> bool:
    """背景用不了的黑场:大面积黑底 + 一行小字的片名卡。叠上面板后整屏发黑。"""
    probe = item.frame or (item.path if item.kind == "still" else "")
    return bool(probe) and Path(probe).is_file() and _luma(probe) < DARK_LUMA


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
    dest = src.parent.parent / "clean" / src.name
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


def _croppable(item: Item) -> bool:
    """只有烧录字幕、没有主持人的视频段:裁掉底部就是干净画面。"""
    return item.kind == "video" and item.problem == "subtitle" and not _has_host(item)


def _fill_tier(item: Item, used: int) -> int:
    """非官方来源、车型没确认(target 不是 yes)的素材整体排到确认过的后面,
    池子里确认过的够用时就不会选到它们(YU7 池里混的 SU7 片段多是这类)。"""
    base = _fill_tier_base(item, used)
    if not item.official and item.target != "yes":
        return base + 10
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


def fill(subject: str, seconds: float, *, hint: str = "", used: dict[str, int] | None = None,
         recent: list[str] | None = None, detail: str = "", role: str = "body") -> list[Piece]:
    """给一个节拍铺背景:从这台车的池子里挑干净片段**连拼**到 `seconds`。

    和 `candidates()` 的根本区别:这里不要求画面对上旁白,也不要求单段撑满时长 ——
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
    返回空列表表示池子里什么都没有。
    """
    if role not in ROLES:
        raise ValueError(f"未知的 role:{role}")
    seconds = float(seconds)
    used = used if used is not None else {}
    recent = recent if recent is not None else []
    lib = Library.load(subject)
    vocab = vocab_of(lib)
    want = vocab.hint_views(hint)
    want_detail = vocab.detail_types_in(detail) if detail else set()
    pool = [i for i in lib.items if i.usable and Path(i.path).is_file() and (
        (i.kind == "video" and i.length >= FILL_MIN_PIECE)
        or (i.kind == "still" and i.view in vocab.angle_of_view and not i.small_still))
        and not is_dark(i)]
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
    fallback = _pool_seconds([i for i in pool if not _third_party(i) and not group_used(i)]) < seconds
    if not fallback:
        pool = [i for i in pool if not _third_party(i)]

    def stage(i: Item, n: int) -> int:
        """没用过的一方素材 → 没用过的第三方 → 用过的(再按档位)。不兜底时全是 0,只看档位。"""
        if not fallback:
            return 0
        if n:
            return 2
        return 1 if _third_party(i) else 0

    def rank(i: Item, recent_groups: set[str], last_view: str) -> tuple:
        # 认出是这台车的排在没认出的前面:没认出的常是同品牌别的车型(YU7 池子里
        # 混着 SU7 的赛道活动),背景里出现别的车比画面不对口严重得多
        n = group_used(i)
        fit = _detail_fit(i, want_detail, vocab)
        tier = _fill_tier(i, n)
        if fit == 0 and i.kind == "still":
            # 这一拍讲的正是这个部件,那张特写就是最贴的素材 —— 提档到和干净
            # 视频同档。否则永远选不到它:静图本来比视频低一档。
            tier = 0
        # 码率不够的视频同档里往后排(不降档:AV1 官方片普遍码率低,降档就全让给静图了)
        return (stage(i, n), tier, i.target != "yes", i.low_bitrate, _branded_still(i),
                group[i.id] in recent_groups,
                bool(want) and i.view not in want, bool(last_view) and i.view == last_view, n,
                _dirt(i), fit, _detail_conflict(i, want_detail, vocab),
                not i.official, -i.length)

    out: list[Piece] = []
    covered = 0.0
    left = list(pool)
    # 一刀最长几秒跟着项目的剪辑节奏旋钮走;没设 = FILL_MAX_PIECE
    max_piece = FILL_MAX_PIECE * knobs.piece_seconds() / knobs.PIECE_SECONDS["mid"]
    while left and covered < seconds - 0.05:
        window = recent[-RECENT_PIECES:]
        recent_groups = {group.get(r, r) for r in window}
        last = by_id.get(window[-1]) if window else None
        i = min(left, key=lambda x: rank(x, recent_groups, last.view if last else ""))
        left.remove(i)
        remaining = seconds - covered
        if i.kind == "still":
            take = min(remaining, max_piece)
            start = 0.0
        else:
            # 源片开头的那一段几乎都是黑场淡入(打标签抽的是段中间的帧,看不出来)
            skip = SOURCE_FADE_IN if i.start < 0.5 else 0.0
            length = i.length - skip
            take = min(length, remaining, max_piece)
            # 别留一截过短的尾巴:够长就把它吃进这一段
            if remaining - take < FILL_MIN_PIECE:
                take = min(length, remaining)
            start = i.start + skip
        if take < min(FILL_MIN_PIECE, remaining):
            continue
        out.append(Piece(item_id=i.id, kind=i.kind, path=clean_still(i), start=start, length=round(take, 3),
                         crop_bottom=SUBTITLE_CROP if _croppable(i) else 0.0,
                         crop_edge=_crop_edge(i), third_party=_third_party(i)))
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
        spare.sort(key=lambda i: (bool(want) and i.view not in want, _branded_still(i),
                                   used.get(i.id, 0), i.target != "yes", i.id))
        k = 0
        while rest > 0.05 and spare:
            i = spare[k % len(spare)]
            k += 1
            take = min(rest, FILL_MAX_PIECE)
            out.append(Piece(item_id=i.id, kind="still", path=clean_still(i), start=0.0,
                             length=round(take, 3)))
            used[i.id] = used.get(i.id, 0) + 1
            recent.append(i.id)
            del recent[:-RECENT_PIECES]
            rest -= take
        if rest > 0.05:
            # 连一张能用的官图都没有:只能把最后一段拉长(总比这一拍没有背景强)
            last = out[-1]
            last.length = round(last.length + rest, 3)
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
    want = vocab.hint_views(hint)
    need = seconds / LONG_TAKE_MAX_SLOWDOWN
    pool = [i for i in lib.items if i.kind == "video" and i.usable and Path(i.path).is_file()
            and not is_dark(i) and (not want or i.view in want)]
    hosts = _host_sources(lib.items)
    pool = [i for i in pool if not _hard_bad(i, hosts)]
    if role != "body":
        pool = [i for i in pool if _confirmed(i) and not _third_party(i)]
    else:
        pool = [i for i in pool if not _third_party(i) or _third_party_ok(i)]

    def usable_len(i: Item) -> float:
        return i.length - (SOURCE_FADE_IN if i.start < 0.5 else 0.0)

    pool = [i for i in pool if usable_len(i) >= need]
    if not pool:
        return None
    group = near_dup_groups(pool)
    recent_groups = {group.get(r, r) for r in recent[-RECENT_PIECES:]}
    i = min(pool, key=lambda x: (_third_party(x), x.target != "yes", x.low_bitrate,
                                 used.get(group[x.id], 0) + used.get(x.id, 0),
                                 group[x.id] in recent_groups, x.view not in LONG_TAKE_VIEWS,
                                 _dirt(x), not x.official, -x.length))
    skip = SOURCE_FADE_IN if i.start < 0.5 else 0.0
    length = usable_len(i)
    take = min(length, seconds)
    start = i.start + skip + (length - take) / 2
    used[i.id] = used.get(i.id, 0) + 1
    recent.append(i.id)
    del recent[:-RECENT_PIECES]
    return Piece(item_id=i.id, kind="video", path=i.path, start=round(start, 3), length=round(take, 3),
                 crop_bottom=SUBTITLE_CROP if _croppable(i) else 0.0, crop_edge=_crop_edge(i),
                 third_party=_third_party(i))
