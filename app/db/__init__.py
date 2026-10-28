"""SQLite 持久化。

分工原则:
- **可编辑的创作内容**(facts / script / storyboard)存 JSON 文件,方便人工查看与修改。
- **运行时状态**(项目元信息、镜头状态、provider task_id、事件流)存 SQLite,
  因为它们是并发写入、需要事务、且断点续跑依赖它们。

WAL 模式 + 每次调用独立连接,适配 FastAPI 的线程池。

按表域拆成子模块,这里统一再导出,调用方仍然写 `db.xxx`。
"""
from __future__ import annotations

import os  # noqa: F401  (测试会 patch db.os.fsync)

#: 是否已建表。放在包上,测试用 monkeypatch 重置;schema.init_db 经 sys.modules 读写。
_INITIALISED = False

from .core import connect, db_path, transaction  # noqa: E402,F401
from .schema import SCHEMA, _column_names, init_db  # noqa: E402,F401
from .jsonio import *  # noqa: E402,F401,F403
from .projects import *  # noqa: E402,F401,F403
from .shots import *  # noqa: E402,F401,F403
from .events import *  # noqa: E402,F401,F403
from .accounts import *  # noqa: E402,F401,F403
from .publications import *  # noqa: E402,F401,F403
from .snapshots import *  # noqa: E402,F401,F403
from .team_plan import *  # noqa: E402,F401,F403
from .team_runs import *  # noqa: E402,F401,F403
from .team_learning import *  # noqa: E402,F401,F403
from .comments import *  # noqa: E402,F401,F403
from .benchmark import *  # noqa: E402,F401,F403
from .outbound import *  # noqa: E402,F401,F403
from .industry import *  # noqa: E402,F401,F403
