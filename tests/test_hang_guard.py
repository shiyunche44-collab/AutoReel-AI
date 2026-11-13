"""服务卡死防护:控制台写不动时不能拖住业务线程;事件循环卡住要留栈。"""
from __future__ import annotations

import logging
import threading
import time

from app import hang_watch, logging_setup


class _BlockingStream:
    """模拟没人读的管道:write 永远不返回。"""

    def __init__(self) -> None:
        self.release = threading.Event()

    def write(self, _s: str) -> int:
        self.release.wait()
        return 0

    def flush(self) -> None:
        pass


def test_console_handler_never_blocks_caller(monkeypatch):
    stream = _BlockingStream()
    monkeypatch.setattr(logging_setup, "CONSOLE_QUEUE_MAX", 50)
    real = logging.StreamHandler
    monkeypatch.setattr(logging, "StreamHandler", lambda *a, **k: real(stream))
    handler = logging_setup._nonblocking_console(logging.Formatter("%(message)s"))
    lg = logging.getLogger("test.hang_guard")
    lg.propagate = False
    lg.addHandler(handler)
    try:
        t0 = time.monotonic()
        for i in range(500):  # 远超队列上限:多的应被丢掉,而不是阻塞
            lg.warning("line %d", i)
        assert time.monotonic() - t0 < 2.0
        assert logging_setup._DropWhenFullHandler.dropped > 0
    finally:
        lg.removeHandler(handler)
        stream.release.set()


def test_uvicorn_access_not_written_directly(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_setup, "_configured", False)
    monkeypatch.setattr(logging_setup, "log_dir", lambda: tmp_path)
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.StreamHandler())
    access.propagate = False
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        logging_setup.setup()
        assert access.handlers == []
        assert access.propagate is True
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def test_hang_dump_writes_all_thread_stacks(tmp_path):
    path = hang_watch._dump(tmp_path, 75)
    text = path.read_text(encoding="utf-8")
    assert path.name.startswith("hang-")
    assert "stalled for 75s" in text
    assert "test_hang_dump_writes_all_thread_stacks" in text
