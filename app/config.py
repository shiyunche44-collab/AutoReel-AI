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
#: 竖屏项目的可选成片时长(秒)。竖屏平台看完播率,档位更短。
SUPPORTED_DURATIONS_PORTRAIT: tuple[int, ...] = (60, 90, 120, 180)

#: 画幅。横屏 16:9 与竖屏 9:16 —— 新建项目时选,决定这个项目的成片画幅。
#:
#: 画幅是**项目属性**:项目行上的 `aspect` 字段说了算(见 `pipeline/aspect.py`)。
#: 品类 YAML 里的 `aspect_ratio` 只是新建时的默认值。
LANDSCAPE = "16:9"
PORTRAIT = "9:16"
ASPECTS: tuple[str, ...] = (LANDSCAPE, PORTRAIT)

#: 两种画幅的成片像素尺寸。**短边都是 1080** —— 叠加层字号按短边推导,
#: 所以横竖两版的字号完全一致,变的只是排布(见 `pipeline/overlays.py`)。
LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

#: 归一化片段的缺省画幅。
#:
#: ⚠️ 这里曾经是 **1080×1920(竖屏 9:16)** —— 那是已删除的竖屏链路留下的
#: 残留。它是 `cards.geo_for` / `render._size` / `timeline` / `narration.make_cover`
#: / `providers.clips` 的**兜底**画幅:解说轨正常路径都显式传
#: 项目自己的画幅(`aspect.frame_size`),所以大多数时候是对的;但任何一处
#: 漏传 size,就会**静默产出另一个画幅**的画面 —— 兜底画幅在项目里是活的雷。
#:
#: 所以它固定为横屏 16:9(与解说轨的默认画幅一致):漏传 size 最多得到一张
#: 16:9 的图,而不是一张竖的。要竖屏必须显式传 size。
OUTPUT_WIDTH, OUTPUT_HEIGHT = LANDSCAPE_SIZE

#: 横屏解说轨的标准画幅比例
FRAME_ASPECT = 16 / 9


def size_for_aspect(aspect: str) -> tuple[int, int]:
    """画幅名 -> 像素尺寸。未知画幅退回横屏。"""
    return PORTRAIT_SIZE if aspect == PORTRAIT else LANDSCAPE_SIZE


def aspect_for_size(size: tuple[int, int]) -> str:
    """像素尺寸 -> 画幅名。竖着的就是竖屏。"""
    w, h = size
    if w and h and h > w:
        return PORTRAIT
    return LANDSCAPE


def frame_is_landscape(size: tuple[int, int]) -> bool:
    """这个画幅是不是横屏 16:9(允许一点点误差)。"""
    w, h = size
    if not w or not h:
        return True
    return abs(w / h - FRAME_ASPECT) <= 0.02

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
#: 任何本机 Chrome / Edge 都能走截图渲染路径。不填就先找已下载的 headless shell
#: (无窗口),再按常见安装位置找。注意:Windows 上 `chrome.exe --version` 会直接弹出
#: 浏览器窗口(HyperFrames 渲染前会探测一次),所以有 headless shell 时一定要优先用它
_HEADLESS_SHELLS = [
    str(p) for root in (MOTION_DIR / ".browser" / "chrome-headless-shell",
                        Path.home() / ".cache" / "hyperframes" / "chrome" / "chrome-headless-shell")
    for p in sorted(root.glob("*/chrome-headless-shell-*/chrome-headless-shell.exe"), reverse=True)]
