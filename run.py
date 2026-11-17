"""AutoReel-AI 启动入口。

用法:
    python run.py                         # 默认 http://127.0.0.1:8848,Web + 后台循环同一进程
    python run.py --port 9000
    python run.py --port 8848 --role web  # 只开 Web(随便重启,不打断渲染)
    python run.py --role worker           # 只跑后台循环(调度器、任务、发布、指标、团队)

`--role web` 和 `--role worker` 配对使用(`scripts/ensure_server.ps1` 默认这么起),
见 `app/background.py`。
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoReel-AI 本地解说视频平台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(单机自用请保持 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8848, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--role", choices=("all", "web", "worker"), default="all",
                        help="all = Web + 后台循环(默认);web = 只开 Web;worker = 只跑后台循环")
    args = parser.parse_args()
    # 进程角色经环境变量传给 app(uvicorn 的 reload 子进程也继承得到)
    os.environ["AUTOREEL_ROLE"] = args.role

    if args.role == "worker":
        from app import worker

        sys.exit(worker.main())

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
