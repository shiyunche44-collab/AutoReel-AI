"""统一风格封面:浅色影棚底图缓存 / 质检、真车抠图、出图尺寸、标题拆主副行。全程不联网。"""
from __future__ import annotations

from pathlib import Path

import pytest
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
    monkeypatch.setattr(cover_art, "pick_car",
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
    if size[1] > size[0]:
        with Image.open(dest.with_name(f"{dest.stem}_3x4{dest.suffix}")) as im:
            assert im.size == (1080, 1440)


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