_BROWSER_CANDIDATES = (
    *_HEADLESS_SHELLS,
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
#: 概念动画的浏览器 GPU:auto = 先试 GPU、卡死再退软件渲染;off = 直接软件渲染(锁屏跑批时用)
MOTION_BROWSER_GPU = os.getenv("MOTION_BROWSER_GPU", "auto").strip().lower()

#: 后台调度器的并发数:同时推进几个项目。
#:
#: 每一路都是重活 —— ffmpeg 子进程,加上概念动画的 Chrome(约 256MB/个)。
#: 峰值 Chrome 进程数 ≈ `PIPELINE_CONCURRENCY` × `MOTION_WORKERS`,内存
#: 吃紧就保持 1,确认有余量再往上调。
PIPELINE_CONCURRENCY = int(os.getenv("PIPELINE_CONCURRENCY", "1"))

#: data/ 体积超过这个值就在首页提示(字节)。默认 10 GB。
DATA_SIZE_WARN_BYTES = int(os.getenv("DATA_SIZE_WARN_BYTES", str(10 * 1024 ** 3)))


#: HyperFrames 要求的 Node 主版本
MOTION_NODE_MIN = 22
_node_cache: list[str] = []


def _node_major(exe: str) -> int:
    import subprocess
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def motion_node() -> str:
    """跑 HyperFrames 的 node(22+)。空串 = 找不到。

    顺序:`MOTION_NODE` 环境变量 → PATH 上的 node(版本够才用)→ nvm-windows
    目录(`NVM_HOME`)里装着的最新一个 22+。全局 `nvm use 16` 之后概念动画不再
    悄悄退回数据卡。结果进程内缓存。"""
    if _node_cache:
        return _node_cache[0]
    found = ""
    env = os.getenv("MOTION_NODE", "").strip()
    if env and _node_major(env) >= MOTION_NODE_MIN:
        found = env
    on_path = shutil.which("node")
    if not found and on_path and _node_major(on_path) >= MOTION_NODE_MIN:
        found = on_path
    nvm = os.getenv("NVM_HOME", "").strip()
    if not found and nvm and Path(nvm).is_dir():
        def ver(d: Path) -> tuple[int, ...]:
            try:
                return tuple(int(x) for x in d.name.lstrip("v").split("."))
            except ValueError:
                return (0,)
        for d in sorted(Path(nvm).glob("v*"), key=ver, reverse=True):
            exe = d / ("node.exe" if os.name == "nt" else "bin/node")
            if ver(d)[0] >= MOTION_NODE_MIN and exe.is_file():
                found = str(exe)
                break
    _node_cache.append(found)
    return found


def motion_status() -> tuple[bool, str]:
    """概念动画能不能渲染。不能也不拦流水线:concept 拍退回数据卡。"""
    node = motion_node()
    cli = MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
    if not node:
        return False, (f"未找到 node {MOTION_NODE_MIN}+(PATH 上的是 v{_node_major(shutil.which('node') or '')}"
                       f",可在 .env 配 MOTION_NODE),概念动画将退回数据卡")
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
#: 是否给成片配 BGM。2026-10-21 重写了混音(旁白侧链闪避,实测与旁白相关 0.98),默认开;BGM_ENABLED=0 关
BGM_ENABLED = os.getenv("BGM_ENABLED", "1") == "1"
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


# --------------------------------------------------------------------------
# 分发(多平台发布 + 数据监控)
# --------------------------------------------------------------------------
#: 分发总开关。关掉后发布与监控都不跑,成片主链路不受影响。
PUBLISH_ENABLED = os.getenv("PUBLISH_ENABLED", "1") not in ("0", "false", "False")
#: 跨账号并发度。**同一账号永远串行** —— 单账号高频上传会触发平台风控。
PUBLISH_CONCURRENCY = int(os.getenv("PUBLISH_CONCURRENCY", "2"))
#: 同账号两次发布之间的随机间隔(秒),避免被判机器批量。
PUBLISH_MIN_INTERVAL = float(os.getenv("PUBLISH_MIN_INTERVAL", "30"))
PUBLISH_MAX_INTERVAL = float(os.getenv("PUBLISH_MAX_INTERVAL", "90"))
#: 平台数据监控
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") not in ("0", "false", "False")
METRICS_INTERVAL_MIN = int(os.getenv("METRICS_INTERVAL_MIN", "30"))
#: 账号凭据文件。只存**用户态凭据**(各平台 cookie / OAuth token);
#: 应用级密钥(client_key / OAuth secret)走 .env,两者职责不同。
ACCOUNTS_FILE = DATA_DIR / "accounts.json"

#: 应用级密钥 —— 开发者资产,不随账号变
DOUYIN_CLIENT_KEY = os.getenv("DOUYIN_CLIENT_KEY", "").strip()
DOUYIN_CLIENT_SECRET = os.getenv("DOUYIN_CLIENT_SECRET", "").strip()
YOUTUBE_CLIENT_ID = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()

#: 浏览器自动化(小红书 / 视频号 / 抖音网页版兜底)。留空则复用本机 Chrome / Edge
#: —— 装 Playwright 的 Python 包即可,**不需要** `playwright install` 再下一份浏览器。
BROWSER_PATH = os.getenv("BROWSER_PATH", "").strip() or MOTION_BROWSER
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "1") not in ("0", "false", "False")

#: 抓取兜底:直连被拒(403 / 412 / 429…)时改用真浏览器再抓一次(`providers/browser_fetch.py`)。
#: 没装 Playwright 时自动退回现状。遇到验证码只记下来等人工过,不自动破解。
BROWSER_FETCH = os.getenv("BROWSER_FETCH", "1") not in ("0", "false", "False")
#: 只对这些 host 兜底(逗号分隔,按后缀匹配);留空 = 全部
BROWSER_FETCH_HOSTS = [h.strip().lower() for h in os.getenv("BROWSER_FETCH_HOSTS", "").split(",")
                       if h.strip()]
BROWSER_FETCH_HEADLESS = os.getenv(
    "BROWSER_FETCH_HEADLESS", "1" if BROWSER_HEADLESS else "0") not in ("0", "false", "False")


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
