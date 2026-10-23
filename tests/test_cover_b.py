"""封面 B(大字版):版式、尺寸、底图排除、文件名。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app import config
from app.pipeline import aspect, library, narration
from app.pipeline import packaging as pk


def _pattern(path: Path, kind: str) -> Path:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (640, 360), (200, 200, 200))
    d = ImageDraw.Draw(im)
    if kind == "grad":
        for x in range(640):
            d.line([(x, 0), (x, 360)], fill=(x // 3, x // 3, 255 - x // 3))
    elif kind == "stripes":
        for x in range(0, 640, 40):
            d.rectangle([x, 0, x + 19, 360], fill=(10, 10, 10))
    else:
        for y in range(0, 360, 60):
            for x in range(0, 640, 60):
                if (x // 60 + y // 60) % 2:
                    d.rectangle([x, y, x + 59, y + 59], fill=(20, 90, 200))
    im.save(path, "JPEG")
    return path


def _still(tmp_path):
    from PIL import Image

    p = tmp_path / "still.jpg"
    Image.new("RGB", (1000, 600), (60, 70, 90)).save(p)
    return p


def test_b_names_and_keep_list():
    from app import maintenance

    assert aspect.cover_name_for(config.LANDSCAPE) == "cover.jpg"
    assert aspect.cover_name_for(config.LANDSCAPE, "b") == "cover_b.jpg"
    assert aspect.cover_name_for(config.PORTRAIT, "b") == "cover_vertical_b.jpg"
    b3 = narration.cover_3x4_path(Path(aspect.cover_name_for(config.PORTRAIT, "b")))
    assert b3.name == "cover_vertical_b_3x4.jpg"
    for name in ("cover_b.jpg", "cover_vertical_b.jpg", "cover_vertical_b_3x4.jpg"):
        assert name in maintenance.PROJECT_KEEP, name


def test_title_layout_ignores_hook_when_empty(tmp_path):
    still = _still(tmp_path)
    a = narration.make_cover(tmp_path / "n.mp4", "标题测试", tmp_path / "a.jpg",
                             subtitle="副", size=(1920, 1080), still=still, kicker="小米YU7")
    b = narration.make_cover(tmp_path / "n.mp4", "标题测试", tmp_path / "b.jpg",
                             subtitle="副", size=(1920, 1080), still=still, kicker="小米YU7",
                             layout="hook", hook="")
    assert a and b and a.read_bytes() == b.read_bytes()


def test_hook_layout_differs_in_landscape(tmp_path):
    from PIL import Image

    still = _still(tmp_path)
    a = narration.make_cover(tmp_path / "n.mp4", "标题测试", tmp_path / "a.jpg",
                             subtitle="副", size=(1920, 1080), still=still, kicker="小米YU7")
    b = narration.make_cover(tmp_path / "n.mp4", "标题测试", tmp_path / "b.jpg",
                             subtitle="副", size=(1920, 1080), still=still, kicker="小米YU7",
                             layout="hook", hook="23.35万")
    assert Image.open(b).size == (1920, 1080)
    assert a.read_bytes() != b.read_bytes()
    assert not list(tmp_path.glob("*.part*"))


def test_hook_layout_portrait_and_3x4(tmp_path):
    from PIL import Image

    still = _still(tmp_path)
    dest = tmp_path / "cover_vertical_b.jpg"
    out = narration.make_cover(tmp_path / "n.mp4", "标题测试", dest, subtitle="副",
                               size=(1080, 1920), still=still, kicker="小米YU7",
                               layout="hook", hook="15分钟")
    assert out == dest and Image.open(dest).size == (1080, 1920)
    assert Image.open(narration.cover_3x4_path(dest)).size == (1080, 1440)
    plain = tmp_path / "cover_vertical.jpg"
    narration.make_cover(tmp_path / "n.mp4", "标题测试", plain, subtitle="副",
                         size=(1080, 1920), still=still, kicker="小米YU7")
    assert plain.read_bytes() != dest.read_bytes()


def test_hook_text_stays_inside_safe_zone(tmp_path):
    """大字不能压到右侧平台按钮的安全区(右 12%)。"""
    still = _still(tmp_path)
    dest = tmp_path / "cover_vertical_b.jpg"
    narration.make_cover(tmp_path / "n.mp4", "标题", dest, size=(1080, 1920), still=still,
                         layout="hook", hook="买的是补能速度")
    g = aspect.portrait_grid((1080, 1920))
    font = narration._hook_font("买的是补能速度", narration._font_file(True), g.right - g.left,
                                narration.COVER_HOOK_MAX_PORT)
    if narration._font_file(True):
        assert font.getlength("买的是补能速度") <= g.right - g.left


# --------------------------------------------------------------------------
# cover_still(exclude)
# --------------------------------------------------------------------------
def _fake_library(monkeypatch, tmp_path, kinds):
    items = []
    for n, kind in enumerate(kinds):
        p = _pattern(tmp_path / f"s{n}.jpg", kind)
        items.append(SimpleNamespace(id=f"i{n}", kind="still", usable=True, path=str(p), problem="",
                                     view="front", target="yes", frame=""))
    lib = SimpleNamespace(items=items)
    monkeypatch.setattr(library.Library, "load", classmethod(lambda cls, subject: lib))
    monkeypatch.setattr(library, "vocab_of", lambda l: SimpleNamespace(primary_views={"front"}, views={"front"}))
    monkeypatch.setattr(library, "is_studio_shot", lambda path: False)
    return items


def test_cover_still_exclude_skips_used_image(monkeypatch, tmp_path):
    items = _fake_library(monkeypatch, tmp_path, ["grad", "stripes"])
    first = library.cover_still("x")
    second = library.cover_still("x", exclude=[first])
    assert first and second and first != second
    assert library.cover_still("x", exclude=[first, second]) is None


def test_cover_still_exclude_skips_near_duplicates(monkeypatch, tmp_path):
    from PIL import Image

    items = _fake_library(monkeypatch, tmp_path, ["grad", "stripes"])
    dup = tmp_path / "dup.jpg"
    Image.open(items[0].path).resize((320, 180)).save(dup, "JPEG")  # 同一张图的缩小版
    items.append(SimpleNamespace(id="i9", kind="still", usable=True, path=str(dup), problem="",
                                 view="front", target="yes", frame=""))
    got = library.cover_still("x", exclude=[items[0].path])
    assert got is not None and Path(got) == Path(items[1].path)


def test_cover_still_without_exclude_unchanged(monkeypatch, tmp_path):
    _fake_library(monkeypatch, tmp_path, ["grad", "stripes", "checker"])
    assert library.cover_still("x") == library.cover_still("x", exclude=())


# --------------------------------------------------------------------------
# 大字来源
# --------------------------------------------------------------------------
def _t(text, kind):
    return pk.TitleOption(text=text, kind=kind, note="")


def test_hook_prefers_number_title_then_price_then_thesis():
    titles = [_t("买屏还是买电", "stake"), _t("YU7补能15分钟,够用吗", "number")]
    assert pk.cover_hook(titles, price="23.35万元起") == "15分钟"
    assert pk.cover_hook([_t("无数字标题", "number")], price="23.35万元起") == "23.35万元"
    assert pk.cover_hook([], thesis="买的是补能速度,不是屏幕") == "买的是补能速度"


def test_hook_ignores_model_code_digits():
    assert pk.cover_hook([_t("YU7够不够用", "number")]) == ""


def test_hook_empty_when_nothing_usable():
    assert pk.cover_hook([], price="", thesis="") == ""
    assert pk.cover_hook([], thesis="最强") == ""
    assert pk.cover_hook([], thesis="全网最强纯电SUV") == ""


# --------------------------------------------------------------------------
# compose 里的 B 版
# --------------------------------------------------------------------------
def test_make_cover_b_in_compose(monkeypatch, tmp_path):
    from app import db
    from app.pipeline import explainer
    from app.pipeline import render as render_mod

    pkg = pk.assemble(None, "小米YU7", allowed=set(), price="23.35万元起")
    monkeypatch.setattr(pk, "load", lambda pid: pkg)
    monkeypatch.setattr(db, "read_json", lambda *a, **k: [])
    monkeypatch.setattr(db, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(config, "project_dir", lambda pid: tmp_path)
    seen = {}

    def fake_build(video, title, dest, **kw):
        seen.update(kw, dest=Path(dest), title=title)
        return Path(dest)

    monkeypatch.setattr(render_mod, "build_cover", fake_build)
    second = tmp_path / "b.jpg"
    monkeypatch.setattr(library, "cover_still", lambda subject, exclude=(): None)
    project = {"subject": "小米YU7", "thesis": "买的是补能速度,不是屏幕", "aspect": config.PORTRAIT}
    got = explainer._make_cover_b("p1", project, "标题", {"kicker": "k", "subtitle": "s"},
                                  (1080, 1920), "v.mp4", tmp_path / "a.jpg")
    assert got and got["same_still"] is True and got["hook"]
    assert seen["layout"] == "hook" and seen["dest"].name == "cover_vertical_b.jpg"
    monkeypatch.setattr(library, "cover_still", lambda subject, exclude=(): second)
    got = explainer._make_cover_b("p1", project, "标题", {"kicker": "k", "subtitle": "s"},
                                  (1080, 1920), "v.mp4", tmp_path / "a.jpg")
    assert got["same_still"] is False and got["still"] == str(second)


def test_make_cover_b_skipped_without_hook(monkeypatch, tmp_path):
    from app import db
    from app.pipeline import explainer

    monkeypatch.setattr(pk, "load", lambda pid: None)
    monkeypatch.setattr(db, "read_json", lambda *a, **k: [])
    assert explainer._make_cover_b("p1", {"subject": "x", "thesis": ""}, "t", {}, (1920, 1080),
                                   "v.mp4", None) is None


def test_hook_skips_count_numbers_like_deliveries():
    titles = [_t("23.2万台交付,YU7已经证明自己", "number")]
    assert pk.cover_hook(titles, price="23.35万元起") == "23.35万元"  # 不拿销量当价格大字
    titles = [_t("23.2万台交付,15万差价值吗", "number")]
    assert pk.cover_hook(titles) == "15万"


def test_hook_title_matches_hook_source():
    titles = [_t("买屏还是买电", "stake"), _t("15万差价,换1003匹值不值?", "number")]
    assert pk.hook_title(titles, "15万") == "15万差价,换1003匹值不值?"
    assert pk.hook_title(titles, "") == ""
    assert pk.hook_title(titles, "99万") == ""
