"""概念动画的渲染回归:每个模版渲一帧,和基线逐像素比。

改完模版或骨架后跑一下,画面变了就会报出来。

用法:
    python scripts/motion_regress.py                  # 渲染全部模版并与基线比对
    python scripts/motion_regress.py --update         # 用当前渲染结果重建基线
    python scripts/motion_regress.py --save-png out/  # 顺便把 PNG 存下来供肉眼比对
    python scripts/motion_regress.py --only neon      # 只跑指定模版

基线在 motion/regress/baseline.json(存的是原始像素的哈希,不是 PNG 文件 —— PNG 的
编码元数据可能变,像素不会)。

只能在装了 HyperFrames + Chrome 的机器上跑,而且要有中文字体:HyperFrames 按页面上
出现的字符裁内嵌字体子集,字体集合不一样,同样的模版渲出来的像素也不一样。所以
Windows 之外跑这个基线对不上,别把它塞进 CI。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.pipeline import concept_styles, motion  # noqa: E402

#: 基线文件
BASELINE = ROOT / "motion" / "regress" / "baseline.json"
#: 并行渲染的浏览器数
WORKERS = 3
#: 每个模版取两个时刻:一个在动画中途,一个在落定之后
EXTRA_AT = 1.2


def targets() -> list[tuple[str, str, str | None, list[float]]]:
    """(名字, 渲染用的 scene, style id, 时刻列表)。"""
    out: list[tuple[str, str, str | None, list[float]]] = []
    for scene in sorted(motion.available_scenes()):
        out.append((scene, scene, None, sorted(set([concept_styles.SAMPLE_AT[0], EXTRA_AT]))))
    for st in motion.styles():
        if st.id == motion.DEFAULT_STYLE:
            continue
        at = st.sample_at or concept_styles.SAMPLE_AT[0]
        out.append((st.id, concept_styles.SAMPLE_SCENE, st.id, sorted(set([at, EXTRA_AT]))))
    return out


def pixel_hash(path: Path) -> str:
    """原始像素的哈希(不看 PNG 容器)。"""
    with Image.open(path) as im:
        return hashlib.sha256(im.convert("RGB").tobytes()).hexdigest()


def render_all(only: list[str], png_dir: Path | None) -> dict[str, dict[str, str]]:
    """渲染所有模版,返回 {模版名: {时刻: 像素哈希}}。"""
    todo = [t for t in targets() if not only or t[0] in only]
    if png_dir:
        png_dir.mkdir(parents=True, exist_ok=True)
    frames: dict[str, dict[str, str]] = {}
    done: list[str] = []
    failed: list[str] = []

    def one(job: tuple[str, str, str | None, list[float]]) -> None:
        name, scene, style, at = job
        try:
            pngs = motion.snapshot(scene, concept_styles.SAMPLE_DATA, concept_styles.SAMPLE_SECONDS,
                                   at, concept_styles.SIZE, motion.cache_dir(), style=style)
            frames[name] = {}
            for png, t in zip(pngs, at):
                frames[name][f"{t:g}"] = pixel_hash(png)
                if png_dir:
                    shutil.copyfile(png, png_dir / f"{name}__t{t:g}.png")
            done.append(name)
            print(f"  {name} ok")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}: {str(exc)[:200]}")
            print(f"  {name} FAIL {str(exc)[:200]}")

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(one, todo))
    if failed:
        for f in failed:
            print(f"  失败:{f}")
        raise SystemExit(f"{len(failed)} 个模版没渲出来,基线对不了")
    return frames


def main() -> None:
    ap = argparse.ArgumentParser(description="概念动画渲染回归")
    ap.add_argument("--update", action="store_true", help="用当前渲染结果重建基线")
    ap.add_argument("--save-png", metavar="DIR", help="把渲染出的 PNG 存到这个目录")
    ap.add_argument("--only", nargs="*", metavar="NAME", help="只跑这些模版")
    args = ap.parse_args()

    print(f"渲染 {len(targets())} 个模版(每个两个时刻)...")
    frames = render_all(args.only or [], Path(args.save_png) if args.save_png else None)

    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({"version": 1, "frames": frames}, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n基线已更新:{BASELINE.relative_to(ROOT)}({sum(len(v) for v in frames.values())} 帧)")
        return

    if not BASELINE.is_file():
        raise SystemExit(f"没有基线 {BASELINE},先跑一次 --update")

    base = json.loads(BASELINE.read_text(encoding="utf-8"))["frames"]
    changed, added = [], []
    for name, got in frames.items():
        want = base.get(name)
        if want is None:
            added.append(name)
            continue
        for t, h in got.items():
            if want.get(t) != h:
                changed.append(f"{name} @ {t}s")
    missing = [] if args.only else sorted(set(base) - set(frames))

    print(f"\n对比 {sum(len(v) for v in frames.values())} 帧")
    if not changed and not added and not missing:
        print("  全部一致 —— 画面没变")
        return
    for c in changed:
        print(f"  变了:{c}")
    for a in added:
        print(f"  基线里没有:{a}")
    for m in missing:
        print(f"  基线里有但这次没渲:{m}")
    print("\n画面变了。确认是想要的改动就重建基线:python scripts/motion_regress.py --update")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
