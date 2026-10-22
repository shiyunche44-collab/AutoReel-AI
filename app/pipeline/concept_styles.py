"""概念动画选模版:看固定示例图选,不在运行时生成任何东西。

每套模版一张示例图(同一份示例数据的电池拍),是仓库里的静态文件 `motion/templates/samples/<模版>.jpg`。
模版之间版式、动效、字体、配色都不同(见 `motion.styles`);页面直接展示这些图,点一下就选定,
项目渲染时才按选定的模版真正出动画。

改了模版、想更新示例图时手动跑一次(约 1~2 分钟,需要 HyperFrames + 浏览器):

    python -m app.pipeline.concept_styles            # 只补缺的
    python -m app.pipeline.concept_styles --all      # 全部重截
"""
from __future__ import annotations

import argparse
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image

from .. import config, db
from ..models import ShotSpec
from . import library, motion, motion_assets

SIZE = (1920, 1080)
#: 示例图存成这个宽度的 jpg(页面上最多显示到 ~400px 宽,留两倍余量放大看)
SAMPLE_WIDTH = 960
#: 手动重截时并行的浏览器数
WORKERS = 3

#: 示例:电池拍(影棚 + 车图 + 3 条引线,最能看出配色、字体、线稿和光效)
SAMPLE_SCENE = "battery"
SAMPLE_SECONDS = 7.0
SAMPLE_AT = [5.5]
SAMPLE_DATA: dict[str, Any] = {
    "subject": "示例车型", "title": "电池与续航", "columns": [], "source": "示例数据",
    "rows": [{"label": "电池容量", "value": "96.3kWh", "values": ["96.3kWh"], "tag": "示例 | 官方"},
             {"label": "CLTC续航", "value": "835km", "values": ["835km"], "tag": "CLTC | 官方"},
             {"label": "电压平台", "value": "871V", "values": ["871V"], "tag": ""}],
}


def samples_dir() -> Path:
    return motion.templates_dir() / "samples"


def sample_path(style_id: str) -> Path:
    return samples_dir() / f"{style_id}.jpg"


def gallery() -> dict[str, Any]:
    """全部模版 + 示例图文件名(没有示例图为 None)。只读文件,不生成。"""
    items = []
    for st in motion.styles():
        p = sample_path(st.id)
        items.append({**st.summary(), "image": p.name if p.is_file() else None})
    return {"styles": items, "default": motion.DEFAULT_STYLE}


def sample_image(name: str) -> Path | None:
    """示例图文件(只认示例目录里的 jpg,防路径穿越)。"""
    if "/" in name or "\\" in name or not name.endswith(".jpg"):
        return None
    p = samples_dir() / name
    return p if p.is_file() else None


def concept_beats(project_id: str) -> list[ShotSpec]:
    specs = [ShotSpec(**d) for d in db.read_json(project_id, "storyboard.json", []) or []]
    return [s for s in specs if s.layout == "concept" and s.overlay and s.duration > 0]


def project_status(project_id: str) -> dict[str, Any]:
    from .explainer import concept_style_of

    return {"style": concept_style_of(project_id), "beats": len(concept_beats(project_id))}


# --------------------------------------------------------------------------
# 手动重截示例图
# --------------------------------------------------------------------------
def _sample_photo() -> motion_assets.Cutout | None:
    """示例用的车图:素材库里第一台能抠出白底侧面图的车;都没有就用示意线稿车。"""
    views = motion.scene_photo_views(SAMPLE_SCENE)
    root = config.DATA_DIR / "library"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not d.is_dir():
            continue
        try:
            lib = library.Library.load(d.name)
        except Exception:  # noqa: BLE001 - 坏的库跳过
            continue
        if lib.category and not lib.category.startswith("auto"):
            continue  # 示例是"电池与续航",只用车的图
        got = motion_assets.hero_still(lib.subject, views)
        if got is not None:
            return got
    return None


def make_samples(everything: bool = False) -> list[str]:
    """截示例图写进 `motion/templates/samples/`,返回失败的模版。everything=False 只补缺的。"""
    ok, why = config.motion_status()
    if not ok:
        raise SystemExit(why)
    todo = [s.id for s in motion.styles() if everything or not sample_path(s.id).is_file()]
    samples_dir().mkdir(parents=True, exist_ok=True)
    photo = _sample_photo()
    failed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        def one(sid: str) -> None:
            try:
                at = [motion.get_style(sid).sample_at] if motion.get_style(sid).sample_at else SAMPLE_AT
                png = motion.snapshot(SAMPLE_SCENE, SAMPLE_DATA, SAMPLE_SECONDS, at, SIZE,
                                      Path(tmp), photo=photo, style=sid)[0]
            except motion.MotionError as exc:
                print(f"  {sid} 失败:{str(exc)[:200]}")
                failed.append(sid)
                return
            im = Image.open(png).convert("RGB")
            im = im.resize((SAMPLE_WIDTH, round(SAMPLE_WIDTH * im.height / im.width)), Image.LANCZOS)
            im.save(sample_path(sid), quality=86, optimize=True)
            print(f"  {sid} ok")

        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(one, todo))
    return failed


def _main() -> None:
    ap = argparse.ArgumentParser(description="重截概念动画模版示例图")
    ap.add_argument("--all", action="store_true", help="全部重截(默认只补缺的)")
    args = ap.parse_args()
    failed = make_samples(args.all)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    _main()
