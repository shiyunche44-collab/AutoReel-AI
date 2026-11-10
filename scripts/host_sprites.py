"""虚拟主持人「柒哥」的形象素材:一次性生成,人工挑选后入库。

    python scripts/host_sprites.py candidates            # 出 4 个 Q版候选 → data/host/candidates/
    python scripts/host_sprites.py poses <候选png> <id>   # 以选定图为参考出嘴型/眨眼 → app/assets/host/<id>/
    python scripts/host_sprites.py expressions <id> [名字...]  # 出表情整图(惊讶/摇头/点赞/指向)→ 同一目录
    python scripts/host_sprites.py expressions <id> --refit   # 不重新生图,只把已有原图重新抠图入库

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


#: 表情整图:闪现 1 秒左右的反应,整张替换(不对嘴型),所以允许姿势变化。名字与 host.REACTIONS 对应
_SAME = "保持完全相同的角色、发型、眼镜、服装、画风和纯白背景,半身正面,无文字,"
EXPRESSIONS = {
    "surprised": _SAME + "表情改成惊讶:眼睛睁大,嘴巴张成圆形,双手抬到胸前张开",
    "shake": _SAME + "表情改成不认同:皱眉,嘴角下撇,右手抬起在胸前摆手表示不行",
    "thumbs": _SAME + "表情改成开心大笑,右手在胸前竖起大拇指",
    "point": _SAME + "表情微笑张嘴说话,只抬起一只手、伸出一根食指指向画面右侧,另一只手自然垂下不做手势",
}


def _fit(cut: Path, raw: Path, out: Path, size: tuple[int, int]) -> Path:
    """抠好的表情图 → 入库规格:补回被抠掉的白 T 恤,再按人物外框对齐到和闭嘴图一样的画布。

    白底上的白衣服会被 rembg 当背景抠成半透明。真正的背景是从上 / 左 / 右三条边连进来的
    全透明区(衣服一直画到下沿,所以底边不算);离它 5 像素以外、又没抠实的地方都是人物内部,
    用原图颜色填实。"""
    import numpy as np
    from scipy import ndimage

    rgba = Image.open(cut).convert("RGBA")
    src = np.asarray(Image.open(raw).convert("RGB").resize(rgba.size, Image.LANCZOS))
    px = np.asarray(rgba).copy()
    alpha = px[..., 3]
    labels, _ = ndimage.label(alpha < 20)
    edge = set(labels[0]) | set(labels[:, 0]) | set(labels[:, -1])
    outside = np.isin(labels, [k for k in edge if k])
    holes = ~ndimage.binary_dilation(outside, iterations=5) & (alpha < 250)
    px[holes, :3] = src[holes]
    px[holes, 3] = 255
    im = Image.fromarray(px, "RGBA")
    im = im.crop(im.split()[3].getbbox())
    w, h = size
    im = im.resize((round(im.width * h / im.height), h), Image.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.alpha_composite(im.crop((0, 0, min(w, im.width), h)))
    canvas.save(out)
    return out


def expressions(host_id: str, names: list[str] | None = None, refit: bool = False) -> None:
    """以入库的闭嘴图为参考出表情整图。出图没有 seed:哪张不满意就单独重跑那一张。

    `refit`:不重新生图,只把 data/host/<id>/ 里已有的原图重新抠图入库(不花额度)。"""
    import io

    d = ASSET_DIR / host_id
    raw_dir = ROOT / "data" / "host" / host_id
    base = Image.open(d / "closed.png").convert("RGBA")
    flat = Image.new("RGB", base.size, (255, 255, 255))
    flat.paste(base, mask=base.split()[3])
    buf = io.BytesIO()
    flat.save(buf, "PNG")
    data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    for name in names or list(EXPRESSIONS):
        raw = raw_dir / f"{name}.png"
        if not refit:
            url = ark_image.generate_one(EXPRESSIONS[name], aspect_ratio="3:4", input_urls=[data])
            _download(url, raw)
        cut = _cutout(raw, raw_dir / f"cut_{name}.png", height=base.height)
        print(_fit(cut, raw, d / f"{name}.png", base.size))


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
    elif len(sys.argv) >= 3 and sys.argv[1] == "expressions":
        rest = [a for a in sys.argv[3:] if a != "--refit"]
        expressions(sys.argv[2], rest or None, refit="--refit" in sys.argv)
    elif len(sys.argv) >= 3 and sys.argv[1] == "compose":
        compose(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
