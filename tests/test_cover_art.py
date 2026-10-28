"""统一风格封面:浅色影棚底图缓存 / 质检、真车抠图、生图兜底那台车、出图尺寸、标题拆主副行。全程不联网。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.library_patch import patch_in_package
from PIL import Image, ImageDraw

from app import config
from app.pipeline import cover_art, motion_assets
from app.providers import images


def _studio_car(path, bg=(250, 250, 250)):
    """白底上的"车":车身 + 驾驶舱 + 两个深色轮子。"""
    im = Image.new("RGB", (800, 450), bg)
    d = ImageDraw.Draw(im)
    d.rectangle((120, 220, 680, 320), fill=(150, 20, 40))
    d.rectangle((260, 150, 540, 220), fill=(120, 20, 35))
    for cx in (240, 560):
        d.ellipse((cx - 50, 270, cx + 50, 370), fill=(20, 20, 20))
    im.save(path)
    return Path(path)


@pytest.fixture
def art(tmp_path, monkeypatch):
    """打开开关、缓存指到临时目录,生图换成本地出一张浅色图,车图用一张假白底车。返回每次生图的提示词。"""
    monkeypatch.setattr(config, "COVER_ART", True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    calls = []

    def fake_generate(prompt, *, output_file="gen.png", aspect_ratio=None, **kw):
        calls.append(prompt)
        return images.ImageRef(url=f"fake://{aspect_ratio}", origin="generate")

    def fake_download(url, dest, **kw):
        w, h = {"16:9": (1280, 720), "9:16": (720, 1280), "3:4": (864, 1152)}[url[7:]]
        Image.new("RGB", (w, h), (200, 200, 196)).save(dest, format="JPEG")
        return Path(dest)

    monkeypatch.setattr(images, "generate", fake_generate)
    monkeypatch.setattr(images, "download", fake_download)
    monkeypatch.setattr(images, "describe", lambda urls, prompt, **kw: "NO")
    monkeypatch.setattr("app.providers.cli.upload_temp_url", lambda p, **kw: "data:x")
    car = _studio_car(tmp_path / "car.png")
    patch_in_package(monkeypatch, cover_art, "pick_car",
                        lambda subject, exclude=(): (motion_assets.cutout(car), car) if subject else None)
    return calls


def test_ratio_and_tone_index():
    assert cover_art.ratio_of((1920, 1080)) == "16x9"
    assert cover_art.ratio_of((1080, 1920)) == "9x16"
    assert cover_art.ratio_of((1080, 1440)) == "3x4"
    assert cover_art.tone_of("p1") == cover_art.tone_of("p1")
    assert cover_art.tone_of("p1", "b") != cover_art.tone_of("p1", "a")


def test_studio_bg_cached(art):
    tone = cover_art.TONES[0]
    first = cover_art.studio_bg(tone, "16x9")
    assert first and first.is_file()
    assert cover_art.studio_bg(tone, "16x9") == first
    assert len(art) == 1 and tone.prompt in art[0] and "no car" in art[0]


def test_generate_rejects_vlm_yes_and_too_dark(art, monkeypatch, tmp_path):
    monkeypatch.setattr(images, "describe", lambda urls, prompt, **kw: "YES")
    assert cover_art.generate("p", "16x9", tmp_path / "x.jpg") is None

    def dark(url, dest, **kw):
        Image.new("RGB", (640, 360), (20, 20, 20)).save(dest, format="JPEG")
        return Path(dest)

    monkeypatch.setattr(images, "download", dark)
    assert cover_art.generate("p", "16x9", tmp_path / "y.jpg") is None


def test_background_falls_back_to_gradient(art, monkeypatch):
    monkeypatch.setattr(images, "generate", lambda *a, **k: None)
    canvas, source = cover_art.background(cover_art.TONES[1], (1080, 1920))
    assert source == "gradient" and canvas.size == (1080, 1920)


@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920)])
@pytest.mark.parametrize("variant,hook", [("a", ""), ("b", "23.35万")])
def test_build_sizes(art, tmp_path, size, variant, hook):
    dest = tmp_path / f"cover_{variant}.jpg"
    got = cover_art.build("p1", dest, size=size, title="23万的YU7,买屏还是买电?", subject="小米YU7",
                          kicker="小米YU7 · 25.35万元起", variant=variant, hook=hook)
    assert got and Path(got["path"]) == dest and got["source"] in ("studio", "gradient")
    assert got["main"] == (hook or "23万的YU7")
    with Image.open(dest) as im:
        assert im.size == size
        # 车画在下半部:车身那一带不再是浅色底
        w, h = size
        x = int(w * (0.75 if w > h else 0.5))
        assert min(im.getpixel((x, y))[1] for y in range(int(h * 0.6), int(h * 0.86), 8)) < 120
    # 横竖项目都按 3:4 和 4:3 各重排一张(小红书 / 抖音网页版的两个封面位)
    with Image.open(dest.with_name(f"{dest.stem}_3x4{dest.suffix}")) as im:
        assert im.size == (1080, 1440)
    with Image.open(dest.with_name(f"{dest.stem}_4x3{dest.suffix}")) as im:
        assert im.size == (1440, 1080)


def test_build_none_without_car(art, tmp_path):
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t", subject="") is None


def test_build_disabled_by_switch(art, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COVER_ART", False)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t", subject="x") is None


def test_split_title():
    assert cover_art.split_title("23.35万起，买续航还是买加速？") == ("23.35万起", "买续航还是买加速？")
    assert cover_art.split_title("值不值得买？理想i9") == ("值不值得买？", "理想i9")
    assert cover_art.split_title("小米YU7这几个数字要看懂", "小米YU7") == ("小米YU7", "这几个数字要看懂")
    assert cover_art.split_title("这几个数字要看懂", "小米YU7") == ("小米YU7", "这几个数字要看懂")
    assert cover_art.short_name("小米YU7 · 25.35万元起") == "小米YU7"


def test_usable_hook_needs_digits():
    assert cover_art.usable_hook("23.35万") == "23.35万"
    assert cover_art.usable_hook("小米YU7版本跨") == ""  # 型号里的 7 不算数字
    assert cover_art.usable_hook("买的是补能") == ""


def test_b_title_switches_when_no_usable_hook():
    from app.pipeline import packaging as pk

    pkg = pk.assemble(None, "小米YU7", allowed=set(), price="23.35万元起")
    a = pkg.titles[0].text
    assert cover_art.b_title(pkg, a, "买的是补能") != a
    assert cover_art.b_title(None, a, "") == a


def test_title_break_without_punctuation():
    from app.pipeline.narration import _font_file

    lines, _ = cover_art._title_lines("小米YU7这几个数字要看懂", _font_file(bold=True), 912, 140)
    assert lines == ["小米YU7这几个", "数字要看懂"]


def test_model_word_and_safe_frame():
    assert cover_art.model_word("小米YU7") == "YU7"
    assert cover_art.model_word("理想i9") == "i9"
    assert cover_art.model_word("腾势N8L") == "N8L" and cover_art.model_word("海豚") == ""
    f = cover_art.Frame.of((1080, 1920))
    assert (f.top, f.height) == (240, 1440)  # 抖音主页宫格裁出的中间 3:4
    assert cover_art.Frame.of((1080, 1440)).top == 0


def test_scrub_plate_removes_third_party_logo():
    """车牌上印着别家标识(汽车之家这类)会被抹成空白车牌;没有字的空白车牌不动。"""
    im = Image.new("RGBA", (800, 400), (150, 20, 40, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((340, 260, 460, 300), fill=(240, 242, 244, 255))
    for x in range(360, 440, 6):  # 蓝色「汽车之家」:细笔画
        d.rectangle((x, 266, x + 2, 280), fill=(40, 80, 200, 255))
    assert cover_art.find_plates(im) == [(340, 260, 461, 301)]
    out = cover_art.scrub_plates(im)
    assert min(out.getpixel((x, 273))[0] for x in range(360, 440)) > 200
    assert out.getpixel((100, 100)) == (150, 20, 40, 255)  # 车身不动

    blank = Image.new("RGBA", (800, 400), (150, 20, 40, 255))
    ImageDraw.Draw(blank).rectangle((340, 260, 460, 300), fill=(240, 242, 244, 255))
    assert cover_art.find_plates(blank) == []


# --------------------------------------------------------------------------
# 生图兜底那台车(素材池里没有白底官图时)
# --------------------------------------------------------------------------
def _ai_env(monkeypatch, tmp_path, *, clean="NO", same="YES"):
    """把生图那条路的外部依赖全换成本地假货。返回调用计数,用来断言"只生一次"。"""
    car = _studio_car(tmp_path / "ai_car.png")
    calls: dict[str, object] = {"generate": 0, "prompt": "", "seed": 0}

    def fake_generate_many(prompt, *, n=1, seed=None, aspect_ratio=None,
                           output_file="gen.png", **kw):
        calls["generate"] = int(calls["generate"]) + 1
        calls["prompt"] = prompt
        calls["seed"] = seed
        calls["kw"] = kw
        return [images.ImageRef(url=f"fake://cand{i}", origin="generate") for i in range(n)]

    def fake_download(url, dest, **kw):
        shutil.copyfile(car, dest)
        return Path(dest)

    def fake_vlm(path, prompt):
        # 同款门问的是"两张图是不是同一台车",题干里有"two images"
        return same if "two images" in prompt else clean

    monkeypatch.setattr(images, "generate_many", fake_generate_many)
    monkeypatch.setattr(images, "download", fake_download)
    patch_in_package(monkeypatch, cover_art, "_vlm_answer", fake_vlm)
    patch_in_package(monkeypatch, cover_art, "pick_reference",
                        lambda subject, exclude=(): (car, "白色比亚迪唐右前45度白底定妆照"))
    patch_in_package(monkeypatch, cover_art, "_ask_ref_attributes",
                        lambda ref: {"model": "BYD Tang", "color": "silver", "body": "SUV"})
    # 生图模式固定成"只走文字生图":图生图那条路要 mcode-tools CLI,在场,
    # 跑测试的机器不一定有 —— 模式表由 cover_art._car_modes 决定,这里钉死它。
    patch_in_package(monkeypatch, cover_art, "_car_modes", lambda: ("t2i",))
    return calls


def test_ai_car_used_when_no_white_bg(art, tmp_path, monkeypatch):
    """素材池抠不出车 → 生图那台车顶上,出来的封面和老模板完全不是一个样。"""
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    dest = tmp_path / "cover_a.jpg"
    got = cover_art.build("p1", dest, size=(1920, 1080), title="23.99万起", subject="比亚迪大唐",
                          kicker="比亚迪大唐 · 23.99万元起")
    assert got and Path(got["path"]) == dest
    assert got["car_source"] == "ai_generated"
    assert got["car_reference"].endswith("ai_car.png")
    assert calls["generate"] == 1
    with Image.open(dest) as im:
        assert im.size == (1920, 1080)
        # 车画在下半部(和真车抠图那条路同一个位置)
        w, h = im.size
        x = int(w * 0.75)
        assert min(im.getpixel((x, y))[1] for y in range(int(h * 0.6), int(h * 0.86), 8)) < 120
    assert (tmp_path / "cover_a_3x4.jpg").is_file()
    assert (tmp_path / "cover_a_4x3.jpg").is_file()


def test_ai_car_rejected_when_model_differs(art, tmp_path, monkeypatch):
    """同款门判否 → 这一台不能用。四张全否就退回老封面(封面不能拦住成片)。"""
    calls = _ai_env(monkeypatch, tmp_path, same="NO")
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐") is None
    # 判否过一次就别再烧钱了
    assert cover_art.build("p1", tmp_path / "b.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐") is None
    assert calls["generate"] == 1


def test_ai_car_cached_not_regenerated(art, tmp_path, monkeypatch):
    """同一台车重出封面不再生图(缓存命中)。"""
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    first = cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                             subject="比亚迪大唐")
    second = cover_art.build("p1", tmp_path / "b.jpg", size=(1920, 1080), title="t",
                              subject="比亚迪大唐")
    assert first and second
    assert calls["generate"] == 1


def test_car_prompt_has_model_and_color():
    """提示词要带车型和颜色:写错颜色等于换了一台车。"""
    prompt = cover_art.car_prompt("比亚迪大唐", {"model": "BYD Tang", "color": "silver",
                                                  "body": "SUV"})
    assert "BYD Tang" in prompt and "比亚迪大唐" in prompt
    assert "silver" in prompt and "SUV" in prompt
    assert len(prompt) <= 1500
    assert "汽车之家" not in prompt and "AUTOHOME" not in prompt


def test_ref_attributes_fall_back_to_desc(art, monkeypatch):
    """视觉模型不可用时,用参照图 desc 里的中文颜色兜底。"""
    patch_in_package(monkeypatch, cover_art, "_ask_ref_attributes", lambda ref: {})
    attrs = cover_art.ref_attributes(Path("x.jpg"), "白色比亚迪唐右前45度白底定妆照")
    assert attrs["color"] == "white"


def test_official_cutout_wins_over_ai(art, tmp_path, monkeypatch):
    """有白底官图就抠图,不去生图(车型不会画错)。"""
    calls = _ai_env(monkeypatch, tmp_path)
    got = cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t", subject="小米YU7")
    assert got and got["car_source"] == "official_cutout"
    assert calls["generate"] == 0


def test_cover_ai_car_switch_off(art, tmp_path, monkeypatch):
    """COVER_AI_CAR=0 → 不生图,行为同今天(抠不到就退回老封面)。"""
    monkeypatch.setattr(config, "COVER_AI_CAR", False)
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐") is None
    assert calls["generate"] == 0


def test_ref_mode_uses_reference_image(art, tmp_path, monkeypatch):
    """图生图:把官方参照图传进生图接口,并且强制走 Connector 后端(直连不支持参考图)。"""
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    patch_in_package(monkeypatch, cover_art, "_car_modes", lambda: ("ref",))
    monkeypatch.setattr("app.providers.cli.connector_available", lambda: True)
    uploaded = {}

    def fake_upload(path, **kw):
        uploaded["path"] = str(path)
        uploaded["backend"] = kw.get("backend")
        return "https://example.invalid/ref.jpg"

    monkeypatch.setattr("app.providers.cli.upload_temp_url", fake_upload)
    got = cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                          subject="比亚迪大唐")
    assert got and got["car_source"] == "ai_generated"
    assert got["car_mode"] == "ref"
    # 参照图确实上传了,而且走的是 mcode(直连给的是 data URI,远端生图服务拿不到)
    assert uploaded["backend"] == "mcode"
    assert uploaded["path"].endswith("ai_car.png")
    # 生图请求带了参考图
    assert calls["kw"].get("input_urls") == ["https://example.invalid/ref.jpg"]
    assert calls["kw"].get("backend") == "mcode"


def test_car_facing_must_match_cover_direction(art, tmp_path, monkeypatch):
    """车头朝向不对(朝左)→ 淘汰。封面版式固定朝右,朝左的车并排放一眼就露馅。"""
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    real_cutout = motion_assets.cutout

    def left_facing(path, item_id=""):
        cut = real_cutout(path, item_id=item_id)
        if cut is None:
            return None
        return motion_assets.Cutout(**{**cut.geometry(), "facing": "left"})

    monkeypatch.setattr(motion_assets, "cutout", left_facing)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐") is None
    assert calls["generate"] == 1


def test_clean_gate_does_not_kill_factory_badges(art, tmp_path, monkeypatch):
    """G2 只拦硬伤(可读文字/水印/第二辆车/车顶加装),不拦原厂车标与轮毂徽章。

    问法写太宽("有没有 logo/badge")会把原厂轮毂中心盖的徽章算成 logo,
    干净候选全被误杀 —— 实测踩过,这里钉住:题干里不能出现"logo/badge"这类问法。"""
    prompt = cover_art.CAR_CLEAN_PROMPT.lower()
    for banned in ("logo", "badge", "emblem"):
        assert banned not in prompt, f"G2 问法里出现了 {banned}:会把原厂徽章误杀"
    assert "roof box" in prompt  # 车顶行李箱这类加装必须拦


def test_clean_gate_catches_text_and_roof_accessories(art, tmp_path, monkeypatch):
    """有可读文字 / 车顶行李箱的候选判否。"""
    calls = _ai_env(monkeypatch, tmp_path, clean="YES")
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐") is None
    assert calls["generate"] == 1


def test_gates_version_invalidates_stale_cache(art, tmp_path, monkeypatch):
    """改了质检门判据,旧缓存里的"全否"结论不能继续挡住 —— 得重跑。"""
    calls = _ai_env(monkeypatch, tmp_path)
    patch_in_package(monkeypatch, cover_art, "pick_car", lambda subject, exclude=(): None)
    assert cover_art.build("p1", tmp_path / "a.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐")
    # 把缓存里的门版本改成旧版 → 下次必须重新生图
    for meta_path in (tmp_path / "_cache" / "cover_theme" / "cars").rglob("meta.json"):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["gates_version"] = cover_art.GATES_VERSION - 1
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    calls["generate"] = 0
    assert cover_art.build("p1", tmp_path / "b.jpg", size=(1920, 1080), title="t",
                           subject="比亚迪大唐")
    assert calls["generate"] == 1


def test_compare_pair_flips_ref_only_when_ref_faces_left(tmp_path):
    """比对图要把两车摆成同朝向:参照图朝左才镜像,朝右就保持原样。"""
    from PIL import Image

    d = tmp_path
    flat = d / "flat.jpg"
    ref = d / "ref.jpg"
    # 左图:深色块在左边;右图:深色块在右边
    Image.new("RGB", (100, 50), (255, 255, 255)).save(flat)
    Image.new("RGB", (100, 50), (255, 255, 255)).save(ref)
    a = Image.open(flat).convert("RGB")
    a.paste((0, 0, 0), (0, 0, 50, 50))
    a.save(flat)
    b = Image.open(ref).convert("RGB")
    b.paste((0, 0, 0), (50, 0, 100, 50))
    b.save(ref)

    out = cover_art._compare_pair(flat, ref, d / "cmp_r.jpg", ref_facing="right")
    with Image.open(out) as im:
        w = im.width
        fw = (w - 12) // 2                      # 每半张的宽(中间留 12px 缝)
        assert im.getpixel((int(fw * 0.25), 300))[0] < 128          # 生成图:左深
        assert im.getpixel((int(fw * 1.75), 300))[0] < 128          # 参照图:右深(没翻)
        assert im.getpixel((int(fw * 1.25), 300))[0] > 200          # 参照图左侧是白的

    out = cover_art._compare_pair(flat, ref, d / "cmp_l.jpg", ref_facing="left")
    with Image.open(out) as im:
        w = im.width
        fw = (w - 12) // 2
        assert im.getpixel((int(fw * 0.25), 300))[0] < 128
        assert im.getpixel((int(fw * 1.25), 300))[0] < 128          # 参照图翻成左深
        assert im.getpixel((int(fw * 1.75), 300))[0] > 200

