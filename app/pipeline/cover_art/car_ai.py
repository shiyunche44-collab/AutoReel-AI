"""封面里的那台车:池子里挑白底官图,没有就生图并过质检门。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from ... import config

from .tone import log
from .reference import CAR_CLEAN_PROMPT, CAR_MODES, CAR_REF_PROMPT, CAR_SAME_PROMPT, COVER_CAR_FACING, GATES_VERSION, _cars_root, _hash, _read_json, _write_json, car_prompt, pick_reference, ref_attributes


# --------------------------------------------------------------------------
# 车
# --------------------------------------------------------------------------
#: 封面车图的视角偏好:前 45° 最像"这台车",正尾最差
VIEW_RANK = {"exterior_45": 0, "exterior_front": 1, "exterior_side": 2, "rear": 4}


def pick_car(subject: str, exclude: Sequence[str | Path] = ()) -> tuple[Any, Path] | None:
    """挑一张能抠干净的整车官图:白底棚拍优先、官方优先、大图优先。返回 (Cutout, 原图)。"""
    from .. import library, motion_assets

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
    # 没有白底官图:用抠图模型从实拍图里抠(车是真车,不是 AI 画的)
    for item in cands[:10]:
        c = motion_assets.cutout(item.path, item_id=item.id, ml=True)
        if c is not None:
            return c, Path(item.path)
    return None


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
    from ...providers import cli, images

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
    from ...providers import cli

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
    from ...providers import cli, images

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
    from ...providers import images
    from .. import library, motion_assets

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
