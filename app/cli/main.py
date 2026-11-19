"""`python -m app.cli` 的子命令。每个命令都是现有函数的薄封装,输出 JSON。"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .. import config, db
from ..llm import handoff
from ..llm.base import LLMOutputInvalid
from ..state import GATE_STATES, TERMINAL_STATES, State

log = logging.getLogger(__name__)

#: 退出码
OK, FAILED, REJECTED, NEEDS_CLAUDE = 0, 1, 2, 3


class Rejected(RuntimeError):
    """命令被流水线拒绝(状态不对、lint 不过、门禁拦截)。退出码 2。"""


def _emit(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=1, default=str) + "\n")
    sys.stdout.flush()


def _project(pid: str) -> dict[str, Any]:
    row = db.get_project(pid)
    if not row:
        raise Rejected(f"项目不存在:{pid}")
    return row


def _read_input(ref: str) -> str:
    """`-` = stdin,否则是文件路径。"""
    if ref == "-":
        return sys.stdin.read()
    return Path(ref).read_text(encoding="utf-8")


def _ask_summary(req: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": req.get("id"),
        "purpose": req.get("purpose"),
        "required_keys": req.get("required_keys"),
        "prompt_chars": len(req.get("system") or "") + len(req.get("user") or ""),
        "show": f"python -m app.cli ask show {req.get('project_id')} {req.get('id')}",
        **({"previous_error": req["previous_error"]} if req.get("previous_error") else {}),
    }


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------
def cmd_new(args: argparse.Namespace) -> dict[str, Any]:
    from ..models import ProjectCreate
    from ..pipeline import orchestrator

    payload = ProjectCreate(
        subject=args.subject, category=args.category, theme=args.theme or None,
        duration=args.duration, aspect=args.aspect, angle=args.angle or None,
        hook=args.hook or None, angle_why=args.why or None, angle_search=args.search or None,
        owner_notes=args.owner_notes or None, title=args.title or None,
        look=args.look or None, series=args.series or None)
    try:
        pid = orchestrator.create_project(payload)
    except orchestrator.PipelineError as exc:
        raise Rejected(str(exc)) from exc
    db.update_project(pid, driver=orchestrator.CLAUDE_DRIVER)
    row = _project(pid)
    return {"project_id": pid, "state": row["state"], "title": row["title"],
            "aspect": row["aspect"], "duration": row["duration"], "theme": row["theme"],
            "next": f"python -m app.cli step {pid}"}


def cmd_adopt(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import orchestrator

    _project(args.project)
    driver = "scheduler" if args.release else orchestrator.CLAUDE_DRIVER
    db.update_project(args.project, driver=driver)
    return {"project_id": args.project, "driver": driver}


def cmd_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = db.list_projects(limit=args.limit)
    if args.claude:
        rows = [r for r in rows if r.get("driver") == "claude"]
    return [{k: r.get(k) for k in ("id", "title", "state", "driver", "aspect", "duration",
                                   "updated_at")} for r in rows]


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    row = _project(args.project)
    pdir = config.project_dir(args.project)
    files = sorted(p.name for p in pdir.glob("*.json")) if pdir.is_dir() else []
    events = db.get_events(args.project, limit=args.events)
    return {
        "project_id": args.project,
        "title": row.get("title"),
        "subject": row.get("subject"),
        "state": row.get("state"),
        "state_reason": row.get("state_reason"),
        "driver": row.get("driver"),
        "aspect": row.get("aspect"),
        "duration": row.get("duration"),
        "spent_cost": row.get("spent_cost"),
        "quota_budget": row.get("quota_budget"),
        "pending_asks": [_ask_summary(r) for r in handoff.pending(args.project)],
        "answered_asks": len(handoff.answered(args.project)),
        "files": files,
        "dir": str(pdir),
        "events": [{k: e.get(k) for k in ("kind", "src", "reason", "created_at", "at")
                    if e.get(k) is not None} for e in events],
        "note": "Claude 会话自己的 token 不进 llm_calls 账本;方舟调用照常记账。",
    }


# --------------------------------------------------------------------------
# 推进
# --------------------------------------------------------------------------
def cmd_step(args: argparse.Namespace) -> dict[str, Any]:
    """一直推进到:要 Claude 回答 / 到门禁 / 终态 / 用完 `--max` 步。"""
    from ..models import GateRequest
    from ..pipeline import locks, orchestrator

    pid = args.project
    row = _project(pid)
    if not orchestrator.is_claude_driven(row):
        raise Rejected("这个项目归后台调度器推进;先 `python -m app.cli adopt <pid>` 接手")
    steps: list[dict[str, Any]] = []
    confirm = args.confirm
    for _ in range(args.max):
        state = State(_project(pid)["state"])
        if state in TERMINAL_STATES or state == State.PAUSED_BUDGET:
            break
        try:
            if state in GATE_STATES:
                if not confirm:
                    break
                gate = "A" if state == State.GATE_A else "B"
                out = orchestrator.confirm_gate(pid, GateRequest(gate=gate, force=args.force))
                confirm = False  # 一次 step 只放行一道门禁
            else:
                out = orchestrator.advance(pid)
        except handoff.NeedsClaude as need:
            return {"project_id": pid, "state": str(state), "steps": steps,
                    "needs_claude": _ask_summary(need.request)}
        except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
            raise Rejected(str(exc)) from exc
        steps.append({k: v for k, v in out.items() if k in (
            "state", "action", "reason", "facts", "segments", "lint_errors", "issues", "shots",
            "layouts", "duration", "warnings", "waiting", "busy", "detail")})
        if out.get("busy"):
            break
        if State(_project(pid)["state"]) == state:
            time.sleep(args.idle)  # 渲染一轮几拍 / 等素材池:同一状态里别空转
    row = _project(pid)
    return {"project_id": pid, "state": row["state"], "state_reason": row.get("state_reason"),
            "steps": steps,
            "pending_asks": [_ask_summary(r) for r in handoff.pending(pid)]}


# --------------------------------------------------------------------------
# 回答文字模型请求
# --------------------------------------------------------------------------
def cmd_ask_list(args: argparse.Namespace) -> dict[str, Any]:
    return {"pending": [_ask_summary(r) for r in handoff.pending(args.project)],
            "answered": handoff.answered(args.project)}


def cmd_ask_show(args: argparse.Namespace) -> str:
    rid = args.id
    if not rid:
        pend = handoff.pending(args.project)
        if not pend:
            raise Rejected("没有待回答的请求")
        rid = str(pend[0]["id"])
    req = handoff.get_request(args.project, rid)
    if req is None:
        raise Rejected(f"没有这个请求:{rid}")
    # 提示词按原文输出,不套 JSON:Claude 要读的是正文
    parts = [
        f"# 请求 {rid}",
        f"- 调用方:{req.get('purpose')}",
        f"- 必需字段:{', '.join(req.get('required_keys') or []) or '(无)'}",
        f"- 回答:python -m app.cli ask answer {args.project} {rid} <回答.json>",
    ]
    if req.get("previous_error"):
        parts.append(f"- 上次回答没通过:{req['previous_error']}")
    parts += ["", "## SYSTEM", req.get("system") or "", "", "## USER", req.get("user") or ""]
    return "\n".join(parts)


def cmd_ask_answer(args: argparse.Namespace) -> dict[str, Any]:
    try:
        data = handoff.put_answer(args.project, args.id, _read_input(args.file))
    except LLMOutputInvalid as exc:
        raise Rejected(str(exc)) from exc
    return {"id": args.id, "saved": True, "keys": sorted(data),
            "next": f"python -m app.cli step {args.project}"}


# --------------------------------------------------------------------------
# 事实与检索(方舟)
# --------------------------------------------------------------------------
def cmd_facts(args: argparse.Namespace) -> dict[str, Any]:
    facts = db.read_json(args.project, "facts.json", []) or []
    keep = ("id", "claim", "basis", "source_kind", "source_tool", "scope", "topic",
            "subject", "compare_with", "enabled", "confidence")
    return {"count": len(facts), "facts": [{k: f.get(k) for k in keep if f.get(k) not in
                                            (None, "")} for f in facts],
            "report": db.read_json(args.project, "facts_report.json", {}) or {}}


def cmd_facts_raw(args: argparse.Namespace) -> list[dict[str, Any]]:
    docs = db.read_json(args.project, "facts_raw.json", []) or []
    return [{"n": i, "tool": d.get("tool"), "query": d.get("query"), "ok": d.get("ok"),
             "chars": len(d.get("text") or ""), "error": d.get("error"),
             **({"text": d.get("text")} if args.full else {})}
            for i, d in enumerate(docs, 1)]


def cmd_facts_add_doc(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import intake
    from ..providers.facts import RawDoc

    row = _project(args.project)
    subject = args.subject or row["subject"]
    text = _read_input(args.file).strip()
    if not text:
        raise Rejected("材料正文是空的")
    doc = RawDoc(tool=args.tool, query=args.query or subject,
                 retrieved_at=time.strftime("%Y-%m-%d"), text=text)
    try:
        n = intake.add_raw_doc(config.project_dir(args.project), subject,
                               row.get("theme") or None, doc)
    except FileNotFoundError as exc:
        raise Rejected(str(exc)) from exc
    return {"subject": subject, "docs": n,
            "note": "下次 step 重跑事实收集时这份材料进抽取提示词(会出一个新的抽取请求)"}


def cmd_search(args: argparse.Namespace) -> list[dict[str, str]]:
    from ..providers import ark_search

    rng = None
    if args.days:
        end = time.strftime("%Y-%m-%d")
        start = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
        rng = ark_search.time_range(start, end)
    return ark_search.search(args.query, count=args.count, time_range=rng,
                             sites=args.sites or None)


def cmd_datapro(args: argparse.Namespace) -> dict[str, Any]:
    from ..providers import ark_datapro

    doc = (ark_datapro.specs(args.subject) if args.kind == "specs"
           else ark_datapro.sales(args.subject, months=args.months))
    return doc.to_dict()


# --------------------------------------------------------------------------
# 门禁 A:改稿 / 改分镜 / 复查
# --------------------------------------------------------------------------
def cmd_script_show(args: argparse.Namespace) -> dict[str, Any]:
    row = _project(args.project)
    return {"title": row.get("title"), "thesis": row.get("thesis"),
            "segments": db.read_json(args.project, "script.json", []) or [],
            "lint": db.read_json(args.project, "lint.json", []) or []}


def cmd_script_put(args: argparse.Namespace) -> dict[str, Any]:
    """门禁 A 改稿:`{"segments": [...], "title": "...", "thesis": "..."}`。返回 lint。"""
    from ..models import ScriptSegment
    from ..pipeline import locks, orchestrator

    data = json.loads(_read_input(args.file))
    segs = [ScriptSegment(**s) for s in data.get("segments") or []]
    if not segs:
        raise Rejected("segments 是空的")
    try:
        issues = orchestrator.update_script(args.project, segs, data.get("title"),
                                            data.get("thesis"))
    except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
        raise Rejected(str(exc)) from exc
    errors = [i for i in issues if i.get("severity") == "error"]
    return {"saved": True, "lint": issues, "errors": len(errors)}


def cmd_lint(args: argparse.Namespace) -> dict[str, Any]:
    """门禁 A 放行前的拦截项(和确认门禁时跑的是同一份)。"""
    from ..pipeline import orchestrator

    row = _project(args.project)
    profile, theme = orchestrator._theme_of(row)
    blockers = orchestrator._gate_a_blockers(args.project, profile, theme)
    return {"blockers": blockers, "pass": not blockers}


def cmd_shots_show(args: argparse.Namespace) -> list[dict[str, Any]]:
    return db.read_json(args.project, "storyboard.json", []) or []


def cmd_shots_put(args: argparse.Namespace) -> dict[str, Any]:
    from ..models import ShotSpec
    from ..pipeline import locks, orchestrator

    specs = [ShotSpec(**s) for s in json.loads(_read_input(args.file))]
    try:
        orchestrator.update_shots(args.project, specs)
    except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
        raise Rejected(str(exc)) from exc
    return {"saved": True, "shots": len(specs)}


# --------------------------------------------------------------------------
# 门禁 B:审片 / 重拍 / 换刀
# --------------------------------------------------------------------------
def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import orchestrator

    return orchestrator.review_progress(args.project)


def cmd_rerender(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import locks, orchestrator

    try:
        return orchestrator.rerender_beats(args.project, args.beats)
    except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
        raise Rejected(str(exc)) from exc


def cmd_cuts(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import orchestrator

    return orchestrator.edit_cuts(args.project)


def cmd_swap(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import locks, orchestrator

    try:
        return orchestrator.swap_cut(args.project, args.key)
    except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
        raise Rejected(str(exc)) from exc


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli",
                                     description="AutoReel 工具层(给 Claude Code 用,输出 JSON)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Callable[[argparse.Namespace], Any], help_: str,
            parent: Any = sub) -> argparse.ArgumentParser:
        p = parent.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    def with_pid(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("project", help="项目 id(p_...)")
        return p

    n = add("new", cmd_new, "建项目(归 Claude 推进)")
    n.add_argument("subject")
    n.add_argument("--duration", type=int, default=120)
    n.add_argument("--aspect", default="", help="16:9 | 9:16,空 = 品类默认")
    n.add_argument("--theme", default="")
    n.add_argument("--category", default="auto_long")
    n.add_argument("--angle", default="")
    n.add_argument("--hook", default="")
    n.add_argument("--why", default="", help="选题依据")
    n.add_argument("--search", default="", help="角度检索词")
    n.add_argument("--owner-notes", default="")
    n.add_argument("--title", default="")
    n.add_argument("--look", default="")
    n.add_argument("--series", default="")

    a = with_pid(add("adopt", cmd_adopt, "把已有项目改归 Claude 推进(--release 还给调度器)"))
    a.add_argument("--release", action="store_true")

    ls = add("list", cmd_list, "列项目")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--claude", action="store_true", help="只列归 Claude 推进的")

    st = with_pid(add("status", cmd_status, "项目状态、待答请求、最近事件"))
    st.add_argument("--events", type=int, default=8)

    sp = with_pid(add("step", cmd_step, "推进到下一个要 Claude 回答 / 门禁 / 终态的地方"))
    sp.add_argument("--confirm", action="store_true", help="放行当前门禁(用户已在对话里同意)")
    sp.add_argument("--force", action="store_true", help="门禁 A 明知有 lint error 仍放行")
    sp.add_argument("--max", type=int, default=60, help="最多推进几步(渲染一步只做几拍)")
    sp.add_argument("--idle", type=float, default=3.0, help="状态没变时歇几秒再推")

    ask = add("ask", lambda a: None, "文字模型请求:list / show / answer")
    ask_sub = ask.add_subparsers(dest="ask_cmd", required=True)
    with_pid(add("list", cmd_ask_list, "待答 / 已答", ask_sub))
    sh = with_pid(add("show", cmd_ask_show, "看请求的提示词原文(不给 id 取最早一个)", ask_sub))
    sh.add_argument("id", nargs="?", default="")
    an = with_pid(add("answer", cmd_ask_answer, "写回答(JSON 文件,- = stdin)", ask_sub))
    an.add_argument("id")
    an.add_argument("file")

    with_pid(add("facts", cmd_facts, "看事实与采集报告"))
    fr = with_pid(add("facts-raw", cmd_facts_raw, "看原始材料清单"))
    fr.add_argument("--full", action="store_true", help="带正文")
    fd = with_pid(add("facts-add-doc", cmd_facts_add_doc, "补一份原始材料(Claude 查到的)"))
    fd.add_argument("file", help="正文文件,- = stdin")
    fd.add_argument("--tool", default="claude.research", help="来源名,进 Fact.source_tool")
    fd.add_argument("--query", default="")
    fd.add_argument("--subject", default="", help="横评时写这份材料属于哪台车")

    se = add("search", cmd_search, "豆包搜索(方舟)")
    se.add_argument("query")
    se.add_argument("--count", type=int, default=10)
    se.add_argument("--days", type=int, default=0, help="只看最近 N 天")
    se.add_argument("--sites", default="", help="限定站点,| 分隔")

    dp = add("datapro", cmd_datapro, "专业数据集(方舟):参数配置 / 月销量")
    dp.add_argument("kind", choices=("specs", "sales"))
    dp.add_argument("subject")
    dp.add_argument("--months", type=int, default=6)

    with_pid(add("script", cmd_script_show, "看脚本与 lint"))
    spu = with_pid(add("script-put", cmd_script_put, "门禁 A 改稿(JSON,- = stdin)"))
    spu.add_argument("file")
    with_pid(add("lint", cmd_lint, "门禁 A 放行前的拦截项"))
    with_pid(add("shots", cmd_shots_show, "看分镜"))
    shp = with_pid(add("shots-put", cmd_shots_put, "门禁 A 改分镜(JSON 数组,- = stdin)"))
    shp.add_argument("file")

    with_pid(add("review", cmd_review, "门禁 B 审片进度"))
    rr = with_pid(add("rerender", cmd_rerender, "门禁 B 重拍这几拍"))
    rr.add_argument("beats", type=int, nargs="+")
    with_pid(add("cuts", cmd_cuts, "剪辑 v2 的刀与候选"))
    sw = with_pid(add("swap", cmd_swap, "换一刀"))
    sw.add_argument("key")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        out = args.fn(args)
    except Rejected as exc:
        _emit({"error": str(exc), "rejected": True})
        return REJECTED
    except handoff.NeedsClaude as need:
        _emit({"needs_claude": _ask_summary(need.request)})
        return NEEDS_CLAUDE
    except Exception as exc:  # noqa: BLE001 - 命令行出口:报错成 JSON
        log.debug("命令失败", exc_info=True)
        _emit({"error": f"{type(exc).__name__}: {exc}"})
        return FAILED
    if isinstance(out, str):
        sys.stdout.write(out + "\n")
    else:
        _emit(out)
    if isinstance(out, dict) and out.get("needs_claude"):
        return NEEDS_CLAUDE
    return OK
