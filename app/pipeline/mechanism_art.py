"""机构剖视图:给 mech 版式的 cutaway 模版供图。

图是生成的(豆包 Seedream,`providers.ark_image`),只当**示意**用:

- 一个部件一张,按「部件 + 描述」缓存在 `data/_cache/mechanism/`,跨项目复用
  (接口没有 seed,同一描述每次出图不同 —— 不缓存的话同一部件每条片子长得都不一样);
- 描述里统一加「不要文字、数字、标志」,而且描述本身不许写数字(加载品类时校验):
  画面上的字和数字只能是我们自己画的标注,出处在事实层;
- 画的是这一类部件的通用结构,不是具体车型的零件。成片里模版会标「示意图」。

出图失败抛 `MechArtError`,调用方把这一拍退回别的画面,不让整拍失败。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from .. import config

_log = logging.getLogger(__name__)

#: 所有剖视图共用的画风与禁区。改了它等于换了一批图(缓存键里有它)
STYLE = (
    "{art}. Studio product render on a plain very dark charcoal background, single object centred, "
    "three-quarter view, clean cut surfaces showing the internal parts, brushed metal with subtle "
    "lime green accent highlights on the moving parts, soft top light, high detail, photorealistic. "
    "Absolutely no text, no letters, no numbers, no labels, no arrows, no logos, no brand badges, "
    "no watermark, no people, no whole car body."
)
ASPECT = "4:3"
_MIN_BYTES = 20_000
#: 同一个部件的几个视角:0 是整体剖视(描述原样),后面的在描述后面补一句。
#: 连着几拍讲同一个部件时轮着用(`explainer.mech.assign_views`),不然七拍都是同一张图
VIEWS = (
    "",
    "extreme close-up of the internal moving parts, shallow depth of field",
    "exploded view with the main parts pulled apart along the central axis",
)
#: 出图后问一遍视觉模型:图上有字 / 数字 / 标志就重画。提示词里禁了也偶尔会画出铭牌
TEXT_CHECK = ("Does this image contain any readable text, letters, numbers, labels, a brand logo "
              "or a watermark? Answer with only YES or NO.")
RETRIES = 1


class MechArtError(RuntimeError):
    pass


def cache_dir() -> Path:
    return config.DATA_DIR / "_cache" / "mechanism"


def prompt_for(art: str, view: int = 0) -> str:
    art = art.strip().rstrip(".")
    extra = VIEWS[view % len(VIEWS)]
    return STYLE.format(art=f"{art}, {extra}" if extra else art)


def path_for(part: str, art: str, view: int = 0) -> Path:
    key = hashlib.sha1(prompt_for(art, view).encode("utf-8")).hexdigest()[:12]
    return cache_dir() / f"{part}_{key}.png"


def says_clean(path: Path) -> bool:
    """问视觉模型图上有没有字 / 数字 / 标志。模型不可用时放行(和封面底图的质检一个做法)。"""
    from ..providers import cli, images

    try:
        answer = images.describe([cli.upload_temp_url(path)], TEXT_CHECK).strip().upper()
    except Exception as exc:  # noqa: BLE001
        _log.info("剖视图视觉质检不可用,跳过:%s", exc)
        return True
    return not (answer.startswith("YES") or "YES" in answer[:12] or answer.startswith(("是", "有")))


def cutaway(part: str, art: str, *, view: int = 0, generate: bool = True) -> Path:
    """这个部件的剖视图。缓存里有就直接用;没有且 `generate` 为真时出一张。
    图上带字的重画一次,还带字就报错(调用方退回别的画面)。"""
    if not art.strip():
        raise MechArtError(f"部件 {part} 没有配生图描述(mech_parts.{part}.art)")
    dest = path_for(part, art, view)
    if dest.is_file() and dest.stat().st_size >= _MIN_BYTES:
        return dest
    if not generate:
        raise MechArtError(f"部件 {part} 的剖视图还没生成")
    last: MechArtError | None = None
    for _ in range(RETRIES + 1):
        try:
            return _generate(part, art, view, dest)
        except MechArtError as exc:
            last = exc
    raise last or MechArtError(f"部件 {part} 的剖视图生成失败")


def _generate(part: str, art: str, view: int, dest: Path) -> Path:
    from ..providers import ark_image
    from ..providers.cli import ConnectorError

    try:
        url = ark_image.generate_one(prompt_for(art, view), aspect_ratio=ASPECT)
        resp = httpx.get(url, timeout=120, follow_redirects=True)
        resp.raise_for_status()
    except (ConnectorError, httpx.HTTPError) as exc:
        raise MechArtError(f"部件 {part} 的剖视图生成失败:{exc}") from exc
    if len(resp.content) < _MIN_BYTES:
        raise MechArtError(f"部件 {part} 的剖视图下载到的文件太小({len(resp.content)} 字节)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(resp.content)
    # 统一存成 PNG(接口给的是 jpeg):模版里固定引用 part.png
    try:
        from PIL import Image

        png = dest.with_suffix(".check.png")
        with Image.open(tmp) as im:
            im.convert("RGB").save(png, "PNG")
        tmp.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - 解不开的图不能进缓存
        tmp.unlink(missing_ok=True)
        raise MechArtError(f"部件 {part} 的剖视图解码失败:{exc}") from exc
    if not says_clean(png):
        png.unlink(missing_ok=True)
        raise MechArtError(f"部件 {part} 的剖视图上有文字或标志,不能用")
    png.replace(dest)
    _log.info("生成剖视图 %s → %s", part, dest.name)
    return dest
