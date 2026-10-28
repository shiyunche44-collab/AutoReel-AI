"""抖音网页版发布测试。

**红线:这里绝不开浏览器、绝不发真稿。** 浏览器会话整个换成假的。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.library_patch import patch_in_package
from PIL import Image

from app.publish import browser
from app.publish.base import Account, PublishError, PublishRequest
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
        self.page = type("P", (), {"wait_for_timeout": lambda self, ms: None, "url": ""})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def goto(self, url, **kw):
        FakeSession.calls.append(("goto", url))

    def exists(self, selector, **kw):
        return selector in ("input[type=file]", "input[placeholder*='标题']",
                            "button:has-text('发布')", "button:text-is('发布')")

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


# --------------------------------------------------------------------------
# 自主声明:AI 生成内容必须挂标识
# --------------------------------------------------------------------------
class FakeDeclarePage:
    """模拟发布页的「自主声明」下拉。

    结构照 2026-10 实跑看到的:标签「自主声明」右边的控件里有个 selectText,
    点开是 radio(label 包着文字),弹层底部有「取消 / 确定」。

    `has_row`:标签右边的控件结构还在不在(结构定位靠它)。
    `has_placeholder`:下拉上是不是还写着「请选择自主声明」(文案定位靠它)。
    """

    def __init__(self, *, options=("内容由AI生成", "内容为个人观点或见解",
                                   "内容为转载信息", "无需添加自主声明"),
                 selected="", has_row=True, has_placeholder=True):
        self.options = list(options)
        self.selected = selected
        self.has_row = has_row
        self.has_placeholder = has_placeholder
        self.clicks: list = []
        self.confirmed = False

    # -- Playwright 面的 API --------------------------------------------
    def locator(self, selector, has_text=""):
        return FakeDeclareLocator(self, selector, has_text)

    def get_by_text(self, text, exact=False):
        return FakeDeclareLocator(self, f"text:{text}", "")

    def wait_for_timeout(self, ms):
        pass


class FakeDeclareLocator:
    def __init__(self, page, selector, has_text):
        self.page = page
        self.selector = selector
        self.has_text = has_text

    @property
    def first(self):
        return self

    def locator(self, selector, has_text=""):
        return FakeDeclareLocator(self.page, selector, has_text)

    def count(self):
        s, p = self.selector, self.page
        if s == douyin._DECLARE_ROW:
            return 1 if p.has_row else 0
        if s == "[class*='selectText']":
            return 1 if p.has_row else 0
        if s == "label":
            return 1 if self.has_text in p.options else 0
        if s == "span.semi-button-content":
            return 1 if self.has_text == "确定" else 0
        if s.startswith("text:"):
            want = s[5:]
            if want == "请选择自主声明":
                return 1 if p.has_placeholder else 0
            return 1 if want in p.options or want == p.selected else 0
        return 0

    def inner_text(self):
        return self.page.selected

    def click(self, **kw):
        p = self.page
        p.clicks.append((self.selector, self.has_text))
        if self.selector == "[class*='selectText']":
            return
        if self.selector == "label":
            p.selected = self.has_text
            return
        if self.selector == "span.semi-button-content":
            p.confirmed = True
            return
        if self.selector.startswith("text:"):
            p.selected = self.selector[5:]
            return


class FakeDeclareSession:
    def __init__(self, page):
        self.page = page


def test_set_declaration_picks_the_ai_option():
    """AI 生成内容要挂上「内容由AI生成」—— 平台社区规则:未标识会被限流。"""
    page = FakeDeclarePage()
    note = douyin.set_declaration(FakeDeclareSession(page))
    assert note == ""
    assert page.selected == douyin.DECLARE_AI
    assert page.confirmed  # 选完得点「确定」才生效
    assert ("label", douyin.DECLARE_AI) in page.clicks


def test_set_declaration_skips_when_already_declared():
    page = FakeDeclarePage(selected=douyin.DECLARE_AI)
    assert douyin.set_declaration(FakeDeclareSession(page)) == ""
    assert page.clicks == []  # 已经声明过就别再点一遍


def test_set_declaration_reports_missing_option():
    """平台改了文案 —— 说清楚,别当成设上了。"""
    page = FakeDeclarePage(options=("无需添加自主声明",))
    note = douyin.set_declaration(FakeDeclareSession(page))
    assert "没有" in note and douyin.DECLARE_AI in note


def test_set_declaration_never_raises():
    """页面改版 / 浏览器抽风 —— 都不能拦住发布。"""
    class Broken:
        def locator(self, *a, **k):
            raise RuntimeError("页面改版")

        def get_by_text(self, *a, **k):
            raise RuntimeError("页面改版")

        def wait_for_timeout(self, ms):
            raise RuntimeError("页面改版")

    class Session:
        page = Broken()

    note = douyin.set_declaration(Session())
    assert note and "声明" in note


def test_set_declaration_reports_missing_row():
    """结构定位和文案定位都找不到 → 说清楚,别当成设上了。"""
    page = FakeDeclarePage(has_row=False, has_placeholder=False)
    note = douyin.set_declaration(FakeDeclareSession(page))
    assert "没找到" in note


def test_set_declaration_falls_back_to_placeholder_text():
    """结构变了(标签右边的控件挪了),但「请选择自主声明」还在 → 还能勾上。"""
    page = FakeDeclarePage(has_row=False, has_placeholder=True)
    note = douyin.set_declaration(FakeDeclareSession(page))
    assert note == ""
    assert page.selected == douyin.DECLARE_AI


def test_publish_browser_declares_ai_before_description(tmp_path, monkeypatch):
    """顺序要紧:填简介会弹「#添加话题」浮层,盖住下面「自主声明」那一行的下拉。"""
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")
    FakeSession.calls = []
    order: list = []
    monkeypatch.setattr(browser, "BrowserSession", FakeSession)
    patch_in_package(monkeypatch, douyin, "resolve_published_work", lambda *a, **k: {})
    patch_in_package(monkeypatch, douyin, "set_covers", lambda session, path: "")
    patch_in_package(monkeypatch, douyin, "set_declaration",
                        lambda session, kind: order.append(("declare", kind)) or "")
    patch_in_package(monkeypatch, douyin, "set_description",
                        lambda session, desc, hashtags="": order.append(("desc",)) or "")
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         title="标题")
    result = DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                                  auth_kind="browser"))
    assert result.ok
    assert order == [("declare", douyin.DECLARE_AI), ("desc",)]


