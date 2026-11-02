"""后台循环吞掉异常时必须留下日志(不再静默)。"""
import logging

from app.metrics import worker


def test_metrics_loop_logs_instead_of_silent(monkeypatch, caplog):
    def boom():
        worker._stop.set()  # 只跑一轮
        raise RuntimeError("tick 炸了")

    monkeypatch.setattr(worker, "_tick", boom)
    monkeypatch.setattr(worker, "TICK_SECONDS", 0)
    worker._stop.clear()
    try:
        with caplog.at_level(logging.WARNING, logger="app.metrics.worker"):
            worker._loop()
    finally:
        worker._stop.clear()
    assert any(r.exc_info and "tick 炸了" in str(r.exc_info[1]) for r in caplog.records)
