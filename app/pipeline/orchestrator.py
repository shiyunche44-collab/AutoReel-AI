"""流水线编排:把各阶段按状态机串起来,并提供给后台调度器轮询。

分工:本模块只管"当前状态该做什么",不直接关心 HTTP。
`advance()` 推进一个非阻塞步骤;后台调度循环在渲染态反复调用它,每次渲染几拍。

两个设计约束贯穿全文件:
1. **门禁态停下等人**。GATE_A / GATE_B 不做任何自动推进。
2. **幂等**。每个阶段先检查产物是否已存在,已存在则跳过,重跑不重复消耗配额。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .. import config, costs, db
from ..categories import ProfileError, load_profile
from ..llm.deepseek import DeepSeekProvider
from ..llm.minimax import MiniMaxProvider
from ..models import GateRequest, ProjectCreate, ScriptSegment, ShotSpec
from ..state import State, assert_transition, can_transition
from . import concept_styles, explainer, intake, library, locks, motion, rhythm, script_gen

log = logging.getLogger(__name__)

#: 需要 LLM 的阶段在缺 Key 时的提示
LLM_REQUIRED_HINT = (
    "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入 Key 后重启服务。"
)

class PipelineError(RuntimeError):
    """流水线无法继续。"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _llm():
    """取可用的 LLM:MiniMax 优先,回退 DeepSeek。两者都没有则返回 None。"""
    minimax = MiniMaxProvider()
    if minimax.available():
        return minimax
    deepseek = DeepSeekProvider()
    if deepseek.available():
        return deepseek
    return None


def _profile(project: dict[str, Any]):
    return load_profile(project["category"])


def _set_state(project_id: str, src: State | str, dst: State | str,
               reason: str = "", cost: float = 0.0) -> None:
    """推进项目状态,并留一条事件。

    两件事合在一个**事务**里:状态与事件必须同生共死 ——
    事件流是本项目唯一的排障依据,"状态变了但没事件"的裂缝
    比没有事件更误导人。

    状态写用 CAS(`WHERE state = :expected`):原来的"读出来 → 判断
    转移合法 → 无条件 UPDATE"中间有 TOCTOU 窗口,两个执行流都读到
    DRAFT_RENDERING 时会各自开渲同一批拍。CAS 把判断和写合成一条
    SQL,窗口就没了。CAS 失败说明状态已被别人推进,放弃本轮。
    """
    assert_transition(src, dst)
    with db.transaction() as conn:
        moved = db.cas_state(project_id, str(src), str(dst), conn=conn, reason=reason)
        if not moved:
            raise PipelineError(
                f"状态已被并发推进(期望 {src},实际不是),本轮放弃"
            )
        db.add_event(project_id, str(dst), src=str(src), reason=reason, cost=cost, conn=conn)


def _facts(project_id: str) -> list:
    from ..models import Fact

    return [Fact(**d) for d in db.read_json(project_id, "facts.json", []) or []]


def _script(project_id: str) -> list:
    from ..models import ScriptSegment

    return [ScriptSegment(**d) for d in db.read_json(project_id, "script.json", []) or []]


def _specs(project_id: str) -> list[ShotSpec]:
    return [ShotSpec(**d) for d in db.read_json(project_id, "storyboard.json", []) or []]


def _save_script(project_id: str, segments: list[ScriptSegment]) -> None:
    db.write_json(project_id, "script.json", [s.model_dump() for s in segments])


def _save_specs(project_id: str, specs: list[ShotSpec]) -> None:
    db.write_json(project_id, "storyboard.json", [s.model_dump() for s in specs])


# --------------------------------------------------------------------------
# 内容方向
# --------------------------------------------------------------------------
def _theme_name(project: dict[str, Any]) -> str:
    """取项目的内容方向;缺失或已失效时回退品类默认方向。"""
    return _profile(project).default_theme


def _theme_of(project: dict[str, Any]):
    profile = _profile(project)
    return profile, profile.theme_spec(project.get("theme") or None)


