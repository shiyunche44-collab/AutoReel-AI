"""方舟直连后端:分流、占位对齐、结果形状。不发真实请求,不消耗额度。"""
from __future__ import annotations

import base64
import io

import pytest

from app.providers import ark_image, ark_search, ark_tts, audio, cli, direct, facts, images


@pytest.fixture
def ark(monkeypatch):
    """把方舟的四个出口换成录音机;`replies` 给每个出口的响应(异常实例会被抛出)。"""
    class Log(list):
        replies: dict[str, object]

    log = Log()
    log.replies = {}

    def reply(name, *args):
        log.append((name, args))
        r = log.replies.get(name)
        if isinstance(r, Exception):
            raise r
        return r(*args) if callable(r) else r

    monkeypatch.setattr(direct, "_ark_tts_item", lambda req, timeout: reply("tts", req))
    monkeypatch.setattr(direct, "_ark_vlm", lambda url, prompt, timeout: reply("vlm", url, prompt))
    monkeypatch.setattr(ark_image, "generate", lambda prompt, **kw: reply("image", prompt, kw))
    monkeypatch.setattr(ark_search, "search", lambda query, **kw: reply("search", query, kw))
    return log


def _png(w=40, h=30) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 音频 / 图片
def test_tts_keeps_positions_when_one_item_fails(ark, monkeypatch, tmp_path):
    def reply(req):
        if req["text"] == "bad":
            raise cli.ConnectorError("ark_tts", "boom")
        return f"url:file:///x/{req['text']}.mp3"

    ark.replies["tts"] = reply
    monkeypatch.setattr(audio, "_download", lambda url, dest: None)
    assets = audio.synthesize_batch(["ok", "bad", "ok"], output_dir=tmp_path)
    assert [a.local_path.name for a in assets] == ["tts_000.mp3", "tts_002.mp3"]
    assert [a.index for a in assets] == [0, 2]


def test_tts_all_failed_raises(ark):
    ark.replies["tts"] = cli.ConnectorError("ark_tts", "quota")
    with pytest.raises(cli.ConnectorError):
        direct.tts_batch({"requests": [{"text": "a", "voice_id": "v"}]}, 10)


def test_generate_image_returns_url_node(ark):
    ark.replies["image"] = ["https://x/i.jpg"]
    ref = images.generate("a red car", aspect_ratio="9:16")
    assert ref is not None and ref.url == "https://x/i.jpg"
    assert ark[0][1][1]["aspect_ratio"] == "9:16"


def test_voice_list():
    assert audio.list_voices() == ark_tts.VOICES


# ---------------------------------------------------------------- 理解 / 检索
def test_upload_temp_url_makes_data_uri_for_images(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png())
    uri = cli.upload_temp_url(p)
    assert uri.startswith("data:image/jpeg;base64,")
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as im:
        assert min(im.size) >= 256  # 视频接口的下限
    mp4 = tmp_path / "v.mp4"
    mp4.write_bytes(b"x")
    assert cli.upload_temp_url(mp4).startswith("file:")


def test_local_path_handles_windows_drive():
    assert str(direct._local_path("file:///F:/a%20b/v.mp4")).replace("\\", "/") == "F:/a b/v.mp4"


def test_describe_images_one_call_per_image(ark):
    ark.replies["vlm"] = "一辆比亚迪汉"
    uri = "data:image/jpeg;base64," + base64.b64encode(_png()).decode()
    text = images.describe([uri, uri], "这是什么车")
    assert "图片 1" in text and "图片 2" in text
    assert len(ark) == 2 and ark[0][1][0] == uri


def test_search_news_uses_doubao_search_and_labels_source(ark, tmp_path):
    ark.replies["search"] = [{"title": "汉 EV 2026 上市", "summary": "CLTC 续航 702 公里,"
                              "800V 平台,五分钟补能 400 公里。", "publish_time": "2026-09-01"}]
    doc = facts.ifind_product_news("比亚迪汉 EV", project_dir=tmp_path)
    assert doc.ok and "702" in doc.text
    assert doc.tool == direct.ARK_SEARCH_LABEL


def test_unsupported_source_fails_softly():
    doc = facts.qcc_profile("比亚迪股份有限公司")
    assert not doc.ok and "没有该能力" in doc.error


def test_source_label_only_renames_search():
    assert cli.source_label("connector__thsifind__search_news") == direct.ARK_SEARCH_LABEL
    assert cli.source_label("connector__matrix__describe_images") == "connector__matrix__describe_images"
