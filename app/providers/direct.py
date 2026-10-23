"""直连后端:用 M Plan 订阅 Key 直接调 MiniMax 开放平台,替代 mcode-tools。

mcode-tools 的登录由 MiniMax Code 应用托管(shared-broker),离开那个应用就调不通。
这里按 **Connector 工具名** 逐个翻译成 HTTP 调用,并返回与 Connector 相同的结果形状 ——
上层的 audio / images / facts 一行不改,`cli.call()` 按配置分流过来。

| Connector 工具 | 直连实现 |
|---|---|
| batch_text_to_audio | `/v1/t2a_v2`(逐条) |
| batch_text_to_music | `/v1/music_generation` |
| generate_image | `/v1/image_generation` |
| get_voice_list | `/v1/get_voice` |
| describe_images | `/v1/coding_plan/vlm`(M Plan 的图片理解,逐张) |
| search_images | Bing 图片检索(开放平台没有图片检索接口) |
| thsifind.search_news | `/v1/coding_plan/search`(M Plan 的网络搜索) |
| 其余同花顺 / 企查查 / listen_audio | 不支持,抛 ConnectorError,由上层按"该来源失败"降级 |

`upload_temp_url` 在直连模式下不上传任何东西:图片转成 data URI(开放平台的
图片理解接口收 data URI),其他文件返回本地 `file://` URI。
"""
from __future__ import annotations

import base64
import io
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from .. import config
from .cli import ConnectorError

#: 网络搜索替代了哪个来源 —— 事实的 source_tool 要如实写成它,而不是同花顺
WEB_SEARCH_LABEL = "minimax.web_search"

_IMAGE_SUFFIXES = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".webp": "webp"}
#: data URI 图片的最长边。首帧 / 参考图 / 质检帧都不需要更大,请求体也小
_DATA_URI_MAX_SIDE = 1600

_V1_STATUS = {
    "preparing": "queued", "queueing": "queued", "queued": "queued",
    "processing": "running", "running": "running",
    "success": "succeeded", "succeeded": "succeeded",
    "fail": "failed", "failed": "failed",
}


