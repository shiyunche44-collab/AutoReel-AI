from app.pipeline import final_qa

SAMPLE = """
[blackdetect @ 0000] black_start:0 black_end:0.2 black_duration:0.2
[blackdetect @ 0000] black_start:12.5 black_end:14 black_duration:1.5
[freezedetect @ 0001] lavfi.freeze_start: 30.2
[freezedetect @ 0001] lavfi.freeze_duration: 5.1
[Parsed_ebur128_0 @ 0002] Summary:

  Integrated loudness:
    I:         -17.3 LUFS
    Threshold: -27.4 LUFS

  Loudness range:
    LRA:         5.0 LU

  True peak:
    Peak:       -0.2 dBFS
"""


def test_analyse_parses_all_measurements():
    r = final_qa.analyse(SAMPLE)
    assert r["lufs"] == -17.3
    assert r["true_peak"] == -0.2
    assert r["black"] == [(0.0, 0.2), (12.5, 1.5)]
    assert r["freeze"] == [(30.2, 5.1)]


def test_problems_flags_only_real_issues():
    msgs = final_qa.problems(final_qa.analyse(SAMPLE), -14)
    text = "\n".join(msgs)
    assert "-17.3 LUFS" in text and "真峰值" in text
    assert "1 处黑场" in text  # 0.2 秒的片头黑帧不算
    assert "画面静止" in text


def test_problems_clean_video_has_no_warning():
    clean = {"lufs": -14.2, "true_peak": -1.6, "black": [], "freeze": []}
    assert final_qa.problems(clean, -14) == []


def test_inspect_without_ffmpeg_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(final_qa.config, "FFMPEG_PATH", None)
    assert final_qa.inspect(tmp_path / "x.mp4") == []


def test_freeze_running_to_end_uses_total_duration():
    log = "[Parsed_freezedetect_0 @ 1] lavfi.freezedetect.freeze_start: 10.0"
    r = final_qa.analyse(log, total=16.0)
    assert r["freeze"] == [(10.0, 6.0)]
