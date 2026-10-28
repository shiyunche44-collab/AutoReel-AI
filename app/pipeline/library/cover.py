"""封面底图:棚拍图判定与官图挑选。"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .model import Item, Library, vocab_of
from .select import near_dup_groups


#: 封面底图:边缘取样带宽(占图宽/高的比例)
COVER_EDGE = 0.045
#: 棚拍白底判据:边缘平均亮度 ≥ 这个且边缘颜色标准差 ≤ 这个 → 白底棚拍图
COVER_STUDIO_LUMA = 236
COVER_STUDIO_SPREAD = 26


def is_studio_shot(path: str | Path) -> bool:
    """是不是白底棚拍图:四边取样都是接近纯白、颜色又很均匀。

    判据是"边缘"而不是"整体":白底棚拍图的车只占中间一小块,整体亮度被拉低;
    而实拍图哪怕主体是白车,背景也一定有灰度渐变和杂色,边缘标准差大得多。

    封面为什么在意这个:棚拍白底图既没有第三方水印(汽车之家之类),
    又能给深色标题留出干净的底,比任何实拍帧都更像"官方海报"。
    """
    try:
        from PIL import Image, ImageStat

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            ex, ey = max(2, int(w * COVER_EDGE)), max(2, int(h * COVER_EDGE))
            edges = [im.crop((0, 0, w, ey)), im.crop((0, h - ey, w, h)),
                     im.crop((0, 0, ex, h)), im.crop((w - ex, 0, w, h))]
            for e in edges:
                st = ImageStat.Stat(e)
                if min(st.mean) < COVER_STUDIO_LUMA:
                    return False
                if sum(st.stddev) > COVER_STUDIO_SPREAD:
                    return False
        return True
    except Exception:  # noqa: BLE001
        return False


def cover_still(subject: str, exclude: Sequence[str | Path] = ()) -> Path | None:
    """挑一张能当封面底图的官图。没有就返回 None(调用方退回抽帧)。

    封面是唯一决定点击率的一帧,对底图的要求比背景高:
    1. **完整** —— 车在画面里,不能是半个车头;
    2. **干净** —— 不带字幕 / 角标 / 第三方水印;
    3. **无水印优先** —— 白底棚拍图最理想(汽车之家这类图库图带角标水印,
       发出去等于替别人打广告);
    4. 认得出是这台车(target == yes)优先。

    `exclude`:已经用过的底图。这些图本身和与它们近重复(dHash)的图都跳过 ——
    封面 B 要一张**看起来不同**的图,同一张图换个角度裁剪不算。
    """
    try:
        lib = Library.load(subject)
    except Exception:  # noqa: BLE001
        return None
    vocab = vocab_of(lib)
    # 主体视角(主背景那一组):整车 / 整体,不做局部特写
    hero = vocab.primary_views or set(vocab.views)
    cands = [i for i in lib.items
             if i.kind == "still" and i.usable and not i.small_still and Path(i.path).is_file()
             and i.problem not in ("cropped", "unclear", "poster")
             and i.view in hero]
    if exclude:
        used = {str(Path(p)) for p in exclude}
        used_ids = {i.id for i in lib.items if str(Path(i.path)) in used}
        groups = near_dup_groups(lib.items)
        banned = {groups.get(u, u) for u in used_ids} | used_ids
        cands = [i for i in cands if i.id not in banned and groups.get(i.id, i.id) not in banned]
    if not cands:
        return None

    def rank(i: Item) -> tuple:
        studio = is_studio_shot(i.path)
        # 白底棚拍 > 认得出是这台车 > 分辨率高
        return (not studio, i.target != "yes", i.problem != "",
                -_still_area(i.path), i.id)

    for item in sorted(cands, key=rank):
        return Path(item.path)
    return None


def _still_area(path: str | Path) -> int:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size[0] * im.size[1]
    except Exception:  # noqa: BLE001
        return 0
