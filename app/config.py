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
# 画面与时长
# --------------------------------------------------------------------------
#: 静图做成推拉镜头、或缺省片长时用的秒数
SHOT_GRID_SECONDS = 6

#: 品类没声明 durations 时的可选成片时长(秒)
SUPPORTED_DURATIONS: tuple[int, ...] = (120, 240, 360, 480)

#: 归一化片段的缺省画幅(品类自己的 frame_size 优先)
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920

#: 按关键词搜索视频的平台,按优先级排列。可选 bilibili / youtube / douyin / tiktok。
#: douyin(含西瓜)没有关键词搜索接口,只能靠 Bing 站内检索拿链接 —— 而 cn.bing
#: 会无视 `site:`,实测一条都搜不到(小米 YU7),白跑一趟,所以默认不开。
#: 抖音素材请手动贴链接进素材池(`library add`)。
#: tiktok 主通路是品牌官方号主页(名单在品类 YAML 的 `official_sites.*.tiktok`),
#: 关键词检索借 DuckDuckGo,遇到人机验证就跳过。国内需要 YTDLP_PROXY。
MEDIA_PLATFORMS: tuple[str, ...] = tuple(
    p.strip().lower()
    for p in os.getenv("MEDIA_PLATFORMS", "bilibili,youtube,tiktok").split(",")
    if p.strip()
)

#: yt-dlp 走的代理(国内访问 YouTube 需要),如 http://127.0.0.1:7890 。
#: 只给 yt-dlp 用:B 站、车媒站点直连更快,不该被代理拖慢。
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()

#: Netscape 格式 cookie 文件(浏览器插件导出)。B 站高清、抖音下载需要。
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES", "").strip()

#: 素材池(`pipeline/library.py`):按车型预先攒素材、按剪辑点切段、打视角标签,
#: 取材时最先从池子里挑。分镜阶段在后台增量构建,不拖住门禁确认。
USE_LIBRARY = os.getenv("USE_LIBRARY", "1") not in ("0", "false", "False")
#: 后台构建一次的时长预算(秒);超时停在当前进度,下次接着做。0 不限
LIBRARY_BUILD_BUDGET = int(os.getenv("LIBRARY_BUILD_BUDGET", "1800"))

# --------------------------------------------------------------------------
# 配额
# --------------------------------------------------------------------------
DEFAULT_QUOTA_BUDGET = float(os.getenv("DEFAULT_QUOTA_BUDGET", "40"))
PLAN_EXPIRES_ON = os.getenv("PLAN_EXPIRES_ON", "2026-10-31")


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
# 概念动画(HyperFrames,见 pipeline/motion.py)
# --------------------------------------------------------------------------
#: motion/ 子工程:package.json 锁了 hyperframes 与 gsap 的版本,`npm install` 后可用
MOTION_DIR = Path(os.getenv("MOTION_DIR") or (BASE_DIR / "motion"))
#: 渲染用的 Chrome。HyperFrames 默认自己下载 chrome-headless-shell,国内常下载中断;
#: 任何本机 Chrome / Edge 都能走截图渲染路径。不填就按常见安装位置找
_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
MOTION_BROWSER = os.getenv("HYPERFRAMES_BROWSER_PATH") or next(
    (p for p in _BROWSER_CANDIDATES if Path(p).is_file()), "")
#: 单拍动画渲染的并发 worker 数(每个 worker 一个 Chrome 进程,约 256MB)
MOTION_WORKERS = int(os.getenv("MOTION_WORKERS", "4"))
#: 单次渲染的超时(秒)
MOTION_TIMEOUT = int(os.getenv("MOTION_TIMEOUT", "300"))


def motion_status() -> tuple[bool, str]:
    """概念动画能不能渲染。不能也不拦流水线:concept 拍退回数据卡。"""
    node = shutil.which("node")
    cli = MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
    if not node:
        return False, "未找到 node(需要 22+),概念动画将退回数据卡"
    if not cli.is_file():
        return False, f"未安装 HyperFrames:在 {MOTION_DIR} 下运行 npm install,概念动画将退回数据卡"
    if not MOTION_BROWSER:
        return False, "未找到 Chrome / Edge,请在 .env 配 HYPERFRAMES_BROWSER_PATH"
    return True, f"HyperFrames: {cli.parent.parent}(浏览器 {MOTION_BROWSER})"


# --------------------------------------------------------------------------
# 生成通道
# --------------------------------------------------------------------------
MCODE_TOOLS = os.getenv("MCODE_TOOLS") or shutil.which("mcode-tools") or ""

#: M Plan 订阅 Key(sk-cp-…)。配了它就能不经 mcode-tools、直连 MiniMax 开放平台
#: (语音 / 音乐 / 生图 / 图片理解 / 网络搜索),额度从订阅套餐里扣。
MINIMAX_PLAN_KEY = os.getenv("MINIMAX_PLAN_KEY", "").strip()
MINIMAX_API_HOST = os.getenv("MINIMAX_API_HOST", "https://api.minimax.cn").rstrip("/")

#: 接口后端:`mcode` = 经 MiniMax Code 托管的 mcode-tools;`direct` = 用订阅 Key 直连。
#: 不填时:有订阅 Key 且找不到 mcode-tools 就走 direct,否则走 mcode。
CONNECTOR_BACKEND = (
    os.getenv("CONNECTOR_BACKEND", "").strip().lower()
    or ("direct" if MINIMAX_PLAN_KEY and not MCODE_TOOLS else "mcode")
)
#: 直连后端用到的模型(mcode 后端由 Connector 自己选)
TTS_MODEL = os.getenv("TTS_MODEL", "speech-2.8-hd")
MUSIC_MODEL = os.getenv("MUSIC_MODEL", "music-2.6")
#: 自备 BGM 目录(放 mp3/m4a/wav)。MiniMax 音乐接口已对新用户关闭(HTTP 410),有曲子就优先用这里的
#: 是否给成片配 BGM。默认关:2026-10-20 实测 BGM 混音把旁白压没了(见 render._build_audio_graph),修好前别开
BGM_ENABLED = os.getenv("BGM_ENABLED", "0") == "1"
#: 自备目录没曲子时,是否按 profile.bgm_genres 从 Mixkit 自动收一批
BGM_AUTO_COLLECT = os.getenv("BGM_AUTO_COLLECT", "1") != "0"
BGM_DIR = Path(os.getenv("BGM_DIR", "")) if os.getenv("BGM_DIR") else None
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "image-01")
#: Connector 调用的默认超时
CONNECTOR_TIMEOUT_DEFAULT = 180


def deepseek_api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def deepseek_base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")


def deepseek_model() -> str:
    return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "auto_long")


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
    (path / "facts_raw").mkdir(exist_ok=True)
    return path
