"""平台注册表:平台规则 + adapter 解析。

**平台规则集中在这一个文件里。** 各平台对标题长度、话题数量、封面尺寸、发布
频率、导流词的规则都写在别处,必然过时 —— 集中在这里,改平台规则只改一个地方。

adapter 用**延迟导入**:平台模块会 import 浏览器 / 网络栈,不该在只查规则的
路径上被拖进来。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 画幅常量
LANDSCAPE = "16:9"
PORTRAIT = "9:16"


@dataclass(frozen=True)
class PlatformRule:
    """一个平台的发布规则。"""

    key: str
    label: str
    #: 首选画幅。16:9 = 横屏生态,9:16 = 竖屏生态。
    aspect: str
    #: 标题硬上限(超过平台会直接拒绝)
    title_max: int
    #: 标题软上限(超过会被折叠,虽然不报错但观众看不到后半句)
    title_soft: int
    #: 简介 / 正文上限
    desc_max: int
    #: 标签数量上限
    tag_max: int
    #: 是否必须写简介
    desc_required: bool = False
    #: 平台特定说明,UI 上提示用户
    note: str = ""
    #: 接入方式说明
    auth_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "aspect": self.aspect,
            "title_max": self.title_max, "title_soft": self.title_soft,
            "desc_max": self.desc_max, "tag_max": self.tag_max,
            "desc_required": self.desc_required, "note": self.note,
            "auth_hint": self.auth_hint,
        }


#: 平台规则表。改平台规则只改这里。
PLATFORMS: dict[str, PlatformRule] = {
    "bilibili": PlatformRule(
        key="bilibili", label="B 站", aspect=LANDSCAPE,
        title_max=80, title_soft=40, desc_max=2000, tag_max=10,
        desc_required=True, note="创作中心 Web 投稿,免资质,需要 SESSDATA + bili_jct。",
        auth_hint="在浏览器 F12 复制 SESSDATA 与 bili_jct,粘贴到账号凭据里。",
    ),
    "douyin": PlatformRule(
        key="douyin", label="抖音", aspect=PORTRAIT,
        title_max=1000, title_soft=55, desc_max=1000, tag_max=5,
        note="开放平台发布(需 video.create 能力),凭据不足时走创作者平台网页版。",
        auth_hint="填 client_key / client_secret 与授权得到的 access_token、open_id。",
    ),
    "youtube": PlatformRule(
        key="youtube", label="YouTube", aspect=LANDSCAPE,
        title_max=100, title_soft=70, desc_max=5000, tag_max=15,
        desc_required=True, note="Data API v3,一次上传消耗 1600 单位配额。",
        auth_hint="填 Google OAuth 的 client id / secret 与 refresh token。",
    ),
    "xiaohongshu": PlatformRule(
        key="xiaohongshu", label="小红书", aspect=PORTRAIT,
        title_max=20, title_soft=20, desc_max=1000, tag_max=10,
        desc_required=True, note="无开放平台 API,走浏览器自动化(需先在浏览器扫码登录)。",
        auth_hint="填创作后台的登录 Cookie,或先在分发台点「登录」用浏览器扫码。",
    ),
    "channels": PlatformRule(
        key="channels", label="视频号", aspect=PORTRAIT,
        title_max=22, title_soft=22, desc_max=1000, tag_max=10,
        desc_required=True, note="无开放平台 API,走视频号助手浏览器自动化。",
        auth_hint="填视频号助手的登录 Cookie,或先在分发台点「登录」用浏览器扫码。",
    ),
}

#: 平台 key → adapter 模块路径(延迟导入)
_ADAPTER_PATHS: dict[str, str] = {
    "bilibili": "app.publish.platforms.bilibili:BilibiliAdapter",
    "douyin": "app.publish.platforms.douyin:DouyinAdapter",
    "youtube": "app.publish.platforms.youtube:YoutubeAdapter",
    "xiaohongshu": "app.publish.platforms.xiaohongshu:XiaohongshuAdapter",
    "channels": "app.publish.platforms.channels:ChannelsAdapter",
}


def rule_for(platform: str) -> PlatformRule:
    """取平台规则。未知平台抛 KeyError —— 宁可炸,也不要默默按 B 站规则发出去。"""
    return PLATFORMS[platform]


def adapter_for(platform: str):
    """取平台 adapter 实例。延迟导入,避免把浏览器 / 网络栈拖进只查规则的路径。"""
    import importlib

    path = _ADAPTER_PATHS.get(platform)
    if not path:
        raise KeyError(f"没有这个平台:{platform}")
    module_name, class_name = path.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def platforms_info() -> list[dict[str, Any]]:
    """平台注册表,给 UI 用。"""
    out = []
    for rule in PLATFORMS.values():
        item = rule.as_dict()
        item["auth_kind"] = getattr(adapter_for(rule.key), "auth_kind", "cookie")
        out.append(item)
    return out
