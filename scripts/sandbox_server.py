"""沙盒服务:自己的库、自己的项目目录,不碰正在跑的 8848。

    python scripts/sandbox_server.py --port 8849 --name mech

8848 占着调度器锁时,同一个库上再起的服务只读、不推进(见 `app/instance_lock.py`)。
想出几条样片验证流水线改动,又不想停用户的服务,就用这个:库是
`data/sandbox-<name>.db`,项目落在 `data/_sandbox/<name>/`,锁文件跟着库名走,
所以它有自己的调度器。素材库、BGM、各类缓存仍和正式服务共用(只读或按内容寻址)。

团队和自动发布在这里一律关掉:沙盒只出片,不抓平台数据,不往任何账号发东西。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoReel-AI 沙盒服务(独立库)")
    parser.add_argument("--port", type=int, default=8849)
    parser.add_argument("--name", default="dev", help="沙盒名:决定库文件和项目目录")
    args = parser.parse_args()
    name = "".join(c for c in args.name if c.isalnum() or c in "-_") or "dev"

    # 必须在导入 app.config 之前:这两个开关在导入时读环境变量
    os.environ["TEAM_ENABLED"] = "0"
    os.environ["PUBLISH_ENABLED"] = "0"

    from app import config

    config.DB_FILENAME = f"sandbox-{name}.db"
    config.PROJECTS_DIR = config.DATA_DIR / "_sandbox" / name

    import uvicorn

    from app.main import app

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
