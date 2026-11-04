"""第三方水印 / 烧录字的视觉复核:补 `logo_detect` 看不见的那一类。

`logo_detect` 只认四个角里一动不动的高对比边缘。别家账号常见的半透明账号名水印
(例:画面右下偏上的「大虾试车真香」)、烧在画面中间的解说字,边缘弱或不在角里,它查不出来。
这里抽几帧让视觉模型指出来,框的结果交给 `logo_detect.crop_for` 判断裁不裁得掉。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

#: 每个源最多看几帧(从不同镜头取)
SAMPLE = 8
#: 至少这么多帧里出现同一处水印才认(单帧可能是画面里的招牌、车牌)
MIN_HITS = 2
#: 烧录字(画面中间的解说 / 标题条)出现在这么多成的帧里,整个源不要(0.25 时中文实拍源几乎全被拦,太严)
BURNED_RATIO = 0.6
#: 框的外扩余量(视觉模型给的框偏紧、偏移)
PAD = 0.03

PROMPT = (
    "这是一段汽车视频的一帧。找出画面里**不属于车和环境本身**的叠加内容,只依据图片回答:\n"
    "A. watermark:别的账号 / 频道 / 平台的水印、账号名、台标(常为半透明小字,可能在任何位置);\n"
    "B. burned_text:烧录在画面里的解说字幕、标题条、大号标语字。\n"
    "车身上的车标、车牌、屏幕里的车机界面、官方厂牌字样不算。\n"
    "只输出一个 JSON:{\"items\":[{\"kind\":\"watermark\"|\"burned_text\",\"text\":\"字面内容\","
    "\"box\":[x0,y0,x1,y1]}]},box 是占整幅画面的 0–1 比例,左上为原点;没有就输出 {\"items\":[]}。"
)


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0)).get("items") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for it in items:
        box = it.get("box") if isinstance(it, dict) else None
        if (it.get("kind") in ("watermark", "burned_text") and isinstance(box, list)
                and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            x0, y0, x1, y1 = (min(max(float(v), 0.0), 1.0) for v in box)
            if x1 > x0 and y1 > y0:
                out.append({"kind": it["kind"], "text": str(it.get("text") or ""),
                            "box": [x0, y0, x1, y1]})
    return out


def _union(boxes: Sequence[Sequence[float]]) -> list[float]:
    """同一处水印在各帧里的框取中位数再外扩:比并集稳,不被一帧的偏差撑大。"""
    def med(vals):
        vals = sorted(vals)
        return vals[len(vals) // 2]
    x0, y0, x1, y1 = (med([b[i] for b in boxes]) for i in range(4))
    return [round(max(0.0, x0 - PAD), 4), round(max(0.0, y0 - PAD), 4),
            round(min(1.0, x1 + PAD), 4), round(min(1.0, y1 + PAD), 4)]


def scan(frames: Sequence[str | Path], timeout: float = 90.0) -> dict:
    """看一个源的若干帧。返回 {"boxes": [水印框…], "burned": 带烧录字的帧占比, "texts": […], "frames": N}。
    某一帧调用失败就跳过那一帧;全失败 → frames=0,调用方当"没结论"。"""
    from ..providers import direct

    boxes: list[list[float]] = []
    texts: list[str] = []
    burned = looked = 0
    for f in list(frames)[:SAMPLE]:
        try:
            uri = direct.image_data_uri(Path(f).read_bytes())
            items = _parse(direct.vlm(uri, PROMPT, timeout))
        except Exception as exc:  # noqa: BLE001 - 单帧失败不阻断入池
            log.warning("水印复核:%s 失败:%s", Path(f).name, exc)
            continue
        looked += 1
        if any(i["kind"] == "burned_text" for i in items):
            burned += 1
        for i in items:
            if i["kind"] == "watermark":
                boxes.append(i["box"])
                texts.append(i["text"])
    if looked == 0:
        return {"boxes": [], "burned": 0.0, "texts": [], "frames": 0}
    return {"boxes": _cluster(boxes), "burned": burned / looked, "texts": sorted(set(texts)),
            "frames": looked}


def _cluster(boxes: Sequence[Sequence[float]]) -> list[list[float]]:
    """中心点相近的框归一处;出现不到 MIN_HITS 次的丢掉。"""
    groups: list[list[Sequence[float]]] = []
    for b in boxes:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        for g in groups:
            gx = sum((x[0] + x[2]) / 2 for x in g) / len(g)
            gy = sum((x[1] + x[3]) / 2 for x in g) / len(g)
            if abs(cx - gx) < 0.12 and abs(cy - gy) < 0.12:
                g.append(b)
                break
        else:
            groups.append([b])
    return [_union(g) for g in groups if len(g) >= MIN_HITS]
