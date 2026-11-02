"""方舟 Agent Plan 后端:按 **Connector 工具名** 逐个翻译成方舟的 HTTP 调用。

返回与旧 Connector 相同的结果形状 —— 上层的 audio / images / facts 一行不改,`cli.call()` 转到这里。

| Connector 工具 | 实现 |
|---|---|
| batch_text_to_audio | 豆包 seed-tts-2.0(`ark_tts`,逐条) |
| generate_image | 豆包 Seedream(`ark_image`) |
| get_voice_list | 内置豆包音色表(`ark_tts.VOICES`) |
| describe_images | 豆包看图(plan/v3 chat/completions,逐张) |
| search_images | Bing 图片检索(套餐没有图片检索) |
| thsifind.search_news | 豆包搜索(`ark_search`) |
| 其余(同花顺 / 企查查 / 音乐 / 语音识别) | 套餐没有,抛 ConnectorError,由上层按"该来源失败"降级 |

`upload_temp_url` 不上传任何东西:图片转成 data URI(看图、图生图都收 data URI),
其他文件返回本地 `file://` URI。
"""
from __future__ import annotations

import base64
import hashlib
import io
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import config
from .cli import ConnectorError

#: 网络搜索替代了哪个来源 —— 事实的 source_tool 要如实写成它,而不是同花顺
ARK_SEARCH_LABEL = "ark.web_search"

_IMAGE_SUFFIXES = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
#: data URI 图片的最长边。首帧 / 参考图 / 质检帧都不需要更大,请求体也小
_DATA_URI_MAX_SIDE = 1600


class _Unsupported(ConnectorError):
    """方舟套餐里没有对应能力。不重试。"""


