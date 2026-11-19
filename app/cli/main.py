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
        look=args.look or None, series=args.series or None,
        angle_evidence=list(args.evidence) or None,
        subjects=[{"name": n} for n in args.subjects] or None)
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


class _Given:
    """把 Claude 已写好的回答当成一次模型调用交给现有函数,复用它们的校验与组装。
    不传回答时只记下提示词(`system` / `user`),返回空结果。"""

    def __init__(self, answer: dict[str, Any] | None = None) -> None:
        self.answer = answer
        self.system = ""
        self.user = ""

    def available(self) -> bool:
        return True

    def structured(self, system: str, user: str, *, required_keys: Sequence[str] | None = None,
                   **_: Any) -> dict[str, Any]:
        self.system, self.user = system, user
        if self.answer is None:
            return {k: [] for k in required_keys or ()}
        try:
            handoff.require_keys(self.answer, list(required_keys or []))
        except LLMOutputInvalid as exc:
            raise Rejected(str(exc)) from exc
        return self.answer


# --------------------------------------------------------------------------
# 选题(热点 → Claude 出角度 → 现有规则过滤)
# --------------------------------------------------------------------------
def _topics_file(category: str) -> Path:
    return config.DATA_DIR / "_claude" / f"topics_{category}.json"


def cmd_topics_fetch(args: argparse.Namespace) -> dict[str, Any]:
    """抓实时热点,连同选题提示词落到 `data/_claude/topics_<品类>.json`。"""
    from ..categories import load_profile
    from ..pipeline import topics
    from ..providers import trends

    profile = load_profile(args.category)
    items, errors = trends.collect(spec=profile.trends, brands=profile.trend_brands)
    rows = [topics._trend_dict(i) for i in items]
    probe = _Given()
    topics.suggest(rows, profile=profile, llm=probe, n=args.n)
    path = _topics_file(args.category)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"category": args.category, "fetched_at": time.time(), "n": args.n,
                                "trends": rows, "system": probe.system, "user": probe.user},
                               ensure_ascii=False), encoding="utf-8")
    return {"trends": len(rows), "errors": errors, "file": str(path),
            "next": f"python -m app.cli topics prompt --category {args.category}"}


def _topics_cache(category: str) -> dict[str, Any]:
    path = _topics_file(category)
    if not path.is_file():
        raise Rejected(f"还没抓热点:先 python -m app.cli topics fetch --category {category}")
    return json.loads(path.read_text(encoding="utf-8"))


def cmd_topics_prompt(args: argparse.Namespace) -> str:
    data = _topics_cache(args.category)
    if not data.get("system"):
        raise Rejected("没抓到热点,没有选题提示词;看 topics fetch 的 errors")
    age = (time.time() - float(data.get("fetched_at") or 0)) / 3600
    return "\n".join([
        f"# 选题请求({args.category},热点 {len(data.get('trends') or [])} 条,{age:.1f} 小时前抓的)",
        f"- 回答:python -m app.cli topics put <回答.json> --category {args.category}",
        "", "## SYSTEM", data["system"], "", "## USER", data["user"]])


def cmd_topics_put(args: argparse.Namespace) -> dict[str, Any]:
    """Claude 写的选题过一遍现有规则(车型要在热点里出现、禁用词、方向合法……)。"""
    from ..categories import load_profile
    from ..pipeline import topics

    data = _topics_cache(args.category)
    answer = json.loads(_read_input(args.file))
    profile = load_profile(args.category)
    ideas = topics.suggest(data.get("trends") or [], profile=profile, llm=_Given(answer),
                           n=int(data.get("n") or 8))
    asked = len(answer.get("topics") or [])
    return {"kept": [t.model_dump() for t in ideas], "dropped": asked - len(ideas),
            "note": "被丢掉的多半是车型不在热点里、角度带禁用词或方向名不对;"
                    "建项目用 new 的 --angle/--hook/--why/--search"}


