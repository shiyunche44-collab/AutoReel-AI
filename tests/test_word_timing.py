from app.pipeline import caption_layer, word_timing


def test_char_times_aligns_exact_words():
    words = [("续航", 0.0, 0.4), ("六百", 0.5, 0.9), ("公里", 0.9, 1.3)]
    t = word_timing.char_times("续航六百公里", words, 1.4)
    assert t == [0.0, 0.2, 0.5, 0.7, 0.9, 1.1]


def test_char_times_interpolates_digits_and_punctuation():
    words = [("续航", 0.0, 0.4), ("六百四十三", 0.5, 1.0), ("公里", 1.0, 1.4)]
    t = word_timing.char_times("续航643公里,", words, 1.5)
    assert len(t) == len("续航643公里,")
    assert t == sorted(t) and t[0] == 0.0 and t[2] >= 0.4 and t[5] == 1.0
    assert t[-1] == t[-2] or t[-1] <= 1.5


def test_char_times_rejects_poor_match():
    assert word_timing.char_times("续航六百公里", [("你好吗", 0, 1)], 1.0) is None
    assert word_timing.char_times("续航", [], 1.0) is None


def test_build_layer_uses_measured_times():
    text = "起步很快,续航六百公里"
    # 前半句慢(念到第 5 个字才 1.5 秒),后半句快
    ct = [0.0, 0.5, 1.0, 1.5, 1.8, 1.8, 2.0, 2.2, 2.4, 2.6, 2.8]
    cue = {"start": 10.0, "end": 13.0, "text": text, "char_t": ct}
    layer = caption_layer.build_layer([cue], (1080, 1920), 30, (255, 200, 0))
    first, second = layer["cues"]
    assert first["start"] == 10.0 and second["start"] == 11.8
    assert first["end"] == second["start"]
    assert second["end"] == 13.0


def test_build_layer_ignores_stale_char_t():
    cue = {"start": 0.0, "end": 2.0, "text": "续航六百公里", "char_t": [0.0, 1.0]}
    layer = caption_layer.build_layer([cue], (1080, 1920), 30, (255, 200, 0))
    assert layer["cues"][0]["start"] == 0.0 and layer["cues"][0]["end"] == 2.0
