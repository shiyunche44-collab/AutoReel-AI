"""常量:接口地址、等待时限、选择器。"""
from __future__ import annotations

import logging


log = logging.getLogger(__name__)

OPEN = "https://open.douyin.com"
#: 创作者平台发布页(网页版兜底)
CREATOR_UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
#: 作品管理页:页面自己会请求 `WORK_LIST_API`,每条作品带播放、完播率、2 秒跳出、涨粉
CREATOR_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
WORK_LIST_API = "/janus/douyin/creator/pc/work_list"
#: 平台文档给的单文件上限
MAX_UPLOAD = 100 * 1024 * 1024
#: 单片建议 20MB
PART_SIZE = 20 * 1024 * 1024
#: 网页版标题框上限(页面上写着 0/30)
TITLE_MAX = 30
#: 网页版等视频传完(页面出现「重新上传」)的时限
UPLOAD_WAIT_MS = 10 * 60 * 1000
#: 发布页打开后,上传控件要等页面把「加载中,请稍候」跑完才出现 —— 实测约 18 秒,
#: 等不够就判「没找到上传入口」,其实页面只是慢(直连真实 IP 时更慢)。
UPLOAD_ENTRY_TIMEOUT_MS = 60 * 1000
#: 点「发布」后页面先挂「正在发布」(实测 20 秒以上),跳到作品管理才算发完 —— 提前关浏览器等于撤稿
PUBLISH_WAIT_MS = 3 * 60 * 1000
PUBLISH_DONE_TEXTS = ("发布成功", "投稿成功")

#: 网页版封面两个位:(位上的提示字, 弹窗里切到这一步的按钮/页签)
COVER_SLOTS = (("横封面4:3", "设置横封面"), ("竖封面3:4", "设置竖封面"))
#: 封面弹窗里「上传封面」那个隐藏的 file input(按钮本身被上传层盖住,点不到)
COVER_INPUT = ("div:has(> .semi-upload):has-text('上传封面') > .semi-upload "
               "> input.semi-upload-hidden-input")
#: 「作品描述」框:抖音网页版用的是自研编辑器(contenteditable 的 div),不是 textarea
DESC_SELECTORS = (
    "div.zone-container[contenteditable=true]",
    "div[contenteditable=true][data-placeholder*='简介']",
    "div[contenteditable=true]",
)
#: 发布后回查作品 id:作品入库有延迟,查几轮
WORK_RESOLVE_ROUNDS = 4
#: 回查两轮之间隔多久(秒)
WORK_RESOLVE_GAP = 6