# --------------------------------------------------------------------------
# 项目创建
# --------------------------------------------------------------------------
def create_project(payload: ProjectCreate) -> str:
    from ..models import new_id

    try:
        profile = _profile({"category": payload.category})
    except ProfileError as exc:
        raise PipelineError(str(exc)) from exc
    if not profile.is_explainer:
        raise PipelineError(f"品类「{profile.label}」不是横屏解说轨,已不再支持。")
    if profile.skeleton:
        raise PipelineError(
            f"品类「{profile.label}」目前是骨架配置,尚未完整实现。"
            f"待补齐:{'; '.join(profile.todo) or '行业逻辑'}"
        )
    if payload.duration not in profile.supported_durations():
        raise PipelineError(
            f"不支持的时长 {payload.duration}s。可选:{list(profile.supported_durations())}"
        )

    theme_name = (payload.theme or "").strip()
    if theme_name and theme_name not in profile.themes:
        raise PipelineError(
            f"品类「{profile.label}」没有名为「{theme_name}」的内容方向。"
            f"可选:{sorted(profile.themes)}"
        )
    if not theme_name:
        theme_name = profile.default_theme

    theme = profile.theme_spec(theme_name)
    subjects = roundup_subjects(payload, theme) if theme.roundup else []

    # 时长能否铺展出正确长度的段落序列,在这里就验掉,
    # 而不是等跑到 FACTS_RESOLVED 才发现配置有问题。
    names = [s["name"] for s in subjects]
    plan = profile.slots_for(payload.duration, theme_name, names or None)
    expected_shots = profile.shots_for(payload.duration)
    # 车多时长短会自动精简每车段落(见 roundup_blocks);配置自检保证车单上限精简后
    # 放得进最短时长,这里只是兜底,不让结论段被截掉
    need = len(profile.roundup_blocks(theme, names, shots=expected_shots)) if theme.roundup else 0
    if need > expected_shots:
        raise PipelineError(
            f"{len(names)} 台车在 {payload.duration}s 下精简后仍需 {need} 拍,只有 {expected_shots} 拍。"
            f"请加长时长或减少车数。"
        )
    if len(plan) != expected_shots:
        raise PipelineError(
            f"方向「{theme_name}」在 {payload.duration}s 下铺展出 {len(plan)} 个段落,"
            f"应为 {expected_shots} 个。请检查 Profile 的 blocks 权重配置。"
        )

    project_id = new_id("p_")
    shot_count = expected_shots
    budget = payload.quota_budget or costs.default_budget_for(payload.duration)
    # 横屏解说轨不调视频生成,只有 TTS、BGM 与几次 LLM
    est = costs.estimate_explainer(payload.duration)["with_retry"]
    now = time.time()
    title = payload.title or (payload.subject if theme.roundup else f"{payload.subject} 解说视频")

    db.create_project(
        {
            "id": project_id,
            "title": title,
            "subject": payload.subject,
            "category": payload.category,
            "theme": theme_name,
            "angle": (payload.angle or "").strip(),
            "subjects": subjects,
            "duration": payload.duration,
            "shot_count": shot_count,
            "narration_mode": payload.narration_mode,
            "state": str(State.DRAFT_INTENT),
            "state_reason": None,
            "quota_budget": budget,
            "spent_cost": 0.0,
            "est_cost": est,
            "created_at": now,
            "updated_at": now,
        }
    )
    angle = (payload.angle or "").strip()
    if angle and (payload.angle_search or payload.angle_evidence):
        db.write_json(project_id, "topic.json", {
            "angle": angle,
            "search": (payload.angle_search or "").strip(),
            "evidence": [str(e)[:300] for e in payload.angle_evidence or []],
        })
    db.add_event(
        project_id, str(State.DRAFT_INTENT),
        reason=f"创建项目:{title}(方向:{theme_name}"
               + (f",角度:{payload.angle.strip()}" if (payload.angle or "").strip() else "")
               + ")",
    )
    return project_id


