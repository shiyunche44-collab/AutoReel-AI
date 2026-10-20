"""视觉判定的共用底座:逐张问视觉模型,散文交给 LLM 转成结构化结论。

素材池打标签(`library.tag_items`)走这条路。

⚠️ 这套形状是被视觉问答工具的两条实测行为逼出来的:

1. 它**恒返回散文描述**,不理会"只输出 JSON"的要求;
2. 一次给多张图,它**只描述第一张** —— 批量提交时其余几张从没被看过,
   解析失败又一律保留,校验实际是走过场。

所以:每张图单独问一次,回答合在**一次** LLM 调用里转成判定(回答已经是
文本,LLM 不需要看图,合在一起问更便宜也更快)。

⚠️ 视觉模型偶尔**会**直接给 JSON,但它自己的判定比我们的规则严得多 ——
实测把"汉 DM"判成不是"汉 EV 2026"、因为看不出年款就判 false,一次剔掉
10 张里的 8 张。所以有 LLM 时**一律**走 LLM 的保守规则(JSON 连同它后面
的判定依据一起交过去),视觉模型的 JSON 只在没有 LLM 或 LLM 失败时兜底。
任何一步失败,对应的图只是"没有结论",由调用方按保守原则保留。

⚠️ 视觉模型还会**只**回一段 JSON、一句描述都没有。去掉 JSON 后什么都不剩,
这张图就成了"没有结论"而被保留 —— 实测小米 YU7 一项目里,网页截图、
海报、被裁的图全是这样混进成片的。所以这类回答**追问一次**,要它用文字
描述;追问仍然只有 JSON,才算没有结论。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

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


#: 回答只有 JSON 时追问用的后缀。
PROSE_RETRY = ("\n\n上一次你只给了 JSON 结论,没有任何描述。这次**不要输出 JSON**,"
               "用几句中文逐条描述你在图里看到了什么:主体是什么、有哪些文字、"
               "主体是否完整。")


def describe_each(urls: list[str], prompt: str | Sequence[str], errors: list[str], *,
                  what: str = "图") -> list[str]:
    """每张图单独问一次视觉模型。单张失败只记一条,返回空串(= 这张没有结论)。

    `prompt` 可以是一个(所有图共用)或与 `urls` 对齐的一组。
    """
    if not urls:
        return []
    prompts = [prompt] * len(urls) if isinstance(prompt, str) else list(prompt)

    def _one(pair: tuple[str, str]) -> str:
        url, text = pair
        try:
            return images.describe([url], text)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"视觉判定调用失败(该{what}保留):{str(exc)[:120]}")
            return ""

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(urls))) as pool:
        return list(pool.map(_one, zip(urls, prompts)))


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*[\[{].*?```", re.DOTALL)
_VERDICT_OBJ_RE = re.compile(r"\{[^{}]*\"(?:problem|is_target_model|subject_whole)\"[^{}]*\}")


def strip_verdict_json(text: str) -> str:
    """去掉回答里视觉模型**自己的** JSON 判定,只留它对画面的描述。

    不去掉的话,LLM 会顺着它的结论走(实测:"描述给出 is_target_model 为 false,
    但未写出具体车型" → 照判 false),写在 system 里让它忽略也不管用。
    """
    text = _FENCED_JSON_RE.sub("", text)
    return _VERDICT_OBJ_RE.sub("", text).strip()


def structure_with_llm(texts: dict[int, str], system: str, llm: Any,
                       errors: list[str], *, what: str = "图"
                       ) -> dict[int, dict[str, Any]] | None:
    """把描述一次性交给 LLM 转成判定。`texts` 的键是 1 起的编号。

    `system` 要求 LLM 输出 `{"verdicts": [{"index": n, ...}]}`。
    没有 LLM 或调用失败返回 None(调用方据此退回视觉模型自己的 JSON);
    没有可转的内容返回空。编号不在输入里的条目丢弃。
    """
    if llm is None:
        return None
    if not texts:
        return {}
    user = "\n\n".join(f"### 图 {n}\n{t[:1500]}" for n, t in sorted(texts.items()))
    try:
        data = llm.structured(system, user, required_keys=["verdicts"], temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"视觉描述结构化失败,这些{what}按未校验保留:{str(exc)[:120]}")
        return None
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


def judge(urls: list[str], prompt: str | Sequence[str], system: str, llm: Any,
          errors: list[str], *, what: str = "图",
          contexts: Sequence[str] | None = None,
          ) -> tuple[list[dict[str, Any] | None], list[str], dict[str, int]]:
    """逐张判定。返回 (与 urls 对齐的结论或 None, 原始回答, 结论来源计数)。

    有 LLM 时,回答去掉视觉模型自己的 JSON 判定后交给 LLM 按 `system` 的规则转;
    只有一段 JSON、没有任何描述的回答没有依据,该图没有结论(= 保留)。
    没有 LLM 或 LLM 调用失败时,才退回视觉模型自己的 JSON。

    只回 JSON 的图追问一次(见模块说明)。`contexts` 与 `urls` 对齐,
    逐图拼在描述前面交给 LLM(如"这个镜头想要的画面"),空串不拼。
    """
    texts = describe_each(urls, prompt, errors, what=what)
    if llm is not None:
        prompts = [prompt] * len(urls) if isinstance(prompt, str) else list(prompt)
        bare = [i for i, t in enumerate(texts) if t and not strip_verdict_json(t)]
        if bare:
            again = describe_each([urls[i] for i in bare],
                                  [prompts[i] + PROSE_RETRY for i in bare], errors, what=what)
            for i, t in zip(bare, again):
                if t and strip_verdict_json(t):
                    texts[i] = t
    ctx = list(contexts or [])
    described = {n: (f"{ctx[n - 1]}\n{d}" if n - 1 < len(ctx) and ctx[n - 1] else d)
                 for n, t in enumerate(texts, start=1) if t
                 for d in [strip_verdict_json(t)] if d}
    structured = structure_with_llm(described, system, llm, errors, what=what)
    verdicts: list[dict[str, Any] | None] = []
    from_json = 0
    for n, t in enumerate(texts, start=1):
        if structured is not None:
            v = structured.get(n)
        else:
            v = parse_one(t) if t else None
            from_json += v is not None
        verdicts.append(v)
    sources = {
        "json": from_json,
        "llm": len(structured or {}),
        "none": sum(1 for v in verdicts if v is None),
    }
    return verdicts, texts, sources
