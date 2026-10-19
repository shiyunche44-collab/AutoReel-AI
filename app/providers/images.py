"""图像 Provider:联网检索、AI 生成、图像理解校验。

用途分工(计划 §4.4):
- `search`   → 真实实拍图,作为 H3 参考图锁定车型/产品外观。
- `generate` → 检索不到或构图不满足时,由 AI 补一张首帧。
- `describe` → 质检:确认参考图确实是目标车型、构图符合镜头需要。

⚠️ 检索图**只作为参考图/首帧进入生成流程**,成片画面始终是生成产物 ——
这样既满足"真实素材优先"的诉求,又规避了直接搬运他人素材的版权风险。
(成片里直接使用真实视频片段是另一条通路,见 `clips.py` 与 `clip_resolver.py`。)

⚠️ 检索工具本身是**可用的**。历史上这里发的是 `{"queries": [query]}`
(字符串数组),而 schema 要求对象数组且每项必含 `query`/`prompt`/`task_name`,
于是每次调用都失败,报告里表现为 searched=0 —— 被误判成"工具整体不可用",
参考图默默退化成 AI 编的虚构车型。**畸形的是本地 payload,不是工具。**
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import cli

SEARCH_TOOL = "connector__matrix__search_images"
GENERATE_TOOL = "connector__matrix__generate_image"
DESCRIBE_TOOL = "connector__matrix__describe_images"

#: 相关性过滤的兜底描述。调用方没给按视角的具体描述时用它 ——
#: `prompt` 是必填项,给空串会让整个请求被 schema 拒掉。
DEFAULT_RELEVANCE_PROMPT = (
    "real photograph of the subject, professional editorial photo, "
    "no diagrams, no charts, no text overlays, no e-commerce listings"
)


@dataclass
class ImageRef:
    url: str
    #: 来源标记:search / generate / commons / wikipedia / user
    origin: str = "search"
    #: 该图覆盖的视角标签(exterior_45 / interior / rear / detail)
    angle: str = ""
    note: str = ""
    #: 授权信息。真实照片必须有,否则无从判断能不能用于成片。
    license: str = ""
    author: str = ""
    width: int | None = None
    #: 检索命中的**原始页面**地址。图片直链通常是 CDN 的临时地址,
    #: 事后无从追溯;记下文章页才查得到"这张图哪来的、拍的是哪台车"。
    source_page: str = ""


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
def search(queries: list[str], *,
           relevance_prompts: dict[str, str] | None = None,
           task_name: str = "reference",
           providers: list[str] | None = None,
           errors: list[str] | None = None) -> list[ImageRef]:
    """联网检索图片。

    `queries` 是**检索串**,`relevance_prompts[query]` 是同一批查询各自的
    相关性过滤描述(由 `CategoryProfile.reference_prompt()` 生成)。
    两者分开是因为检索工具把这两件事拆成了两个字段,而它们的最优写法
    完全不同:检索串用中文命中率更高,相关性描述必须用英文。

    一次调用传全部查询 —— schema 的 `queries` 本来就是数组,过去逐条调用
    是畸形 payload 时代的遗留写法,会白白多花 N-1 次往返。

    `errors` 用于把失败原因交回调用方。这一点很关键:曾在这里直接
    `except ConnectorError: continue`,于是"检索工具挂了"和"没搜到"
    在报告里长得一模一样(searched=0 / errors=[]),上层完全看不出
    该去修工具还是该换个查询词 —— 参考图默默退化成 AI 编的假车图。
    """
    refs: list[ImageRef] = []
    seen: set[str] = set()
    unique = [q for q in dict.fromkeys(queries) if q and q.strip()]
    if not unique:
        return refs

    prompts = relevance_prompts or {}
    payload_queries = [
        {
            "query": q,
            # prompt 是必填项,给空串会被 schema 直接拒掉整个请求
            "prompt": prompts.get(q) or DEFAULT_RELEVANCE_PROMPT,
            "task_name": task_name,
        }
        for q in unique
    ]
    args: dict[str, Any] = {"queries": payload_queries}
    if providers:
        args["providers"] = providers

    try:
        result = cli.call(SEARCH_TOOL, args, timeout=180)
    except cli.ConnectorError as exc:
        if errors is not None:
            errors.append(f"检索失败({len(unique)} 条查询):{exc.message}")
        return refs

    for item in _iter_images(result):
        # ⚠️ 不要拿 `link` 兜底:它是**文章页**地址不是图片地址。
        # 混进来等于把一个 HTML 页面 URL 交给 H3 当参考图。
        url = item.get("url") or item.get("image_url")
        if not url or str(url) in seen:
            continue
        seen.add(str(url))
        source_page = str(item.get("link") or "")
        source = str(item.get("source") or "")
        title = str(item.get("title") or "")
        refs.append(ImageRef(
            url=str(url),
            origin="search",
            note=title or source or "",
            author=source or "",
            # 检索图来自各家的公开页面,授权形态不统一。这里如实记来源,
            # 由人判断能不能用,而不是假装每张都有明确许可。
            license=f"检索命中:{source_page or source or '未知来源页'}",
            source_page=source_page,
        ))
    return refs


#: 可能承载图片数组的键。检索工具实测返回**每个查询一个对象**的数组,
#: 即 `[{"query": "...", "images": [...]}, ...]` —— 而不是"顶层 dict 里有 images"。
#: 过去只按后一种形态解析,于是每条查询的 10 张图全被跳过,报告里表现为
#: searched=0,又一次"工具坏了"的假象。
_CONTAINER_KEYS = ("images", "results", "data", "success_items", "items")


def _iter_images(result: Any) -> Iterable[dict[str, Any]]:
    """从检索结果的不同可能形态中迭代出图片条目。"""
    if isinstance(result, dict):
        for key in _CONTAINER_KEYS:
            nested = result.get(key)
            if isinstance(nested, list):
                yield from _iter_images(nested)
                return
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, dict):
                # 可能是"每查询一个结果对象",也可能直接就是图片条目
                if any(k in item for k in _CONTAINER_KEYS):
                    yield from _iter_images(item)
                else:
                    yield item
            elif isinstance(item, str):
                yield {"url": item}


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def generate(prompt: str, *, output_file: str = "gen.png",
             input_urls: list[str] | None = None,
             aspect_ratio: str | None = None,
             timeout: int = 300) -> ImageRef | None:
    """文生图 / 图生图。返回含 URL 的 ImageRef,失败返回 None(不抛异常,由调用方决定降级)。"""
    request: dict[str, Any] = {"prompt": prompt, "output_file": output_file}
    if input_urls:
        request["input_urls"] = input_urls
    if aspect_ratio:
        request["aspect_ratio"] = aspect_ratio

    try:
        result = cli.call(GENERATE_TOOL, {"requests": [request]}, timeout=timeout)
    except cli.ConnectorError:
        return None

    items = result.get("success_items") if isinstance(result, dict) else None
    if isinstance(result, list) and result:
        items = result
    if not items:
        return None
    item = items[0] if isinstance(items, list) else items
    node_id = item.get("node_id") if isinstance(item, dict) else None
    if not node_id:
        return None
    try:
        url = cli.get_asset_url(str(node_id))
    except cli.ConnectorError:
        return None
    return ImageRef(url=url, origin="generate", note=prompt[:120])


# --------------------------------------------------------------------------
# 理解 / 校验
# --------------------------------------------------------------------------
def describe(image_urls: list[str], prompt: str, *, timeout: int = 180) -> str:
    """对图片做视觉问答。用于校验参考图是否为目标车型、构图是否符合镜头。"""
    if not image_urls:
        return ""
    payload = {
        "image_info": [{"url": url} for url in image_urls[:10]],
        "prompt": prompt,
    }
    try:
        result = cli.call(DESCRIBE_TOOL, payload, timeout=timeout)
    except cli.ConnectorError:
        return ""
    return _flatten_text(result)


def _flatten_text(result: Any) -> str:
    """把视觉工具的各种返回形态压成纯文本。"""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(_flatten_text(item) for item in result)
    if isinstance(result, dict):
        for key in ("content", "text", "answer", "result", "message", "description"):
            if key in result:
                return _flatten_text(result[key])
        return "\n".join(f"{k}: {v}" for k, v in result.items() if isinstance(v, (str, int, float)))
    return str(result)


# --------------------------------------------------------------------------
# 本地化
# --------------------------------------------------------------------------
def download(url: str, dest: str | Path, *, timeout: int = 180,
             headers: dict[str, str] | None = None) -> Path:
    """下载图片到项目目录,便于 ffmpeg 抽帧与 Web 预览。

    `headers` 用于需要标识自己的站点(如 Wikimedia 会 403 掉匿名下载)。
    """
    import httpx

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True,
                      headers=headers or None) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                fh.write(chunk)
    return dest
