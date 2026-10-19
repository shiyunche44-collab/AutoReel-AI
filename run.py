"""aimovice 启动入口。

用法:
    python run.py                # 默认 http://127.0.0.1:8848
    python run.py --port 9000
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="aimovice 本地解说视频平台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址(单机自用请保持 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8848, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
