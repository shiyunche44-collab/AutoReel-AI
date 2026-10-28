"""lint 的基础件:禁用词、数据型陈述判断、LintIssue。"""
from __future__ import annotations

import logging
import re
from ...categories import CategoryProfile
from ..grounding import arabicize

from .scrub import _BENIGN_FIRST_RE


log = logging.getLogger(__name__)

#: 阿拉伯数字/百分号/单位 —— 出现即视为"数据型陈述",必须有事实引用
_NUMERIC_RE = re.compile(r"\d")

#: 年款型号(如 "2026 款"、"2024 款")不是性能参数,不要求事实引用。
#: 这类数字若也拦截,lint 会产生大量噪声,使用者会习惯性忽略它 ——
#: 那才是真正危险的:一个总在喊狼来了的告警等于没有告警。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")

#: 型号代号(YU7、SU7、X5、V6s)里的数字是名字不是参数。只认"1–4 个字母 + 1–2 位数字",
#: 这样 "CLTC643""752V""700TOPS" 这类真参数不会被误剥
_MODEL_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{1,2}(?![\d.])[A-Za-z]*")


#: 广告法绝对化用语:所有品类共享的红线
AD_ABSOLUTE_TERMS = ("最强", "第一", "顶级", "永久", "唯一", "100%")


def forbidden_in(text: str, profile: CategoryProfile) -> list[str]:
    """文本命中的禁用词(品类禁用词 + 广告法绝对化用语)。"第一次""第一眼"这类不算。"""
    plain = _BENIGN_FIRST_RE.sub("", text or "")
    terms = dict.fromkeys((*profile.forbidden_terms, *AD_ABSOLUTE_TERMS))
    return [t for t in terms if t and t in plain]


def has_numeric_claim(text: str) -> bool:
    """判断文本是否含**需要事实支撑**的数据型陈述。

    剥离年款年份后再判断,其余数字(参数、价格、续航、加速等)一律要求引用。
    """
    if not text:
        return False
    # 中文数量词("五百九十三公里")同样是数据,先统一成阿拉伯数字再判断
    stripped = _MODEL_CODE_RE.sub("", _YEAR_RE.sub("", arabicize(text)))
    return bool(_NUMERIC_RE.search(stripped))


class LintIssue:
    __slots__ = ("segment_idx", "severity", "message")

    def __init__(self, segment_idx: int, severity: str, message: str) -> None:
        self.segment_idx = segment_idx
        self.severity = severity
        self.message = message

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict:
        return {
            "segment_idx": self.segment_idx,
            "severity": self.severity,
            "message": self.message,
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"[{self.severity}] seg{self.segment_idx}: {self.message}"
