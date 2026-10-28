"""JSON 创作内容读写(facts / script / storyboard)。"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .. import config

log = logging.getLogger(__name__)


def _json_path(project_id: str, name: str) -> Path:
    return config.project_dir(project_id) / name


#: `os.replace` 在目标正被别的进程打开时会失败(Windows 的共享冲突)。
#: 读方(read_json / 页面预览)随时可能正开着这个文件,重试几次即可。
_REPLACE_RETRIES = 6


def _atomic_write(path: Path, text: str) -> None:
    """把一段文本原子地写到 path。

    做法:写同目录下的临时文件 → flush → fsync → os.replace 覆盖。
    `os.replace` 在同卷上是原子替换(Windows 亦然),所以读者要么看到
    旧内容,要么看到新内容,**不会看到半截**。

    为什么必须这样:直接 `path.write_text` 写到一半被kill会留下截断的
    文件。README 自己就写了"托管后台任务有 1 小时上限会把服务收走" ——
    写到一半被杀不是假设,是常态。而 `read_json` 对损坏文件是**静默**
    返回默认值的,于是半截的 script.json 会被当成"文件不存在",
    流水线重新生成一份,用户在门禁 A 手改的稿就这么没了。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(_REPLACE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == _REPLACE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
        # 理论上到不了这里
        os.replace(tmp, path)
    except BaseException:
        # 失败就把临时文件清掉,别在项目目录里留垃圾
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def write_json(project_id: str, name: str, payload: Any) -> Path:
    path = _json_path(project_id, name)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def _quarantine_corrupt(path: Path, exc: Exception) -> None:
    """损坏的 JSON 另存一份,不当作"文件不存在"。

    静默返回默认值是最坏的处理方式:流水线以为文件没了,重新生成,
    用户手改的稿被覆盖,而且**事后完全查不出发生过什么**。
    改名保留(而不是删除)是为了留证据。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, backup)
    except OSError:
        return
    log.warning("JSON 损坏,已另存为 %s:%s", backup.name, exc)


def read_json(project_id: str, name: str, default: Any = None) -> Any:
    path = _json_path(project_id, name)
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _quarantine_corrupt(path, exc)
        return default
    except OSError:
        return default
