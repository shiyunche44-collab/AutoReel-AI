"""剪辑 v2 渲染:嵌入字体的配置。"""
from __future__ import annotations

from app.pipeline.edit import edl
from app.pipeline.edit import render as er


def test_font_file_env(tmp_path, monkeypatch):
    monkeypatch.delenv("EDIT_V2_FONT_FILE", raising=False)
    assert er.font_file() is None
    ttf = tmp_path / "Heavy.ttf"
    ttf.write_bytes(b"x")
    monkeypatch.setenv("EDIT_V2_FONT_FILE", f'"{ttf}"')
    assert er.font_file() == ttf
    ttc = tmp_path / "msyh.ttc"
    ttc.write_bytes(b"x")
    monkeypatch.setenv("EDIT_V2_FONT_FILE", str(ttc))
    assert er.font_file() is None  # 浏览器认不了 ttc
    monkeypatch.setenv("EDIT_V2_FONT_FILE", str(tmp_path / "missing.otf"))
    assert er.font_file() is None


def test_stage_media_embeds_font(tmp_path, monkeypatch):
    ttf = tmp_path / "Heavy.otf"
    ttf.write_bytes(b"font")
    monkeypatch.setenv("EDIT_V2_FONT_FILE", str(ttf))
    tl = edl.Timeline(width=1920, height=1080, fps=30, duration=1.0)
    props = er.stage_media(tl, tmp_path / "pub")
    assert props["font"] == "editfont.otf"
    assert (tmp_path / "pub" / "editfont.otf").read_bytes() == b"font"


def test_source_digest_changes_with_component(tmp_path, monkeypatch):
    from app.pipeline import caption_layer

    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "Edit.jsx"
    f.write_text("a")
    monkeypatch.setattr(caption_layer, "REMOTION_DIR", tmp_path)
    before = er.source_digest()
    f.write_text("b")
    assert er.source_digest() != before
