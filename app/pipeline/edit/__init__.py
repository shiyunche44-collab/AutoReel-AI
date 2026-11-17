"""剪辑 v2:统一时间线(EDL) + Remotion 主合成。设计见 docs/edit-v2.md。

和旧合成(`explainer.compose` → `render.compose`)的区别:

- 旧:每拍先烧成一段 mp4(背景 + 叠加层一起烧死),再 concat 硬拼。
- 新:实拍拍不再提前烧死,而是按旁白的**字级时间**在分句处切成 1–3 秒的子镜头,
  每个子镜头按**它那半句在讲什么**单独取材;叠加层、花字字幕、转场、推拉都写进一份
  `timeline.json`,由 Remotion 一次合成。数据卡 / 概念动画 / 机构画面仍用逐拍渲染的成品。

试点只开竖屏(`EDIT_V2=1`),失败自动退回旧合成,不拦出片。
"""
from __future__ import annotations

from .edl import Timeline, build_timeline, enabled  # noqa: F401
