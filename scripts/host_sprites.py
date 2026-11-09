"""虚拟主持人「柒哥」的形象素材:一次性生成,人工挑选后入库。

    python scripts/host_sprites.py candidates            # 出 4 个 Q版候选 → data/host/candidates/
    python scripts/host_sprites.py poses <候选png> <id>   # 以选定图为参考出嘴型/眨眼 → app/assets/host/<id>/

生图走方舟 Seedream(会消耗套餐额度);抠图用 rembg。出图没有 seed,不满意就重跑。
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.providers import ark_image  # noqa: E402

CAND_DIR = ROOT / "data" / "host" / "candidates"
ASSET_DIR = ROOT / "app" / "assets" / "host"

BASE = ("Q版二头身卡通男生形象,汽车博主「柒哥」,30岁左右,短发,友善自信,"
        "半身正面朝向镜头,双手自然放在身前,纯白色背景,无文字,无车标,"
        "扁平插画风格,线条干净,高清")
LOOKS = [
    "穿深蓝色夹克,戴黑框眼镜,微笑闭嘴",
    "穿白色T恤外搭橙色马甲,手里拿着车钥匙,微笑闭嘴",
    "穿灰色卫衣,戴耳机挂在脖子上,微笑闭嘴",
    "穿浅蓝衬衫,竖起一个大拇指,微笑闭嘴",
]
#: 姿态不变,只改嘴和眼:图生图保持同一角色
POSES = {
    "closed": "保持完全相同的角色、姿势、构图、颜色和背景,只把嘴巴改成自然闭合的微笑",
    "half": "保持完全相同的角色、姿势、构图、颜色和背景,只把嘴巴改成说话时微微张开",
    "open": "保持完全相同的角色、姿势、构图、颜色和背景,只把嘴巴改成说话时张大",
    "blink": "保持完全相同的角色、姿势、构图、颜色和背景,嘴巴闭合微笑,只把眼睛改成闭眼",
}


def _download(url: str, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    out.write_bytes(resp.content)
    return out


def _cutout(src: Path, out: Path, height: int = 720) -> Path:
    from rembg import new_session, remove

    im = Image.open(src).convert("RGB")
    rgba = remove(im, session=new_session("isnet-general-use"))
    rgba = rgba.resize((round(rgba.width * height / rgba.height), height), Image.LANCZOS)
    rgba.save(out)
    return out


def candidates() -> None:
    for i, look in enumerate(LOOKS):
        url = ark_image.generate_one(f"{BASE},{look}", aspect_ratio="3:4")
        path = _download(url, CAND_DIR / f"cand_{i}.png")
        print(path)


def poses(ref: Path, host_id: str) -> None:
    data = "data:image/png;base64," + base64.b64encode(ref.read_bytes()).decode()
    out_dir = ASSET_DIR / host_id
    raw_dir = ROOT / "data" / "host" / host_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, prompt in POSES.items():
        url = ark_image.generate_one(prompt, aspect_ratio="3:4", input_urls=[data])
        raw = _download(url, raw_dir / f"{name}.png")
        print(_cutout(raw, raw_dir / f"cut_{name}.png"))


#: 局部贴片框(相对宽高,以 720 高的闭嘴图为准):图生图出来的各张身体和脸型会有细微偏差,
#: 整张切换会抖,所以只把嘴 / 眼这一小块对齐后贴回闭嘴底图
PATCHES = {"half": (0.40, 0.44, 0.60, 0.56), "open": (0.40, 0.44, 0.60, 0.56),
           "blink": (0.27, 0.33, 0.73, 0.46)}


def _align(base, var, box, search: int = 24):
    """在 ±search 像素内找贴片的最佳平移(灰度 SSD,只比框外一圈环带,避开要换的部位)。"""
    import numpy as np

    b = np.asarray(base.convert("L"), dtype=np.float32)
    v = np.asarray(var.convert("L"), dtype=np.float32)
    x0, y0, x1, y1 = box
    pad = 20
    ring = np.ones((y1 - y0 + 2 * pad, x1 - x0 + 2 * pad), bool)
    ring[pad:-pad, pad:-pad] = False
    ref = b[y0 - pad:y1 + pad, x0 - pad:x1 + pad]
    best, arg = None, (0, 0)
    for dy in range(-search, search + 1, 2):
        for dx in range(-search, search + 1, 2):
            cand = v[y0 - pad + dy:y1 + pad + dy, x0 - pad + dx:x1 + pad + dx]
            if cand.shape != ref.shape:
                continue
            err = float(((cand - ref)[ring] ** 2).mean())
            if best is None or err < best:
                best, arg = err, (dx, dy)
    return arg


def compose(host_id: str) -> None:
    """把 half/open/blink 的嘴、眼对齐后贴回 closed 底图,写入 app/assets/host/<id>/。"""
    from PIL import ImageFilter

    d = ASSET_DIR / host_id
    raw = ROOT / "data" / "host" / host_id
    base = Image.open(raw / "cut_closed.png").convert("RGBA")
    base.save(d / "closed.png")
    w, h = base.size
    for name, rel in PATCHES.items():
        var = Image.open(raw / f"cut_{name}.png").convert("RGBA")
        if var.size != base.size:
            var = var.resize(base.size, Image.LANCZOS)
        box = (round(rel[0] * w), round(rel[1] * h), round(rel[2] * w), round(rel[3] * h))
        dx, dy = _align(base, var, box)
        patch = var.crop((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
        mask = Image.new("L", patch.size, 0)
        inset = 8
        mask.paste(255, (inset, inset, patch.width - inset, patch.height - inset))
        mask = mask.filter(ImageFilter.GaussianBlur(5))
        out = base.copy()
        out.paste(patch, box[:2], mask)
        out.save(d / f"{name}.png")
        print(name, "shift", dx, dy)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "candidates":
        candidates()
    elif len(sys.argv) >= 4 and sys.argv[1] == "poses":
        poses(Path(sys.argv[2]), sys.argv[3])
        compose(sys.argv[3])
    elif len(sys.argv) >= 3 and sys.argv[1] == "compose":
        compose(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