# --------------------------------------------------------------------------
# 包装(标题 / 简介 / 首评)
# --------------------------------------------------------------------------
def cmd_package(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import packaging

    _project(args.project)
    pkg = packaging.load(args.project)
    if pkg is None:
        raise Rejected("还没有包装:合成(COMPOSING)时会出一个包装请求,或用 package-put 直接写")
    return pkg.model_dump()


def cmd_package_prompt(args: argparse.Namespace) -> str:
    """包装提示词原文(和合成时发出的请求同一份)。"""
    from ..pipeline import orchestrator, packaging

    profile, _ = orchestrator._theme_of(_project(args.project))
    probe = _Given()
    packaging.generate(args.project, profile, probe)
    return "\n".join([
        f"# 包装请求 {args.project}",
        f"- 回答:python -m app.cli package-put {args.project} <回答.json>",
        "", "## SYSTEM", probe.system, "", "## USER", probe.user])


def cmd_package_put(args: argparse.Namespace) -> dict[str, Any]:
    """写包装:过标题 / 首评校验(数字要出自事实或稿子、禁用词、车名开头……),
    不合格的条目由模板兜底。保留用户已选的封面。"""
    from ..pipeline import orchestrator, packaging

    profile, _ = orchestrator._theme_of(_project(args.project))
    answer = json.loads(_read_input(args.file))
    if not isinstance(answer, dict) or not answer.get("titles"):
        raise Rejected("回答要有 titles(以及 comment、replies),格式见 package-prompt")
    pkg = packaging.ensure(args.project, profile, _Given(answer), force=True)
    wrote = [str(t.get("text") if isinstance(t, dict) else t)
             for t in answer.get("titles") or []]
    kept = [t.text for t in pkg.titles]
    return {"saved": True, "titles": kept,
            "replaced_by_template": [t for t in wrote if t and t not in kept],
            "packaging": pkg.model_dump()}


# --------------------------------------------------------------------------
# 概念动画:Claude 为某一拍现写 HyperFrames 片段
# --------------------------------------------------------------------------
#: 新片段的起点:没给 --from、这一拍也没指定库场景时用它(结构最简、注释最全)
_MOTION_STARTER = "battery"


def _ascii_starter(text: str) -> str:
    """库场景复制成项目片段时去掉中文注释(lint 只许 ASCII):HTML 注释只留 `@xxx` 元信息行,
    `//` 与 `/* */` 注释里有中文就整条删。代码里的中文字面量(如 kicker 文案)留着,
    交给 lint 指出来,由 Claude 改成从 `__DATA__` 取。"""
    import re

    def html_comment(m: re.Match[str]) -> str:
        body = m.group(1)
        if body.isascii():
            return m.group(0)
        metas = re.findall(r"@[A-Za-z_-]+[^\n@]*", body)
        metas = [s.strip() for s in metas if s.isascii()]
        return f"<!-- {' '.join(metas)} -->" if metas else ""

    text = re.sub(r"<!--(.*?)-->", html_comment, text, flags=re.S)
    text = re.sub(r"/\*(?:(?!\*/).)*?[^\x00-\x7f](?:(?!\*/).)*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^(\s*)//[^\n]*[^\x00-\x7f][^\n]*\n", "", text)
    return re.sub(r"(?m)[ \t]+//[^\n]*[^\x00-\x7f][^\n]*$", "", text)


def _motion_beat(pid: str, beat: int) -> dict[str, Any]:
    """这一拍做动画要用的一切:分镜、数据、拍长、画幅、事实、片段路径。"""
    from ..pipeline import aspect, motion, orchestrator

    row = _project(pid)
    spec = next((s for s in orchestrator._specs(pid) if s.idx == beat), None)
    if spec is None:
        raise Rejected(f"没有第 {beat} 拍(还没分镜,或拍号不对:shots {pid})")
    profile, _ = orchestrator._theme_of(row)
    overlay = dict(spec.overlay or {})
    path = motion.project_scene_path(pid, beat)
    data = motion.scene_data(overlay, str(row.get("subject") or ""),
                             getattr(profile.vocab, "concept_ranges", None)) if overlay else {}
    if path.is_file():
        data.update(motion.project_scene_data(path))
    return {"row": row, "spec": spec, "overlay": overlay, "data": data, "path": path,
            "side": path.with_suffix(".json"), "size": aspect.frame_size(row),
            "seconds": max(0.5, float(spec.duration)), "facts": orchestrator._facts(pid)}


def _motion_lint(m: dict[str, Any]) -> list[dict[str, str]]:
    from ..pipeline import motion_lint

    if not m["path"].is_file():
        raise Rejected(f"第 {m['spec'].idx} 拍还没有片段:先 motion new")
    issues = motion_lint.lint(m["path"].read_text(encoding="utf-8"), m["data"], m["facts"],
                              m["seconds"], m["size"])
    return [i.to_dict() for i in issues]


def _motion_ready(m: dict[str, Any]) -> list[dict[str, str]]:
    issues = _motion_lint(m)
    errs = [i for i in issues if i["severity"] == "error"]
    if errs:
        raise Rejected("片段没过 lint,先改:" + ";".join(
            f"{i['message']}({i['where']})" if i["where"] else i["message"] for i in errs[:6]))
    return issues


def cmd_motion_info(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import motion

    m = _motion_beat(args.project, args.beat)
    spec = m["spec"]
    notes = []
    if spec.layout != "concept":
        notes.append(f"这一拍版式是 {spec.layout or '(空)'},现写片段只在 concept 拍生效:"
                     "先 shots-put 把 layout 改成 concept(overlay 里 rows 照事实填)")
    return {
        "beat": spec.idx, "layout": spec.layout, "section": spec.section,
        "narration": spec.narration_zh, "seconds": m["seconds"],
        "size": list(m["size"]), "overlay": m["overlay"], "data": m["data"],
        "fragment": str(m["path"]), "fragment_exists": m["path"].is_file(),
        "sidecar": str(m["side"]),
        "facts": [{"id": f.id, "claim": f.claim, "basis": f.basis} for f in m["facts"]
                  if f.enabled],
        "library_scenes": sorted(motion.available_scenes()),
        "notes": notes,
    }


def cmd_motion_new(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import motion

    m = _motion_beat(args.project, args.beat)
    if m["path"].is_file() and not args.force:
        raise Rejected(f"已有片段 {m['path']};要从头来加 --force")
    base = args.base or str(m["overlay"].get("scene") or "") or _MOTION_STARTER
    src = motion.scenes_dir() / f"{base}.html"
    if not src.is_file():
        raise Rejected(f"库里没有场景 {base}:可选 {', '.join(sorted(motion.available_scenes()))}")
    m["path"].parent.mkdir(parents=True, exist_ok=True)
    text = _ascii_starter(src.read_text(encoding="utf-8"))
    m["path"].write_text(text, encoding="utf-8", newline="\n")
    left = sorted({ch for ch in text if not ch.isascii()})
    return {"fragment": str(m["path"]), "from": base, "sidecar": str(m["side"]),
            "non_ascii_left": "".join(left)[:40],
            "data_keys": sorted(m["data"]),
            "rules": "片段只许 ASCII(中文写进 sidecar JSON,经 window.__DATA__ 注入);"
                     "不联网;画面上的数字都要出自事实;时间轴不超过拍长"}


def cmd_motion_lint(args: argparse.Namespace) -> dict[str, Any]:
    issues = _motion_lint(_motion_beat(args.project, args.beat))
    return {"issues": issues, "errors": sum(i["severity"] == "error" for i in issues),
            "pass": not any(i["severity"] == "error" for i in issues)}


def cmd_motion_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    """截几帧 PNG 给 Claude 看(暗色底、不铺实拍;正式渲染时压在这一拍的实拍上)。"""
    from ..pipeline import motion

    m = _motion_beat(args.project, args.beat)
    warnings = _motion_ready(m)
    at = [float(x) for x in args.at.split(",") if x.strip()] if args.at else [
        round(m["seconds"] * r, 2) for r in (0.15, 0.5, 0.95)]
    try:
        pngs = motion.snapshot(m["path"], m["data"], m["seconds"], at, m["size"],
                               m["path"].parent / "_snap")
    except motion.MotionError as exc:
        raise Rejected(f"截帧失败:{exc}") from exc
    return {"frames": [str(p) for p in pngs], "at": at, "warnings": warnings,
            "next": "用 Read 看图:构图、文字溢出、安全区、动效节点"}


def cmd_motion_render(args: argparse.Namespace) -> dict[str, Any]:
    """渲成整段 mp4 预览(按内容缓存)。正式成片由渲染阶段自己渲,这里只是先看动效。"""
    from ..pipeline import motion

    m = _motion_beat(args.project, args.beat)
    warnings = _motion_ready(m)
    dest = m["path"].with_suffix(".preview.mp4")
    try:
        out = motion.render(m["path"], m["data"], m["seconds"], m["size"], dest)
    except motion.MotionError as exc:
        raise Rejected(f"渲染失败:{exc}") from exc
    return {"video": str(out), "seconds": m["seconds"], "warnings": warnings,
            "check": f'ffmpeg -v error -i "{out}" -f null -'}


# --------------------------------------------------------------------------
# 配音:看质检、试听
# --------------------------------------------------------------------------
def cmd_voice(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import knobs

    row = _project(args.project)
    nar = db.read_json(args.project, "narration.json", {}) or {}
    qa = db.read_json(args.project, "voice_qa.json", {}) or {}
    if not nar:
        raise Rejected(f"还没配音:旁白在 ASSETS_READY 之后生成(现在 {row['state']})")
    return {
        "voice": knobs.voice(),
        "sentences": nar.get("sentence_count"),
        "chars": nar.get("total_chars"),
        "duration": nar.get("total_duration"),
        "chars_per_second": round(float(nar.get("total_chars") or 0)
                                  / max(float(nar.get("total_duration") or 0), 0.1), 2),
        "narration": nar.get("narration_path"),
        "subtitles": nar.get("ass_path"),
        "bgm": nar.get("bgm_path"),
        "warnings": nar.get("warnings") or [],
        "qa": {"errors": [i + 1 for i in qa.get("errors") or []],  # 第几句(1 起)
               "findings": qa.get("findings") or [], "redone": qa.get("redone") or [],
               "asr_available": qa.get("asr_available")} if qa else {},
    }


# --------------------------------------------------------------------------
# 生图(方舟 Seedream)
# --------------------------------------------------------------------------
def cmd_image(args: argparse.Namespace) -> dict[str, Any]:
    """生图存进项目的 `images/`(或 --out 目录),返回路径给 Claude 看图挑。"""
    import httpx

    from ..llm import ledger
    from ..providers import ark_image

    if args.project:
        _project(args.project)
        out_dir = config.project_dir(args.project) / "images"
    else:
        out_dir = Path(args.out or config.DATA_DIR / "_claude" / "images")
    out_dir.mkdir(parents=True, exist_ok=True)
    with ledger.tag(project_id=args.project or ""):  # 账本由 ark_image 自己记
        urls = ark_image.generate(args.prompt, n=args.n, aspect_ratio=args.ratio or None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths, failed = [], []
    for i, url in enumerate(urls):
        try:
            resp = httpx.get(url, timeout=60)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            failed.append(f"{url}:{exc}")
            continue
        ext = ".png" if "png" in resp.headers.get("content-type", "") else ".jpg"
        path = out_dir / f"{stamp}_{i + 1}{ext}"
        path.write_bytes(resp.content)
        paths.append(str(path))
    (out_dir / f"{stamp}.prompt.txt").write_text(args.prompt, encoding="utf-8")
    return {"images": paths, "failed": failed, "prompt": args.prompt,
            "next": "用 Read 看图,不满意就改提示词重生"}


# --------------------------------------------------------------------------
# 封面
# --------------------------------------------------------------------------
def cmd_cover(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import aspect, packaging

    row = _project(args.project)
    pdir = config.project_dir(args.project)
    files = {v: pdir / aspect.cover_name(row, v) for v in ("a", "b")}
    pkg = packaging.load(args.project)
    return {"covers": {v: str(p) for v, p in files.items() if p.is_file()},
            "titles": [t.text for t in pkg.titles] if pkg else [],
            "picked": pkg.picked if pkg else {},
            "rendered": bool(db.read_json(args.project, "render.json", {}))}


def cmd_cover_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    """按选定的标题重画 A / B 封面(不重新合成视频)。"""
    from ..pipeline import packaging
    from ..pipeline.cover_art import builder

    _project(args.project)
    if args.title is not None:
        pkg = packaging.load(args.project)
        if pkg is None or not 0 <= args.title < len(pkg.titles):
            raise Rejected("没有这条标题:先 package 看下标")
        packaging.set_picked(args.project, title=args.title)
    out = builder.rebuild(args.project)
    if not out.get("ok"):
        raise Rejected(str(out.get("reason") or "封面重画失败"))
    a, b = out.get("a") or {}, out.get("b") or {}
    if not a:
        raise Rejected("A 版封面没画出来(退回了老封面):看 /api/degradations 的 cover.*")
    return {"a": a.get("path"), "b": b.get("path"), "main": a.get("main"), "sub": a.get("sub"),
            "car_source": a.get("car_source"), "next": "用 Read 看两张封面"}


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
    """门禁 B 自审一页看全:进度、时间线体检、预览在不在 / 是不是最新、这个项目的降级。
    `--qa` 再对预览跑逐帧 + 音频 QA,返回报告和联系表路径(用 Read 看)。"""
    import subprocess
    import sys

    from .. import degrade
    from ..pipeline import orchestrator
    from ..pipeline.edit import preview as edit_preview
    from ..pipeline.edit import project as edit_project

    row = _project(args.project)
    out: dict[str, Any] = {"state": row["state"], "progress": orchestrator.review_progress(
        args.project)}
    try:
        out["edit"] = edit_project.check(args.project)
    except KeyError:
        out["edit"] = None
    out["preview"] = edit_preview.status(args.project)
    out["degradations"] = degrade.recent(20, project_id=args.project)
    if args.qa:
        video = edit_preview.path(args.project)
        if not video.is_file():
            raise Rejected("还没有整片预览:先 edit preview")
        proc = subprocess.run(
            [sys.executable, str(config.BASE_DIR / "scripts" / "review_render.py"), args.project,
             "--artifact", video.name], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800)
        qa_dir = config.project_dir(args.project) / "_render" / "qa"
        out["qa"] = {"failed": proc.returncode != 0,
                     "report": str(qa_dir / "qa_report.md"),
                     "sheets": sorted(str(p) for p in (qa_dir / "sheets").glob("*.jpg")),
                     "summary": proc.stdout.strip().splitlines()[-12:],
                     "stderr": proc.stderr.strip()[-500:]}
    return out



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
# 剪辑(门禁 B):Claude 看拼图、钉选画面
# --------------------------------------------------------------------------
def _beats_filter(spec: str) -> set[int] | None:
    if not spec.strip():
        return None
    out: set[int] = set()
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


def _edit_dir(pid: str) -> Path:
    return config.project_dir(pid) / "_edit"


def cmd_edit_sheet(args: argparse.Namespace) -> dict[str, Any]:
    """刀表拼图:每刀一格(这一刀第一段素材的中间帧),写着刀号、起止、半句、来源。"""
    from ..pipeline.edit import project as edit_project
    from ..pipeline.edit import sheet

    _project(args.project)
    beats = _beats_filter(args.beats)
    rows = [r for r in edit_project.cuts(args.project) if beats is None or r["beat"] in beats]
    if not rows:
        raise Rejected("没有刀可看:还没有剪辑 v2 时间线,或拍号不对")
    tiles = []
    for r in rows:
        mark = ("钉 " if r["pinned"] else "") + (r["via"] or "") + (
            "" if not r["need"] else (" 对题" if r["match"] else " 没对上"))
        tiles.append((edit_project.cut_thumb(args.project, r["key"]),
                      [f"{r['key']}  {r['start']:.1f}-{r['end']:.1f}s  {mark}", r["text"],
                       ", ".join(p["item_id"] for p in r["pieces"])]))
    tag = args.beats.replace(",", "_") or "all"
    paths = sheet.build(tiles, _edit_dir(args.project) / "sheets" / f"cuts_{tag}.jpg")
    return {"sheets": [str(p) for p in paths], "cuts": len(rows),
            "next": "用 Read 看图;要换的刀 → edit candidates <pid> <key>"}


def cmd_edit_candidates(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline import orchestrator
    from ..pipeline.edit import sheet

    _project(args.project)
    try:
        got = orchestrator.edit_candidates(args.project, args.key, pages=args.pages)
    except orchestrator.PipelineError as exc:
        raise Rejected(str(exc)) from exc
    work = _edit_dir(args.project) / "candidates"
    safe = args.key.replace(":", "_")
    tiles = []
    for c in got["candidates"]:
        frame = Path(c["frame"]) if c["frame"] else None
        if frame is None and c["kind"] == "video":
            frame = sheet.frame_of(Path(c["path"]), (c["start"] + c["end"]) / 2,
                                   work / f"{safe}_{c['n']}.jpg")
        flags = " ".join(x for x in ("现用" if c["current"] else "", "官方" if c["official"] else "",
                                     f"用过{c['used']}" if c["used"] else "",
                                     f"对题:{c['match']}" if c["match"] else "") if x)
        dur = f"{c['seconds']}s" if c["seconds"] else "静图"
        tiles.append((frame, [f"#{c['n']} {c['item_id']}  {c['view']} {dur}  {flags}",
                              c["desc"] or "(无描述)"]))
    paths = sheet.build(tiles, work / f"{safe}.jpg") if tiles else []
    work.mkdir(parents=True, exist_ok=True)
    (work / f"{safe}.json").write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
    got["sheets"] = [str(p) for p in paths]
    got["next"] = f"用 Read 看图挑;edit pick {args.project} {args.key} <item_id> [<item_id> ...]"
    return got


def _pick_spec(pid: str, key: str, item_id: str) -> dict[str, Any]:
    """钉选的一段:长视频切出来的块(`id~k`)按最近一次 candidates 记下的起止截。"""
    cache = _edit_dir(pid) / "candidates" / f"{key.replace(':', '_')}.json"
    if "~" in item_id and cache.is_file():
        for c in json.loads(cache.read_text(encoding="utf-8")).get("candidates") or []:
            if c.get("item_id") == item_id:
                return {"item_id": item_id, "start": c["start"], "end": c["end"]}
    return {"item_id": item_id}


def _pin(pid: str, pins: dict[str, list[Any]], unpin: list[str]) -> dict[str, Any]:
    from ..pipeline import locks, orchestrator

    _project(pid)
    spec = {k: [_pick_spec(pid, k, x) if isinstance(x, str) else dict(x) for x in v]
            for k, v in pins.items()}
    try:
        out = orchestrator.pin_cuts(pid, spec, unpin)
    except (orchestrator.PipelineError, locks.ProjectBusy) as exc:
        raise Rejected(str(exc)) from exc
    out["next"] = "改完一批再 edit preview 看整片(edit check 看体检)"
    return out


def cmd_edit_pick(args: argparse.Namespace) -> dict[str, Any]:
    return _pin(args.project, {args.key: list(args.items)}, [])


def cmd_edit_put(args: argparse.Namespace) -> dict[str, Any]:
    data = json.loads(_read_input(args.file))
    if not isinstance(data, dict) or not isinstance(data.get("pins", {}), dict):
        raise Rejected('格式:{"pins": {"3:0": ["item_id", ...]}, "unpin": ["5:1"]}')
    return _pin(args.project, dict(data.get("pins") or {}),
                [str(k) for k in data.get("unpin") or []])


def cmd_edit_unpin(args: argparse.Namespace) -> dict[str, Any]:
    return _pin(args.project, {}, list(args.keys))


def cmd_edit_check(args: argparse.Namespace) -> dict[str, Any]:
    from ..pipeline.edit import project as edit_project

    row = _project(args.project)
    try:
        return edit_project.check(args.project,
                                  still_max=0.0 if row.get("theme") == "mechanism" else None)
    except KeyError as exc:
        raise Rejected(str(exc.args[0])) from exc


def cmd_edit_preview(args: argparse.Namespace) -> dict[str, Any]:
    """按当前时间线当场渲整片预览(不排 jobs,不依赖 worker)。"""
    from ..pipeline.edit import preview as edit_preview

    _project(args.project)
    try:
        got = edit_preview.render_once(args.project)
    except Exception as exc:  # noqa: BLE001 —— 渲染炸了原样报给 Claude
        raise Rejected(f"预览渲染失败:{exc}") from exc
    if got.get("skipped"):
        raise Rejected(str(got["skipped"]))
    return {"video": got["path"], "check": f'ffmpeg -v error -i "{got["path"]}" -f null -',
            "next": "review <pid> --qa 出逐帧联系表和音频检查"}


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
    n.add_argument("--evidence", action="append", default=[],
                   help="热点依据原文一行(可多次),作为一份事实素材")
    n.add_argument("--subjects", action="append", default=[],
                   help="横评车单里的一台车(可多次);此时 subject 是横评标题")

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

    tp = add("topics", lambda a: None, "选题:fetch / prompt / put")
    tp_sub = tp.add_subparsers(dest="topics_cmd", required=True)
    tf = add("fetch", cmd_topics_fetch, "抓实时热点并生成选题提示词", tp_sub)
    tf.add_argument("--n", type=int, default=8, help="要几个选题")
    tpp = add("prompt", cmd_topics_prompt, "看选题提示词原文", tp_sub)
    tpu = add("put", cmd_topics_put, "交选题(JSON,- = stdin),按规则过滤", tp_sub)
    tpu.add_argument("file")
    for p in (tf, tpp, tpu):
        p.add_argument("--category", default="auto_long")

    with_pid(add("package", cmd_package, "看包装(标题 / 简介 / 首评)"))
    with_pid(add("package-prompt", cmd_package_prompt, "看包装提示词原文"))
    pkp = with_pid(add("package-put", cmd_package_put, "写包装(JSON,- = stdin)"))
    pkp.add_argument("file")

    mo = add("motion", lambda a: None, "概念动画(Claude 现写片段):info / new / lint / snapshot / render")
    mo_sub = mo.add_subparsers(dest="motion_cmd", required=True)
    mi = with_pid(add("info", cmd_motion_info, "这一拍的旁白、数据、拍长、事实", mo_sub))
    mn = with_pid(add("new", cmd_motion_new, "从库场景复制一份起点片段", mo_sub))
    mn.add_argument("--from", dest="base", default="", help="库场景名,默认用分镜里的 scene")
    mn.add_argument("--force", action="store_true", help="覆盖已有片段")
    ml = with_pid(add("lint", cmd_motion_lint, "片段硬校验", mo_sub))
    ms = with_pid(add("snapshot", cmd_motion_snapshot, "截几帧 PNG", mo_sub))
    ms.add_argument("--at", default="", help="秒,逗号分隔;默认开头 / 中间 / 结尾")
    mr = with_pid(add("render", cmd_motion_render, "渲整段 mp4 预览(几十秒)", mo_sub))
    for p in (mi, mn, ml, ms, mr):
        p.add_argument("beat", type=int)

    with_pid(add("voice", cmd_voice, "旁白时长、语速、语音质检结果"))
    im = add("image", cmd_image, "方舟 Seedream 生图")
    im.add_argument("prompt")
    im.add_argument("--project", default="", help="存进这个项目的 images/")
    im.add_argument("--out", default="", help="不挂项目时的输出目录")
    im.add_argument("--n", type=int, default=2)
    im.add_argument("--ratio", default="16:9", help="16:9 | 9:16 | 3:4 | 4:3 | 1:1")
    with_pid(add("cover", cmd_cover, "看封面与可选标题"))
    cr = with_pid(add("cover-rebuild", cmd_cover_rebuild, "按选定标题重画 A / B 封面"))
    cr.add_argument("--title", type=int, default=None, help="选第几条标题(package 里的下标)")

    with_pid(add("script", cmd_script_show, "看脚本与 lint"))
    spu = with_pid(add("script-put", cmd_script_put, "门禁 A 改稿(JSON,- = stdin)"))
    spu.add_argument("file")
    with_pid(add("lint", cmd_lint, "门禁 A 放行前的拦截项"))
    with_pid(add("shots", cmd_shots_show, "看分镜"))
    shp = with_pid(add("shots-put", cmd_shots_put, "门禁 A 改分镜(JSON 数组,- = stdin)"))
    shp.add_argument("file")

    rv = with_pid(add("review", cmd_review, "门禁 B 审片:进度、体检、预览、降级"))
    rv.add_argument("--qa", action="store_true",
                    help="对整片预览跑逐帧 + 音频 QA(scripts/review_render.py,较慢)")
    rr = with_pid(add("rerender", cmd_rerender, "门禁 B 重拍这几拍"))
    rr.add_argument("beats", type=int, nargs="+")
    with_pid(add("cuts", cmd_cuts, "剪辑 v2 的刀与候选"))
    sw = with_pid(add("swap", cmd_swap, "换一刀"))
    sw.add_argument("key")

    ed = add("edit", lambda a: None,
             "剪辑(门禁 B):sheet / candidates / pick / put / unpin / check / preview")
    ed_sub = ed.add_subparsers(dest="edit_cmd", required=True)
    es = with_pid(add("sheet", cmd_edit_sheet, "整片刀表拼图(每刀一格)", ed_sub))
    es.add_argument("--beats", default="", help="只看这几拍,如 3-7 或 2,5")
    ec = with_pid(add("candidates", cmd_edit_candidates, "某一刀的候选画面 + 拼图", ed_sub))
    ec.add_argument("key", help="拍号:第几刀,如 3:0")
    ec.add_argument("--pages", type=int, default=1, help="每页 6 个,多翻几页")
    ep = with_pid(add("pick", cmd_edit_pick, "钉选一刀的画面(可接几段)", ed_sub))
    ep.add_argument("key")
    ep.add_argument("items", nargs="+", help="素材 id(候选里的 item_id),按顺序拼接")
    eput = with_pid(add("put", cmd_edit_put, "批量钉选 / 放开", ed_sub))
    eput.add_argument("file", help='{"pins": {"3:0": ["id", ...]}, "unpin": ["5:1"]};- = stdin')
    eu = with_pid(add("unpin", cmd_edit_unpin, "放开钉选(回到自动取材)", ed_sub))
    eu.add_argument("keys", nargs="+")
    with_pid(add("check", cmd_edit_check, "时间线体检", ed_sub))
    with_pid(add("preview", cmd_edit_preview, "当场渲一版整片预览(几分钟,可后台跑)", ed_sub))
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