class _Unsupported(ConnectorError):
    """直连模式下没有对应能力。不重试。"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _key() -> str:
    if not config.MINIMAX_PLAN_KEY:
        raise _Unsupported("direct", "未配置 MINIMAX_PLAN_KEY(M Plan 订阅 Key)")
    return config.MINIMAX_PLAN_KEY


def _error_message(data: Any) -> str | None:
    """开放平台有两种错误形态:V2 的 OpenAI 风格 `{"type":"error","error":{...}}`,
    V1 的 `base_resp.status_code != 0`。成功时返回 None。"""
    if not isinstance(data, dict):
        return None
    if data.get("type") == "error" or (isinstance(data.get("error"), dict) and "task" not in data):
        err = data.get("error") or {}
        return str(err.get("message") or err or "未知错误")
    base = data.get("base_resp")
    if isinstance(base, dict) and base.get("status_code") not in (0, None):
        return f"{base.get('status_code')}: {base.get('status_msg', '')}"
    return None


def request(method: str, path: str, *, tool: str, json_body: dict | None = None,
            params: dict | None = None, timeout: float = 120, retries: int = 2) -> dict:
    """发一次开放平台请求。429 / 5xx / 网络错误重试,其余错误直接抛。"""
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    url = f"{config.MINIMAX_API_HOST}{path}"
    last: ConnectorError | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.request(method, url, headers=headers, json=json_body,
                                 params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            last = ConnectorError(tool, f"网络错误:{type(exc).__name__}: {exc}")
        else:
            try:
                data = resp.json()
            except ValueError:
                data = {"type": "error", "error": {"message": resp.text[:300]}}
            msg = _error_message(data)
            if resp.status_code < 400 and msg is None:
                return data
            last = ConnectorError(tool, f"HTTP {resp.status_code}:{msg or resp.text[:300]}",
                                  code=resp.status_code)
            if resp.status_code not in (429, 500, 502, 503, 504, 529):
                raise last
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    assert last is not None
    raise last


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
    """直连结果里的 node_id 是 `url:<地址>`,这里原样还原成地址。"""
    if node_id.startswith("url:"):
        return node_id[4:]
    raise ConnectorError("get_asset_url", f"直连模式不认识的资源标识: {node_id}")


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
# 视频
# --------------------------------------------------------------------------
def _audio_node(data: dict) -> str | None:
    """`output_format=url` 时 data.audio 是地址;万一回来的是 hex,落成临时文件不好追踪,
    直接当失败处理 —— 调用方会把这条当缺失,而不是拿到一个打不开的路径。"""
    audio = (data.get("data") or {}).get("audio")
    if isinstance(audio, str) and audio.startswith("http"):
        return "url:" + audio
    return None


def tts_batch(args: dict, timeout: float) -> dict:
    """逐条合成。失败条目占位为 node_id=None:调用方按位置对齐,不能让后面的条目前移。"""
    items: list[dict] = []
    errors: list[str] = []
    for req in args.get("requests") or []:
        voice = {"voice_id": req.get("voice_id"), "speed": req.get("speed", 1),
                 "vol": req.get("volume", 1), "pitch": config.TTS_PITCH}
        if req.get("emotion"):
            voice["emotion"] = req["emotion"]
        body = {
            "model": config.TTS_MODEL, "text": req["text"], "stream": False,
            "voice_setting": voice,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000,
                              "format": "mp3", "channel": 1},
            "output_format": "url",
        }
        try:
            node = _audio_node(request("POST", "/v1/t2a_v2", tool="text_to_audio",
                                       json_body=body, timeout=timeout))
        except ConnectorError as exc:
            node, errors = None, errors + [exc.message]
        items.append({"node_id": node, "file_name": req.get("output_file", "")})
    if not any(i["node_id"] for i in items) and errors:
        raise ConnectorError("text_to_audio", errors[0])
    return {"success_items": items}


def music_batch(args: dict, timeout: float) -> dict:
    items = []
    for req in args.get("requests") or []:
        body = {"model": config.MUSIC_MODEL, "prompt": req["prompt"],
                "is_instrumental": not req.get("lyrics"), "output_format": "url",
                "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"}}
        if req.get("lyrics"):
            body["lyrics"] = req["lyrics"]
        data = request("POST", "/v1/music_generation", tool="text_to_music",
                       json_body=body, timeout=timeout, retries=0)
        items.append({"node_id": _audio_node(data), "file_name": req.get("output_file", "")})
    return {"success_items": items}


def generate_image(args: dict, timeout: float) -> dict:
    items = []
    for req in args.get("requests") or []:
        body = {"model": config.IMAGE_MODEL, "prompt": req["prompt"][:1500],
                "response_format": "url", "n": 1}
        if req.get("aspect_ratio"):
            body["aspect_ratio"] = req["aspect_ratio"]
        data = request("POST", "/v1/image_generation", tool="generate_image",
                       json_body=body, timeout=timeout, retries=0)
        urls = (data.get("data") or {}).get("image_urls") or []
        items.append({"node_id": "url:" + urls[0] if urls else None})
    return {"success_items": items}


def voice_list(args: dict, timeout: float) -> dict:
    data = request("POST", "/v1/get_voice", tool="get_voice_list",
                   json_body={"voice_type": "system"}, timeout=timeout)
    return {"available_voices": data.get("system_voice") or []}


# --------------------------------------------------------------------------
# 理解
# --------------------------------------------------------------------------
def vlm(image_url: str, prompt: str, timeout: float) -> str:
    """M Plan 的图片理解。只收 data URI,外链先下载再编码。"""
    if not image_url.startswith("data:"):
        image_url = image_data_uri(_fetch_bytes(image_url))
    data = request("POST", "/v1/coding_plan/vlm", tool="describe_images",
                   json_body={"prompt": prompt, "image_url": image_url}, timeout=timeout)
    return str(data.get("content") or "")


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


def parse_search(data: dict) -> list[dict]:
    """网络搜索返回 → 新闻形状的条目。只留正文有用的几个字段,省 token。"""
    rows = data.get("organic") or data.get("results") or data.get("data") or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        out.append({k: r[k] for k in ("title", "snippet", "content", "date", "link", "url")
                    if r.get(k)})
    return out


def web_search_news(args: dict, timeout: float) -> dict:
    query = str(args.get("query", "")).strip()
    if not query:
        raise ConnectorError("search_news", "查询串为空")
    data = request("POST", "/v1/coding_plan/search", tool="web_search",
                   json_body={"q": query}, timeout=timeout)
    rows = parse_search(data)
    if not rows:
        raise ConnectorError("search_news", f"网络搜索无结果:{json.dumps(data, ensure_ascii=False)[:200]}")
    return {"data": rows[: int(args.get("size") or 10)]}


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------
HANDLERS: dict[str, Callable[[dict, float], Any]] = {
    "connector__matrix__batch_text_to_audio": tts_batch,
    "connector__matrix__batch_text_to_music": music_batch,
    "connector__matrix__generate_image": generate_image,
    "connector__matrix__get_voice_list": voice_list,
    "connector__matrix__describe_images": describe_images,
    "connector__matrix__search_images": search_images,
    "connector__thsifind__search_news": web_search_news,
}


def source_label(tool: str) -> str:
    """事实来源的真实名字:直连时 search_news 实际是网络搜索。"""
    return WEB_SEARCH_LABEL if tool == "connector__thsifind__search_news" else tool


def call(tool: str, args: dict, *, timeout: float) -> Any:
    handler = HANDLERS.get(tool)
    if handler is None:
        raise _Unsupported(tool, "直连模式不支持该工具(同花顺 / 企查查 / 语音识别仍需经 MiniMax Code)")
    return handler(args, timeout)


def health_check() -> tuple[bool, str]:
    try:
        voices = voice_list({}, 30)["available_voices"]
    except ConnectorError as exc:
        return False, f"直连 MiniMax 失败:{exc.message}"
    return True, f"直连 MiniMax 开放平台({config.MINIMAX_API_HOST}),系统音色 {len(voices)} 个"
