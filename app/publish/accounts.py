"""账号与凭据管理。

**谁说了算**:SQLite 的 `publish_accounts` 表是唯一真相(source of truth);
`data/accounts.json` 是每次变更后自动写出的**镜像**,作用有两个:

1. 换机器时把这个文件拷过去就能导入;
2. 备份方便 —— 账号和凭据在一起,不用去翻 SQLite。

反过来也成立:文件里存在但表里没有的账号会被 `import_from_file()` 补进来
(所以手工编辑这个文件也能加账号)。

**凭据不出现在任何 API 响应里**(和 `main.py` 开头写死的原则一致):
对外一律走 `account_view()`,只暴露"有没有凭据"和末 4 位。

应用级密钥(client_key / OAuth secret)不在这儿,它们在 `.env` 里 —— 那是开发者
资产,不随账号变。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import db
from ..models import new_id
from .base import Account
from .registry import PLATFORMS

#: 凭据里这些键是敏感的,展示时必须打码
_SECRET_HINTS = ("token", "secret", "cookie", "key", "password", "session")


def _is_secret(key: str) -> bool:
    low = key.lower()
    return any(h in low for h in _SECRET_HINTS)


def mask_value(value: str) -> str:
    """打码:只留末 4 位。太短的整块打掉。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


def mask_credential(credential: dict[str, Any] | None) -> dict[str, Any]:
    """凭据脱敏视图。"""
    out: dict[str, Any] = {}
    for key, value in (credential or {}).items():
        if isinstance(value, str) and _is_secret(key):
            out[key] = mask_value(value)
        else:
            out[key] = value
    return out


def _row_to_account(row: dict[str, Any]) -> Account:
    def _load(value: str) -> dict[str, Any]:
        try:
            data = json.loads(value or "{}")
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    return Account(
        id=row["id"],
        platform=row["platform"],
        name=row["name"],
        auth_kind=row.get("auth_kind") or "cookie",
        credential=_load(row.get("credential") or "{}"),
        meta=_load(row.get("meta") or "{}"),
        enabled=bool(row.get("enabled", 1)),
    )


def list_accounts(platform: str | None = None, *, only_enabled: bool = False) -> list[Account]:
    rows = db.list_accounts(platform)
    accounts = [_row_to_account(r) for r in rows]
    if only_enabled:
        accounts = [a for a in accounts if a.enabled]
    return accounts


def get_account(account_id: str) -> Account | None:
    row = db.get_account(account_id)
    return _row_to_account(row) if row else None


def account_view(account: Account) -> dict[str, Any]:
    """给 API / UI 的脱敏视图。凭据只露"有没有"和末 4 位。"""
    cred = account.credential
    return {
        "id": account.id,
        "platform": account.platform,
        "platform_label": PLATFORMS[account.platform].label if account.platform in PLATFORMS
        else account.platform,
        "name": account.name,
        "auth_kind": account.auth_kind,
        "enabled": account.enabled,
        "meta": account.meta,
        "credential": mask_credential(cred),
        "has_credential": bool(cred),
        "credential_keys": sorted(cred.keys()),
        "updated_at": getattr(account, "updated_at", 0.0),
    }


def save_account(*, platform: str, name: str, credential: dict[str, Any] | None = None,
                 meta: dict[str, Any] | None = None, auth_kind: str = "cookie",
                 account_id: str | None = None, enabled: bool = True) -> Account:
    """新增或更新一个账号。`account_id` 为空则新建。"""
    if platform not in PLATFORMS:
        raise ValueError(f"没有这个平台:{platform}")
    now = time.time()
    if account_id:
        existing = db.get_account(account_id)
        if not existing:
            raise ValueError("账号不存在")
        fields: dict[str, Any] = {"platform": platform, "name": name, "auth_kind": auth_kind,
                                  "meta": json.dumps(meta or {}, ensure_ascii=False),
                                  "enabled": 1 if enabled else 0}
        if credential is not None:
            fields["credential"] = json.dumps(credential, ensure_ascii=False)
        db.update_account(account_id, **fields)
        account = get_account(account_id)
    else:
        account_id = new_id("acc_")
        db.create_account({
            "id": account_id, "platform": platform, "name": name, "auth_kind": auth_kind,
            "credential": json.dumps(credential or {}, ensure_ascii=False),
            "meta": json.dumps(meta or {}, ensure_ascii=False),
            "enabled": 1 if enabled else 0,
            "created_at": now, "updated_at": now,
        })
        account = get_account(account_id)
    assert account is not None
    write_mirror()
    return account


def delete_account(account_id: str) -> None:
    db.delete_account(account_id)
    write_mirror()


# --------------------------------------------------------------------------
# 凭据镜像文件
# --------------------------------------------------------------------------
def mirror_path() -> Path:
    from .. import config

    return config.ACCOUNTS_FILE


def write_mirror() -> None:
    """把全部账号写进 `data/accounts.json`。写失败不抛 —— 镜像只是备份手段。"""
    path = mirror_path()
    payload = {
        "version": 1,
        "written_at": time.time(),
        "accounts": [
            {
                "id": a.id, "platform": a.platform, "name": a.name,
                "auth_kind": a.auth_kind, "credential": a.credential,
                "meta": a.meta, "enabled": a.enabled,
            }
            for a in list_accounts()
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # 凭据文件不应对同机其他用户可读
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def import_from_file() -> int:
    """把镜像文件里表里没有的账号补进来。返回导入条数。"""
    path = mirror_path()
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    known = {a.id for a in list_accounts()}
    added = 0
    for item in payload.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        account_id = str(item.get("id") or "").strip()
        platform = str(item.get("platform") or "").strip()
        if not account_id or account_id in known or platform not in PLATFORMS:
            continue
        db.create_account({
            "id": account_id, "platform": platform,
            "name": str(item.get("name") or platform),
            "auth_kind": str(item.get("auth_kind") or "cookie"),
            "credential": json.dumps(item.get("credential") or {}, ensure_ascii=False),
            "meta": json.dumps(item.get("meta") or {}, ensure_ascii=False),
            "enabled": 1 if item.get("enabled", True) else 0,
            "created_at": time.time(), "updated_at": time.time(),
        })
        added += 1
    return added
