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

#: 旁白只走独立 TTS(数据库仍有 narration_mode 这一列)
NarrationMode = Literal["tts"]
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
    #: 口径 / 版本 / 年份(如 "CLTC · 标准版 · 2026款"),上屏时跟着数字走
    basis: str = ""
    #: 来源类型:official=官方发布/官网,media=媒体报道,derived=由其它事实推算
    source_kind: Literal["official", "media", "derived", ""] = ""
    #: 多车横评:这条事实属于车单里哪台车;空串 = 单车项目或全片共用
    subject: str = ""
    #: **对比事实**:这条事实讲的是本车与 `compare_with` 这台车的同一维度取值。
    #: 非空 = 它是竞品对比段专用的料,只能出现在竞品对比段,
    #: 混进本车的参数面板就是把别家的数字安到本车头上。
    compare_with: str = ""
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
    #: 多车横评:这一段讲车单里的哪台车;空串 = 单车项目或开场 / 结论这类共用段
    subject: str = ""


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
    #: 分镜时从素材池点名的实拍素材(`library.Item.id`)。非空时取材先用它,
    #: visual_zh 描述的就是这段实拍;失效了再按画面描述挑
    pool_id: str = ""
    # ---- 以下只有横屏解说轨(track=explainer)用,见 pipeline/explainer.py ----
    #: 版式:broll / panel / highlight / card / stat
    layout: str = ""
    #: 叠加层数据(核对过事实):panel = {kind, title, rows, source};
    #: highlight = {kind, lines}
    overlay: dict | None = None
    #: 底部章节条上的章节名
    chapter: str = ""
    #: 背景画面偏好:exterior / interior / any —— 只影响排序,不做硬过滤
    view: str = ""
    #: 多车横评:这一拍讲哪台车(背景素材、叠加层车名、事实核对都按它);空串 = 共用拍
    subject: str = ""


class Shot(BaseModel):
    """镜头规格 + 运行态。

    解说轨的画面是**本地渲染**的(ffmpeg / PIL),没有"提交给生成服务再轮询"
    这一步,所以没有 task_id、没有提交时间、没有链序号 —— 那些是已删除的
    竖屏生成链路留下的。`shots` 表里对应的列**保留**(删列要重建表,对本地
    数据有风险),但 Python 侧不再有它们的语义。
    """

    spec: ShotSpec
    status: ShotStatus = "pending"
    #: 生成档位: draft / final
    stage: Literal["draft", "final"] = "final"
    model: str = ""
    draft_path: str | None = None
    final_path: str | None = None
    #: 重试次数(已达上限则不再自动重试)
    attempts: int = 0
    error: str | None = None
    #: 画面来源:real = 素材池实拍背景;card = 数据卡片,由代码直接渲染;ok = 还没渲染
    consistency: Consistency = "ok"
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
# 项目
# --------------------------------------------------------------------------
class SubjectEntry(BaseModel):
    """多车横评车单里的一台车。"""

    name: str = Field(min_length=1, max_length=40, description="车型名,如 '小米YU7'")
    #: 分组标注(如价位段「10-15万」),只进车名旁的标注和脚本提示,不分章
    group: str = Field(default="", max_length=20)


class ProjectCreate(BaseModel):
    subject: str = Field(min_length=1, description="选题/车型/产品型号,如 '比亚迪汉 EV 2026 款'")
    category: str = "auto_long"
    #: 内容方向名。为空时取品类默认方向(Profile 的 default_theme)。
    theme: str | None = Field(default=None, description="内容方向,如 'iteration'")
    #: 目标时长(秒)。成片跟着旁白走,这只是目标
    duration: int = 240
    narration_mode: NarrationMode = "tts"
    quota_budget: float | None = None
    title: str | None = None
    #: 选题角度(来自热点选题或手填),如「月销破 3 万背后:它到底赢在哪」。
    #: 只影响事实检索与脚本叙事;subject 仍是干净的车型名,素材池按它建。
    angle: str | None = Field(default=None, max_length=120)
    #: 热点选题附带的角度检索词(如「特斯拉 Model Y 召回 2026」);空 = 由角度原句提取
    angle_search: str | None = Field(default=None, max_length=60)
    #: 热点选题依据的热点原文行,作为一份事实素材进入抽取
    angle_evidence: list[str] | None = Field(default=None, max_length=12)
    #: 多车横评的车单(方向为 roundup 时必填);此时 subject 是选题名
    subjects: list[SubjectEntry] | None = Field(default=None, max_length=8)


class ScriptPatch(BaseModel):
    segments: list[ScriptSegment] | None = None
    title: str | None = None
    #: 全片主论点(那句判断)。改了它,结论段要跟着回扣。
    thesis: str | None = None


class ShotPatch(BaseModel):
    shots: list[ShotSpec]


class GateRequest(BaseModel):
    gate: Literal["A", "B"]
    #: 旧前端的逐镜头批准列表;门禁 B 现在一次审完全片,这个字段被忽略
    approved: list[int] | None = None
    #: 门禁 A:脚本里还有 lint error 时默认拦下,不放行去烧 TTS 和渲染的钱。
    #: 置 True = 明知有问题仍然继续(需要显式给出,不能默认放行)。
    force: bool = False


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
    #: 选题角度;空串 = 无
    angle: str = ""
    #: 多车横评车单;单车项目为空
    subjects: list[SubjectEntry] = Field(default_factory=list)
    duration: int
    shot_count: int
    state: str
    #: 停在这个状态的原因(配额暂停 / 失败时写明卡在哪)。列表页要显示它 ——
    #: 只说"失败了"不说是为什么,等于让人自己去看日志。
    state_reason: str = ""
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
    #: 全片主论点(那句判断),门禁 A 页面可改
    thesis: str = ""
    #: 全片共享参考图集条目:{url, angle, origin, note}


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
