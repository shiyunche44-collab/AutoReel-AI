"""重启前看服务在不在忙。给 `scripts/restart_server.ps1` 用,也能单独跑。

忙 = 下面任何一样:

- 调度器手里有项目(`/api/health` 的 `scheduler.busy_projects` / `running`):
  正在渲染、合成或备料,杀掉会写坏 mp4、项目翻 FAILED;
- 有发布正在上传(`publications.state = 'uploading'`):杀掉会留半截投稿;
- 有团队角色正在跑(`team_runs.status = 'running'`):分析员、运营官开着浏览器。

后两样只算本次服务启动之后的行(`--since`,进程启动时刻的 epoch 秒)。
被强杀的旧进程会留下永远 running / uploading 的行,不按启动时刻过滤的话,
一次强杀之后就再也重启不了。

服务不应答时返回 unknown:可能正卡在渲染里,也可能真卡死了。
真卡死交给看门狗(`ensure_server.ps1`),这里不替它判断。

输出一行 JSON,退出码 0 = 空闲,1 = 忙,2 = 判断不了。
只读数据库,不导入 `app`(`python -I` 跑,不受当前目录影响)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "AutoReel-AI.db"


def health(port: int, timeout: float) -> dict | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 不应答就是判断不了,原因不重要
        return None


def db_busy(since: float) -> dict[str, list[str]]:
    if not DB.exists():
        return {"uploading": [], "team_running": []}
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        uploading = [f"{r[0]}({r[1]})" for r in conn.execute(
            "SELECT id, platform FROM publications WHERE state = 'uploading' AND updated_at >= ?",
            (since,))]
        team = [str(r[0]) for r in conn.execute(
            "SELECT role FROM team_runs WHERE status = 'running' AND started_at >= ?",
            (since,))]
    finally:
        conn.close()
    return {"uploading": uploading, "team_running": team}


def check(port: int, since: float, timeout: float) -> tuple[int, dict]:
    out: dict = {"port": port, "since": since}
    h = health(port, timeout)
    if h is None:
        out["verdict"] = "unknown"
        out["reason"] = "health 不应答"
        return 2, out
    sched = h.get("scheduler") or {}
    projects = sorted(set(sched.get("busy_projects") or []) | set(sched.get("running") or []))
    out["projects"] = projects
    # 只读 Web(没抢到调度锁)不推进、不发布,库里的 running 行不是它的
    background_ok = (h.get("background") or {}).get("ok", True)
    out["background"] = background_ok
    out.update(db_busy(since) if background_ok else {"uploading": [], "team_running": []})
    busy = bool(projects or out["uploading"] or out["team_running"])
    out["verdict"] = "busy" if busy else "idle"
    return (1 if busy else 0), out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--since", type=float, default=0.0,
                    help="服务进程启动时刻(epoch 秒);0 = 最近 1 小时")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)
    since = args.since or time.time() - 3600
    code, out = check(args.port, since, args.timeout)
    sys.stdout.write(json.dumps(out, ensure_ascii=True) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
