"""分发账号。"""
from __future__ import annotations

import logging
import time
from typing import Any

from .core import connect
from .schema import init_db

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 分发账号
# --------------------------------------------------------------------------
def create_account(row: dict[str, Any]) -> None:
    init_db()
    now = time.time()
    row = {"enabled": 1, "credential": "{}", "meta": "{}", **row}
    row.setdefault("created_at", now)
    row["updated_at"] = now
    with connect() as conn:
        conn.execute(
            """INSERT INTO publish_accounts
               (id, platform, name, auth_kind, credential, meta, enabled, created_at, updated_at)
               VALUES (:id, :platform, :name, :auth_kind, :credential, :meta, :enabled,
                       :created_at, :updated_at)""",
            row,
        )


def get_account(account_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        cur = conn.execute("SELECT * FROM publish_accounts WHERE id = ?", (account_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def list_accounts(platform: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if platform:
            cur = conn.execute(
                "SELECT * FROM publish_accounts WHERE platform = ? ORDER BY created_at",
                (platform,),
            )
        else:
            cur = conn.execute("SELECT * FROM publish_accounts ORDER BY platform, created_at")
        return [dict(r) for r in cur.fetchall()]


def update_account(account_id: str, **fields: Any) -> None:
    if not fields:
        return
    init_db()
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE publish_accounts SET {assignments} WHERE id = :id",
            {"id": account_id, **fields},
        )


def delete_account(account_id: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM publish_accounts WHERE id = ?", (account_id,))
