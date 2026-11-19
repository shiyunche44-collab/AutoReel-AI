"""把文字模型调用交给 Claude Code 会话(Claude 驱动的项目用)。

流水线里每个要调文字模型的地方(事实抽取、写稿、自修、分镜、剪辑规划、包装……)
拿到的都是一个 `structured(system, user, required_keys)` 接口。这里实现同一个接口,
但**不发 HTTP**:

1. 项目目录下 `claude/<id>.answer.json` 已有回答 → 校验必需字段后原样返回;
2. 没有 → 把完整提示词写成 `claude/<id>.request.json`,抛 `NeedsClaude` 停下这一步。

Claude 读请求、写回答(`python -m app.cli ask answer`),再跑一次同一步:前面答过的
请求按 id 直接复用,走到下一个没答的地方再停。`<id>` 是提示词内容的指纹,
所以提示词、黄金快照、grounding 与 lint 一行不用改,Claude 也只能按同样的 JSON 形状作答。

`NeedsClaude` 继承 `BaseException`:各调用点为了"模型挂了也能出片"写了很多
`except Exception` 兜底(选不出版式就全 broll……),停下等 Claude 时不能被它们吞成降级。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from . import ledger
from .base import LLMOutputInvalid, extract_json, require_keys

#: 项目目录下存请求 / 回答的子目录
DIRNAME = "claude"
#: 账本里的 provider / model
PROVIDER = "claude"
MODEL = "claude-code"


class NeedsClaude(BaseException):  # noqa: N818 - 不是错误,是"停下等人答"
    """这一步要 Claude 先回答一个请求。`request` 是请求内容(含 id 与文件路径)。"""

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__(f"等待 Claude 回答 {request.get('id')}({request.get('purpose')})")
        self.request = request


def request_id(system: str, user: str, required_keys: list[str] | None) -> str:
    raw = "\x1e".join([system, user, ",".join(required_keys or [])])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def folder(project_id: str) -> Path:
    from .. import config

    return config.project_dir(project_id) / DIRNAME


def request_path(project_id: str, rid: str) -> Path:
    return folder(project_id) / f"{rid}.request.json"


def answer_path(project_id: str, rid: str) -> Path:
    return folder(project_id) / f"{rid}.answer.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def pending(project_id: str) -> list[dict[str, Any]]:
    """还没回答的请求(按创建时间)。"""
    out = []
    for path in sorted(folder(project_id).glob("*.request.json")):
        req = _read(path)
        if req and not answer_path(project_id, str(req.get("id"))).is_file():
            out.append(req)
    return sorted(out, key=lambda r: float(r.get("created_at") or 0))


def answered(project_id: str) -> list[dict[str, Any]]:
    """已回答的请求摘要(id、调用方、时间)。"""
    out = []
    for path in sorted(folder(project_id).glob("*.request.json")):
        req = _read(path)
        if req and answer_path(project_id, str(req.get("id"))).is_file():
            out.append({k: req.get(k) for k in ("id", "purpose", "created_at")})
    return out


def get_request(project_id: str, rid: str) -> dict[str, Any] | None:
    return _read(request_path(project_id, rid))


def put_answer(project_id: str, rid: str, answer: dict[str, Any] | str) -> dict[str, Any]:
    """写入一个回答。先按请求的必需字段校验,不合格抛 `LLMOutputInvalid`,不落盘。"""
    req = get_request(project_id, rid)
    if req is None:
        raise LLMOutputInvalid(f"没有这个请求:{rid}")
    data = extract_json(answer) if isinstance(answer, str) else answer
    if not isinstance(data, dict):
        raise LLMOutputInvalid("回答必须是一个 JSON 对象")
    require_keys(data, list(req.get("required_keys") or []))
    _write(answer_path(project_id, rid), data)
    return data


class ClaudeHandoff:
    """文字模型通道:回答由 Claude Code 会话写在项目目录里。"""

    name = PROVIDER
    model = MODEL

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def available(self) -> bool:
        return True

    def structured(
        self,
        system: str,
        user: str,
        *,
        required_keys: list[str] | None = None,
        temperature: float = 0.6,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        started = time.monotonic()
        rid = request_id(system, user, required_keys)
        purpose = ledger.caller()
        answer = _read(answer_path(self.project_id, rid))
        error = ""
        if answer is not None:
            try:
                require_keys(answer, required_keys)
            except LLMOutputInvalid as exc:
                error = str(exc)
            else:
                ledger.record(kind="text", provider=PROVIDER, model=MODEL, started=started,
                              ok=True, system=system, prompt_chars=len(system) + len(user),
                              purpose=purpose)
                return answer
        request = {
            "id": rid,
            "purpose": purpose,
            "project_id": self.project_id,
            "created_at": time.time(),
            "required_keys": list(required_keys or []),
            "system": system,
            "user": user,
            "answer_path": str(answer_path(self.project_id, rid)),
            **({"previous_error": error} if error else {}),
        }
        old = get_request(self.project_id, rid)
        if old and not error:
            request["created_at"] = old.get("created_at", request["created_at"])
        _write(request_path(self.project_id, rid), request)
        raise NeedsClaude(request)

    def chat(self, system: str, user: str, *, temperature: float = 0.6,
             json_mode: bool = True) -> str:
        return json.dumps(self.structured(system, user), ensure_ascii=False)
