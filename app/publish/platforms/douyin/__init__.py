"""抖音适配器:开放平台优先,创作者平台网页版兜底。

**两条路**:

1. **开放平台(正规路)**:OAuth2 换 token → 分片上传 → `create_video` 建片。
   需要企业开发者资质与 `video.create` 能力审核。返回 `item_id` 就能查数据。
2. **创作者平台网页版(兜底)**:Playwright 驱动已登录的浏览器走发布页。
   没有资质时的唯一路,但页面改版就得跟进。

**100MB 限制**:开放平台单文件 ≤100MB。我们的片子 1080p 五分钟能有 200MB+,
所以超限时先用 ffmpeg 转一版 720p 再传 —— 平台自己推荐的也是 720p 起。
"""

from __future__ import annotations

import json  # noqa: F401
import logging  # noqa: F401
import re  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401
from ...base import Account, AccountStatus, MetricSnapshot, PlatformRef, PublishError, PublishRequest, PublishResult  # noqa: F401
from ...http import UPLOAD_TIMEOUT, new_client, request_json  # noqa: F401
from ...registry import PORTRAIT  # noqa: F401

from .consts import (  # noqa: E402,F401
    COVER_INPUT,
    COVER_SLOTS,
    CREATOR_MANAGE,
    CREATOR_UPLOAD,
    DESC_SELECTORS,
    MAX_UPLOAD,
    OPEN,
    PART_SIZE,
    PUBLISH_DONE_TEXTS,
    PUBLISH_WAIT_MS,
    TITLE_MAX,
    UPLOAD_ENTRY_TIMEOUT_MS,
    UPLOAD_WAIT_MS,
    WORK_LIST_API,
    WORK_RESOLVE_GAP,
    WORK_RESOLVE_ROUNDS,
    log,
)
from .cover import (  # noqa: E402,F401
    COVER_PROMPT_TEXTS,
    _cover_srcs,
    _dialog_preview,
    clear_topic_popup,
    close_cover_editor,
    cover_files,
    dismiss_cover_prompt,
    set_covers,
    slot_covered,
)
from .publish_page import (  # noqa: E402,F401
    DECLARE_AI,
    DECLARE_NONE,
    _DECLARE_ROW,
    _click_publish,
    _config,
    _declaration_trigger,
    _declare_enabled,
    click_publish,
    dismiss_topic_popup,
    publish_confirmed,
    publish_done,
    set_declaration,
    set_description,
    wait_publish_ready,
    wait_upload_done,
)
from .adapter import (  # noqa: E402,F401
    DouyinAdapter,
)
from .works import (  # noqa: E402,F401
    PROGRESS_API,
    WORK_DETAIL,
    _FETCH_JSON_JS,
    _int,
    fetch_creator_works,
    fetch_work_progress,
    parse_progress,
    resolve_published_work,
)
from .comments import (  # noqa: E402,F401
    COMMENT_ITEMS_API,
    COMMENT_LIST_API,
    COMMENT_PAGES,
    CREATOR_COMMENTS,
    _CommentPage,
    _EMOJI_RE,
    _MARK_REPLY_JS,
    _MARK_SEND_JS,
    comment_text_key,
    fetch_creator_comments,
    send_comment_replies,
)
from .profile import (  # noqa: E402,F401
    PROFILE_POST_API,
    USER_PAGE,
    _SEC_UID,
    fetch_profile_posts,
    sec_uid_of,
)
