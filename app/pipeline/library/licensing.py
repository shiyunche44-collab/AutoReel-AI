"""素材授权:状态、用途范围、有效期,以及取材时的闸门。

"公开视频""官方号发布""能在线播放"都不等于允许下载后二次剪辑发布。池子照常下载、分析
(授权不明也先入池),但**取材**(`select.fill` / `long_take`)要过这里:
`config.LICENSE_GATE=enforce` 时授权不明、过期、禁止、用途不在范围内的素材一律不取;
`warn`(默认)不拦,只在每段的使用记录里标出授权状态。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

from ... import config
from .model import Item, Library, Source

log = logging.getLogger(__name__)

UNVERIFIED, PENDING, ALLOWED, RESTRICTED, DENIED, EXPIRED = (
    "unverified", "pending", "allowed", "restricted", "denied", "expired")
STATUSES = (UNVERIFIED, PENDING, ALLOWED, RESTRICTED, DENIED, EXPIRED)
STATUS_LABEL = {UNVERIFIED: "未核验", PENDING: "待确认", ALLOWED: "允许使用",
                RESTRICTED: "限制使用", DENIED: "禁止使用", EXPIRED: "授权过期"}
#: 能放行的两种状态(restricted 还要用途匹配)
_PASSING = (ALLOWED, RESTRICTED)

#: 取材用途。目前只有背景(信息解说的底)
BACKGROUND = "background"
#: restricted 没写范围时的默认用途:官网公开素材只当背景参考
DEFAULT_RESTRICTED_SCOPE = (BACKGROUND,)

REVIEW_APPROVED, REVIEW_REJECTED = "approved", "rejected"


@dataclass(frozen=True)
class License:
    """一段素材当前生效的授权。"""
    status: str
    scope: tuple[str, ...] = ()
    proof: str = ""
    expires: float = 0.0
    rejected: bool = False

    def allows(self, purpose: str = BACKGROUND, *, now: float | None = None) -> bool:
        if self.rejected or self.status not in _PASSING:
            return False
        if self.expires and self.expires <= (time.time() if now is None else now):
            return False
        if self.status == RESTRICTED:
            return purpose in (self.scope or DEFAULT_RESTRICTED_SCOPE)
        return not self.scope or purpose in self.scope


def _scope_of(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in (text or "").replace("，", ",").split(",") if p.strip())


def is_official_site(src: Source) -> bool:
    """车企官网自己发布的视频(含官网回放页的 m3u8、官网嵌入的播放器)。"""
    return src.platform == "site" or src.uploader == "官网"


def license_of_source(src: Source, *, now: float | None = None) -> License:
    """源的生效授权。没人工标过的按来源推断;到期的 allowed / restricted 记为 expired。"""
    status = src.license_status
    if status not in STATUSES:
        status = RESTRICTED if is_official_site(src) else UNVERIFIED
    proof = src.license_proof or (src.url if status == RESTRICTED and not src.license_status else "")
    if status in _PASSING and src.license_expires and src.license_expires <= (
            time.time() if now is None else now):
        status = EXPIRED
    return License(status=status, scope=_scope_of(src.license_scope), proof=proof,
                   expires=src.license_expires, rejected=src.review_status == REVIEW_REJECTED)


def license_of_unsourced(item: Item) -> License:
    """没挂源的素材(官图):官方图库 / 厂商媒体图 → 限制使用(只当背景),其余未核验。"""
    if item.official:
        return License(status=RESTRICTED, proof=item.license or item.url)
    return License(status=UNVERIFIED, proof=item.license or item.url)


class LicenseBook:
    """池子里每段素材的授权查询。按源 id 建一次索引,避免取材时反复线性查。"""

    def __init__(self, lib: Library, *, now: float | None = None):
        self.now = time.time() if now is None else now
        self._sources = {s.id: s for s in lib.sources}

    def source_of(self, item: Item) -> Source | None:
        return self._sources.get(item.source) if item.source else None

    def license(self, item: Item) -> License:
        src = self.source_of(item)
        return license_of_source(src, now=self.now) if src else license_of_unsourced(item)

    def allows(self, item: Item, purpose: str = BACKGROUND) -> bool:
        return self.license(item).allows(purpose, now=self.now)

    def origin_url(self, item: Item) -> str:
        """成片使用记录里的出处:有源就是源地址,官图是出处页。"""
        src = self.source_of(item)
        return src.url if src else item.url

    def keep(self, items: Iterable[Item], purpose: str = BACKGROUND) -> list[Item]:
        """闸门:enforce 只留授权通过的;warn 全留(调用方靠使用记录看授权状态)。"""
        items = list(items)
        if gate_mode() != "enforce":
            return items
        kept = [i for i in items if self.allows(i, purpose)]
        if len(kept) < len(items):
            log.info("授权闸门拦下 %d/%d 段素材(用途 %s)", len(items) - len(kept), len(items), purpose)
        return kept


def gate_mode() -> str:
    return "enforce" if config.LICENSE_GATE == "enforce" else "warn"


def set_license(lib: Library, source_ids: Sequence[str], *, status: str | None = None,
                scope: str | None = None, expires: float | None = None, proof: str | None = None,
                review: str | None = None, reviewer: str = "", event: str | None = None) -> int:
    """人工核验:给一批源写授权 / 审核。给了才改,没给的保持原样。返回改了几个源。"""
    if status is not None and status not in STATUSES:
        raise ValueError(f"未知的授权状态:{status}(可选 {', '.join(STATUSES)})")
    if review not in (None, "", REVIEW_APPROVED, REVIEW_REJECTED):
        raise ValueError(f"未知的审核状态:{review}")
    n = 0
    for src in lib.sources:
        if src.id not in source_ids:
            continue
        if status is not None:
            src.license_status = status
        if scope is not None:
            src.license_scope = scope
        if expires is not None:
            src.license_expires = expires
        if proof is not None:
            src.license_proof = proof
        if event is not None:
            src.event = event
        if review is not None:
            src.review_status = review
            src.reviewed_by, src.reviewed_at = reviewer, time.time()
        n += 1
    return n


def summary(lib: Library, *, now: float | None = None) -> dict:
    """池子的授权汇总:各状态的源数、视频段数,以及当前闸门下取材会被拦的段数。"""
    book = LicenseBook(lib, now=now)
    sources = {s: 0 for s in STATUSES}
    for src in lib.sources:
        sources[license_of_source(src, now=book.now).status] += 1
    segs = {s: 0 for s in STATUSES}
    blocked = 0
    for i in lib.items:
        lic = book.license(i)
        segs[lic.status] += 1
        if not lic.allows(BACKGROUND, now=book.now):
            blocked += 1
    return {"gate": gate_mode(),
            "sources": {STATUS_LABEL[k]: v for k, v in sources.items() if v},
            "segments": {STATUS_LABEL[k]: v for k, v in segs.items() if v},
            "blocked_if_enforced": blocked}
