from app.pipeline import teardown


def test_cut_density_buckets():
    d = teardown.cut_density([1.0, 2.0, 11.0, 25.0], 30.0)
    assert [b["cuts"] for b in d] == [2, 1, 1, 0]


def test_speech_stats_rate_and_coverage():
    segs = [{"start": 0, "end": 2, "text": "你好,世界"}, {"start": 3, "end": 5, "text": "再见"}]
    s = teardown.speech_stats(segs, 10.0)
    assert s["chars"] == 6
    assert s["spoken_seconds"] == 4.0
    assert s["coverage"] == 0.4
    assert s["chars_per_second"] == 1.5


def test_render_report_writes_html(tmp_path):
    (tmp_path / "opening").mkdir()
    m = {"probe": {"duration": 20.0}, "region": [0, 0, 960, 540], "cuts": [5.0, 12.0],
         "cut_density": teardown.cut_density([5.0, 12.0], 20.0),
         "transcript": [{"start": 1.0, "end": 3.0, "text": "开场"}],
         "speech": {"chars_per_second": 4.0, "coverage": 0.5}, "loudness": {}, "subtitle": {},
         "chapters": [{"t": 0, "title": "引言"}, {"t": 10, "title": "外观"}], "sheets": []}
    p = teardown.render_report(tmp_path, m)
    text = p.read_text(encoding="utf-8")
    assert "引言" in text and "外观" in text and "开场" in text
