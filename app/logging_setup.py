"""结构化日志的统一入口。

此前整个项目**一次也没用过 `logging`**:日志只是 `scripts/serve.ps1` 把
stdout 重定向到 `data/logs/server-*.log`,排障时只能靠猜"这会儿到底
跑到哪一步了"。调度器那圈 `except Exception: pass` 更是让失败彻底
静默 —— 项目不再推进,界面永远显示 pending,没人知道发生过什么。

这里给出统一入口:
- **控制台**:带级别与时间,给人实时看;
- **data/logs/aimovice.log**:按天轮转(留 14 天),给事后追溯看。

只用标准库,不引依赖。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from . import config

#: 一行日志的格式。时间在前 —— 排障时第一眼就要知道"什么时候"。
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 日志文件名(按天轮转)
LOG_FILENAME = "aimovice.log"

#: 保留多少天的历史日志
LOG_BACKUP_DAYS = 14

_configured = False


def log_dir() -> Path:
    """日志目录:data/logs/。"""
    return config.DATA_DIR / "logs"


def setup(level: int | str = logging.INFO) -> None:
    """配置根 logger。幂等:重复调用只生效一次。

    文件 handler 打不开时(磁盘满、目录只读)**不能因此让服务起不来** ——
    日志是辅助,不是前提。控制台那份一定会装上。
    """
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(level)
    # uvicorn 自己装过 handler;不摘掉会重复打印
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        config.ensure_dirs()
        log_dir().mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir() / LOG_FILENAME,
            when="midnight",
            backupCount=LOG_BACKUP_DAYS,
            encoding="utf-8",
        )
    except OSError:
        logging.getLogger(__name__).warning("日志文件不可写,只用控制台输出")
    else:
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """取 logger。用标准库的 `logging.getLogger`,模块名即 logger 名。"""
    return logging.getLogger(name)
