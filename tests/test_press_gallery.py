"""媒体中心图库(press):YAML 地址按型号匹配、Cloudinary 列表解析、车型页抽图、入池按尺寸筛。全部离线。"""
from __future__ import annotations

import json
from pathlib import Path

from app import config
from app.pipeline import library as lib_mod
from app.providers import clips, official, webassets
from app.providers.images import ImageRef

TESLA = {
    "aliases": ("特斯拉", "Tesla", "Model 3", "Model Y", "Cybertruck"),
    "sites": ("tesla.cn", "tesla.com"),
    "press": (
        "Model Y=https://digitalassets.tesla.com/tesla-contents/image/list/tesla-gallery-new-model-y.json",
        "Model Y L=https://digitalassets.tesla.com/tesla-contents/image/list/tesla-gallery-model-y-l.json",
    ),
}
XIAOMI = {
    "aliases": ("小米汽车", "小米SU7", "小米YU7"),
    "sites": ("xiaomiev.com",),
    "press": ("YU7=https://www.xiaomiev.com/yu7", "SU7=https://www.xiaomiev.com/su7"),
}
LIST_URL = "https://digitalassets.tesla.com/tesla-contents/image/list/tesla-gallery-new-model-y.json"


def test_press_entries_match_the_exact_model():
    assert official.press_entries("特斯拉Model Y", TESLA) == [TESLA["press"][0].split("=", 1)[1]]
    assert official.press_entries("特斯拉 Model Y L", TESLA) == [TESLA["press"][1].split("=", 1)[1]]
    assert official.press_entries("小米YU7", XIAOMI) == ["https://www.xiaomiev.com/yu7"]
    assert official.press_entries("特斯拉Model 3", TESLA) == []


def test_parse_cloudinary_list_keeps_large_originals():
    body = json.dumps({"resources": [
        {"public_id": "ModelY_36", "format": "jpg", "width": 4000, "height": 2250, "version": 1759443432},
        {"public_id": "thumb", "format": "jpg", "width": 800, "height": 450, "version": 1},
        {"format": "jpg", "width": 4000, "height": 2250},
    ]})
    got = official.parse_cloudinary_list(body, LIST_URL)
    assert got == [("https://digitalassets.tesla.com/tesla-contents/image/upload/"
                    "w_2560,q_auto:good/v1759443432/ModelY_36.jpg", 4000, 2250)]
    assert official.parse_cloudinary_list("not json", LIST_URL) == []


def test_press_images_from_product_page_drops_mobile_and_sibling_models_png_last(monkeypatch):
    page = """
      <img src="https://s1.xiaomiev.com/a/yu7_20260521/pc/5-3-1.png">
      <img src="https://s1.xiaomiev.com/a/yu7_20260521/pc/1.jpg?x-fds-process=image/resize,q_100">
      <img src="https://s1.xiaomiev.com/a/yu7_20260521/m/1.jpg">
      <img src="https://s1.xiaomiev.com/a/su7_20260107/pc/home.jpg">
      <img src="https://s1.xiaomiev.com/a/yu7_20260521/pc/13.jpg">
    """
    monkeypatch.setattr(clips, "fetch_page", lambda url: page)
    refs = official.press_images("小米YU7", XIAOMI)
    assert [r.url.split("/a/")[1] for r in refs] == [
        "yu7_20260521/pc/1.jpg?x-fds-process=image/resize,q_100", "yu7_20260521/pc/13.jpg",
        "yu7_20260521/pc/5-3-1.png"]   # png 多是图标,排到最后
    assert all(r.origin == "official:press" and r.license.startswith("厂商媒体用途") for r in refs)


def test_press_images_reports_a_broken_address(monkeypatch):
    monkeypatch.setattr(webassets, "_http_text", lambda url, **k: '{"resources": []}')
    errors: list[str] = []
    assert official.press_images("特斯拉Model Y", TESLA, errors=errors) == []
    assert len(errors) == 1 and "媒体中心" in errors[0]


def test_ingest_drops_press_images_below_min_size(tmp_path, monkeypatch):
    from PIL import Image

    from app.providers import images as images_mod

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    sizes = {"https://x/big.jpg": (2560, 1440), "https://x/icon.png": (32, 32)}

    def fake_download(url, dest, **k):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", sizes[url], (120, 120, 120)).save(dest, "JPEG")

    monkeypatch.setattr(images_mod, "download", fake_download)
    monkeypatch.setitem(official.GALLERIES, "product_images", official.Gallery(
        lambda subject, spec, errors: [ImageRef(url=u, origin="official:press") for u in sizes],
        min_long_side=1920))
    lib = lib_mod.Library(subject="好孩子口袋车", category="baby_long")
    assert lib_mod.ingest_gallery(lib, []) == 1
    assert [i.url for i in lib.items] == ["https://x/big.jpg"]
    assert len(list((lib.dir / "stills").iterdir())) == 1
