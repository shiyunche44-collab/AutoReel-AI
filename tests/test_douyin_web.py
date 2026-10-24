"""抖音网页版发布测试。

**红线:这里绝不开浏览器、绝不发真稿。** 浏览器会话整个换成假的。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.publish import browser
from app.publish.base import Account, PublishRequest
from app.publish.platforms import douyin
from app.publish.platforms.douyin import DouyinAdapter, cover_files, set_covers


def _img(path: Path, size: tuple[int, int]) -> Path:
    Image.new("RGB", size, (200, 200, 200)).save(path, format="JPEG")
    return path


def test_cover_files_prefers_relaid_ratios(tmp_path):
    cover = _img(tmp_path / "cover.jpg", (1920, 1080))
    four_three = _img(tmp_path / "cover_4x3.jpg", (1440, 1080))
    three_four = _img(tmp_path / "cover_3x4.jpg", (1080, 1440))
    assert cover_files(cover) == [("横封面4:3", "设置横封面", four_three),
                                  ("竖封面3:4", "设置竖封面", three_four)]


def test_cover_files_falls_back_only_when_orientation_matches(tmp_path):
    wide = _img(tmp_path / "cover.jpg", (1920, 1080))
    assert cover_files(wide) == [("横封面4:3", "设置横封面", wide)]
    tall = _img(tmp_path / "cover_vertical.jpg", (1080, 1920))
    assert cover_files(tall) == [("竖封面3:4", "设置竖封面", tall)]
    assert cover_files(None) == []
    assert cover_files(tmp_path / "missing.jpg") == []


def test_set_covers_never_raises(tmp_path):
    class BrokenPage:
        def eval_on_selector_all(self, *a, **k):
            raise RuntimeError("页面改版")

        def locator(self, *a, **k):
            raise RuntimeError("页面改版")

    class Session:
        page = BrokenPage()

    assert set_covers(Session(), None) == "没有封面文件,用平台自动截帧"
    note = set_covers(Session(), _img(tmp_path / "cover.jpg", (1920, 1080)))
    assert note.startswith("封面没设上")


class FakeSession:
    """假浏览器会话:记下调用,页面文本里带「重新上传」和「发布成功」。"""

    calls: list = []

    def __init__(self, key, **kw):
        self.page = type("P", (), {"wait_for_timeout": lambda self, ms: None})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def goto(self, url, **kw):
        FakeSession.calls.append(("goto", url))

    def exists(self, selector, **kw):
        return selector in ("input[type=file]", "input[placeholder*='标题']",
                            "button:has-text('发布')")

    def upload_file(self, selector, path, **kw):
        FakeSession.calls.append(("upload", Path(path).name))

    def wait_text(self, text, **kw):
        return True

    def fill(self, selector, value, **kw):
        FakeSession.calls.append(("fill", value))

    def click(self, selector, **kw):
        FakeSession.calls.append(("click", selector))

    def save_state(self):
        return Path(".")

    def page_text(self):
        return "发布成功"


def test_publish_browser_sets_cover_before_clicking_publish(tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")
    cover = _img(tmp_path / "cover.jpg", (1920, 1080))
    FakeSession.calls = []
    monkeypatch.setattr(browser, "BrowserSession", FakeSession)
    monkeypatch.setattr(douyin, "set_covers",
                        lambda session, path: FakeSession.calls.append(("cover", path.name))
                        or "封面可能没换上:竖封面3:4")
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         cover_path=cover, title="一" * 40)
    result = DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                                  auth_kind="browser"))
    assert result.ok and result.state == "submitted"
    assert "封面可能没换上" in result.message
    names = [c[0] for c in FakeSession.calls]
    assert names == ["goto", "upload", "fill", "cover", "click"]
    assert FakeSession.calls[2] == ("fill", "一" * douyin.TITLE_MAX)
