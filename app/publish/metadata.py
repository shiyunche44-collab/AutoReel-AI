"""文案生成:把项目内容变成各平台能直接用的标题 / 简介 / 标签。

**全是纯函数** —— 输入一个 `ContentContext`,输出各平台的文案,不做任何网络
调用、不读数据库。这样长度裁剪、话题格式、免责声明追加都能单测。

为什么值得单独抽一层:各平台对标题长度、话题数量、正文格式的要求各不相同,
写散在 adapter 里的话,改一个平台的规则要翻五个文件。这里是**唯一的规则入口**
(平台规则本身在 `registry.py`)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .registry import PlatformRule, rule_for

#: 标题切分时算"安全边界"的字符:空格与中英文标点。
#: 切在这些位置前面,不会把一个词劈成两半。
_BOUNDARY = " \t　,。.!！?？;；:：、|·—-/／()（）「」《》\"'“”‘’"

#: 品类专属的通用标签。品类不在表里时用 `_GENERIC_TAGS`。
_CATEGORY_TAGS: dict[str, tuple[str, ...]] = {
    "auto_long": ("汽车解说", "购车指南", "新车评测", "新能源车", "汽车"),
    "baby_long": ("母婴好物", "育儿好物", "选购指南", "母婴", "好物推荐"),
}
_GENERIC_TAGS: tuple[str, ...] = ("产品解说", "选购指南", "深度解析")

#: 简介里最多放几段脚本。全放进去就变成长文,没人看完。
_DESC_SEGMENTS = 3


@dataclass(frozen=True)
class ContentContext:
    """文案生成的输入。全部来自项目已有产物,不重新跑 LLM。"""

    subject: str = ""
    title: str = ""
    thesis: str = ""
    #: 脚本段落文本(按顺序)
    segments: list[str] = field(default_factory=list)
    #: 事实主题标签,用来派生关键词
    topics: list[str] = field(default_factory=list)
    #: 品类要求的免责声明
    disclaimers: list[str] = field(default_factory=list)
    #: 素材来源说明(成片时写的)
    source_note: str = ""
    category: str = ""
    subject_label: str = "产品"


def clamp_title(text: str, limit: int, soft: int = 0) -> str:
    """按长度裁标题,**不切在词中间**。

    `soft` 是软上限(超过会被平台折叠但不报错),`limit` 是硬上限(超过直接拒绝)。
    给了 soft 就按 soft 裁 —— 留着反正看不见,不如裁掉。

    裁的时候往回退到最近的空格 / 标点;退不回太多(会砍掉一半)就硬切。
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    target = min(soft or limit, limit)
    if len(text) <= target:
        return text
    cut = text[: max(1, target - 1)]
    best = -1
    for idx, ch in enumerate(cut):
        if ch in _BOUNDARY:
            best = idx
    if best >= max(4, int(target * 0.5)):
        cut = cut[:best]
    return cut.rstrip(" ,.。!！?？、|·—-/／") + "…"


def build_tags(ctx: ContentContext, limit: int) -> list[str]:
    """派生标签。顺序即重要度,超限就截。

    来源:主体名 → 事实主题 → 品类通用词。
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        tag = re.sub(r"[\s,，、#]+", "", str(value or "")).strip()
        if not tag or tag in seen or len(tag) > 20:
            return
        seen.add(tag)
        out.append(tag)

    _add(ctx.subject)
    for topic in ctx.topics:
        _add(topic)
    for tag in _CATEGORY_TAGS.get(ctx.category, _GENERIC_TAGS):
        _add(tag)
    if ctx.subject_label and ctx.subject_label != "产品":
        _add(ctx.subject_label)
    return out[:max(0, limit)]


def build_description(ctx: ContentContext, rule: PlatformRule) -> str:
    """简介 / 正文:开头几段 + 免责声明 + 素材说明。

    **不放话题标签** —— 话题是平台自己的字段(B站 tag、YouTube tags),
    抖音/小红书由 adapter 拼到正文里,这里保持干净,免得出现两遍。
    """
    parts: list[str] = []
    hook = [s.strip() for s in ctx.segments[:_DESC_SEGMENTS] if s and s.strip()]
    if hook:
        parts.append("\n".join(hook))
    if ctx.thesis:
        parts.append(ctx.thesis)
    for text in ctx.disclaimers:
        if text:
            parts.append(text)
    if ctx.source_note:
        parts.append(ctx.source_note)
    body = "\n\n".join(p for p in parts if p).strip()
    if len(body) > rule.desc_max:
        body = clamp_title(body, rule.desc_max)
    return body


def hashtags(tags: list[str], limit: int = 5) -> str:
    """抖音 / 小红书的话题串:`#标签1 #标签2`。空标签不产生裸 `#`。"""
    out = []
    for tag in tags[:limit]:
        tag = re.sub(r"[\s#]+", "", tag)
        if tag:
            out.append(f"#{tag}")
    return " ".join(out)


def metadata_for(platform: str, ctx: ContentContext) -> dict[str, Any]:
    """生成一个平台的完整文案。"""
    rule = rule_for(platform)
    title = clamp_title(ctx.title or ctx.subject, rule.title_max, rule.title_soft)
    tags = build_tags(ctx, rule.tag_max)
    description = build_description(ctx, rule)
    return {
        "platform": platform,
        "title": title,
        "description": description,
        "tags": tags,
        "hashtags": hashtags(tags),
        "rule": rule.as_dict(),
    }


def metadata_for_all(ctx: ContentContext, platforms: list[str]) -> dict[str, dict[str, Any]]:
    """一次生成多个平台的文案(UI 里的逐平台预览)。"""
    return {p: metadata_for(p, ctx) for p in platforms if p in _known()}


def _known() -> set[str]:
    from .registry import PLATFORMS

    return set(PLATFORMS)
