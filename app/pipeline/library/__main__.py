"""命令行入口:`python -m app.pipeline.library`。"""
from __future__ import annotations

import sys

from app.pipeline.library import _cli

sys.exit(_cli())
