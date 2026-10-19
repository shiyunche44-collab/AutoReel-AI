"""流水线编排:把各阶段按状态机串起来,并提供给后台调度器轮询。

分工:本模块只管"当前状态该做什么",不直接关心 HTTP。
`advance()` 推进一个非阻塞步骤,`poll_running()` 供后台循环驱动长耗时生成。

两个设计约束贯穿全文件:
1. **门禁态停下等人**。GATE_A / GATE_B 不做任何自动推进。
2. **幂等**。每个阶段先检查产物是否已存在,已存在则跳过,重跑不重复消耗配额。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Sequence

from .. import config, costs, db
from ..categories import load_profile
from ..llm.deepseek import DeepSeekProvider
from ..llm.minimax import MiniMaxProvider
from ..models import GateRequest, ProjectCreate, ScriptSegment, ShotSpec
from ..providers import clips as clips_mod
from ..state import State, assert_transition
from . import (  # noqa: F401 - 显式列出便于阅读依赖
    asset_resolver,
    clip_resolver,
    generate,
    intake,
    media_query,
    narration,
    prompt_compiler,
    qa,
    render,
    script_gen,
    storyboard,
)

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
    assert_transition(src, dst)
    db.update_project(project_id, state=str(dst), state_reason=reason)
    db.add_event(project_id, str(dst), src=str(src), reason=reason, cost=cost)


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


def _refs(project_id: str) -> list:
    import json

    path = config.project_dir(project_id) / "refs.json"
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _ref_urls_for(project_id: str, angle: str) -> list[str]:
    from .asset_resolver import references_for_angle

    class _Ref:
        def __init__(self, d: dict[str, Any]) -> None:
            self.url = d.get("url", "")
            self.angle = d.get("angle", "")
            self.origin = d.get("origin", "search")
            self.note = d.get("note", "")

    return references_for_angle([_Ref(d) for d in _refs(project_id)], angle, config.CATEGORY_REF_LIMIT)


# --------------------------------------------------------------------------
# 内容方向
# --------------------------------------------------------------------------
def _theme_name(project: dict[str, Any]) -> str:
    """取项目的内容方向;缺失或已失效时回退品类默认方向。"""
    return _profile(project).default_theme


def _theme_of(project: dict[str, Any]):
    profile = _profile(project)
    return profile, profile.theme_spec(project.get("theme") or None)


def _approved_indices(project_id: str) -> set[int] | None:
    """读回门禁 B 已批准的镜头集合。

    ⚠️ 这段逻辑此前**写了却从不读**:`confirm_gate` 把批准列表写进
    `approved_shots.json`,而 `poll_running` 调 `tick()` / `stage_complete()`
    时都没传 `approved`,于是逐镜头批准完全失效。返回 None 表示"没有批准文件",
    此时按全量放行(旧项目与首轮流程的语义)。
    """
    raw = db.read_json(project_id, "approved_shots.json", None)
    if raw is None:
        return None
    try:
        return {int(i) for i in raw}
    except (TypeError, ValueError):
        return None


def _review_batches(project_id: str, shot_count: int) -> list[list[int]]:
    """把镜头按一致性链切成审片批次。

    批次边界必须对齐链边界 —— 把一条链劈成两半会让后半段的镜头拿不到前驱尾帧,
    连续性直接断掉。
    """
    batch_chains = max(1, config.GATE_B_BATCH_CHAINS)
    per_batch = max(1, config.CHAIN_GROUP_SIZE) * batch_chains
    return [
        list(range(start, min(start + per_batch, shot_count)))
        for start in range(0, shot_count, per_batch)
    ]


def _review_state(project_id: str, shot_count: int) -> dict[str, Any]:
    """读取/初始化审片进度。幂等,可跨服务重启续用。"""
    batches = _review_batches(project_id, shot_count)
    saved = db.read_json(project_id, "review_state.json", None) or {}
    return {
        "batch_chains": config.GATE_B_BATCH_CHAINS,
        "total_batches": len(batches),
        "cursor": int(saved.get("cursor", 0)),
        "approved": sorted(saved.get("approved", []) or []),
        "batches": batches,
    }


def _save_review_state(project_id: str, state: dict[str, Any]) -> None:
    payload = {k: v for k, v in state.items() if k != "batches"}
    db.write_json(project_id, "review_state.json", payload)


# --------------------------------------------------------------------------
# 项目创建
# --------------------------------------------------------------------------
def create_project(payload: ProjectCreate) -> str:
    from ..models import new_id

    profile = _profile({"category": payload.category})
    if profile.skeleton:
        raise PipelineError(
            f"品类「{profile.label}」目前是骨架配置,尚未完整实现。"
            f"待补齐:{'; '.join(profile.todo) or '行业逻辑'}"
        )
    if payload.duration not in config.SUPPORTED_DURATIONS:
        raise PipelineError(
            f"不支持的时长 {payload.duration}s。可选:{list(config.SUPPORTED_DURATIONS)}"
        )

    theme_name = (payload.theme or "").strip()
    if theme_name and theme_name not in profile.themes:
        raise PipelineError(
            f"品类「{profile.label}」没有名为「{theme_name}」的内容方向。"
            f"可选:{sorted(profile.themes)}"
        )
    if not theme_name:
        theme_name = profile.default_theme

    # 时长能否铺展出正确长度的段落序列,在这里就验掉,
    # 而不是等跑到 FACTS_RESOLVED 才发现配置有问题。
    plan = profile.plan_for(payload.duration, theme_name)
    expected_shots = config.shots_for_duration(payload.duration)
    if len(plan) != expected_shots:
        raise PipelineError(
            f"方向「{theme_name}」在 {payload.duration}s 下铺展出 {len(plan)} 个段落,"
            f"应为 {expected_shots} 个。请检查 Profile 的 blocks 权重配置。"
        )

    project_id = new_id("p_")
    shot_count = config.shots_for_duration(payload.duration)
    budget = payload.quota_budget or costs.default_budget_for(payload.duration)
    now = time.time()
    title = payload.title or f"{payload.subject} 解说视频"

    db.create_project(
        {
            "id": project_id,
            "title": title,
            "subject": payload.subject,
            "category": payload.category,
            "theme": theme_name,
            "duration": payload.duration,
            "shot_count": shot_count,
            "narration_mode": payload.narration_mode,
            "state": str(State.DRAFT_INTENT),
            "state_reason": None,
            "quota_budget": budget,
            "spent_cost": 0.0,
            "est_cost": costs.estimate_project(payload.duration)["with_retry"],
            "created_at": now,
            "updated_at": now,
        }
    )
    db.add_event(
        project_id, str(State.DRAFT_INTENT),
        reason=f"创建项目:{title}(方向:{theme_name})",
    )
    return project_id


# --------------------------------------------------------------------------
# 单步推进
# --------------------------------------------------------------------------
def advance(project_id: str, *, confirmed: bool = False) -> dict[str, Any]:
    """推进一个步骤。返回本次动作的描述。

    `confirmed=True` 表示"人工门禁已通过",允许越过 GATE_A/GATE_B。
    必须显式传这个参数,而不是靠调用方改状态:后台调度循环也在不停调
    advance(),若它能自行放行门禁,两道人工审核就形同虚设。
    """
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    state = State(project["state"])
    profile, theme = _theme_of(project)
    subject = project["subject"]

    if state in (State.GATE_A, State.GATE_B) and not confirmed:
        return {"state": str(state), "action": "等待人工确认", "waiting": True}
    if state in (State.DONE, State.FAILED, State.CANCELLED):
        return {"state": str(state), "action": "已结束", "waiting": False}
    if state == State.PAUSED_BUDGET:
        return {"state": str(state), "action": "已暂停(配额)", "waiting": True}

    llm = _llm()

    if state == State.DRAFT_INTENT:
        facts, report = intake.run(project_id, profile, subject, theme=theme.name, llm=llm)
        db.update_project(project_id, shot_count=config.shots_for_duration(project["duration"]))
        _set_state(project_id, state, State.FACTS_RESOLVED,
                   reason=f"采集 {len(report['sources_ok'])} 个来源,"
                          f"抽取 {len(facts)} 条事实(方向:{theme.label})")
        return {"state": str(State.FACTS_RESOLVED), "facts": len(facts), "report": report}

    if state == State.FACTS_RESOLVED:
        if llm is None:
            raise PipelineError(LLM_REQUIRED_HINT)
        facts = _facts(project_id)
        plan = list(profile.plan_for(project["duration"], theme.name))
        segments, title = script_gen.generate(
            subject=subject, profile=profile, facts=facts, plan=plan,
            duration=project["duration"], llm=llm, theme=theme,
        )
        segments = script_gen.normalize_segments(segments, plan)
        segments = script_gen.ensure_disclaimers(segments, profile)
        issues = script_gen.compliance_lint(segments, profile, facts, theme=theme)
        blocking = [i for i in issues if i.is_error]
        db.write_json(project_id, "lint.json", [i.to_dict() for i in issues])
        _save_script(project_id, segments)
        db.update_project(project_id, title=title)
        _set_state(project_id, state, State.SCRIPT_READY,
                   reason=f"脚本 {len(segments)} 段,lint 错误 {len(blocking)} 条")
        return {"state": str(State.SCRIPT_READY), "segments": len(segments),
                "lint_errors": len(blocking), "issues": [i.to_dict() for i in blocking[:5]]}

    if state == State.SCRIPT_READY:
        if llm is None:
            raise PipelineError(LLM_REQUIRED_HINT)
        segments = _script(project_id)
        facts = _facts(project_id)
        specs = storyboard.build(
            subject=subject, profile=profile, segments=segments, facts=facts,
            duration=project["duration"], llm=llm,
        )
        problems = storyboard.validate_specs(specs, project["duration"])
        if problems:
            raise PipelineError("分镜自检未通过:" + "; ".join(problems))
        _save_specs(project_id, specs)
        _set_state(project_id, state, State.STORYBOARD_READY, reason=f"分镜 {len(specs)} 个镜头")
        return {"state": str(State.STORYBOARD_READY), "shots": len(specs)}

    if state == State.STORYBOARD_READY:
        _set_state(project_id, state, State.GATE_A, reason="等待确认脚本与分镜")
        return {"state": str(State.GATE_A), "waiting": True}

    if state == State.GATE_A:
        # 确认后:素材 → 提示词 → 草稿
        project_dir = config.project_dir(project_id)
        refs, report = asset_resolver.build_reference_set(
            subject=subject, profile=profile, specs=_specs(project_id),
            project_dir=project_dir, limit=config.CATEGORY_REF_LIMIT, llm=llm,
        )
        _set_state(project_id, state, State.ASSETS_READY,
                   reason=f"参考图 {len(refs)} 张(检索 {report['searched']})")

        if llm is None:
            raise PipelineError(LLM_REQUIRED_HINT)
        # 真实镜头也照常编译提示词:门禁 B 不满意某个真实镜头时要能"重试"
        # 回退生成,缺提示词就退不回去。几十次 LLM 调用比这条退路便宜。
        compiled, errors = compile_all_prompts(
            project_id, profile=profile, subject=subject,
            narration_mode=project["narration_mode"], llm=llm, stage="draft",
        )
        db.write_json(project_id, "prompt_errors.json", errors)
        generate.initialise_shots(project_id, _specs(project_id), stage="draft",
                                  ref_limit=len(refs))
        # 逐镜头取材:必须在 initialise_shots 之后 —— 命中的镜头靠写成
        # succeeded 来"顶掉"草稿生成,shot 行要先存在。
        real_shots = _collect_media(project_id, subject, profile, _specs(project_id),
                                    refs, llm)
        _set_state(project_id, State.ASSETS_READY, State.PROMPTS_READY,
                   reason=f"编译 {compiled} 条提示词,校验失败 {len(errors)} 条"
                          + (f",{real_shots} 个镜头用真实素材" if real_shots else ""))
        _set_state(project_id, State.PROMPTS_READY, State.DRAFT_RENDERING,
                   reason=f"开始草稿生成({config.DRAFT_MODEL})")
        return {"state": str(State.DRAFT_RENDERING), "prompts": compiled,
                "prompt_errors": len(errors), "real_clips": real_shots}

    if state == State.PROMPTS_READY:
        generate.initialise_shots(project_id, _specs(project_id), stage="draft")
        _set_state(project_id, state, State.DRAFT_RENDERING, reason="开始草稿生成")
        return {"state": str(State.DRAFT_RENDERING)}

    if state == State.DRAFT_RENDERING:
        result = poll_running(project_id, "draft")
        if result.get("stage_complete"):
            _set_state(project_id, state, State.GATE_B, reason="草稿完成,等待审片")
            return {"state": str(State.GATE_B), "waiting": True, **result}
        return {"state": str(state), **result}

    if state == State.GATE_B:
        # 确认后:只把**新增批准**的镜头送进成片档。
        # 40 镜头一次性审完再开跑毫无意义,分批审才有意义:审完一批就能开跑一批。
        specs = _specs(project_id)
        refs = _refs(project_id)
        approved = _approved_indices(project_id)
        if approved is None:
            approved = set(range(len(specs)))

        # 差集:已编译过的不再编译 —— 重复编译会重复烧 LLM 额度。
        todo = [
            s.idx for s in specs
            if s.idx in approved and not generate.load_prompt(project_id, "final", s.idx)
        ]
        if llm is None and todo:
            raise PipelineError(LLM_REQUIRED_HINT)
        compiled, errors = compile_all_prompts(
            project_id, profile=profile, subject=subject,
            narration_mode=project["narration_mode"], llm=llm, stage="final",
            has_first_frame=True, only_idx=todo,
        )
        db.write_json(project_id, "prompt_errors_final.json", errors)
        generate.initialise_shots(project_id, specs, stage="final", ref_limit=len(refs))

        # 真实素材:门禁 A 之后取好的素材,按批应用到成片档,省掉成片生成。
        # 必须在 initialise_shots 之后 —— 它靠把镜头写成 succeeded 来"顶掉"
        # 那次生成,而 initialise_shots 之前 shot 行还不存在。
        real_shots = _inject_clips(project_id, subject, profile, specs, approved, refs, llm)

        review = _review_state(project_id, len(specs))
        _set_state(project_id, State.GATE_B, State.FINAL_RENDERING,
                   reason=f"开始成片生成({config.FINAL_MODEL}),编译 {compiled} 条提示词,"
                          f"本批放行 {len(approved)} 个镜头"
                          f"(第 {review['cursor'] + 1}/{review['total_batches']} 批)"
                          + (f",{real_shots} 个镜头用真实素材" if real_shots else ""))
        return {"state": str(State.FINAL_RENDERING), "prompts": compiled,
                "approved": sorted(approved), "review": review,
                "real_clips": real_shots}

    if state == State.FINAL_RENDERING:
        result = poll_running(project_id, "final")
        if result.get("stage_complete"):
            specs = _specs(project_id)
            approved = _approved_indices(project_id) or set()
            review = _review_state(project_id, len(specs))
            unreviewed = [i for i in range(len(specs)) if i not in approved]
            if unreviewed:
                # 还有没审的批次:回门禁 B 等下一批审片,不要进 QA ——
                # 否则会拿半片素材去合成,产出缺镜头的短片。
                _set_state(project_id, state, State.GATE_B,
                           reason=f"本批成片完成,还有 {len(unreviewed)} 个镜头待审"
                                  f"(第 {min(review['cursor'] + 1, review['total_batches'])}"
                                  f"/{review['total_batches']} 批)")
                return {"state": str(State.GATE_B), "waiting": True,
                        "review": review, **result}
            report = qa.run(project_id, specs)
            _set_state(project_id, state, State.QA_PASSED,
                       reason=f"质检 {report['pass_count']} 通过 / {report['fail_count']} 不通过")
            return {"state": str(State.QA_PASSED), "qa": report}
        return {"state": str(state), **result}

    if state == State.QA_PASSED:
        _set_state(project_id, state, State.COMPOSING, reason="生成旁白与合成")
        return finish(project_id)

    if state == State.COMPOSING:
        return finish(project_id)

    return {"state": str(state), "action": "无可执行步骤"}


def missing_final_shots(project_id: str) -> list[int]:
    """分镜里还没有成片的镜头序号。

    分批审片下这个检查是必须的:用户可能只批了前两批就点完成,
    而 `finish()` 过去只要有任一镜头成功就开渲,会静默产出一部缺镜头的短片 ——
    内容少了但文件正常,发出去才发现。
    """
    specs = _specs(project_id)
    rows = db.get_shots(project_id, "final") or []
    done = {r["idx"] for r in rows if r["status"] == "succeeded" and r.get("final_path")}
    return [s.idx for s in specs if s.idx not in done]


def finish(project_id: str) -> dict[str, Any]:
    """合成阶段:旁白 → BGM → 字幕 → 渲染 → 封面。"""
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    profile = _profile(project)
    specs = _specs(project_id)

    missing = missing_final_shots(project_id)
    if missing:
        preview = ", ".join(str(i) for i in missing[:12])
        more = f" 等 {len(missing)} 个" if len(missing) > 12 else ""
        raise PipelineError(
            f"还有 {len(missing)} 个镜头没有成片(镜头 {preview}{more})。"
            f"请先在门禁 B 审完并生成这些批次,再合成 —— "
            f"否则会产出一部缺镜头的短片。"
        )

    rows = [r for r in db.get_shots(project_id, "final") if r["status"] == "succeeded"]
    rows.sort(key=lambda r: r["idx"])
    shot_paths = [Path(r["final_path"]) for r in rows if r.get("final_path")]
    if not shot_paths:
        raise PipelineError("没有可用的成片镜头,无法合成")

    nar = narration.build(
        project_id, specs,
        narration_mode=project["narration_mode"], profile=profile,
    )
    result = render.compose(
        project_id, shot_paths,
        ass_path=nar.ass_path,
        narration_path=nar.narration_path,
        bgm_path=nar.bgm_path,
    )
    if not result.ok or not result.output_path:
        raise PipelineError("合成失败:" + "; ".join(result.warnings or []))

    cover = render.build_cover(
        result.output_path, project["title"],
        config.project_dir(project_id) / "cover.jpg",
        subtitle=f"{project['duration']}秒 · {profile.label}解说",
    )
    db.write_json(project_id, "render.json", {
        **result.to_dict(),
        "cover": str(cover) if cover else None,
        "narration": nar.to_dict(),
    })
    _set_state(project_id, State.COMPOSING, State.DONE,
               reason=f"成片完成:{result.output_path.name}")
    return {"state": str(State.DONE), "output": str(result.output_path),
            "cover": str(cover) if cover else None, "duration": result.duration}


# --------------------------------------------------------------------------
# 长耗时阶段轮询
# --------------------------------------------------------------------------
def poll_running(project_id: str, stage: str) -> dict[str, Any]:
    """驱动一次生成 tick。供后台循环反复调用。"""
    project = db.get_project(project_id)
    if not project:
        return {"error": "项目不存在"}
    specs = _specs(project_id)
    ref_urls = _ref_urls_for(project_id, specs[0].angle if specs else "exterior_45")

    # 门禁 B 的逐镜头批准集合必须传下去,否则 `ready_shots` 会放行全部镜头 ——
    # 分批审片就退化成了"一次性审完 40 条",而且批准语义直接失效。
    approved = _approved_indices(project_id) if stage == "final" else None

    result = generate.tick(project_id, stage, ref_urls=ref_urls, approved=approved)
    payload = result.to_dict()
    payload["degraded"] = generate.degraded_report(project_id, stage)
    payload["stage_complete"] = generate.stage_complete(project_id, stage, approved)

    if payload["stage_complete"]:
        payload["action"] = f"{stage} 阶段完成"
    elif payload["paused_budget"]:
        db.update_project(project_id, state=str(State.PAUSED_BUDGET),
                          state_reason=payload.get("reason") or "超出配额上限")
    else:
        payload["action"] = f"{stage}:轮询 {payload['polled']},提交 {len(payload['submitted'])}"
    return payload


# --------------------------------------------------------------------------
# 提示词编译
# --------------------------------------------------------------------------
def compile_all_prompts(
    project_id: str,
    *,
    profile,
    subject: str,
    narration_mode: str,
    llm,
    stage: str,
    has_first_frame: bool = False,
    only_idx: Sequence[int] | None = None,
) -> tuple[int, dict[str, list[str]]]:
    """编译分镜的提示词。返回 (成功数, {镜头序号: 错误列表})。

    `only_idx` 限制本次编译的镜头范围 —— 分批审片时只为新增批准的镜头编译,
    重复编译既浪费 LLM 额度,也会让已批准镜头的提示词被重写。
    """
    specs = _specs(project_id)
    wanted = None if only_idx is None else set(only_idx)
    compiled = 0
    errors: dict[str, list[str]] = {}
    for spec in specs:
        if wanted is not None and spec.idx not in wanted:
            continue
        section = profile.section(spec.section)
        try:
            prompt, problems = prompt_compiler.compile_with_llm(
                spec,
                subject=subject,
                profile=profile,
                llm=llm,
                narration_mode=narration_mode,
                has_first_frame=has_first_frame,
            )
        except Exception as exc:  # noqa: BLE001 - 单镜头失败不阻断整体
            errors[str(spec.idx)] = [f"编译失败:{exc}"]
            continue
        if problems:
            errors[str(spec.idx)] = problems
            continue
        generate.save_prompt(project_id, stage, spec.idx, prompt)
        compiled += 1
    return compiled, errors


# --------------------------------------------------------------------------
# 人工干预
# --------------------------------------------------------------------------
def _merge_approved(project_id: str, shot_count: int,
                    approved: list[int] | None) -> set[int]:
    """把本批批准的镜头并入审片状态,返回全量已批准集合。

    - `approved` 为 None:一次性批准全部(旧的单次门禁语义)。
    - 否则只并入本批,并把游标推进到"第一个尚未整批批准"的批次。
    """
    state = _review_state(project_id, shot_count)
    current = set(state["approved"])
    if approved is None:
        current = set(range(shot_count))
    else:
        current |= {int(i) for i in approved if 0 <= int(i) < shot_count}

    cursor = 0
    for batch in state["batches"]:
        if all(i in current for i in batch):
            cursor += 1
        else:
            break

    state["approved"] = sorted(current)
    state["cursor"] = cursor
    _save_review_state(project_id, state)
    # 同步给 generate 层:ready_shots / stage_complete 都读它
    db.write_json(project_id, "approved_shots.json", sorted(current))
    return current


def _real_ref_images(refs: Sequence[Any]) -> list:
    """参考图集里的**真实**图片(去掉 AI 兜底图),供静图镜头使用。

    门禁 A 拿到的是 `ImageRef` 对象,门禁 B 从 refs.json 读回的是 dict,两种都要认。
    """
    ImageRef = asset_resolver.images.ImageRef
    out: list = []
    for r in refs:
        if isinstance(r, dict):
            r = ImageRef(**{k: v for k, v in r.items() if k in ImageRef.__dataclass_fields__})
        if isinstance(r, ImageRef) and r.origin != "generate":
            out.append(r)
    return out


def _collect_media(project_id: str, subject: str, profile, specs: Sequence[ShotSpec],
                   refs: Sequence[Any], llm=None) -> int:
    """门禁 A 之后:逐镜头取材,命中的镜头直接顶掉草稿生成。返回生效的镜头数。

    取材结果存成 `clips.json`(取材计划),门禁 B 复用它,不再重抓网络。
    失败不阻断:任何异常都记进 clip_report.json,全片照常走生成。
    """
    if not config.USE_REAL_CLIPS:
        return 0
    try:
        errors: list[str] = []
        queries, brand = media_query.build(subject, profile, specs, llm=llm, errors=errors)
        db.write_json(project_id, "media_queries.json",
                      {"brand": brand, "shots": media_query.to_json(queries)})
        ref_images = _real_ref_images(refs)
        assignment, report = clip_resolver.collect(
            subject=subject, profile=profile, specs=specs,
            project_dir=config.project_dir(project_id),
            page_urls=clips_mod.page_urls_from(ref_images),
            limit=config.REAL_CLIP_LIMIT, queries=queries, brand=brand,
            ref_images=ref_images, llm=llm,
        )
        report["errors"] = errors + report.get("errors", [])
        applied = clip_resolver.apply_clips(project_id, assignment, stage="draft")
        report.update({"status": "ok", "runs": 1, "applied_draft": applied, "applied": 0})
        clip_resolver.persist(project_id, report, assignment)
        db.add_event(project_id, "assets", reason=report.get("summary", ""))
        return applied
    except Exception as exc:  # noqa: BLE001 - 补充通路,失败不影响主流程
        clip_resolver.persist(project_id, {
            "enabled": False,
            "status": "failed",
            "runs": 1,
            "applied": 0,
            "errors": [f"真实素材采集异常:{type(exc).__name__}: {exc}"],
            "summary": "真实素材采集异常,已全部退回生成",
        }, {})
        db.add_event(project_id, "assets", reason="真实素材采集异常,已全部退回生成")
        return 0


def _inject_clips(project_id: str, subject: str, profile, specs: Sequence[ShotSpec],
                  approved: set[int], refs: Sequence[Any], llm=None) -> int:
    """给已批准的镜头应用真实素材(成片档)。返回本次生效的镜头数。

    三条自我约束:
    1. **失败不阻断** —— 这一层是补充,任何异常都记进 clip_report.json 后
       静默退回生成,不能因为"抓不到视频"卡住整条流水线;
    2. **只碰已批准的镜头** —— 还没审的镜头不该提前被定死画面;
    3. **不重复抓取** —— 门禁 A 之后已经取好素材(`clips.json`),每批门禁
       只是把同一份计划应用到这一批;硬失败过的项目也不再重试。
       只有门禁 A 时还没有这套逻辑的老项目,才在这里补抓一次。
    """
    if not config.USE_REAL_CLIPS:
        return 0

    previous = clip_resolver.load_report(project_id)
    if previous.get("status") == "failed":
        return 0

    try:
        if previous:
            plan = clip_resolver.load_plan(project_id)
            report = previous
        else:
            ref_images = _real_ref_images(refs)
            plan, report = clip_resolver.collect(
                subject=subject, profile=profile, specs=specs,
                project_dir=config.project_dir(project_id),
                page_urls=clips_mod.page_urls_from(ref_images),
                limit=config.REAL_CLIP_LIMIT, ref_images=ref_images, llm=llm,
            )
            report.update({"status": "ok", "runs": 1})

        done = {r["idx"] for r in db.get_shots(project_id, "final")
                if r["status"] == "succeeded" and r.get("consistency") == "real"}
        todo = {i: c for i, c in plan.items() if i in approved and i not in done}
        applied = clip_resolver.apply_clips(project_id, todo, stage="final")
        report["applied"] = int(report.get("applied", 0)) + applied
        clip_resolver.persist(project_id, report, plan)
        if applied:
            db.add_event(project_id, "assets",
                         reason=f"本批 {applied} 个已批准镜头使用真实素材")
        return applied
    except Exception as exc:  # noqa: BLE001 - 补充通路,失败不影响主流程
        # 只写报告、不动 clips.json:取材计划可能是好的,坏的只是这一次应用
        db.write_json(project_id, "clip_report.json", {
            **previous,
            "enabled": False,
            "status": "failed",
            "runs": int(previous.get("runs", 0)) + 1,
            "errors": [*previous.get("errors", []),
                       f"真实素材应用异常:{type(exc).__name__}: {exc}"],
            "summary": "真实素材应用异常,其余镜头退回生成",
        })
        db.add_event(project_id, "assets", reason="真实素材采集异常,已全部退回生成")
        return 0


def confirm_gate(project_id: str, request: GateRequest) -> dict[str, Any]:
    """确认门禁,推进到下一阶段。"""
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    state = State(project["state"])

    if request.gate == "A":
        if state != State.GATE_A:
            raise PipelineError(f"当前状态 {state} 不在门禁 A")
        return advance(project_id, confirmed=True)

    if state != State.GATE_B:
        raise PipelineError(f"当前状态 {state} 不在门禁 B")

    shot_count = len(_specs(project_id))
    merged = _merge_approved(project_id, shot_count, request.approved)
    review = _review_state(project_id, shot_count)
    if request.approved is not None:
        db.add_event(project_id, str(State.GATE_B),
                     reason=f"批准 {len(request.approved)} 个镜头,"
                            f"累计 {len(merged)}/{shot_count}")
    return advance(project_id, confirmed=True)


def review_progress(project_id: str) -> dict[str, Any]:
    """门禁 B 的批次进度,供前端渲染分批审片视图。"""
    shot_count = len(_specs(project_id))
    state = _review_state(project_id, shot_count)
    approved = set(state["approved"])
    batches = []
    for n, batch in enumerate(state["batches"]):
        done = [i for i in batch if i in approved]
        batches.append({
            "index": n,
            "shots": batch,
            "approved": done,
            "pending": [i for i in batch if i not in approved],
            "ready": bool(batch) and len(done) == len(batch),
        })
    state["batch_list"] = batches
    state["pending_total"] = sum(len(b["pending"]) for b in batches)
    return state


def update_script(project_id: str, segments: list[ScriptSegment], title: str | None) -> None:
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    if State(project["state"]) not in (State.GATE_A, State.STORYBOARD_READY):
        raise PipelineError("只有门禁 A 阶段可以修改脚本")
    _save_script(project_id, segments)
    if title:
        db.update_project(project_id, title=title)


def update_shots(project_id: str, specs: list[ShotSpec]) -> None:
    project = db.get_project(project_id)
    if not project:
        raise PipelineError("项目不存在")
    if State(project["state"]) not in (State.GATE_A, State.PROMPTS_READY):
        raise PipelineError("只有门禁 A / 提示词就绪阶段可以修改分镜")
    problems = storyboard.validate_specs(specs, project["duration"])
    if problems:
        raise PipelineError("分镜自检未通过:" + "; ".join(problems))
    _save_specs(project_id, specs)


def retry(project_id: str, idx: int, stage: str) -> None:
    generate.retry_shot(project_id, idx, stage)


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
