"""成片落盘要原子。

直接让 ffmpeg 写 `output.mp4` 的话,进程在编码中途被杀(README:托管后台
任务有 1 小时上限)会留下**半截文件** —— 而 `has_output` 只看"文件在不
在",会把一个打不开的坏片当成成片报出去。

所以:ffmpeg 先写 `output.mp4.part`,解码校验过了再改名。改名是原子的,
读者要么看到完整的成片,要么看不到。

同理,封面也不能是半张 —— 它是决定点击率的那一帧,看着像有、其实打不开
比没有更糟。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import config
from app.pipeline import narration, render


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    yield tmp_path
    from app import db
    db._INITIALISED = False


def _fake_ffmpeg(monkeypatch, *, ok: bool = True):
    """把 ffmpeg 换成假的:把输出参数那个路径写成占位文件。"""
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=1800, cwd=None):
        args = list(args)
        calls.append(args)
        if not ok:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(render, "_run", fake_run)
    monkeypatch.setattr(render.timeline_mod, "probe_safe", lambda *a, **k: 12.0)
    monkeypatch.setattr(render, "_decodes_clean", lambda p: True)
    return calls


def _shot(tmp_path: Path) -> Path:
    p = tmp_path / "shot.mp4"
    p.write_bytes(b"fake")
    return p


def test_compose_renames_the_temp_file_into_place(store, monkeypatch):
    """ffmpeg 写的是 .part,校验过了才改名成 output.mp4。"""
    tmp_path = store
    calls = _fake_ffmpeg(monkeypatch)
    shot = _shot(tmp_path)

    result = render.compose("p_atomic", [shot], size=(1920, 1080), durations=[12.0])

    assert result.ok
    out = config.project_dir("p_atomic") / "output.mp4"
    assert out.is_file(), "成片应该在正式位置"
    # 没有任何一次 ffmpeg 调用是直接写 output.mp4 的
    for args in calls:
        if Path(args[-1]).name == "mix.wav":
            continue  # 单独混音那一步写的是中间文件
        assert Path(args[-1]).name != "output.mp4", "ffmpeg 不该直接写正式成片"
        # 临时文件必须保留扩展名 —— ffmpeg 靠它推断封装格式
        assert Path(args[-1]).suffix in (".mp4", ".mov", ".mkv"), \
            f"ffmpeg 的输出文件名必须带可识别的扩展名:{args[-1]}"
    # 临时文件已经被改名走,不该留下
    assert not list(config.project_dir("p_atomic").glob("*.part.*"))


def test_failed_compose_leaves_no_output_and_no_temp(store, monkeypatch):
    """合成失败:既不留正式成片,也不留半截的临时文件。"""
    _fake_ffmpeg(monkeypatch, ok=False)
    shot = _shot(store)

    result = render.compose("p_atomic", [shot], size=(1920, 1080), durations=[12.0])

    assert result.ok is False
    project = config.project_dir("p_atomic")
    assert not (project / "output.mp4").exists(), "失败的合成不该产出成片"
    assert not list(project.glob("*.part.*")), "不该留下半截文件"


def test_has_output_is_false_for_a_partial_file(store, monkeypatch):
    """半截文件不能被当成成片 —— has_output 只看 output.mp4 在不在,
    所以 output.mp4 必须只在校验通过后才出现。"""
    _fake_ffmpeg(monkeypatch, ok=False)
    shot = _shot(store)
    render.compose("p_atomic", [shot], size=(1920, 1080), durations=[12.0])
    assert not (config.project_dir("p_atomic") / "output.mp4").is_file()


# --------------------------------------------------------------------------
# 既没旁白也没 BGM
# --------------------------------------------------------------------------
def test_audio_graph_without_narration_and_bgm_is_valid():
    """TTS 全失败 + BGM 也失败时,音频滤镜不能引用不存在的标签。

    `[bgmraw]` 只在有 BGM 时才建,这时引用它 ffmpeg 会直接报
    "Invalid stream specifier";而 loudnorm 作用在纯静音上会算出 NaN,
    aac 编码器随后报 "Input contains (near) NaN/+-Inf"。
    两种情况都会让成片**出不来** —— 而代码注释写的是"允许它产出来"。
    """
    filt, label = render._build_audio_graph(False, False, 0.0)
    assert "[bgmraw]" not in filt, "bgmraw 这个标签这时根本不存在"
    assert "loudnorm" not in filt, "loudnorm 作用在纯静音上会产出 NaN"
    assert filt, "仍然要有一路静音音轨 —— 成片得是能播的文件"
    assert label == "[aout]"


def test_audio_graph_with_bgm_only_uses_bgmraw():
    """只有 BGM 时仍然走 [bgmraw] 那条路(别把两种情况搞混)。"""
    filt, label = render._build_audio_graph(False, True, 0.0)
    assert "[bgmraw]" in filt
    assert label == "[aout]"


def test_audio_graph_with_narration_only():
    filt, label = render._build_audio_graph(True, False, 0.0)
    assert "[narr0]" in filt
    assert "[bgmraw]" not in filt
    assert label == "[aout]"


# --------------------------------------------------------------------------
# 旁白断音(2026-10-23):混音必须单独一条命令,出片前数音频包
# --------------------------------------------------------------------------
def test_narration_is_mixed_separately_from_video(store, monkeypatch):
    """视频与 loudnorm 同一条 ffmpeg 会成段丢音频帧,所以先混出 mix.wav,
    主命令只封装它,里面不能再有任何音频滤镜。"""
    calls = _fake_ffmpeg(monkeypatch)
    monkeypatch.setattr(render, "_audio_intact", lambda p, s: None)
    nar = store / "narration.wav"
    nar.write_bytes(b"fake")
    bgm = store / "bgm.mp3"
    bgm.write_bytes(b"fake")

    result = render.compose("p_atomic", [_shot(store)], narration_path=nar, bgm_path=bgm,
                            size=(1920, 1080), durations=[12.0])

    assert result.ok
    mix_calls = [a for a in calls if Path(a[-1]).name == "mix.wav"]
    assert len(mix_calls) == 1
    mix = mix_calls[0]
    assert "-f" not in mix[:mix.index("-filter_complex")], "混音命令不该带视频输入"
    assert "[0:a]" in mix[mix.index("-filter_complex") + 1]
    main = [a for a in calls if Path(a[-1]).suffix == ".mp4" and "concat" in a][-1]
    graph = main[main.index("-filter_complex") + 1]
    assert "loudnorm" not in graph and "amix" not in graph
    assert main[main.index("-map", main.index("[vout]")) + 1] == "1:a"


def test_compose_retries_when_audio_track_has_holes(store, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch)
    seen: list[int] = []

    def holey(path, seconds):
        seen.append(1)
        return "成片音轨有 3 处断档"

    monkeypatch.setattr(render, "_audio_intact", holey)
    nar = store / "narration.wav"
    nar.write_bytes(b"fake")
    result = render.compose("p_atomic", [_shot(store)], narration_path=nar,
                            size=(1920, 1080), durations=[12.0])
    assert len(seen) == 2, "不合格要重合一次再验"
    assert any("断档" in w for w in result.warnings)
    assert len([a for a in calls if "concat" in a]) == 2


def test_audio_problem_finds_timestamp_holes():
    ok = "\n".join(f"{i * 0.021333:.6f},0.021333" for i in range(470))
    assert render.audio_problem(ok, 10.0) is None
    holey = "0.000000,0.021333\n0.021333,0.021333\n3.500000,0.021333\n"
    msg = render.audio_problem(holey, 3.5)
    assert msg and "断档" in msg
    assert "只有" in render.audio_problem("0.0,0.5\n0.5,0.5\n", 10.0)
    assert render.audio_problem("", 10.0) == "成片没有音轨"


@pytest.mark.skipif(not (config.FFMPEG_PATH and config.FFMPEG_PATH.is_file()), reason="需要 ffmpeg")
def test_real_compose_keeps_every_audio_frame(store, monkeypatch):
    """真 ffmpeg:两段视频 + 10 秒旁白,成片音轨一帧不能少。"""
    monkeypatch.setattr(render, "_decodes_clean", lambda p: True)
    shots = []
    for i in range(2):
        s = store / f"s{i}.mp4"
        render._run(["-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24",
                     "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "5",
                     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(s)])
        shots.append(s)
    nar = store / "nar.wav"
    render._run(["-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                 "-t", "10", str(nar)])
    result = render.compose("p_real", shots, narration_path=nar, size=(320, 180),
                            durations=[5.0, 5.0])
    assert result.ok, result.warnings
    assert render._audio_intact(result.output_path, 10.0) is None
    assert not any("断档" in w or "只有" in w for w in result.warnings or [])


# --------------------------------------------------------------------------
# 封面
# --------------------------------------------------------------------------
def test_cover_is_written_atomically(store, monkeypatch):
    """封面也是先写 .part 再改名。"""
    from PIL import Image

    src = store / "still.jpg"
    Image.new("RGB", (640, 360), (200, 200, 200)).save(src)
    dest = store / "cover.jpg"

    # still=给一张干净的底图(没有底图时它会去视频里抽帧,这里没有视频)
    out = narration.make_cover(src, "标题", dest, size=(1920, 1080), still=src)
    assert out is not None and out.is_file()
    assert not list(store.glob("*.part.*"))
