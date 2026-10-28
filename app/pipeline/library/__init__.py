"""素材池:按车型先把真实素材攒好、看过、打好标签,取材时直接从池子里挑。

为什么要有素材池
----------------
旧的取材是"每个镜头现搜现下":拿分镜的检索词去 B 站搜,在搜到的长视频里
**按固定起点**切 6 秒,再抽一帧质检。实测小米 YU7 一项目,10 个镜头只有
3 个用上真实素材 —— 不是网上没有,是切的位置不对:宣传片 15% 处可能正好是
字幕卡,评测视频 40% 处可能是主持人口播。质检毙掉之后换个起点再赌一次,
又慢又贵,赌输就退回 AI。

素材池把顺序倒过来:
1. **攒**:按车型找一批源(品牌官方号视频、宣传片、实拍、手动贴的链接、
   汽车之家官图),整条下载或分窗下载;
2. **切**:ffmpeg 场景检测,在**原片自己的剪辑点**切开 —— 每一段都是
   完整的一个镜头,不会切在转场中间;
3. **看**:每段抽中间帧问一次视觉模型,打上视角(前脸 / 侧面 / 车尾 / 行驶 /
   内饰 / 中控屏 / 细节)、是不是这台车、有没有字幕海报等问题;
4. **挑**:取材时按镜头要的视角,从标好的段里拼出一个够长的连续窗口。

池子按车型存在 `data/library/<车型>/`,跨项目复用:同一台车做第二条视频时,
攒和看的成本是零。

增量构建
--------
`build()` 每次只做还没做的事:没下的源下、没打标签的段打标签、没抓的官图抓。
有时长预算(`LIBRARY_BUILD_BUDGET`),超时就停在当前进度,下次接着做。
中途任何一步失败都只是这个源 / 这一段没有,不抛异常。

手动链接
--------
自动搜索搜不到的好素材(抖音官方号、某条 B 站评测的某一段),用
`python -m app.pipeline.library add 小米YU7 <链接>...` 加进来,下次 build 时下载。
抖音只能走这条路:抖音没有可用的站内搜索,而且抖音素材剪回抖音容易被判搬运。
"""

from __future__ import annotations

import argparse  # noqa: F401
import functools  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
from dataclasses import asdict, dataclass, field, fields  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Iterable, Sequence  # noqa: F401
from ... import config  # noqa: F401
from ...categories import CategoryProfile, Vocab, load_profile  # noqa: F401
from ...providers import clips as clips_mod  # noqa: F401
from ...providers import images, official, platform_video  # noqa: F401
from .. import knobs, logo_detect, media_check, vision_verdict  # noqa: F401

from .model import (  # noqa: E402,F401
    BLOCKING,
    CONFIRMED_VIDEO_TARGET,
    COVERAGE_MIN_SECONDS,
    DUP_BITS,
    EARLY_STOP_RATIO,
    EARLY_STOP_SAMPLE,
    ENOUGH_USABLE_SECONDS,
    FILL_MIN_PIECE,
    FOOTAGE_HINT_MISSING_VIEWS,
    FOOTAGE_HINT_SECONDS,
    HOST_SOURCE_RATIO,
    Item,
    LIB_ROOT_NAME,
    Library,
    MAX_AUTO_SOURCES,
    MAX_OFFICIAL_SOURCES,
    MAX_SITE_SOURCES,
    MAX_TAGGED_PER_SOURCE,
    MIN_TAG_SECONDS,
    OFFICIAL_ENOUGH_SOURCES,
    REDISCOVER_MIN_GAP,
    REDISCOVER_SECONDS,
    REDISCOVER_THIN_SECONDS,
    ROUNDUP_ENOUGH_SECONDS,
    ROUNDUP_MAX_SOURCES,
    SCENE_THRESHOLD,
    STILL_SECONDS,
    Source,
    TAG_BATCH,
    THIRD_PARTY_WITH_OFFICIAL,
    WHOLE_MAX_SECONDS,
    WINDOWS_PER_LONG_SOURCE,
    WINDOW_SECONDS,
    _confirmed,
    _hard_bad,
    _has_host,
    _host_sources,
    _save_lock,
    _save_locks,
    _save_locks_guard,
    _third_party,
    _third_party_ok,
    confirmed_seconds,
    coverage_seconds,
    footage_gaps,
    is_waste,
    normalise_url,
    platform_of,
    profile_of,
    short_id,
    slug,
    usable_seconds,
    vocab_of,
)
from .sources import (  # noqa: E402,F401
    JUNK_TITLE_WORDS,
    OFFICIAL_MAX_SECONDS,
    PAGE_IMAGES_MAX,
    SHOWCASE_TITLE_WORDS,
    _IMAGE_EXTS,
    _dedupe_hits,
    _source_of,
    add_images,
    add_links,
    discover,
    discovery_queries,
    english_name,
    rank_sources,
    site_hits,
)
from .ingest import (  # noqa: E402,F401
    _GALLERY_VIEW,
    _PTS_RE,
    _add_still,
    _thumb,
    check_logos,
    download_windows,
    extract_frame,
    ingest_gallery,
    ingest_source,
    scene_cuts,
    split_segments,
)
from .tagging import (  # noqa: E402,F401
    TAG_SYSTEM,
    _dhash,
    _same_frame,
    apply_tag,
    inherit_duplicates,
    mark_official_duplicates,
    reject_sources,
    tag_items,
    tag_prompt,
    tag_system,
)
from .builder import (  # noqa: E402,F401
    _running,
    _running_lock,
    _tag_queue,
    build,
    build_async,
    build_many_async,
    is_building,
    pool_status,
)
from .select import (  # noqa: E402,F401
    BRANDED_GALLERIES,
    BRANDED_PLATFORMS,
    DARK_LUMA,
    EDGE_CROP,
    FILL_DUP_BITS,
    FILL_MAX_PIECE,
    LONG_TAKE_MAX_SLOWDOWN,
    LONG_TAKE_VIEWS,
    Piece,
    RECENT_PIECES,
    ROLES,
    SOURCE_FADE_IN,
    SUBTITLE_CROP,
    THIRD_PARTY_SAFE_CROP,
    _branded_still,
    _crop_edge,
    _croppable,
    _detail_conflict,
    _detail_fit,
    _dirt,
    _fill_tier,
    _fill_tier_base,
    _luma,
    _pool_seconds,
    _probe,
    clean_still,
    fill,
    is_dark,
    long_take,
    near_dup_groups,
)
from .cover import (  # noqa: E402,F401
    COVER_EDGE,
    COVER_STUDIO_LUMA,
    COVER_STUDIO_SPREAD,
    _still_area,
    cover_still,
    is_studio_shot,
)
from .sheet import (  # noqa: E402,F401
    contact_sheet,
)
from .cli import (  # noqa: E402,F401
    _cli,
)
