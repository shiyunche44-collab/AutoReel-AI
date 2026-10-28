"""命令行入口。"""
from __future__ import annotations

import json
import logging
from typing import Sequence

from .builder import rebuild


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    from ... import db

    ap = argparse.ArgumentParser(description="统一风格封面:给已成片的项目重出封面")
    ap.add_argument("--project", action="append", default=[], help="重出这个项目的封面(可多次)")
    ap.add_argument("--all", action="store_true", help="重出所有已成片项目的封面")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ids = list(args.project)
    if args.all:
        ids += [p["id"] for p in db.list_projects(limit=1000)
                if db.read_json(p["id"], "render.json", None)]
    for pid in dict.fromkeys(ids):
        print(pid, json.dumps(rebuild(pid), ensure_ascii=False, default=str))
    return 0