# --------------------------------------------------------------------------
# 文件:data URI / 本地 URI
# --------------------------------------------------------------------------
def image_data_uri(raw: bytes) -> str:
    """把图片压成最长边 ≤1600 的 JPEG data URI。太小的图放大到 256(视频接口下限)。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        scale = min(1.0, _DATA_URI_MAX_SIDE / max(im.size))
        if min(im.size) * scale < 256:
            scale = 256 / min(im.size)
        if scale != 1.0:
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def upload_temp_url(local_path: str | Path) -> str:
    path = Path(local_path)
    if not path.is_file():
        raise ConnectorError("upload_temp_url", f"文件不存在: {path}")
    if path.suffix.lower() in _IMAGE_SUFFIXES:
        return image_data_uri(path.read_bytes())
    return path.resolve().as_uri()


def get_asset_url(node_id: str) -> str:
    """方舟结果里的 node_id 是 `url:<地址>`,这里原样还原成地址。"""
    if node_id.startswith("url:"):
        return node_id[4:]
    raise ConnectorError("get_asset_url", f"不认识的资源标识: {node_id}")


def _fetch_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    if url.startswith("file:"):
        return _local_path(url).read_bytes()
    resp = httpx.get(url, timeout=60, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.content


def _local_path(url: str) -> Path:
    """`file:///F:/x.mp4` → `F:/x.mp4`;Windows 盘符路径要去掉开头的斜杠。"""
    p = urllib.parse.unquote(urllib.parse.urlparse(url).path)
    if len(p) > 2 and p[0] == "/" and p[2] == ":":
        p = p[1:]
    return Path(p)


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def _ark_tts_item(req: dict, timeout: float) -> str:
    """豆包 seed-tts-2.0 合成一句,落成本地文件,返回 `url:file://…`(与旧 Connector 的结果同形)。"""
    from . import ark_tts

    voice = req.get("voice_id") or ""
    key = hashlib.sha1(f"{ark_tts.resolve_voice(voice)}|{req.get('speed', 1)}|{req['text']}"
                       .encode("utf-8")).hexdigest()
    dest = config.DATA_DIR / "_cache" / "ark_tts_raw" / f"{key}.mp3"
    if not dest.is_file() or dest.stat().st_size == 0:
        ark_tts.synthesize_to_file(req["text"], voice_id=voice, speed=req.get("speed", 1.0),
                                   dest=dest, timeout=timeout)
    return "url:" + dest.resolve().as_uri()


def tts_batch(args: dict, timeout: float) -> dict:
    """逐条合成。失败条目占位为 node_id=None:调用方按位置对齐,不能让后面的条目前移。"""
    items: list[dict] = []
    errors: list[str] = []
    for req in args.get("requests") or []:
        try:
            node = _ark_tts_item(req, timeout)
        except ConnectorError as exc:
            node, errors = None, errors + [exc.message]
        items.append({"node_id": node, "file_name": req.get("output_file", "")})
    if not any(i["node_id"] for i in items) and errors:
        raise ConnectorError("ark_tts", errors[0])
    return {"success_items": items}


def generate_image(args: dict, timeout: float) -> dict:
    """每个请求出 n 张(逐张请求),每张都是一个 success_item。
    上层逐张过质检门后挑一张 —— 封面那台"AI 车"就是这么挑出来的。"""
    from . import ark_image

    items = []
    for req in args.get("requests") or []:
        urls = ark_image.generate(req["prompt"], n=max(1, int(req.get("n") or 1)),
                                  aspect_ratio=req.get("aspect_ratio"),
                                  input_urls=req.get("input_urls"), timeout=timeout)
        items.extend({"node_id": "url:" + u} for u in urls)
    return {"success_items": items}


def voice_list(args: dict, timeout: float) -> dict:
    from . import ark_tts

    return {"available_voices": list(ark_tts.VOICES)}


# --------------------------------------------------------------------------
# 理解
# --------------------------------------------------------------------------
def _ark_vlm(image_url: str, prompt: str, timeout: float) -> str:
    """方舟 Agent Plan 的豆包看图(OpenAI 兼容 chat/completions)。429 / 5xx / 网络错误重试。"""
    if not config.ARK_API_KEY:
        raise _Unsupported("describe_images", "未配置 ARK_API_KEY")
    body = {"model": config.ARK_VLM_MODEL, "max_tokens": 4000,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt}]}]}
    last: ConnectorError | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(f"{config.ARK_BASE_URL}/chat/completions", json=body, timeout=timeout,
                              headers={"Authorization": f"Bearer {config.ARK_API_KEY}"})
        except httpx.HTTPError as exc:
            last = ConnectorError("describe_images", f"网络错误:{type(exc).__name__}: {exc}")
        else:
            if resp.status_code == 200:
                try:
                    return str(resp.json()["choices"][0]["message"]["content"] or "")
                except (ValueError, KeyError, IndexError, TypeError):
                    raise ConnectorError("describe_images", f"方舟返回格式不对:{resp.text[:200]}")
            last = ConnectorError("describe_images", f"HTTP {resp.status_code}:{resp.text[:200]}",
                                  code=resp.status_code)
            if resp.status_code not in (429, 500, 502, 503, 504):
                raise last
        time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last


def vlm(image_url: str, prompt: str, timeout: float) -> str:
    """看图。只收 data URI,外链先下载再编码。"""
    if not image_url.startswith("data:"):
        image_url = image_data_uri(_fetch_bytes(image_url))
    return _ark_vlm(image_url, prompt, timeout)


def describe_images(args: dict, timeout: float) -> dict:
    urls = [i.get("url") for i in args.get("image_info") or [] if i.get("url")]
    texts = [vlm(u, args.get("prompt", ""), timeout) for u in urls]
    if len(texts) == 1:
        return {"content": texts[0]}
    return {"content": "\n\n".join(f"图片 {i + 1}:{t}" for i, t in enumerate(texts))}


def search_images(args: dict, timeout: float) -> list[dict]:
    """开放平台没有图片检索。用 Bing 图片顶上,结果形状与 Connector 一致。"""
    from . import webassets

    out = []
    for q in args.get("queries") or []:
        query = q.get("query") if isinstance(q, dict) else str(q)
        try:
            hits = webassets.bing_images_cached(query, limit=10)
        except Exception as exc:  # noqa: BLE001 - 单条查询失败不影响其他
            raise ConnectorError("search_images", f"Bing 图片检索失败:{exc}") from exc
        out.append({"query": query, "images": [
            {"url": h.url, "link": h.source_page, "title": h.note, "source": "bing"}
            for h in hits
        ]})
    return out


def web_search_news(args: dict, timeout: float) -> dict:
    from . import ark_search

    query = str(args.get("query", "")).strip()
    if not query:
        raise ConnectorError("search_news", "查询串为空")
    rows = ark_search.search(
        query, count=int(args.get("size") or 10), timeout=timeout,
        time_range=ark_search.time_range(args.get("time_start"), args.get("time_end")))
    if not rows:
        raise ConnectorError("search_news", f"豆包搜索无结果:{query}")
    return {"data": rows}


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------
HANDLERS: dict[str, Callable[[dict, float], Any]] = {
    "connector__matrix__batch_text_to_audio": tts_batch,
    "connector__matrix__generate_image": generate_image,
    "connector__matrix__get_voice_list": voice_list,
    "connector__matrix__describe_images": describe_images,
    "connector__matrix__search_images": search_images,
    "connector__thsifind__search_news": web_search_news,
}


def source_label(tool: str) -> str:
    """事实来源的真实名字:search_news 实际是豆包搜索。"""
    return ARK_SEARCH_LABEL if tool == "connector__thsifind__search_news" else tool


def call(tool: str, args: dict, *, timeout: float) -> Any:
    handler = HANDLERS.get(tool)
    if handler is None:
        raise _Unsupported(tool, "方舟 Agent Plan 没有该能力(同花顺 / 企查查 / 音乐 / 语音识别)")
    return handler(args, timeout)


def health_check() -> tuple[bool, str]:
    """只查 Key 配没配,不发请求(每次请求都扣套餐额度)。"""
    if not config.ARK_API_KEY:
        return False, "未配置 ARK_API_KEY(方舟 Agent Plan 专属 API Key)"
    return True, (f"方舟 Agent Plan:TTS seed-tts-2.0 / 生图 {config.IMAGE_MODEL}"
                  f" / 看图 {config.ARK_VLM_MODEL} / 豆包搜索")
