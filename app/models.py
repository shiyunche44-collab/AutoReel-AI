"""领域模型。

分层原则:**规格(Spec)与运行态(Status)分离**。
- `ScriptSegment` / `ShotSpec` 是 LLM 产出的、可被用户编辑的"意图"层。
- `Shot` 在 `ShotSpec` 之上叠加生成过程状态(task_id、产物路径、重试次数),供调度器与 UI 使用。
这样用户在门禁 A 修改分镜时,不会连带抹掉已经跑出来的生成结果。
"""
from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

NarrationMode = Literal["tts", "native"]
ShotStatus = Literal[
    "pending",      # 尚未提交
    "submitted",    # 已提交,等待 provider 返回
    "queued",       # provider 排队中
    "running",      # provider 生成中
    "succeeded",    # 已成功并落盘
    "failed",       # 失败
    "skipped",      # 被跳过(如用户标记不需要)
]
Consistency = Literal["ok", "degraded", "real", "card"]


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}{raw}" if prefix else raw


# --------------------------------------------------------------------------
# 事实层
# --------------------------------------------------------------------------
class Fact(BaseModel):
    """一条可追溯的事实。脚本中每个数字型陈述都必须指向其中之一。"""

    id: str = Field(default_factory=lambda: new_id("f_"))
    claim: str
    source_tool: str
    source_query: str = ""
    retrieved_at: str = Field(default_factory=lambda: time.strftime("%Y-%m-%d"))
    confidence: Literal["high", "medium", "low"] = "medium"
    #: 原始返回落盘位置(相对项目目录),便于人工复核
    raw_ref: str | None = None
    #: 事实主题标签,便于脚本按主题挑选
    topic: str = ""
    #: 事实的**层级**:company=公司工商/财务,product=产品参数,industry=行业
    #: 二者不可混用 —— 用公司注册地址去讲产品卖点是解说视频的典型翻车方式。
    scope: Literal["company", "product", "industry"] = "company"
    #: 用户是否保留(可在 Web 端剔除)
    enabled: bool = True


# --------------------------------------------------------------------------
# 脚本层
# --------------------------------------------------------------------------
class ScriptSegment(BaseModel):
    idx: int
    #: 段落名,如 "开场钩子" / "核心卖点" / "购买建议"
    section: str = ""
    text: str
    #: 引用的 fact id 列表。数字型陈述必须有引用,否则合规 lint 拦截。
    fact_refs: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 分镜层
# --------------------------------------------------------------------------
class ShotSpec(BaseModel):
    """单个镜头的创作规格(用户可在门禁 A 编辑)。"""

    idx: int
    duration: float = 6.0
    #: 所属结构段落
    section: str = ""
    #: 中文画面描述 —— 提示词编译时翻译为英文
    visual_zh: str
    #: 运镜(中文);编译器会归一到官方运镜词表
    camera_zh: str = ""
    #: 环境音(中文)
    ambience_zh: str = ""
    #: 该镜头对应的解说词/字幕文本
    narration_zh: str = ""
    #: 参考图视角标签,如 exterior_45 / interior / rear / detail
    angle: str = "exterior_45"
    #: 画面中可见的文字(如仪表读数、车型标识)。**缺失时用占位符,禁止臆造。**
    on_screen_text: list[str] = Field(default_factory=list)
    #: 数据卡片规格(核对过事实的参数 / 对比 / 时间线 / 柱状图)。非空时这个镜头
    #: 由代码渲染,不取材、不生成。形状见 `cards.normalise()`。
    card: dict | None = None


class Shot(BaseModel):
    """镜头规格 + 运行态。"""

    spec: ShotSpec
    status: ShotStatus = "pending"
    #: 生成档位: draft / final
    stage: Literal["draft", "final"] = "final"
    model: str = ""
    #: provider 返回的 task_id —— 提交即持久化,是断点续跑的关键
    provider_task_id: str | None = None
    submitted_at: float | None = None
    last_polled_at: float | None = None
    draft_path: str | None = None
    final_path: str | None = None
    #: 重试次数(已达上限则不再自动重试)
    attempts: int = 0
    error: str | None = None
    #: 一致性链状态:ok = 共享参考图 + 尾帧串联均生效;degraded = 降级;
    #: real = 该镜头用的是网上抓来的真实视频片段,没经过生成;
    #: card = 数据卡片,由代码直接渲染(见 pipeline/cards.py)
    consistency: Consistency = "ok"
    #: 所在链的序号与链内位置
    chain_id: int = 0
    chain_pos: int = 0
    #: 估算等效用量(美元,仅估算)
    est_cost: float = 0.0

    @property
    def idx(self) -> int:
        return self.spec.idx

    @property
    def artifact(self) -> str | None:
        return self.draft_path if self.stage == "draft" else self.final_path

    @property
    def done(self) -> bool:
        return self.status == "succeeded"


# --------------------------------------------------------------------------
# 提示词层
# --------------------------------------------------------------------------
class CompiledPrompt(BaseModel):
    shot_idx: int
    text: str
    #: 校验错误列表,空列表表示通过
    errors: list[str] = Field(default_factory=list)
    #: 编译是否走了 LLM(False = 走了确定性兜底模板)
    used_llm: bool = False
    #: I2VA 前置对齐行是否注入
    used_first_frame: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    subject: str = Field(min_length=1, description="选题/车型,如 '比亚迪汉 EV 2026 款'")
    category: str = "auto"
    #: 内容方向名。为空时取品类默认方向(Profile 的 default_theme)。
    theme: str | None = Field(default=None, description="内容方向,如 'iteration'")
    duration: int = 60
    narration_mode: NarrationMode = "tts"
    quota_budget: float | None = None
    title: str | None = None


class ScriptPatch(BaseModel):
    segments: list[ScriptSegment] | None = None
    title: str | None = None


class ShotPatch(BaseModel):
    shots: list[ShotSpec]


class GateRequest(BaseModel):
    gate: Literal["A", "B"]
    #: GATE_B 可携带逐镜头决定:approved 的镜头才进成片
    approved: list[int] | None = None


class RetryRequest(BaseModel):
    stage: Literal["draft", "final"] = "final"


class ProjectSummary(BaseModel):
    id: str
    title: str
    subject: str
    category: str
    #: 内容方向名;旧数据为空串,表示使用品类默认方向
    theme: str = ""
    #: 方向的中文标签,仅用于展示
    theme_label: str = ""
    duration: int
    shot_count: int
    state: str
    est_cost: float
    spent_cost: float
    quota_budget: float
    created_at: float
    updated_at: float
    has_output: bool


class ProjectDetail(ProjectSummary):
    narration_mode: str
    quota_budget: float
    state_reason: str | None = None
    facts: list[Fact] = Field(default_factory=list)
    script: list[ScriptSegment] = Field(default_factory=list)
    shots: list[Shot] = Field(default_factory=list)
    #: 全片共享参考图集条目:{url, angle, origin, note}
    refs: list[dict] = Field(default_factory=list)
    #: 真实素材报告:哪些镜头用了网上抓来的视频片段、来源是什么、省了多少。
    #: 空字典表示本项目没启用这条通路(老项目 / USE_REAL_CLIPS=0)。
    clip_report: dict = Field(default_factory=dict)
    prompt_errors: dict[str, list[str]] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# 其它
# --------------------------------------------------------------------------
class SubtitleCue(BaseModel):
    index: int
    start: float
    end: float
    text: str
    shot_idx: int


class JobEvent(BaseModel):
    id: int | None = None
    project_id: str
    created_at: float
    src: str | None = None
    dst: str
    reason: str = ""
    cost: float = 0.0
