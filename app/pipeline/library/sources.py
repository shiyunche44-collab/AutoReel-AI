"""找源:检索词、官网 / 媒体页视频、排序过滤,以及手动加链接 / 加图。"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Iterable, Sequence
from ... import config
from ...categories import Vocab
from ...providers import clips as clips_mod
from ...providers import official, platform_video

from .model import Library, MAX_AUTO_SOURCES, MAX_OFFICIAL_SOURCES, MAX_SITE_SOURCES, OFFICIAL_ENOUGH_SOURCES, Source, THIRD_PARTY_WITH_OFFICIAL, normalise_url, platform_of, vocab_of
from .ingest import _add_still


# --------------------------------------------------------------------------
# 攒:发现源
# --------------------------------------------------------------------------
def english_name(subject: str, aliases: Sequence[str]) -> str:
    """YouTube 用英文名搜。别名里有带拉丁字母且含主体型号的(Xiaomi YU7)就用它。"""
    latin = re.findall(r"[A-Za-z0-9]+", subject)
    code = [t for t in latin if re.search(r"\d", t) or t.isupper()]
    for a in aliases:
        if re.search(r"[A-Za-z]", a) and code and all(c.lower() in a.lower() for c in code):
            return a
    brand = next((a for a in aliases if re.fullmatch(r"[A-Za-z][A-Za-z ]+", a)), "")
    return " ".join(x for x in (brand.split()[0] if brand else "", *latin) if x) or subject


def discovery_queries(subject: str, en: str, vocab: Vocab) -> tuple[list[str], list[str]]:
    """(中文检索词, 英文检索词)。挑的都是"主体多人少"的视频类型,模板在 profile 里。"""
    zh = [q.format(subject=subject, en=en) for q in vocab.discovery_zh]
    en_q = [q.format(subject=subject, en=en) for q in vocab.discovery_en]
    return zh, en_q


def site_hits(subject: str, spec: dict[str, tuple[str, ...]], errors: list[str]
              ) -> list[platform_video.PlatformHit]:
    """车企官网车型页 / 媒体中心里的视频(直链 mp4、嵌入的 B 站 / YouTube 播放器)。
    无水印、清晰度最高,排在所有源前面。页面要点名这台车,别的车型的素材按地址剔掉
    (`official.site_assets`)。"""
    if not spec or not (spec.get("sites") or spec.get("media")):
        return []
    try:
        videos, _ = official.site_assets(subject, spec, errors=errors)
    except Exception as exc:  # noqa: BLE001 - 官网改版只是少一路
        errors.append(f"官网视频抓取失败:{str(exc)[:120]}")
        return []
    out: list[platform_video.PlatformHit] = []
    for v in videos[:MAX_SITE_SOURCES]:
        name = v.url.rsplit("/", 1)[-1].split("?", 1)[0][:60]
        out.append(platform_video.PlatformHit(
            platform=v.platform or "site", url=v.url, title=f"{subject} 官网 {name}",
            uploader="官网", official=True, verified=True))
    return out


def discover(lib: Library, aliases: Sequence[str], errors: list[str], *,
             max_sources: int = MAX_AUTO_SOURCES, tiktok_handles: Sequence[str] = (),
             spec: dict[str, tuple[str, ...]] | None = None) -> int:
    """搜一轮新源加进池子。官网视频 → 官方号的视频 → 其余。返回新增的源数。"""
    subject = lib.subject
    vocab = vocab_of(lib)
    en = english_name(subject, aliases)
    hits: list[platform_video.PlatformHit] = []
    # 官网的视频不走标题过滤(文件名不是标题),直接排最前
    site = site_hits(subject, spec or {}, errors)
    if aliases and "bilibili" in config.MEDIA_PLATFORMS:
        # 官方号投稿直接按号列:关键词搜索只能碰到官方号的两三条
        hits += official.bilibili_official(subject, aliases, markers=vocab.official_markers,
                                           limit=MAX_OFFICIAL_SOURCES * 2, errors=errors)
    if aliases:
        hits += official.platform_official(subject, aliases, platforms=config.MEDIA_PLATFORMS,
                                           errors=errors, markers=vocab.official_markers)
    if tiktok_handles and "tiktok" in config.MEDIA_PLATFORMS:
        hits += official.tiktok_official(subject, tiktok_handles, names=[en],
                                         aliases=aliases, errors=errors)
    zh_q, en_q = discovery_queries(subject, en, vocab)
    zh_platforms = [p for p in config.MEDIA_PLATFORMS if p == "bilibili"]
    en_platforms = [p for p in config.MEDIA_PLATFORMS if p in ("youtube", "tiktok")]
    for q in zh_q:
        hits += platform_video.search(q, platforms=zh_platforms, errors=errors)
    for q in en_q:
        hits += platform_video.search(q, platforms=en_platforms, errors=errors)
    for h in hits:
        h.official = h.official or bool(aliases and platform_video.is_official(
            h, aliases, markers=vocab.official_markers))
    ranked = [*_dedupe_hits(site),
              *rank_sources(_dedupe_hits(hits), subject, names=[en], vocab=vocab)]

    # 官方源和第三方源分开计数:官方的先收满,第三方只补位
    auto = [s for s in lib.sources if s.added_by == "auto"]
    n_official = sum(1 for s in auto if s.official)
    n_other = len(auto) - n_official
    # 横评的轻量池(max_sources 很小)官方 + 第三方合计不超过 max_sources;正常池官方单独计
    separate = max_sources >= MAX_AUTO_SOURCES
    official_cap = MAX_OFFICIAL_SOURCES if separate else max_sources
    added = 0
    for h in (h for h in ranked if h.official):
        if n_official >= official_cap:
            break
        got = lib.add_source(_source_of(h))
        n_official += got
        added += got
    other_cap = max_sources if separate else max(0, max_sources - n_official)
    if n_official >= OFFICIAL_ENOUGH_SOURCES:
        other_cap = min(other_cap, THIRD_PARTY_WITH_OFFICIAL)
    for h in (h for h in ranked if not h.official):
        if n_other >= other_cap:
            break
        got = lib.add_source(_source_of(h))
        n_other += got
        added += got
    lib.discovered_at = time.time()
    return added


def _source_of(h: platform_video.PlatformHit) -> Source:
    return Source(url=h.url, platform=h.platform, title=h.title, uploader=h.uploader,
                  duration=h.duration, official=h.official)


#: 标题里出现这些词的源不要:游戏画面、新闻事件、渲染猜想、玩具手办。
#: 品类专属的(汽车的 forza、车模……)在 profile 的 vocab.junk_words,两边合并
JUNK_TITLE_WORDS = (
    "游戏", "手办", "乐高", "lego", "渲染", "猜想", "曝光", "谍照", "事故", "起火",
    "维权", "系统更新", "投屏", "教程", "怎么",
)
#: 标题里出现这些词的排前面:广告、宣传片、实拍展示,画面里主体多人少。
#: 品类专属的在 vocab.showcase_words
SHOWCASE_TITLE_WORDS = (
    "宣传片", "广告", "tvc", "官方", "official", "commercial", "debut", "film",
    "实拍", "首发",
)
#: 官方号的长视频(发布会)也收:分窗下载,发布会里放的宣传片正是好素材
OFFICIAL_MAX_SECONDS = 3 * 3600


def rank_sources(hits: Sequence[platform_video.PlatformHit], subject: str, *,
                 names: Sequence[str] = (), vocab: Vocab | None = None
                 ) -> list[platform_video.PlatformHit]:
    """过滤 + 排序:官方号 → 广告 / 宣传片 / 实拍类标题 → 其余;口播类和垃圾标题不要。

    `names` 是车型的其他叫法(英文名):YouTube / TikTok 文案是英文的,
    "比亚迪海豹06" 认不出 "BYD SEAL 06"。
    """
    vocab = vocab or Vocab()
    junk = (*JUNK_TITLE_WORDS, *vocab.junk_words)
    showcase_words = (*SHOWCASE_TITLE_WORDS, *vocab.showcase_words)
    out: list[platform_video.PlatformHit] = []
    for h in hits:
        title = h.title.lower()
        limit = OFFICIAL_MAX_SECONDS if h.official else platform_video.MAX_SOURCE_SECONDS
        if h.duration and not (platform_video.MIN_SOURCE_SECONDS <= h.duration <= limit):
            continue
        if h.title and not any(platform_video.title_relevant(h.title, n)
                               for n in (subject, *names) if n):
            continue
        if any(w in title for w in junk):
            continue
        # 口播 / 直播类 rank 只是排后面,这里直接不要:攒素材要的是车。官方发布会除外
        if not h.official and any(w in title for w in platform_video._TALKY_WORDS):
            continue
        out.append(h)

    def _key(h: platform_video.PlatformHit) -> tuple[int, int]:
        showcase = any(w in h.title.lower() for w in showcase_words)
        return (0 if h.official else 1, 0 if showcase else 1)

    return sorted(out, key=_key)


def _dedupe_hits(hits: Iterable[platform_video.PlatformHit]) -> list[platform_video.PlatformHit]:
    seen: dict[str, platform_video.PlatformHit] = {}
    for h in hits:
        key = normalise_url(h.url)
        if key in seen:
            seen[key].official = seen[key].official or h.official
            continue
        seen[key] = h
    return list(seen.values())


def add_links(subject: str, urls: Iterable[str]) -> list[str]:
    """手动加链接。返回新加的地址。"""
    lib = Library.load(subject)
    added: list[str] = []
    for url in urls:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            continue
        if lib.add_source(Source(url=url, platform=platform_of(url), added_by="manual")):
            added.append(normalise_url(url))
        else:
            # 已有但失败过的,手动再加一次就重试
            for s in lib.sources:
                if s.url == normalise_url(url) and s.status == "failed":
                    s.status, s.error = "pending", ""
    lib.save()
    return added


#: 当图片直接收的扩展名
_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
#: 一个详情页最多收几张(电商详情页动辄几十张长图,前面的主图最有用)
PAGE_IMAGES_MAX = 20


def add_images(subject: str, refs: Iterable[str], *, category: str = "",
               errors: list[str] | None = None) -> int:
    """手动加图:图片地址、本地图片文件,或商品详情页 / 官网产品页(抓页面里的大图)。
    入池后还没打标签,下次 build 时打 —— 认不出是目标产品的不当背景。返回新增张数。

    京东 / 天猫这类页面反爬重,抓不到就把图另存到本地再加。
    """
    errors = errors if errors is not None else []
    lib = Library.load(subject)
    if category:
        lib.category = category
    have = {i.url for i in lib.items if i.kind == "still"}
    added = 0
    for ref in refs:
        ref = ref.strip()
        if not ref:
            continue
        is_url = ref.startswith(("http://", "https://"))
        if not is_url and not Path(ref).is_file():
            errors.append(f"不是图片地址也不是本地文件:{ref[:80]}")
            continue
        path_part = ref.split("?", 1)[0].lower()
        if not is_url or path_part.endswith(_IMAGE_EXTS):
            urls, page = [ref if is_url else str(Path(ref).resolve())], ""
        else:
            body = clips_mod.fetch_page(ref)
            urls = official.extract_images(body, ref)[:PAGE_IMAGES_MAX] if body else []
            page = ref
            if not urls:
                errors.append(f"页面里没抓到图(可能有反爬,把图存到本地再加):{ref[:80]}")
                continue
        for url in urls:
            if url in have:
                continue
            item = _add_still(lib, url, errors, platform="manual", referer=page,
                              license=f"手动添加:{page or ref}")
            if item is not None:
                have.add(url)
                added += 1
    lib.save()
    return added
