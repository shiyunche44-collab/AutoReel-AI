"""视频生成 Provider(计划 §2.3 的模型契约)。

两个模型的能力差异在这里被强制收敛,上层调度器不需要关心:
- `MiniMax-Hailuo-2.3`:仅 6/10 秒、静音、无多模态参考 —— 草稿档。
- `MiniMax-H3`:4-15 秒、768P/2K、支持首/尾帧与参考图、原生音频 —— 成片档。
- `MiniMax-H3-Max`:5-15 秒、480P/768P、仅首/尾帧、无参考图、原生音频。

**提交即持久化**是本模块最重要的契约:`submit()` 返回的 task_id 必须立刻落库,
否则 H3 的 15-30 分钟等待在进程重启后会丢失,导致重复提交与重复消耗配额。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from . import cli

SUBMIT_TOOL = "connector__matrix__submit_video_generation"
QUERY_TOOL = "connector__matrix__query_video_generation"


class Model(str, Enum):
    H3 = "MiniMax-H3"
    H3_MAX = "MiniMax-H3-Max"
    HAILUO_23 = "MiniMax-Hailuo-2.3"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    min_duration: int
    max_duration: int
    #: 允许的时长白名单,None 表示区间内任意整数
    allowed_durations: tuple[int, ...] | None = None
    resolutions: tuple[str, ...] = ("768P",)
    #: 是否支持 reference_images(多模态参考)
    supports_reference_images: bool = False
    #: 是否支持尾帧
    supports_last_frame: bool = False
    #: 是否输出原生音频
    native_audio: bool = True
    #: 是否支持指定画幅。Hailuo-2.3 **不支持**:它的文生视频固定 16:9,
    #: 只有走首帧图模式才跟随输入图画幅。所以草稿档拿回来的必然是横屏,
    #: 竖屏要靠渲染层裁切 —— 这是模型限制,不是配置错误。
    supports_ratio: bool = True
    #: 是否支持 image-to-video(首帧图输入)。
    #: 注意这是**实测**结论而非文档结论:MiniMax-Hailuo-2.3 的 schema
    #: 明写着支持 input_image + reference_type=first_frame,但实际提交
    #: 一律 connector_tool_error(latency_ms=0),纯文生视频才正常。
    #: 文档与实现不一致时以实测为准,否则每个带图的镜头都会白等一次失败,
    #: 而失败信息还完全看不出是图的问题。
    supports_image_input: bool = True


MODEL_SPECS: dict[str, ModelSpec] = {
    Model.H3.value: ModelSpec(
        Model.H3.value, 4, 15, None, ("768P", "2K"), True, True, True
    ),
    Model.H3_MAX.value: ModelSpec(
        Model.H3_MAX.value, 5, 15, None, ("480P", "768P"), False, True, True
    ),
    Model.HAILUO_23.value: ModelSpec(
        Model.HAILUO_23.value, 6, 6, (6, 10), ("768P", "1080P"),
        False, False, False, supports_ratio=False, supports_image_input=False,
    ),
}

#: 各模型轮询间隔(秒)。H3 慢,快模型高频轮询没意义。
POLL_INTERVAL = {
    Model.H3.value: 60,
    Model.H3_MAX.value: 20,
    Model.HAILUO_23.value: 20,
}


class VideoValidationError(ValueError):
    """请求参数不满足模型契约。**在提交前拦截,避免产生计费任务。**"""


def validate(model: str, *, duration: int, resolution: str, ref_count: int = 0,
             has_last_frame: bool = False) -> None:
    spec = MODEL_SPECS.get(model)
    if spec is None:
        raise VideoValidationError(f"未知模型: {model}")
    if spec.allowed_durations is not None:
        if duration not in spec.allowed_durations:
            raise VideoValidationError(
                f"{model} 仅支持 {spec.allowed_durations} 秒,收到 {duration}"
            )
    elif not (spec.min_duration <= duration <= spec.max_duration):
        raise VideoValidationError(
            f"{model} 时长需在 {spec.min_duration}-{spec.max_duration} 秒之间,收到 {duration}"
        )
    if resolution not in spec.resolutions:
        raise VideoValidationError(
            f"{model} 不支持分辨率 {resolution},可选 {spec.resolutions}"
        )
    if ref_count and not spec.supports_reference_images:
        raise VideoValidationError(
            f"{model} 不支持多模态参考图,请改用 {Model.H3.value} 或改用首帧模式"
        )
    if has_last_frame and not spec.supports_last_frame:
        raise VideoValidationError(f"{model} 不支持尾帧输入")


def native_audio(model: str) -> bool:
    spec = MODEL_SPECS.get(model)
    return bool(spec and spec.native_audio)


def supports_reference_images(model: str) -> bool:
    """该模型能否吃多模态参考图。

    调用方必须先问这个再决定是否挂参考图 —— 直接传了再让 validate 报错,
    表现出来是"镜头直接失败",而根因只是模型不支持,本可以退化成首帧模式。
    """
    spec = MODEL_SPECS.get(model)
    return bool(spec and spec.supports_reference_images)


def supports_last_frame(model: str) -> bool:
    spec = MODEL_SPECS.get(model)
    return bool(spec and spec.supports_last_frame)


def supports_ratio(model: str) -> bool:
    """该模型能否指定画幅。False 表示 ratio 参数会被上游忽略。"""
    spec = MODEL_SPECS.get(model)
    return bool(spec and spec.supports_ratio)


def supports_image_input(model: str) -> bool:
    """该模型能否做图生视频(首帧图输入)。以实测为准,不信 schema。"""
    spec = MODEL_SPECS.get(model)
    return bool(spec and spec.supports_image_input)


# --------------------------------------------------------------------------
# 提交
# --------------------------------------------------------------------------
def _as_url(path_or_url: str | Path | None) -> str | None:
    """生成接口只接受 URL。本地文件需先上传。"""
    if path_or_url is None:
        return None
    text = str(path_or_url)
    if text.startswith(("http://", "https://")):
        return text
    return cli.upload_temp_url(text)


def _extract_task_id(result: Any) -> str:
    """从提交结果中取出 task_id(信封形态在不同工具间略有差异)。"""
    if isinstance(result, str):
        return result
    if isinstance(result, list) and result:
        result = result[0]
    if isinstance(result, dict):
        for key in ("task_id", "taskId", "id"):
            if result.get(key):
                return str(result[key])
        # 兼容 {success_items:[{task_id:...}]} 这类嵌套
        for key in ("success_items", "data", "result"):
            nested = result.get(key)
            if isinstance(nested, list) and nested:
                return _extract_task_id(nested[0])
            if isinstance(nested, dict):
                return _extract_task_id(nested)
    raise cli.ConnectorError(SUBMIT_TOOL, f"提交结果中没有 task_id: {result}")


def submit(
    *,
    model: str,
    prompt: str,
    duration: int,
    resolution: str,
    ratio: str = "9:16",
    first_frame: str | Path | None = None,
    last_frame: str | Path | None = None,
    reference_images: list[str | Path] | None = None,
    timeout: int | None = None,
) -> str:
    """提交一条视频生成任务,返回 task_id。

    调用前务必已经 `validate()` 过 —— 格式错误的请求可能照样渲染并计费。
    """
    refs = reference_images or []
    validate(
        model,
        duration=duration,
        resolution=resolution,
        ref_count=len(refs),
        has_last_frame=last_frame is not None,
    )

    args: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
    }
    # 不支持画幅的模型(Hailuo-2.3)传了也会被上游忽略,
    # 干脆不传,免得日志里出现"我设了 9:16 却拿到 16:9"的假象。
    if supports_ratio(model):
        args["ratio"] = ratio
    first_url = _as_url(first_frame)
    last_url = _as_url(last_frame)
    if first_url and not supports_image_input(model):
        # 实测 Hailuo-2.3 收不了图。与其提交一次注定失败的请求
        # (上游只回一句 latency_ms=0,看不出是图的问题),不如直接降级成文生视频。
        first_url = None
    if first_url:
        args["input_image"] = first_url
    if last_url:
        args["last_frame_image"] = last_url
    if first_url and not last_url:
        # schema 明确:input_image 要配合 reference_type 使用才是"关键帧模式"。
        # 不传 reference_type 时,上游可能把图当普通参考图而不是首帧,
        # 一致性链的"上一镜尾帧 → 下一镜首帧"就悄悄失效了。
        #
        # 只在**没有**同时给尾帧时设置:枚举只有 first_frame / last_frame /
        # subject,没有 first_last_frame;首尾帧模式是由 last_frame_image
        # 的存在触发的,此时不应再塞一个 reference_type 进去。
        args["reference_type"] = "first_frame"
    ref_urls = [u for u in (_as_url(p) for p in refs) if u]
    if ref_urls:
        args["reference_images"] = ref_urls

    result = cli.call(
        SUBMIT_TOOL, args, timeout=timeout or cli.config.CONNECTOR_TIMEOUT_GENERATE
    )
    return _extract_task_id(result)


# --------------------------------------------------------------------------
# 轮询
# --------------------------------------------------------------------------
@dataclass
class VideoStatus:
    status: str            # queued / running / succeeded / failed / cancelled
    video_url: str | None = None
    file_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")

    @property
    def is_success(self) -> bool:
        return self.status == "succeeded"


def query(task_id: str, model: str, *, timeout: int = 60) -> VideoStatus:
    result = cli.call(QUERY_TOOL, {"task_id": task_id, "model": model}, timeout=timeout)
    if isinstance(result, list) and result:
        result = result[0]
    if not isinstance(result, dict):
        return VideoStatus(status="unknown", raw={"value": result})

    status = str(result.get("status", "unknown")).lower()
    error = result.get("failure_reason") or result.get("error") or result.get("message")
    return VideoStatus(
        status=status,
        video_url=result.get("video_url") or result.get("url"),
        file_id=result.get("file_id"),
        error=str(error) if error else None,
        raw=result,
    )


def poll_until_settled(
    task_id: str,
    model: str,
    *,
    timeout_seconds: int,
    sleep_fn=time.sleep,
    on_poll=None,
) -> VideoStatus:
    """阻塞轮询直到任务终态或超时。**只用于测试与草稿档**,成片档应由后台调度器驱动。"""
    interval = POLL_INTERVAL.get(model, 30)
    deadline = time.time() + timeout_seconds
    while True:
        status = query(task_id, model)
        if on_poll:
            on_poll(status)
        if status.is_terminal:
            return status
        if time.time() >= deadline:
            return VideoStatus(status="timeout", error=f"轮询超过 {timeout_seconds}s")
        sleep_fn(interval)


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
def download(video_url: str, dest: str | Path) -> Path:
    """把生成的视频下载到本地。

    provider 的下载链接是短时有效的,因此成功后必须立即落盘,
    不能等到渲染阶段再去取。
    """
    import httpx

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", video_url, timeout=300, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                fh.write(chunk)
    if dest.stat().st_size == 0:
        raise cli.ConnectorError("download", f"下载到空文件: {dest}")
    return dest
