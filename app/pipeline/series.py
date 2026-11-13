"""栏目:同一类选题挂在同一个栏目下,带期号,观众知道「下一条是什么」,才有关注的理由。

- 栏目名来自品类 vocab `series`(选题类型 model / band / topic → 栏目名),
  选题类型按发布日星期轮换(`team.editor.slot_kind`)。
  键也可以是片型 id(如 buyguide):该片型固定挂这个栏目,优先于星期(`team.plan._series_of`)。
- 期号存在项目的 variant 里(`series` / `episode`),建项目时定:
  同栏目里未取消 / 未失败的项目的最大期号 + 1;返工沿用原项目的期号。
- 栏目出现在:脚本结尾(固定「下期预告」+ 关注口播)、封面角标、发布标签。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .. import db

#: 这些状态的项目不占期号(放弃了,观众看不到)
_DEAD_STATES = ("CANCELLED", "FAILED")


def name_for(vocab: Any, kind: str) -> str:
    """选题类型 → 栏目名;品类没配 / 类型不认识 → 空串(不挂栏目)。"""
    table = getattr(vocab, "series", None) or {}
    return str(table.get(kind) or "").strip()


def next_episode(category: str, name: str) -> int:
    """同栏目下一期的期号。"""
    if not name:
        return 0
    db.init_db()
    top = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT variant, state FROM projects WHERE category = ?", (category,)).fetchall()
    for raw, state in rows:
        if str(state) in _DEAD_STATES:
            continue
        try:
            v = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict) and v.get("series") == name:
            top = max(top, int(v.get("episode") or 0))
    return top + 1


def of_variant(variant: Mapping[str, Any] | None) -> tuple[str, int]:
    v = variant or {}
    return str(v.get("series") or ""), int(v.get("episode") or 0)


def chip(variant: Mapping[str, Any] | None) -> str:
    """封面角标:「柒哥帮你挑·第3期」。没栏目 = 空串。"""
    name, ep = of_variant(variant)
    if not name:
        return ""
    return f"{name}·第{ep}期" if ep > 0 else name


def script_rule(variant: Mapping[str, Any] | None, host: str = "") -> str:
    """脚本提示词里的栏目要求;没栏目 = 空串(提示词与以前逐字一样)。"""
    name, ep = of_variant(variant)
    if not name:
        return ""
    who = host or "我"
    nth = f"第 {ep} 期" if ep > 0 else ""
    return (f"   - 这是栏目「{name}」{nth}。最后一句先预告具体的下期内容,"
            f"再用一句短话请观众关注{who},下期「{name}」接着聊。")
