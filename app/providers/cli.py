"""`mcode-tools` Connector 子进程封装。

这是整个平台与生成通道之间的唯一接口层。所有上层模块(images/audio/facts)
都必须经过这里,好处是超时、重试、错误归一化、临时文件清理只写一遍。

Windows 注意事项:
- `mcode-tools` 是 `.cmd` 批处理文件,`subprocess.run([...])` 列表形式无法直接执行,
  必须用 `shell=True` 并自行做命令行转义。这里所有参数都来自程序内部常量或
  程序自己生成的临时文件路径,**不接受任何外部用户输入**,因此不存在注入面。
- 复杂参数一律走 `--args-file`,避免 PowerShell/cmd 的引号地狱。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .. import config

log = logging.getLogger(__name__)

#: 外部能力调用统计:工具名 -> {calls, errors, seconds}
#:
#: 此前外部调用**完全没有**可观测性 —— 不知道调了哪些工具、调了多少次、
#: 慢在哪、错在哪。`/api/health` 把这份统计暴露出来,排查"是不是额度
#: 烧在某个工具上"时不用再猜。
_CALL_STATS: dict[str, dict[str, float]] = {}
_CALL_STATS_LOCK = threading.Lock()


def call_stats() -> dict[str, dict[str, float]]:
    """外部能力调用统计的快照。"""
    with _CALL_STATS_LOCK:
        return {tool: dict(row) for tool, row in _CALL_STATS.items()}


def _record_call(tool: str, seconds: float, ok: bool) -> None:
    with _CALL_STATS_LOCK:
        row = _CALL_STATS.setdefault(tool, {"calls": 0.0, "errors": 0.0, "seconds": 0.0})
        row["calls"] += 1
        row["seconds"] += seconds
        if not ok:
            row["errors"] += 1


def reset_call_stats() -> None:
    """清空统计(自检脚本用)。"""
    with _CALL_STATS_LOCK:
        _CALL_STATS.clear()


class ConnectorError(RuntimeError):
    """Connector 调用失败。"""

    def __init__(self, tool: str, message: str, *, code: int | None = None) -> None:
        super().__init__(f"[{tool}] {message}")
        self.tool = tool
        self.message = message
        self.code = code


class ConnectorUnavailable(ConnectorError):
    """CLI 不存在或不可执行。"""


class ConnectorTimeout(ConnectorError):
    """调用超时。"""


def is_direct() -> bool:
    """是否走直连后端(M Plan 订阅 Key 直连开放平台,不经 mcode-tools)。"""
    return config.CONNECTOR_BACKEND == "direct"


def source_label(tool: str) -> str:
    """一次调用实际用的数据源名,写进事实的 source_tool。直连时与工具名可能不同。"""
    if is_direct():
        from . import direct

        return direct.source_label(tool)
    return tool


def cli_path() -> str:
    path = config.MCODE_TOOLS
    if not path:
        raise ConnectorUnavailable(
            "mcode-tools",
            "未找到 mcode-tools CLI。请在 .env 中配置 MCODE_TOOLS,或确保它在系统 PATH 中。",
        )
    if not Path(path).is_file() and not Path(path).exists():
        found = os.popen(f"where {path}").read().strip()
        if not found:
            raise ConnectorUnavailable("mcode-tools", f"配置的路径不存在:{path}")
        return found.splitlines()[0]
    return path


def _quote(value: str) -> str:
    """cmd.exe 参数转义。"""
    return '"' + value.replace('"', '""') + '"'


def run_raw(args: list[str], *, timeout: int) -> str:
    """执行一次 CLI,返回 stdout。失败时抛 ConnectorError。"""
    try:
        exe = cli_path()
    except ConnectorUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - 防御
        raise ConnectorUnavailable("mcode-tools", str(exc)) from exc

    command = " ".join([_quote(exe), *(_quote(a) for a in args)])
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConnectorTimeout("mcode-tools", f"调用超时({timeout}s)") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise ConnectorError(
            "mcode-tools",
            f"退出码 {proc.returncode}: {stderr or stdout or '(无输出)'}",
        )
    if not stdout:
        raise ConnectorError("mcode-tools", f"无输出。stderr: {stderr or '(空)'}")
    return stdout


def _extract_json(raw: str) -> Any:
    """从可能带前后噪声的输出中提取 JSON。

    CLI 偶尔会在 JSON 前后打印日志行,这里做一次稳健截取。
    """
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ConnectorError("mcode-tools", f"无法解析 JSON 输出: {text[:400]}")


def call(
    tool: str,
    args: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
    retries: int = 2,
    retry_delay: float = 3.0,
) -> Any:
    """调用一个 Connector 工具并返回其 JSON 结果。

    参数通过临时文件传递(`--args-file`),调用后立即删除。
    `args` 必须是单个 JSON 对象 —— Connector 不接受数组/标量作为顶层参数。
    """
    payload = args if args is not None else {}
    if not isinstance(payload, dict):
        raise ValueError("Connector 参数必须是单个 JSON 对象")

    timeout = timeout or config.CONNECTOR_TIMEOUT_DEFAULT
    if is_direct():
        from . import direct  # 延迟导入:direct 依赖本模块的异常类

        started = time.monotonic()
        try:
            out = direct.call(tool, payload, timeout=timeout)
        except Exception:
            _record_call(tool, time.monotonic() - started, ok=False)
            raise
        _record_call(tool, time.monotonic() - started, ok=True)
        return out
    last_error: ConnectorError | None = None

    for attempt in range(retries + 1):
        tmp_path: str | None = None
        started = time.monotonic()
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="aimovice_args_")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)

            raw = run_raw(
                ["connector", "call", tool, "--args-file", tmp_path], timeout=timeout
            )
            result = _extract_json(raw)
            _record_call(tool, time.monotonic() - started, ok=True)
            return _unwrap(tool, result)
        except ConnectorError as exc:
            last_error = exc
            _record_call(tool, time.monotonic() - started, ok=False)
            # 连接级/超时类错误值得重试;参数错误重试无意义
            if isinstance(exc, (ConnectorUnavailable,)) or "非法参数" in exc.message:
                raise
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    assert last_error is not None
    raise last_error


def _unwrap(tool: str, result: Any) -> Any:
    """归一化 Connector 的响应信封。

    观察到的形态:`{"code":0,"message":"ok", ...payload}`,payload 与 code 同级。

    **返回形状的约定**(调用方必须两种都兼容):

    - 有 ``code`` 且非 0/200 → 抛 `ConnectorError`;
    - payload 为空 → 返回整个响应;
    - payload 恰好**只有一个键**、且它的值是**非空 list** → 直接返回那个 list;
    - 其余情况 → 返回 payload 字典。

    最后那条是"形状嗅探":它让某些工具的调用方少写一层取值。但也意味着
    同一个工具可能这次给 list、下次给 dict —— **调用方不能只按一种形状
    写**。`audio._extract_success_items` 与 `images` 的取图逻辑都同时
    兼容两种形状,新增调用方时照做。
    """
    if isinstance(result, dict) and "code" in result:
        code = result.get("code")
        # 0 = ok;部分工具用 200 表示成功
        if code not in (0, 200, "0"):
            raise ConnectorError(
                tool, f"返回错误码 {code}: {result.get('message', '')}", code=code
            )
        payload = {k: v for k, v in result.items() if k not in ("code", "message")}
        if payload:
            # 把单键 payload 再解一层,避免调用方到处取 ["xxx"][0]
            if len(payload) == 1:
                only = next(iter(payload.values()))
                if isinstance(only, list) and only:
                    return only
            return payload
        return result
    return result


# --------------------------------------------------------------------------
# 资源 URL 与下载
# --------------------------------------------------------------------------
def get_asset_url(node_id: str, *, timeout: int = 60) -> str:
    """把 Drive node_id 换成一个可下载的短时链接。"""
    if is_direct():
        from . import direct

        return direct.get_asset_url(node_id)
    raw = run_raw(["get_asset_url", node_id], timeout=timeout)
    data = _extract_json(raw)
    if isinstance(data, dict):
        url = data.get("download_url") or data.get("temp_url")
        if url:
            return str(url)
    raise ConnectorError("get_asset_url", f"未找到下载地址: {data}")


def upload_temp_url(local_path: str | Path, *, mime_type: str | None = None,
                    timeout: int = 300) -> str:
    """把本地文件上传为临时 URL(生成接口只接受 URL,不接受本地路径)。"""
    path = Path(local_path)
    if not path.is_file():
        raise ConnectorError("upload_temp_url", f"文件不存在: {path}")
    if is_direct():
        from . import direct

        return direct.upload_temp_url(path)
    args = ["upload_temp_url", str(path)]
    if mime_type:
        args += ["--mime-type", mime_type]
    raw = run_raw(args, timeout=timeout)
    data = _extract_json(raw)
    if isinstance(data, dict):
        url = data.get("temp_url") or data.get("download_url")
        if url:
            return str(url)
    raise ConnectorError("upload_temp_url", f"未找到临时地址: {data}")


def health_check() -> tuple[bool, str]:
    """连通性自检。用于启动横幅与设置页。"""
    if is_direct():
        from . import direct

        return direct.health_check()
    try:
        exe = cli_path()
    except ConnectorError as exc:
        return False, exc.message
    try:
        raw = run_raw(["connector", "tools", "--keyword", "audio"], timeout=60)
        data = _extract_json(raw)
        tools = data.get("tools", []) if isinstance(data, dict) else []
        return True, f"CLI 可用({exe}),已发现 {len(tools)} 个语音相关工具"
    except ConnectorError as exc:
        return False, exc.message