def test_publish_browser_can_skip_the_ai_declaration(tmp_path, monkeypatch):
    """真发了非 AI 的片子,单次请求能关掉声明。"""
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")
    FakeSession.calls = []
    called: list = []
    monkeypatch.setattr(browser, "BrowserSession", FakeSession)
    patch_in_package(monkeypatch, douyin, "resolve_published_work", lambda *a, **k: {})
    patch_in_package(monkeypatch, douyin, "set_covers", lambda session, path: "")
    patch_in_package(monkeypatch, douyin, "set_description", lambda session, desc, hashtags="": "")
    patch_in_package(monkeypatch, douyin, "set_declaration",
                        lambda session, kind: called.append(kind) or "")
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         title="标题", extra={"declare_ai": False})
    DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                          auth_kind="browser"))
    assert called == []


def test_publish_browser_sets_cover_before_clicking_publish(tmp_path, monkeypatch):
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")
    cover = _img(tmp_path / "cover.jpg", (1920, 1080))
    FakeSession.calls = []
    monkeypatch.setattr(browser, "BrowserSession", FakeSession)
    patch_in_package(monkeypatch, douyin, "resolve_published_work", lambda *a, **k: {})
    patch_in_package(monkeypatch, douyin, "set_covers",
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
    # 左栏「作品发布」也含"发布"两字,必须按文字精确点真正的发布按钮
    assert FakeSession.calls[-1] == ("click", "button:text-is('发布')")


def test_publish_browser_fills_in_the_real_work_id(tmp_path, monkeypatch):
    """网页版发布拿不到作品 id —— 要回查作品管理页补上,不然发布记录没有链接。"""
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")
    FakeSession.calls = []
    monkeypatch.setattr(browser, "BrowserSession", FakeSession)
    patch_in_package(monkeypatch, douyin, "resolve_published_work",
                        lambda *a, **k: {"aweme_id": "7700", "url": "https://www.douyin.com/video/7700"})
    patch_in_package(monkeypatch, douyin, "set_covers", lambda session, path: "")
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         title="标题")
    result = DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                                  auth_kind="browser"))
    assert result.ok and result.remote_id == "7700"
    assert result.remote_url == "https://www.douyin.com/video/7700"
    assert "7700" in result.message


def test_publish_browser_reports_expired_login(tmp_path, monkeypatch):
    """落到扫码登录页 = 登录失效,要明说,别报成"页面改版"。"""
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")

    class LoggedOut(FakeSession):
        def exists(self, selector, **kw):
            return False

        def page_text(self):
            return "抖音创作者中心 扫码登录 验证码登录"

    monkeypatch.setattr(browser, "BrowserSession", LoggedOut)
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         title="标题")
    with pytest.raises(PublishError) as err:
        DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                             auth_kind="browser"))
    assert "登录已失效" in err.value.message and err.value.credential


def test_publish_browser_reports_sms_verification(tmp_path, monkeypatch):
    """点发布后弹短信验证码 = 要本人操作,不能当成"没成功提示"去重试。"""
    video = tmp_path / "output.mp4"
    video.write_bytes(b"x")

    class Sms(FakeSession):
        def page_text(self):
            return "接收短信验证码 请输入验证码"

    monkeypatch.setattr(browser, "BrowserSession", Sms)
    patch_in_package(monkeypatch, douyin, "set_covers", lambda session, path: "")
    req = PublishRequest(project_id="p", account_id="a", platform="douyin", video_path=video,
                         title="标题")
    with pytest.raises(PublishError) as err:
        DouyinAdapter().publish(req, Account(id="a", platform="douyin", name="抖",
                                             auth_kind="browser"))
    assert "短信验证" in err.value.message and not err.value.retryable


def test_douyin_uses_fixed_profile_but_explicit_storage_does_not():
    assert browser.BrowserSession("douyin").uses_profile
    assert not browser.BrowserSession("douyin", storage="x.json").uses_profile
    assert not browser.BrowserSession("xiaohongshu").uses_profile


def test_stable_user_agent_is_the_headed_chrome_one(tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    (tmp_path / "154.0.7000.1").mkdir()
    (tmp_path / "155.0.8059.86").mkdir()
    (tmp_path / "Locales").mkdir()
    ua = browser.stable_user_agent(exe)
    assert "Chrome/155.0.0.0 " in ua and "Headless" not in ua
    assert browser.stable_user_agent(None) == ""
    assert browser.stable_user_agent(tmp_path / "nowhere" / "chrome.exe") == ""
