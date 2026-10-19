"""视觉判定的共用底座:逐张问视觉模型,散文交给 LLM 转成结构化结论。

参考图校验(`asset_resolver`)和片段质检(`clip_resolver`)走的是同一条路,
所以抽到这里,两边各写一份必然漂移。

⚠️ 这套形状是被视觉问答工具的两条实测行为逼出来的:

1. 它**恒返回散文描述**,不理会"只输出 JSON"的要求;
2. 一次给多张图,它**只描述第一张** —— 批量提交时其余几张从没被看过,
   解析失败又一律保留,校验实际是走过场。

所以:每张图单独问一次;回答是 JSON 就直接用,是散文就合在**一次** LLM
调用里转成判定(散文已经是文本,LLM 不需要看图,合在一起问更便宜也更快)。
任何一步失败,对应的图只是"没有结论",由调用方按保守原则保留。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..providers import images

#: 逐张提问的并发数。视觉问答走 CLI 子进程,开太多会被限流
WORKERS = 4


def parse_verdicts(text: str) -> list[dict[str, Any]]:
    """从自由文本里抠出 JSON 数组。解析不出来就返回空列表。

    故意保守:抠不出来就返回空,让调用方按"未校验"处理。**绝不猜** ——
    把解析失败当成"全部不合格"会把素材层变成新的失败源。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def parse_one(text: str) -> dict[str, Any] | None:
    """从单张图的回答里抠出判定对象。数组取第一项,带 `problem` 的对象直接用。"""
    found = parse_verdicts(text)
    if found:
        return found[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) and "problem" in data else None


def describe_each(urls: list[str], prompt: str, errors: list[str], *,
                  what: str = "图") -> list[str]:
    """每张图单独问一次视觉模型。单张失败只记一条,返回空串(= 这张没有结论)。"""
    if not urls:
        return []

    def _one(url: str) -> str:
        try:
            return images.describe([url], prompt)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"视觉判定调用失败(该{what}保留):{str(exc)[:120]}")
            return ""

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(urls))) as pool:
        return list(pool.map(_one, urls))


def structure_with_llm(texts: dict[int, str], system: str, llm: Any,
                       errors: list[str], *, what: str = "图"
                       ) -> dict[int, dict[str, Any]]:
    """把散文描述一次性交给 LLM 转成判定。`texts` 的键是 1 起的编号。

    `system` 要求 LLM 输出 `{"verdicts": [{"index": n, ...}]}`。
    失败返回空 —— 等于这些图没有结论。编号不在输入里的条目丢弃。
    """
    if not texts or llm is None:
        return {}
    user = "\n\n".join(f"### 图 {n}\n{t[:1500]}" for n, t in sorted(texts.items()))
    try:
        data = llm.structured(system, user, required_keys=["verdicts"], temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"视觉描述结构化失败,这些{what}按未校验保留:{str(exc)[:120]}")
        return {}
    out: dict[int, dict[str, Any]] = {}
    for v in data.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        try:
            n = int(v.get("index", 0))
        except (TypeError, ValueError):
            continue
        if n in texts:
            out[n] = v
    return out


def judge(urls: list[str], prompt: str, system: str, llm: Any, errors: list[str], *,
          what: str = "图") -> tuple[list[dict[str, Any] | None], list[str], dict[str, int]]:
    """逐张判定。返回 (与 urls 对齐的结论或 None, 原始回答, 结论来源计数)。

    模型直接给了 JSON 就用,否则交给 LLM 转;都不成则 None。
    """
    texts = describe_each(urls, prompt, errors, what=what)
    direct = [parse_one(t) if t else None for t in texts]
    prose = {n: t for n, (t, v) in enumerate(zip(texts, direct), start=1) if t and v is None}
    structured = structure_with_llm(prose, system, llm, errors, what=what)
    verdicts = list(direct)
    for n, v in structured.items():
        verdicts[n - 1] = v
    sources = {
        "json": sum(1 for v in direct if v is not None),
        "llm": len(structured),
        "none": sum(1 for v in verdicts if v is None),
    }
    return verdicts, texts, sources
