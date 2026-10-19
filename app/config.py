"""全局配置与运行期路径解析。

设计要点:
- ffmpeg 不在本机系统 PATH,必须显式解析,解析失败要抛出可读错误而不是静默降级。
- 所有密钥只从本地 .env 读取,绝不写入任何日志或返回体。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROJECTS_DIR = DATA_DIR / "projects"
CATEGORIES_DIR = Path(__file__).resolve().parent / "categories"

# 首次导入时加载 .env(若存在)。已存在的真实环境变量优先。
load_dotenv(BASE_DIR / ".env")


# --------------------------------------------------------------------------
# 视频模型常量(契约见计划 §2.3)
# --------------------------------------------------------------------------
#: 镜头时间栅格。6 秒同时满足 Hailuo-2.3(仅 6/10 秒)与 H3(4-15 秒),
#: 是草稿档与成片档唯一通用的片长。
SHOT_GRID_SECONDS = 6

#: 可选成片时长(秒)与其对应镜头数。
#: 240 秒 = 40 镜头,与 180 秒同为 2 个一致性波次,成片档墙钟相同 ——
#: 多出的 10 个镜头不增加等待时间,只多消耗等效用量。
SUPPORTED_DURATIONS: tuple[int, ...] = (60, 90, 120, 180, 240)

DRAFT_MODEL = os.getenv("DRAFT_MODEL", "MiniMax-Hailuo-2.3")
FINAL_MODEL = os.getenv("FINAL_MODEL", "MiniMax-H3")
FINAL_RESOLUTION = os.getenv("FINAL_RESOLUTION", "768P")

ASPECT_RATIO = "9:16"
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

#: 全片共享参考图集上限。H3 前 5 张参考图免费,品类 Profile 可覆盖此值。
CATEGORY_REF_LIMIT = 5

#: 是否允许用网上抓来的真实视频片段替换部分成片镜头。
#:
#: 做成开关而不是写死,是因为它会引入**网络抓取**这一类新的外部依赖 ——
#: 有人只想跑纯生成,或者网络环境不通,需要一个明确的"关"的位置。
#: 关掉时整条流水线行为与引入本特性之前完全一致。
USE_REAL_CLIPS = os.getenv("USE_REAL_CLIPS", "1") not in ("0", "false", "False")

#: 每个项目最多用多少条真实片段。
REAL_CLIP_LIMIT = int(os.getenv("REAL_CLIP_LIMIT", "4"))

#: 一致性链分组大小:每链 N 个镜头,链内串行、链间并行。
CHAIN_GROUP_SIZE = 3

#: 门禁 B 分批审片:每批放行几条一致性链。
#: 批次 = 连续若干条链,按链切分保证不会把一条链劈成两半(链内首帧依赖)。
#: 40 镜头 / 3 = 14 链 → 4 批,每批 12 镜头。
GATE_B_BATCH_CHAINS = int(os.getenv("GATE_B_BATCH_CHAINS", "4"))

#: 只生成前 N 个镜头(0 = 全部)。用于链路冒烟测试:
#: 先跑通一整条一致性链(通常正好 CHAIN_GROUP_SIZE 个镜头),
#: 确认"生成 → 质检 → 拼接 → 字幕 → 混音"全程无误,再放开跑整片。
SHOT_LIMIT = int(os.getenv("SHOT_LIMIT", "0"))

#: H3 支持的画幅比例(竖屏 9:16 在列)。
SUPPORTED_RATIOS = ("adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def shots_for_duration(duration: int) -> int:
    """按 6 秒栅格计算镜头数。"""
    if duration % SHOT_GRID_SECONDS != 0:
        raise ValueError(f"时长必须是 {SHOT_GRID_SECONDS} 的整数倍,收到 {duration}")
    return duration // SHOT_GRID_SECONDS


# --------------------------------------------------------------------------
# 并发与配额
# --------------------------------------------------------------------------
GEN_MAX_CONCURRENCY = int(os.getenv("GEN_MAX_CONCURRENCY", "8"))
MAX_RETRY_DRAFT = int(os.getenv("MAX_RETRY_DRAFT", "2"))
MAX_RETRY_FINAL = int(os.getenv("MAX_RETRY_FINAL", "2"))
DEFAULT_QUOTA_BUDGET = float(os.getenv("DEFAULT_QUOTA_BUDGET", "40"))
PLAN_EXPIRES_ON = os.getenv("PLAN_EXPIRES_ON", "2026-10-31")

#: H3 长任务轮询参数(计划 §4.5)
POLL_INTERVAL_SLOW = 60      # H3:15-30 分钟
POLL_INTERVAL_FAST = 20      # H3-Max / Hailuo-2.3
POLL_TIMEOUT_SECONDS = 7200  # 2 小时后标记超时


# --------------------------------------------------------------------------
# 字幕与音频
# --------------------------------------------------------------------------
#: 单条 TTS 文本的最大汉字数。过长会导致语调疲劳与断句错误。
MAX_TTS_CHARS = 30
#: 句间停顿(毫秒)
SENTENCE_GAP_MS = 300
#: ASS 字幕单行最大汉字数
MAX_SUBTITLE_LINE_CHARS = 16
#: 成片响度目标
TARGET_LUFS = -14


# --------------------------------------------------------------------------
# ffmpeg 解析
# --------------------------------------------------------------------------
def _resolve_binary(env_key: str, default_dir: str | None, exe: str) -> Path | None:
    """按 环境变量 → 默认目录 → 系统 PATH 的顺序解析二进制路径。"""
    configured = os.getenv(env_key)
    if configured:
        candidate = Path(configured).expanduser()
        # 允许直接给目录,也可直接给 exe 全路径
        if candidate.is_dir():
            candidate = candidate / exe
        if candidate.is_file():
            return candidate
        found = shutil.which(configured)
        return Path(found) if found else None

    if default_dir:
        candidate = Path(default_dir) / exe
        if candidate.is_file():
            return candidate

    found = shutil.which(exe)
    return Path(found) if found else None


#: 本机 ffmpeg 安装位置(不在系统 PATH)。
_DEFAULT_FFMPEG_DIR = r"C:\ffmpeg\bin"

FFMPEG_PATH = _resolve_binary("FFMPEG_BIN", _DEFAULT_FFMPEG_DIR, "ffmpeg.exe")
FFPROBE_PATH = _resolve_binary("FFMPEG_BIN", _DEFAULT_FFMPEG_DIR, "ffprobe.exe")
FONTS_DIR = os.getenv("FFMPEG_FONTS_DIR", r"C:\Windows\Fonts")


def ffmpeg_status() -> tuple[bool, str]:
    """返回 (是否可用, 人类可读说明)。用于启动自检与 Web 横幅。"""
    if FFMPEG_PATH and FFMPEG_PATH.is_file():
        probe = "ffprobe 可用" if (FFPROBE_PATH and FFPROBE_PATH.is_file()) else "ffprobe 缺失(时长探测将退化)"
        return True, f"ffmpeg: {FFMPEG_PATH}({probe})"
    checked = [env for env in ("FFMPEG_BIN", _DEFAULT_FFMPEG_DIR) if env]
    return False, (
        "未找到 ffmpeg。已尝试 .env 中的 FFMPEG_BIN、默认目录 "
        + _DEFAULT_FFMPEG_DIR
        + " 与系统 PATH。请在 .env 中显式配置 FFMPEG_BIN。"
    )


# --------------------------------------------------------------------------
# 生成通道
# --------------------------------------------------------------------------
MCODE_TOOLS = os.getenv("MCODE_TOOLS") or shutil.which("mcode-tools") or ""
#: Connector 调用的默认超时(视频提交快,查询快;生图/检索较慢)
CONNECTOR_TIMEOUT_DEFAULT = 180
CONNECTOR_TIMEOUT_GENERATE = 600


def deepseek_api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def deepseek_base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")


def deepseek_model() -> str:
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "auto")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def project_dir(project_id: str) -> Path:
    """返回项目工作目录,不存在则创建。"""
    safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError(f"非法 project_id: {project_id!r}")
    path = PROJECTS_DIR / safe
    path.mkdir(parents=True, exist_ok=True)
    (path / "shots").mkdir(exist_ok=True)
    (path / "drafts").mkdir(exist_ok=True)
    (path / "refs").mkdir(exist_ok=True)
    (path / "audio").mkdir(exist_ok=True)
    (path / "prompts").mkdir(exist_ok=True)
    (path / "facts_raw").mkdir(exist_ok=True)
    return path
