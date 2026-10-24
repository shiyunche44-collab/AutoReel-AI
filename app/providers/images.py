"""图像 Provider:联网检索、AI 生成、图像理解校验。

用途分工:
- `search`   → 真实实拍图,进素材池当背景。
- `generate` → AI 生图。统一风格封面的空影棚底(`cover_art`)和"素材池里没有白底官图时
  按车名生成的那台车"(`cover_art.ai_car`)都走这里。
- `describe` → 视觉理解:素材池打标签时逐张描述画面(见 `vision_verdict`)。

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
             seed: int | None = None,
             n: int = 1,
             timeout: int = 300,
             backend: str = "") -> ImageRef | None:
    """文生图 / 图生图。返回含 URL 的 ImageRef,失败返回 None(不抛异常,由调用方决定降级)。"""
    refs = generate_many(prompt, output_file=output_file, input_urls=input_urls,
                         aspect_ratio=aspect_ratio, seed=seed, n=n, timeout=timeout,
                         backend=backend)
    return refs[0] if refs else None


def generate_many(prompt: str, *, output_file: str = "gen.png",
                  input_urls: list[str] | None = None,
                  aspect_ratio: str | None = None,
                  seed: int | None = None,
                  n: int = 1,
                  timeout: int = 300,
                  backend: str = "") -> list[ImageRef]:
    """一次生多个候选。返回所有成功的 ImageRef,失败返回空列表。

    `seed` 固定下来同样的提示词能复现同一批图 —— 封面那台"AI 车"靠它做幂等:
    改了提示词或换了种子,缓存自然失效,不会混用两套车图。

    ⚠️ 参考图(`input_urls`)只有 Connector 后端认;直连的 MiniMax 开放平台
    `subject_reference` 只支持 `type="character"`(人像),**车用不了**,
    所以车只能靠文字提示 + 事后与官方图比对来保证车型。

    ⚠️ 两条后端的"多候选"写法不同,别混:
    - 直连(`/v1/image_generation`):一次请求带 `n`;但**传了 seed,n 张内容趋同**,
      要多张不同图就别传 seed。
    - Connector(`connector__matrix__generate_image`):schema 是封闭的,
      **只认 `prompt` / `input_urls` / `aspect_ratio` / `output_file` / `resolution`**,
      传 `n` 或 `seed` 会被整个请求拒掉。多候选靠 `requests` 数组里放多条同提示词的请求
      —— 服务端逐条随机生成,每条给不同的 `output_file` 以免下载时互相覆盖。
    """
    requests: list[dict[str, Any]] = []
    if backend == "mcode":
        # Connector:请求数组逐条随机,每条一个候选
        base = output_file.rsplit(".", 1)[0] if "." in output_file else output_file
        for i in range(max(1, int(n))):
            req: dict[str, Any] = {"prompt": prompt}
            if input_urls:
                req["input_urls"] = input_urls
            if aspect_ratio:
                req["aspect_ratio"] = aspect_ratio
            req["output_file"] = f"{base}_{i}.png"
            requests.append(req)
    else:
        request: dict[str, Any] = {"prompt": prompt, "output_file": output_file}
        if n and int(n) > 1:
            request["n"] = int(n)
        if seed is not None:
            request["seed"] = int(seed)
        if input_urls:
            request["input_urls"] = input_urls
        if aspect_ratio:
            request["aspect_ratio"] = aspect_ratio
        requests.append(request)

    try:
        result = cli.call(GENERATE_TOOL, {"requests": requests}, timeout=timeout,
                          backend=backend)
    except cli.ConnectorError:
        return []

    items = result.get("success_items") if isinstance(result, dict) else None
    if isinstance(result, list) and result:
        items = result
    if not items:
        return []
    if not isinstance(items, list):
        items = [items]
    refs: list[ImageRef] = []
    for item in items:
        node_id = item.get("node_id") if isinstance(item, dict) else None
        if not node_id:
            continue
        try:
            url = cli.get_asset_url(str(node_id), backend=backend)
        except cli.ConnectorError:
            continue
        refs.append(ImageRef(url=url, origin="generate", note=prompt[:120]))
    return refs


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
