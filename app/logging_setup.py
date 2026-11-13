"""结构化日志的统一入口。

此前整个项目**一次也没用过 `logging`**:日志只是 `scripts/serve.ps1` 把
stdout 重定向到 `data/logs/server-*.log`,排障时只能靠猜"这会儿到底
跑到哪一步了"。调度器那圈 `except Exception: pass` 更是让失败彻底
静默 —— 项目不再推进,界面永远显示 pending,没人知道发生过什么。

这里给出统一入口:
- **控制台**:带级别与时间,给人实时看;
- **data/logs/AutoReel-AI.log**:按天轮转(留 14 天),给事后追溯看。

只用标准库,不引依赖。
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
from pathlib import Path

from . import config

#: 一行日志的格式。时间在前 —— 排障时第一眼就要知道"什么时候"。
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 日志文件名(按天轮转)
LOG_FILENAME = "AutoReel-AI.log"

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

    root.addHandler(_nonblocking_console(fmt))
    # uvicorn 的访问日志 / 错误日志自带直写 stdout 的 handler,而访问日志是在
    # **事件循环线程**里写的:stdout 一堵,整个 Web 就冻住。摘掉它们的 handler,
    # 统一走根 logger 上那个不阻塞的控制台。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for handler in list(lg.handlers):
            lg.removeHandler(handler)
        lg.propagate = True

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
        # 前端每 5 秒轮询一次状态,访问日志进文件只会刷屏
        file_handler.addFilter(lambda r: not r.name.startswith("uvicorn.access"))
        root.addHandler(file_handler)

    _configured = True


#: 控制台队列上限。满了就丢,绝不让业务线程等控制台。
CONSOLE_QUEUE_MAX = 10_000


class _DropWhenFullHandler(logging.handlers.QueueHandler):
    """入队失败(队列满)就丢掉这条,而不是阻塞或往 stderr 打异常。"""

    dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            type(self).dropped += 1


def _nonblocking_console(fmt: logging.Formatter) -> logging.Handler:
    """控制台输出放到单独线程里写。

    2026-11-13 事故:8848 由一个会话用管道接着 stdout 启动,会话没了、管道没人读,
    缓冲写满后 `StreamHandler.emit` 永远阻塞 —— 它拿着 handler 锁,于是**所有**
    打日志的线程(事件循环、调度器、团队线程)一起卡死,CPU 为 0,端口在听却
    不应答,当天闭环整个没跑。现在只有这个监听线程会被堵住,队列满了就丢日志,
    文件日志和业务都不受影响。
    """
    q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=CONSOLE_QUEUE_MAX)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    listener = logging.handlers.QueueListener(q, stream, respect_handler_level=True)
    listener.start()  # 守护线程,进程退出不等它
    return _DropWhenFullHandler(q)


def get_logger(name: str) -> logging.Logger:
    """取 logger。用标准库的 `logging.getLogger`,模块名即 logger 名。"""
    return logging.getLogger(name)
