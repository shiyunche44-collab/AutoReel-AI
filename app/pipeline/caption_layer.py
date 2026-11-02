"""花字字幕层:把字幕 cue 渲成带透明通道的 webm,成片时 overlay 到画面上。

轻包装(`looks.Look.sub_layer`)用它代替 ASS 字幕:关键词(数字 / 价格 / 车型代号)
变强调色并弹出放大,整句弹入。组件在 `remotion/src/Layer.jsx`,这里只负责
 - 把 cue 切成短句、标出关键词、写成 props(`build_layer`);
 - 调 Remotion CLI 渲染 vp8+alpha(`render_layer`,按内容哈希缓存)。

Remotion 不可用(没有 Node 22+ / 没装依赖 / 渲染失败)时返回 None,调用方退回 ASS 字幕,
不拦出片。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .. import config

_log = logging.getLogger(__name__)

REMOTION_DIR = Path(__file__).resolve().parents[2] / "remotion"
_CLI = Path("node_modules") / "@remotion" / "cli" / "remotion-cli.js"
#: 版本进缓存键:组件改了(升这个数)旧缓存作废
LAYER_VERSION = 1
#: 单条显示字幕的字数上限(对标中位约 9 字,留一点余量)
MAX_CHARS = 12
RENDER_TIMEOUT = int(os.getenv("REMOTION_TIMEOUT", "1800"))

#: 关键词:数字(带单位)、车型/配置代号(YU7、L2、800V)
_HOT = re.compile(
    r"\d[\d,.]*\s*(?:万元?|公里|千米|km|kWh|kW|kw|度|秒|分钟|马力|匹|牛米|N·m|英寸|mm|毫米|米|吨|kg|%|倍|个|颗|款|V|A|T)?"
    r"|[A-Za-z][A-Za-z0-9\-]*\d[A-Za-z0-9\-]*|[A-Z]{2,}"
)
_BREAKS = "，。！？；、,.!?;：:"


def split_cue(text: str, limit: int = MAX_CHARS) -> list[str]:
    """一句话切成不超过 `limit` 字的短句:先按标点切,仍超长的就近对半切。"""
    text = (text or "").strip()
    if not text:
        return []
    parts: list[str] = []
    buf = ""
    for i, ch in enumerate(text):
        # 小数点 / 千分位("3.23"、"1,200")不是断句点
        inside_number = (ch in ".,:" and 0 < i < len(text) - 1
                         and text[i - 1].isdigit() and text[i + 1].isdigit())
        if ch in _BREAKS and not inside_number:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    out: list[str] = []
    for p in parts:
        while len(p) > limit:
            n = -(-len(p) // -(-len(p) // limit))  # 均分,不留孤字
            out.append(p[:n])
            p = p[n:]
        out.append(p)
    return [p for p in out if p]


def mark_words(text: str) -> list[dict[str, Any]]:
    """一条短句 → [{text, hot, at}]:`at` 是该词在句内的字符起点占比(0~1),用来对齐弹出时间。"""
    words: list[dict[str, Any]] = []
    pos = 0
    n = max(1, len(text))
    for m in _HOT.finditer(text):
        if m.start() > pos:
            words.append({"text": text[pos:m.start()], "hot": False, "at": pos / n})
        words.append({"text": m.group(0), "hot": True, "at": m.start() / n})
        pos = m.end()
    if pos < len(text):
        words.append({"text": text[pos:], "hot": False, "at": pos / n})
    return words


def build_layer(cues: Sequence[Mapping[str, Any]], size: tuple[int, int], fps: int,
                accent: tuple[int, int, int], *, duration: float | None = None) -> dict[str, Any]:
    """字幕 cue(旁白每句一条)→ Remotion props。长句切短,时间按字数比例分。"""
    shown: list[dict[str, Any]] = []
    for cue in cues:
        start, end = float(cue["start"]), float(cue["end"])
        pieces = split_cue(str(cue.get("text") or ""))
        total = sum(len(p) for p in pieces)
        if not pieces or end <= start or total <= 0:
            continue
        char_t = cue.get("char_t")
        text = str(cue.get("text") or "")
        if not (char_t and len(char_t) == len(text)):
            char_t = None  # 没量过,或改字后长度对不上:按字数比例估
        pos = 0
        spans = []
        for p in pieces:
            at = text.find(p, pos)
            at = pos if at < 0 else at
            spans.append(at)
            pos = at + len(p)
        t = start
        for k, p in enumerate(pieces):
            d = (end - start) * len(p) / total
            if char_t:
                t = start + char_t[spans[k]]
                nxt = start + char_t[spans[k + 1]] if k + 1 < len(pieces) else end
                d = max(0.05, nxt - t)
            words = []
            for w in mark_words(p):
                if char_t:
                    ws = start + char_t[min(len(text) - 1, spans[k] + round(w["at"] * len(p)))]
                else:
                    ws = t + w["at"] * d
                words.append({**w, "start": round(max(t, ws), 3)})
            for w in words:
                w.pop("at")
            shown.append({"start": round(t, 3), "end": round(t + d, 3), "words": words})
            t += d
    end_all = max((c["end"] for c in shown), default=0.0)
    return {
        "width": size[0], "height": size[1], "fps": fps,
        "duration": round(duration if duration else end_all + 0.1, 3),
        "cues": shown,
        "style": {"accent": "#%02X%02X%02X" % tuple(accent)},
    }


def browser() -> str:
    """渲染用的浏览器:配置的那个还在就用它,否则退回本机 Chrome / Edge(.env 里的路径可能已失效)。"""
    for cand in (config.MOTION_BROWSER, *config._BROWSER_CANDIDATES):
        if cand and Path(cand).is_file():
            return str(cand)
    return ""


def available() -> tuple[bool, str]:
    """能不能渲:Node 22+、依赖装好了、有浏览器。返回 (可用, 说明)。"""
    if os.getenv("REMOTION_LAYER", "1").strip().lower() in ("0", "false", "off"):
        return False, "REMOTION_LAYER=0,已关闭"
    if not config.motion_node():
        return False, f"找不到 Node {config.MOTION_NODE_MIN}+"
    if not (REMOTION_DIR / _CLI).is_file():
        return False, "remotion/ 依赖没装:在 remotion/ 下 npm install"
    if not browser():
        return False, "未找到 Chrome / Edge,请在 .env 配 HYPERFRAMES_BROWSER_PATH"
    return True, "Remotion 可用"


def _cache_key(layer: Mapping[str, Any]) -> str:
    blob = json.dumps([LAYER_VERSION, layer], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _env() -> dict[str, str]:
    env = dict(os.environ, REMOTION_TELEMETRY="off", NO_COLOR="1")
    node = config.motion_node()
    if node:
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    if config.FFMPEG_PATH:
        env["PATH"] = str(config.FFMPEG_PATH.parent) + os.pathsep + env["PATH"]
    return env


def render_layer(layer: Mapping[str, Any], work_dir: Path) -> Path | None:
    """渲成 `work_dir/captions_<hash>.webm`(vp8 + alpha)。失败 / 不可用返回 None。"""
    if not layer.get("cues"):
        return None
    ok, why = available()
    if not ok:
        _log.info("花字层跳过:%s", why)
        return None
    work_dir.mkdir(parents=True, exist_ok=True)
    key = _cache_key(layer)
    out = work_dir / f"captions_{key}.webm"
    if out.is_file() and out.stat().st_size > 0:
        return out
    props = work_dir / f"captions_{key}.json"
    props.write_text(json.dumps(layer, ensure_ascii=False), encoding="utf-8")
    tmp = work_dir / f"captions_{key}.part.webm"
    cmd = [
        config.motion_node(), str(REMOTION_DIR / _CLI), "render", "src/index.jsx", "Layer", tmp.as_posix(),
        "--codec=vp8", "--pixel-format=yuva420p", "--image-format=png",
        f"--props={props.as_posix()}", f"--browser-executable={browser()}",
        "--log=error", "--overwrite",
    ]
    try:
        proc = subprocess.run(cmd, cwd=REMOTION_DIR, env=_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=RENDER_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("花字层渲染失败:%s", exc)
        tmp.unlink(missing_ok=True)
        return None
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        _log.warning("花字层渲染失败:%s", (proc.stderr or proc.stdout or "")[-300:])
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, out)
    return out
