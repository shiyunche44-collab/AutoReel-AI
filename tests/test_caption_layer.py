from app.pipeline import caption_layer as cl
from app.pipeline import looks


def test_split_cue_short_pieces():
    parts = cl.split_cue("零百加速仅3.23秒,续航835公里,售价25.35万起")
    assert parts == ["零百加速仅3.23秒", "续航835公里", "售价25.35万起"]  # 小数点不断句
    assert all(len(p) <= cl.MAX_CHARS for p in parts)


def test_split_cue_long_piece_halves_without_orphan():
    parts = cl.split_cue("这是一句没有任何标点但是特别特别长的口播文字")
    assert len(parts) > 1
    assert all(2 < len(p) <= cl.MAX_CHARS for p in parts)
    assert "".join(parts) == "这是一句没有任何标点但是特别特别长的口播文字"


def test_mark_words_hot_numbers_and_codes():
    words = cl.mark_words("小米YU7售价25.35万起")
    assert [w["text"] for w in words if w["hot"]] == ["YU7", "25.35万"]
    assert "".join(w["text"] for w in words) == "小米YU7售价25.35万起"
    assert words[0]["at"] == 0


def test_build_layer_timing_covers_cue():
    cues = [{"start": 1.0, "end": 4.0, "text": "零百加速仅3.23秒,续航835公里"}]
    layer = cl.build_layer(cues, (1920, 1080), 24, (255, 90, 31))
    shown = layer["cues"]
    assert len(shown) == 2
    assert shown[0]["start"] == 1.0 and abs(shown[-1]["end"] - 4.0) < 0.01
    assert shown[0]["end"] == shown[1]["start"]
    assert layer["style"]["accent"] == "#FF5A1F"
    assert all("at" not in w for c in shown for w in c["words"])


def test_build_layer_skips_empty_and_inverted():
    cues = [{"start": 1.0, "end": 1.0, "text": "x"}, {"start": 2.0, "end": 3.0, "text": ""}]
    assert cl.build_layer(cues, (1920, 1080), 24, (0, 0, 0))["cues"] == []


def test_render_layer_without_cues_is_none(tmp_path):
    assert cl.render_layer({"cues": []}, tmp_path) is None


def test_render_layer_unavailable_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTION_LAYER", "0")
    assert cl.render_layer({"cues": [{"start": 0, "end": 1, "words": []}]}, tmp_path) is None


def test_no_look_uses_layer_by_default():
    assert looks.get("lite").sub_layer is False
    assert not any(lk.sub_layer for lk in looks.LOOKS.values())