def roundup_subjects(payload: ProjectCreate, theme) -> list[dict[str, str]]:
    """横评车单:去空白、按车名去重,台数要在方向的 subject_count 范围内。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload.subjects or []:
        name = " ".join(item.name.split())
        key = name.replace(" ", "").lower()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "group": " ".join(item.group.split())})
    lo, hi = theme.subject_count
    if not lo <= len(out) <= hi:
        raise PipelineError(f"「{theme.label}」需要 {lo}–{hi} 台车,车单里有 {len(out)} 台(去重后)。")
    return out


# --------------------------------------------------------------------------
# 单步推进
# --------------------------------------------------------------------------
#: 后台每轮渲染几拍(一拍约 5 秒;调度循环是同步的,一轮不能拖太久)
EXPLAINER_BEATS_PER_TICK = 4


def advance(project_id: str, *, confirmed: bool = False, force: bool = False,
            stop_event: threading.Event | None = None) -> dict[str, Any]:
    """推进一个步骤。返回本次动作的描述。

    `confirmed=True` 表示"人工门禁已通过",允许越过 GATE_A/GATE_B。
    必须显式传这个参数,而不是靠调用方改状态:后台调度循环也在不停调
    advance(),若它能自行放行门禁,两道人工审核就形同虚设。

    `force=True` 表示门禁 A 的 lint error 明知故犯仍然放行(只给显式确认用)。

    `stop_event`:停机信号(跨线程可见)。渲染时在**每拍之间**检查它 ——
    手头这一拍做完就收手,剩下的拍留到下次。服务被收走时就不会留下
    半截 mp4,重启后从断点接着跑。

    **本函数自己是加锁的那一层**:所有会写项目的入口都从这里过一遍,
    保证同一时刻只有一个执行流在推进这个项目(见 `pipeline/locks.py`)。
    拿不到锁时返回 `{"busy": True}`,不阻塞、不报错 —— 正在跑的那一路
    会把这活干完,下一轮再来。
    """
    with locks.project_lock(project_id) as got:
        if not got:
            row = db.get_project(project_id) or {}
            return {"state": row.get("state") or "", "action": "项目正在推进中",
                    "busy": True, "waiting": False}
        try:
            return _advance(project_id, confirmed=confirmed, force=force,
                            stop_event=stop_event)
        except costs.BudgetExceeded as exc:
            # 额度用尽:不是错误,是"该停"。进 PAUSED_BUDGET,提额后可恢复。
            return _pause_for_budget(project_id, exc)


def _pause_for_budget(project_id: str, exc: Exception) -> dict[str, Any]:
    """因配额暂停项目。

    `State.PAUSED_BUDGET` 之前是**不可达**的 —— 转移表里有它,代码里
    没有任何路径能进去。也就是说 README 宣称的"项目级节流阈值"从来没
    生效过:一次失控的 TTS/LLM 循环可以把整个套餐烧穿,毫无拦截。

    现在:超限时进 PAUSED_BUDGET,并把暂停前的状态记在 `resume_state`,
    提额后从那里继续。
    """
    row = db.get_project(project_id) or {}
    cur = str(row.get("state") or "")
    try:
        db.update_project(project_id, resume_state=cur)
        _set_state(project_id, cur, State.PAUSED_BUDGET, reason=str(exc))
    except Exception:  # noqa: BLE001 - 暂停失败也不能让调用方炸掉
        log.exception("把项目 %s 转入 PAUSED_BUDGET 失败", project_id)
    return {"state": str(State.PAUSED_BUDGET), "action": "已暂停(配额)",
            "waiting": True, "detail": str(exc)}


def set_budget(project_id: str, budget: float) -> dict[str, Any]:
    """改项目的额度上限;若项目正因配额暂停,顺带恢复到暂停前的状态。"""
    row = db.get_project(project_id)
    if not row:
        raise PipelineError("项目不存在")
    budget = max(0.0, float(budget))
    with locks.require_project_lock(project_id):
        db.update_project(project_id, quota_budget=budget)
        out: dict[str, Any] = {"budget": budget, "state": row["state"]}
        if State(row["state"]) == State.PAUSED_BUDGET:
            resume = str(row.get("resume_state") or "")
            try:
                target = State(resume)
            except ValueError:
                target = State.GATE_A
            if can_transition(State.PAUSED_BUDGET, target):
                _set_state(project_id, State.PAUSED_BUDGET, target,
                           reason=f"额度提到 {budget:.2f},继续")
                out["state"] = str(target)
                out["resumed"] = True
        return out


def _advance(project_id: str, *, confirmed: bool = False, force: bool = False,
             stop_event: threading.Event | None = None) -> dict[str, Any]:
    """`advance` 的临界区部分。调用方必须已持有项目锁。"""
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    state = State(project["state"])
    profile, theme = _theme_of(project)
    subject = project["subject"]
    angle = (project.get("angle") or "").strip()
    #: 横评车单;单车项目为空
    subjects = db.project_subjects(project) if theme.roundup else []
    names = [s["name"] for s in subjects]

    if state in (State.GATE_A, State.GATE_B) and not confirmed:
        return {"state": str(state), "action": "等待人工确认", "waiting": True}
    if state in (State.DONE, State.FAILED, State.CANCELLED):
        return {"state": str(state), "action": "已结束", "waiting": False}
    if state == State.PAUSED_BUDGET:
        return {"state": str(state), "action": "已暂停(配额)", "waiting": True}

    llm = _llm()

    if state == State.DRAFT_INTENT:
        topic = db.read_json(project_id, "topic.json", {}) or {}
        # 配额:事实抽取要调 LLM 与检索接口,调用前先预留额度
        with costs.project_spend(project_id, costs.stage_reserve("facts")):
            if theme.roundup:
                facts, report = intake.run_roundup(project_id, profile, subjects,
                                                   theme=theme.name, angle=angle,
                                                   angle_search=topic.get("search", ""),
                                                   evidence=topic.get("evidence") or (), llm=llm)
            else:
                facts, report = intake.run(project_id, profile, subject, theme=theme.name,
                                           angle=angle, angle_search=topic.get("search", ""),
                                           evidence=topic.get("evidence") or (), llm=llm)
        if not report.get("sources_ok") and report.get("sources_failed"):
            # 一个来源都没通是环境问题(CLI 缺失、登录失效),不是"这款车没数据"。
            # 放行的话会拿 0 条事实写出一份全是定性描述的脚本,看上去还像成功了。
            first = report["sources_failed"][0]
            raise PipelineError(f"事实来源全部失败,已停在事实采集:{first.get('error', '')}")
        db.update_project(project_id, shot_count=profile.shots_for(project["duration"]))
        thin = report.get("subjects_thin") or []
        _set_state(project_id, state, State.FACTS_RESOLVED,
                   reason=f"采集 {len(report['sources_ok'])} 个来源,"
                          f"抽取 {len(facts)} 条事实(方向:{theme.label})"
                          + (f";缺事实的车:{'、'.join(thin)}" if thin else ""))
        return {"state": str(State.FACTS_RESOLVED), "facts": len(facts), "report": report}

    if state == State.FACTS_RESOLVED:
        if llm is None:
            raise PipelineError(LLM_REQUIRED_HINT)
        facts = _facts(project_id)
        slots = profile.drop_unbacked_slots(
            profile.slots_for(project["duration"], theme.name, names or None),
            (f.topic for f in facts if f.enabled))
        plan = [s.section for s in slots]
        # 配额:脚本生成是一次长输入长输出的 LLM 调用,调用前先预留额度
        with costs.project_spend(project_id, costs.stage_reserve("script")):
            segments, title, thesis = script_gen.generate(
                subject=subject, profile=profile, facts=facts, plan=plan,
                duration=project["duration"], llm=llm, theme=theme, angle=angle,
                **({"slots": slots, "subjects": subjects} if theme.roundup else {}),
            )
        segments = script_gen.normalize_segments(segments, slots)
        # 段落太长 = 一拍太长 = 这一拍里没有画面变化(复盘:每 5–6 秒要变一次)。
        # 在对齐计划之后拆,免得被 normalize_segments 又并回去。
        segments = script_gen.split_segments(segments)
        segments = script_gen.ensure_disclaimers(segments, profile)
        issues = script_gen.compliance_lint(segments, profile, facts, theme=theme, thesis=thesis)
        blocking = [i for i in issues if i.is_error]
        db.write_json(project_id, "lint.json", [i.to_dict() for i in issues])
        _save_script(project_id, segments)
        db.update_project(project_id, title=title, thesis=thesis)
        _set_state(project_id, state, State.SCRIPT_READY,
                   reason=f"脚本 {len(segments)} 段,lint 错误 {len(blocking)} 条")
        return {"state": str(State.SCRIPT_READY), "segments": len(segments),
                "lint_errors": len(blocking), "issues": [i.to_dict() for i in blocking[:5]]}

    if state == State.SCRIPT_READY:
        if names:
            _start_library_many(project_id, names, profile, llm)
        else:
            _start_library(project_id, subject, profile, llm)
        segments = _script(project_id)
        facts = _facts(project_id)
        log: list[dict] = []
        if llm is None:
            specs = explainer.fallback_beats(subject=subject, profile=profile, segments=segments,
                                             subjects=names)
        else:
            # 配额:逐拍版式是一次 LLM 调用
            with costs.project_spend(project_id, costs.stage_reserve("beats")):
                specs = explainer.build_beats(subject=subject, profile=profile, segments=segments,
                                              facts=facts, llm=llm, card_log=log, subjects=subjects)
        problems = explainer.validate_beats(specs)
        if problems:
            raise PipelineError("分镜自检未通过:" + "; ".join(problems))
        _save_specs(project_id, specs)
        db.write_json(project_id, "cards.json", log)
        db.update_project(project_id, shot_count=len(specs))
        counts: dict[str, int] = {}
        for s in specs:
            counts[s.layout] = counts.get(s.layout, 0) + 1
        _set_state(project_id, state, State.STORYBOARD_READY,
                   reason=f"分镜 {len(specs)} 拍(" + ",".join(
                       f"{k} {v}" for k, v in sorted(counts.items())) + ")"
                          + (f",{len(log)} 拍的叠加数据核对后降级" if log else ""))
        return {"state": str(State.STORYBOARD_READY), "shots": len(specs), "layouts": counts}

    if state == State.STORYBOARD_READY:
        _set_state(project_id, state, State.GATE_A, reason="等待确认脚本与分镜")
        return {"state": str(State.GATE_A), "waiting": True}

    if state == State.GATE_A:
        # 门禁 A 是最后一道人工确认。机器查得了的错(套话开场、空洞评价、
        # 没出处的数字、对比章节拿厂商口径当论据)不该靠眼睛一条条找 ——
        # 这里再跑一遍 lint,有 error 就不放行,免得带着已知缺陷去烧
        # TTS 和逐拍渲染的钱。force=True 表示明知有问题仍然继续。
        blockers = _gate_a_blockers(project_id, profile, theme)
        if blockers and not force:
            detail = " | ".join(b["message"] for b in blockers[:5])
            more = f"(共 {len(blockers)} 处)" if len(blockers) > 5 else ""
            raise PipelineError(
                f"门禁 A 未放行:脚本还有 {len(blockers)} 处必须修的问题{more}。{detail}"
                " —— 改完再确认,或显式选择「仍然继续」。"
            )
        specs = _sync_storyboard(project_id, _script(project_id))
        # 配额:合成旁白(TTS)是本项目最贵的一步,调用之前先按字数预留额度。
        # 额度不够就**不调用**,直接抛 BudgetExceeded → 进 PAUSED_BUDGET。
        chars = sum(len(s.narration_zh or "") for s in specs)
        with costs.project_spend(project_id,
                                  costs.stage_reserve("tts", chars=chars)) as spend:
            try:
                specs, nar = explainer.time_beats(project_id, specs, profile)
            except RuntimeError as exc:
                raise PipelineError(str(exc)) from exc
            # 记账:只算这次真正送去合成的句子和新生成的 BGM(缓存命中的不花钱)
            spend.actual(costs.explainer_spent(nar.to_dict()))
        _save_specs(project_id, specs)
        # 节奏体检:画面变化间隔 + broll 占比。写进 rhythm.json,门禁和前端都能看
        report = rhythm.report(specs)
        db.write_json(project_id, "rhythm.json", report)
        total = sum(s.duration for s in specs)
        explainer.init_beats(project_id, specs)
        cache_note = (f",{nar.cached_sentences} 句命中缓存" if nar.cached_sentences else "") + \
            (",BGM 复用曲库" if nar.bgm_reused else "")
        _set_state(project_id, state, State.ASSETS_READY,
                   reason=f"旁白 {len(nar.sentences)} 句{cache_note},成片约 {total:.0f} 秒"
                          + (f";节奏:{';'.join(report['problems'])}" if report["problems"] else ""))
        _set_state(project_id, State.ASSETS_READY, State.PROMPTS_READY,
                   reason="解说轨不编译生成提示词")
        _set_state(project_id, State.PROMPTS_READY, State.DRAFT_RENDERING,
                   reason=f"开始逐拍渲染({len(specs)} 拍)")
        return {"state": str(State.DRAFT_RENDERING), "duration": round(total, 1),
                "warnings": list(nar.warnings or []) + list(report["problems"]),
                "rhythm": report}

    if state == State.PROMPTS_READY:
        _set_state(project_id, state, State.DRAFT_RENDERING, reason="开始逐拍渲染")
        return {"state": str(State.DRAFT_RENDERING)}

    if state == State.DRAFT_RENDERING:
        specs = _specs(project_id)
        result = explainer.render_pending(project_id, specs, subject=subject, profile=profile,
                                          limit=EXPLAINER_BEATS_PER_TICK, subjects=names,
                                          should_stop=(stop_event.is_set if stop_event else None))
        if result["stage_complete"]:
            _set_state(project_id, state, State.GATE_B, reason="各拍渲染完成,等待审片")
            return {"state": str(State.GATE_B), "waiting": True, **result}
        failed = [r for r in db.get_shots(project_id, "draft") if r["status"] == "failed"]
        if failed and len(failed) == len(result["remaining"]) and not result["rendered"]:
            raise PipelineError("这几拍渲染失败:" + "; ".join(result["errors"][:3]))
        return {"state": str(state), **result}

    if state == State.GATE_B:
        specs = _specs(project_id)
        # 门禁 B 审的就是成片画面:草稿直接转成片,没有第二遍生成
        n = explainer.promote_all(project_id, specs)
        _set_state(project_id, state, State.FINAL_RENDERING, reason=f"{n} 拍转为成片")
        return {"state": str(State.FINAL_RENDERING), "promoted": n}

    if state == State.FINAL_RENDERING:
        done = {r["idx"] for r in db.get_shots(project_id, "final")
                if r["status"] == "succeeded" and r.get("final_path")}
        missing = [s.idx for s in _specs(project_id) if s.duration > 0 and s.idx not in done]
        if missing:
            # 门禁 B 里重拍过、还没渲完的拍:回草稿档补渲
            _set_state(project_id, state, State.GATE_B,
                       reason=f"还有 {len(missing)} 拍没有成片,请重拍完成后再确认")
            return {"state": str(State.GATE_B), "waiting": True, "missing": missing}
        _set_state(project_id, state, State.QA_PASSED,
                   reason="解说轨画面来自已审各拍,跳过视觉质检")
        return {"state": str(State.QA_PASSED)}

    if state in (State.QA_PASSED, State.COMPOSING):
        if state == State.QA_PASSED:
            _set_state(project_id, state, State.COMPOSING, reason="拼接各拍与混音")
        try:
            payload = explainer.compose(project_id, _specs(project_id), profile,
                                        project["title"])
        except RuntimeError as exc:
            raise PipelineError(str(exc)) from exc
        _set_state(project_id, State.COMPOSING, State.DONE,
                   reason=f"成片完成:{Path(payload['output_path']).name}")
        return {"state": str(State.DONE), "output": payload["output_path"],
                "cover": payload.get("cover"), "duration": payload.get("duration")}

    return {"state": str(state), "action": "无可执行步骤"}


def _start_library(project_id: str, subject: str, profile, llm=None) -> None:
    """后台增量构建这个主体的素材池(见 `library.py`)。所有品类都建:平台搜片 +
    profile 声明的静图来源,官方渠道配置只是额外补充。已经在建就什么都不做。
    失败只记事件,不影响主流程。

    **打标签是要花钱的**(每张图一次视觉调用),所以先按项目预留额度,
    建完再按实际调用数结算。额度不够就**不建池** —— 池子只是背景素材,
    没有它片子照样出(画面重复一些),不值得为它把整条片子停掉。
    """
    if not config.USE_LIBRARY:
        return
    try:
        before = library.Library.load(subject).vlm_calls
        reserved = costs.reserve_project(project_id, costs.stage_reserve("library"))
    except costs.BudgetExceeded as exc:
        db.add_event(project_id, "assets", reason=f"素材池构建因配额不足未启动:{exc}")
        return
    except Exception as exc:  # noqa: BLE001
        db.add_event(project_id, "assets", reason=f"素材池构建启动失败:{exc}")
        return

    def _done(stats: dict) -> None:
        delta = max(0, int(stats.get("vlm_calls") or 0) - before)
        costs.commit_project(project_id, reserved, costs.estimate_library(delta))

    try:
        if library.build_async(subject, profile=profile, llm=llm, on_done=_done):
            db.add_event(project_id, "assets", reason=f"素材池后台构建已启动({subject})")
        else:
            costs.release_project(project_id, reserved)
    except Exception as exc:  # noqa: BLE001
        costs.release_project(project_id, reserved)
        db.add_event(project_id, "assets", reason=f"素材池构建启动失败:{exc}")


def _start_library_many(project_id: str, names: list[str], profile, llm=None) -> None:
    """横评:车单上每台车一个轻量素材池,一个后台线程按车依次建(见 `library.build_many_async`)。"""
    if not config.USE_LIBRARY:
        return
    try:
        started = library.build_many_async(names, profile=profile, llm=llm,
                                           enough_seconds=library.ROUNDUP_ENOUGH_SECONDS,
                                           max_sources=library.ROUNDUP_MAX_SOURCES)
        if started:
            db.add_event(project_id, "assets", reason=f"素材池后台构建已启动({'、'.join(started)})")
    except Exception as exc:  # noqa: BLE001
        db.add_event(project_id, "assets", reason=f"素材池构建启动失败:{exc}")


def _gate_a_blockers(project_id: str, profile, theme) -> list[dict]:
    """门禁 A 前的最后一遍 lint,只取 error 级问题。

    读的是**当前**的脚本(用户可能在门禁 A 页面改过稿),所以这里重新跑
    一遍而不是复用生成时的 lint.json。
    """
    try:
        segments = _script(project_id)
        facts = _facts(project_id)
        thesis = (db.get_project(project_id) or {}).get("thesis") or ""
    except Exception:  # noqa: BLE001 - 读不到脚本就别拦,交给后面报错
        return []
    issues = script_gen.compliance_lint(segments, profile, facts, theme=theme, thesis=thesis)
    return [i.to_dict() for i in issues if i.is_error]


def confirm_gate(project_id: str, request: GateRequest) -> dict[str, Any]:
    """确认门禁,推进到下一阶段。门禁 B 一次审完全片。

    ⚠️ 这里**不自己加锁**:`advance()` 内部已经加了项目锁,而项目锁是
    **非重入**的 —— 这里再拿一次会自己把自己锁死。状态检查因此保留在
    外面(给用户一个明确的错误),而真正的推进在锁内重新读一次状态。
    """
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    state = State(project["state"])
    expected = State.GATE_A if request.gate == "A" else State.GATE_B
    if state != expected:
        raise PipelineError(f"当前状态 {state} 不在门禁 {request.gate}")
    if expected == State.GATE_B:
        _save_review_state(project_id, {"cursor": 1,
                                        "approved": list(range(len(_specs(project_id))))})
    kwargs: dict[str, Any] = {"confirmed": True}
    if request.force:
        kwargs["force"] = True
    out = advance(project_id, **kwargs)
    if out.get("busy"):
        raise locks.ProjectBusy("该项目正在推进中,请稍后重试")
    return out


def review_progress(project_id: str) -> dict[str, Any]:
    """门禁 B 的审片进度,供前端渲染审片视图。整片一批。"""
    shot_count = len(_specs(project_id))
    saved = db.read_json(project_id, "review_state.json", None) or {}
    approved = {int(i) for i in saved.get("approved", []) or []}
    batch = list(range(shot_count))
    done = [i for i in batch if i in approved]
    pending = [i for i in batch if i not in approved]
    batches = [{"index": 0, "shots": batch, "approved": done, "pending": pending,
                "ready": bool(batch) and not pending}] if batch else []
    return {"total_batches": len(batches), "cursor": int(saved.get("cursor", 0)),
            "approved": sorted(approved), "batches": [batch] if batch else [],
            "batch_list": batches, "pending_total": len(pending)}


def _save_review_state(project_id: str, state: dict[str, Any]) -> None:
    db.write_json(project_id, "review_state.json", state)


def _sync_storyboard(project_id: str, segments: list[ScriptSegment]) -> list[ShotSpec]:
    """脚本改了就同步分镜的旁白,返回同步后的分镜。

    门禁 A 的脚本编辑器改的是 script.json,TTS 读的是却分镜里的 narration_zh。
    不同步的话,在门禁 A 改的稿根本进不了成片 —— 改完看着像生效,成片还是旧词。
    """
    specs = _specs(project_id)
    if not specs:
        return specs
    if len(specs) != len(segments):
        raise PipelineError(
            f"脚本 {len(segments)} 段与分镜 {len(specs)} 拍对不上,旁白没法按拍对应。"
            "请在脚本页保持段落数量不变再保存。")
    synced = explainer.sync_narration(specs, segments, _facts(project_id))
    problems = explainer.validate_beats(synced)
    if problems:
        raise PipelineError("同步后分镜自检未通过:" + "; ".join(problems))
    _save_specs(project_id, synced)
    return synced


def update_script(project_id: str, segments: list[ScriptSegment],
                  title: str | None, thesis: str | None = None) -> list[dict]:
    """保存脚本,并返回这一稿的 lint 结果(门禁 A 的预审报告)。

    持项目锁:此刻流水线可能正在同步分镜或渲染,两个执行流同时写
    storyboard.json 会后写覆盖先写。拿不到锁就抛 ProjectBusy(409),
    不阻塞 —— 正在跑的那一轮会自己推进。
    """
    with locks.require_project_lock(project_id):
        project = db.get_project(project_id)
        if not project:
            raise PipelineError("项目不存在")
        if State(project["state"]) not in (State.GATE_A, State.STORYBOARD_READY):
            raise PipelineError("只有门禁 A 阶段可以修改脚本")
        # 先同步再落盘:同步不过(段落数对不上、大字全废)就别改坏现有状态
        _sync_storyboard(project_id, segments)
        _save_script(project_id, segments)
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if thesis is not None:
            fields["thesis"] = thesis.strip()
        if fields:
            db.update_project(project_id, **fields)
        profile, theme = _theme_of(project)
        issues = script_gen.compliance_lint(segments, profile, _facts(project_id), theme=theme,
                                           thesis=fields.get("thesis", project.get("thesis") or ""))
        db.write_json(project_id, "lint.json", [i.to_dict() for i in issues])
        return [i.to_dict() for i in issues]


def update_shots(project_id: str, specs: list[ShotSpec]) -> None:
    with locks.require_project_lock(project_id):
        project = db.get_project(project_id)
        if not project:
            raise PipelineError("项目不存在")
        if State(project["state"]) not in (State.GATE_A, State.PROMPTS_READY):
            raise PipelineError("只有门禁 A / 提示词就绪阶段可以修改分镜")
        profile, _theme = _theme_of(project)
        specs = explainer.enforce_rhythm(list(specs), profile)
        problems = explainer.validate_beats(specs)
        if problems:
            raise PipelineError("分镜自检未通过:" + "; ".join(problems))
        _save_specs(project_id, specs)


def retry(project_id: str, idx: int, stage: str) -> None:
    """解说轨的"重拍" = 换一组背景重渲这一拍。渲染只在 DRAFT_RENDERING 里跑,
    门禁 B 上点重拍要先退回去,渲完会自己回到门禁 B。

    持项目锁:渲染途中重拍,会把该拍重置为 pending,而在途的渲染随后
    把状态覆盖回 succeeded —— 用户的"换背景重拍"就被静默吞掉了。
    拿不到锁说明正有人在渲染,直接告诉用户稍后重试。
    """
    with locks.require_project_lock(project_id):
        project = db.get_project(project_id)
        if not project:
            raise PipelineError("项目不存在")
        explainer.rerender(project_id, idx)
        if State(project["state"]) == State.GATE_B:
            _set_state(project_id, State.GATE_B, State.DRAFT_RENDERING,
                       reason=f"重拍第 {idx} 拍(换背景)")


#: 概念动画还没开始渲染的状态:换模版只记下来,渲染时就用新模版
_STYLE_FREE = (State.DRAFT_INTENT, State.FACTS_RESOLVED, State.SCRIPT_READY, State.STORYBOARD_READY,
               State.GATE_A, State.ASSETS_READY, State.PROMPTS_READY)


def project_names(project: dict[str, Any]) -> tuple[str, list[str]]:
    """(主车, 横评车单)。单车项目车单为空。"""
    _, theme = _theme_of(project)
    subjects = db.project_subjects(project) if theme.roundup else []
    return project["subject"], [s["name"] for s in subjects]


def set_concept_style(project_id: str, style: str) -> dict[str, Any]:
    """选定概念动画模版。渲染前选:只记下来。门禁 B 上换:只把概念动画那几拍退回去用新模版重渲
    (背景、其他拍都不动),渲完自己回到门禁 B。渲染中 / 成片阶段不让换(会和正在跑的渲染打架)。

    持项目锁:换模版要把概念动画那几拍退回 pending,与在途渲染抢同一批
    拍,不加锁就会两路一起写。
    """
    with locks.require_project_lock(project_id):
        project = db.get_project(project_id)
        if not project:
            raise PipelineError("项目不存在")
        if style not in {s.id for s in motion.styles()}:
            raise PipelineError(f"没有这个模版:{style}")
        state = State(project["state"])
        if state not in _STYLE_FREE and state != State.GATE_B:
            raise PipelineError("现在不能换概念动画模版:渲染中请等到审片(门禁 B),成片阶段已经定稿")
        before = explainer.concept_style_of(project_id)
        db.write_json(project_id, "concept_style.json", {"style": style})
        redo: list[int] = []
        if state == State.GATE_B and style != before:
            redo = [s.idx for s in concept_styles.concept_beats(project_id)]
            for idx in redo:
                db.update_shot(project_id, idx, "draft", status="pending", draft_path=None, error=None)
                db.update_shot(project_id, idx, "final", status="pending", final_path=None, error=None)
            if redo:
                _set_state(project_id, State.GATE_B, State.DRAFT_RENDERING,
                           reason=f"概念动画换模版 {motion.get_style(style).name},重渲 {len(redo)} 拍")
        elif style != before:
            db.add_event(project_id, str(state), src=str(state),
                         reason=f"概念动画模版:{motion.get_style(style).name}")
        return {"style": style, "rerender": redo}


def run_until_waiting(project_id: str, max_steps: int = 12) -> list[dict[str, Any]]:
    """连续推进直到遇到门禁/终态。给"一键跑"用。"""
    steps: list[dict[str, Any]] = []
    for _ in range(max_steps):
        try:
            step = advance(project_id)
        except PipelineError:
            raise
        steps.append(step)
        if step.get("waiting"):
            break
        project = db.get_project(project_id)
        if not project or State(project["state"]) in (
            State.DONE, State.FAILED, State.CANCELLED, State.PAUSED_BUDGET
        ):
            break
    return steps
