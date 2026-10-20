"""直连后端:请求组装、响应解析、分流。不发真实请求,不消耗额度。"""
from __future__ import annotations

import base64
import io

import pytest

from app import config
from app.providers import audio, cli, direct, facts, images


@pytest.fixture
def calls(monkeypatch):
    """把 direct.request 换成录音机;`replies` 按 path 前缀给出响应。"""
    class Log(list):
        replies: dict[str, object]

    log = Log()
    replies: dict[str, object] = {}

    def fake(method, path, *, tool, json_body=None, params=None, timeout=120, retries=2):
        log.append({"method": method, "path": path, "json": json_body, "params": params})
        for prefix, reply in replies.items():
            if path.startswith(prefix):
                if isinstance(reply, Exception):
                    raise reply
                return reply(json_body) if callable(reply) else reply
        raise AssertionError(f"未预期的请求 {path}")

    monkeypatch.setattr(direct, "request", fake)
    monkeypatch.setattr(config, "CONNECTOR_BACKEND", "direct")
    monkeypatch.setattr(config, "MINIMAX_PLAN_KEY", "sk-cp-test")
    log.replies = replies
    return log


def _png(w=40, h=30) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 音频 / 图片
def test_tts_keeps_positions_when_one_item_fails(calls, monkeypatch, tmp_path):
    def reply(body):
        if body["text"] == "bad":
            raise cli.ConnectorError("t2a", "boom")
        return {"data": {"audio": f"https://x/{body['text']}.mp3"}}

    calls.replies["/v1/t2a_v2"] = reply
    fetched: list[tuple[str, str]] = []
    monkeypatch.setattr(audio, "_download", lambda url, dest: fetched.append((url, dest.name)))
    assets = audio.synthesize_batch(["ok", "bad", "ok"], output_dir=tmp_path)
    assert [a.local_path.name for a in assets] == ["tts_000.mp3", "tts_002.mp3"]
    assert calls[0]["json"]["output_format"] == "url"
    assert calls[0]["json"]["model"] == config.TTS_MODEL


def test_tts_all_failed_raises(calls):
    calls.replies["/v1/t2a_v2"] = cli.ConnectorError("t2a", "quota")
    with pytest.raises(cli.ConnectorError):
        direct.tts_batch({"requests": [{"text": "a", "voice_id": "v"}]}, 10)


def test_generate_image_returns_url_node(calls):
    calls.replies["/v1/image_generation"] = {"data": {"image_urls": ["https://x/i.jpg"]},
                                             "base_resp": {"status_code": 0}}
    ref = images.generate("a red car", aspect_ratio="9:16")
    assert ref is not None and ref.url == "https://x/i.jpg"


def test_voice_list(calls):
    calls.replies["/v1/get_voice"] = {"system_voice": [{"voice_id": "v1"}],
                                      "base_resp": {"status_code": 0}}
    assert audio.list_voices() == [{"voice_id": "v1"}]


# ---------------------------------------------------------------- 理解 / 检索
def test_upload_temp_url_makes_data_uri_for_images(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_BACKEND", "direct")
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


def test_describe_images_one_call_per_image(calls):
    calls.replies["/v1/coding_plan/vlm"] = lambda body: {"content": "一辆比亚迪汉",
                                                         "base_resp": {"status_code": 0}}
    uri = "data:image/jpeg;base64," + base64.b64encode(_png()).decode()
    text = images.describe([uri, uri], "这是什么车")
    assert "图片 1" in text and "图片 2" in text
    assert len(calls) == 2 and calls[0]["json"]["image_url"] == uri


def test_search_news_uses_web_search_and_labels_source(calls, tmp_path):
    calls.replies["/v1/coding_plan/search"] = {
        "organic": [{"title": "汉 EV 2026 上市", "snippet": "CLTC 续航 702 公里",
                     "date": "2026-09-01", "link": "https://x/a", "extra": "drop"}],
        "base_resp": {"status_code": 0}}
    doc = facts.ifind_product_news("比亚迪汉 EV", project_dir=tmp_path)
    assert doc.ok and "702" in doc.text
    assert doc.tool == direct.WEB_SEARCH_LABEL
    assert "drop" not in doc.text


def test_unsupported_source_fails_softly(calls):
    doc = facts.qcc_profile("比亚迪股份有限公司")
    assert not doc.ok and "直连模式不支持" in doc.error


def test_error_message_shapes():
    assert direct._error_message({"type": "error", "error": {"message": "bad (2013)"}}) == "bad (2013)"
    assert direct._error_message({"base_resp": {"status_code": 1004, "status_msg": "auth"}}) \
        == "1004: auth"
    assert direct._error_message({"task": {"status": "failed", "error": {"message": "x"}}}) is None
    assert direct._error_message({"task_id": "1"}) is None


def test_backend_defaults(monkeypatch):
    monkeypatch.setattr(config, "CONNECTOR_BACKEND", "mcode")
    assert not cli.is_direct()
    assert cli.source_label("connector__thsifind__search_news") == "connector__thsifind__search_news"
