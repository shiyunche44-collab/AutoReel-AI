"""横屏解说轨(profile `track: explainer`):对标「三驱兄弟」式的信息型长视频。

核心是**画面的角色**:实拍只是"这台车的背景画面",不要求对上旁白 ——
随便哪段干净的都行,短镜头连拼(`library.fill`),信息全部靠叠加层传达。

一拍 = 一段旁白 = 一个版式:

    broll      全屏背景画面
    panel      背景 + 左侧参数面板(核对过事实的 3–5 行)
    highlight  背景缩成小窗 + 两行大字关键词
    card       整屏数据卡(spec / compare / bars / timeline,沿用 cards.py)
    stat       背景压暗 + 中央一个大数字从 0 滚到目标值(核对过事实的一个参数)
    concept    整屏概念动画(HyperFrames 渲染 motion/scenes/,电池、补能、雷达这类
               "看不见的东西";数字同样只来自核对过的事实,渲染不了退回数据卡)

broll 拍按规则自动轮换三种样式(不经 LLM):全屏 / 左上角角标 / 两三栏分屏。
拍内各段之间用 xfade 转场,静图运镜按片段散开挑;每章第一拍开头叠 1.2 秒章节标牌。

时长不再是 6 秒栅格:门禁 A 确认后**先跑 TTS**,每拍的时长 = 它那几句旁白的实际
时长(含句间停顿),画面按这个时长去铺。所以字幕、旁白、画面天然对齐。

底部章节条常驻(当前章高亮),直接烧进每一拍。
"""

from __future__ import annotations

import functools  # noqa: F401
import logging  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401
import zlib  # noqa: F401
from dataclasses import replace  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Sequence  # noqa: F401
from ... import config, db  # noqa: F401
from ...categories import CategoryProfile, Vocab  # noqa: F401
from ...models import Fact, ScriptSegment, ShotSpec  # noqa: F401
from .. import aspect, cards, knobs, library, looks, motion, motion_assets, narration, overlays, rhythm  # noqa: F401
from .. import render as render_mod  # noqa: F401
from ..grounding import numbers_in  # noqa: F401

from .consts import (  # noqa: E402,F401
    BROLL_STYLES,
    HOOK_CUT,
    HOOK_CUT_RETRIES,
    HOOK_SECONDS,
    KEYWORD_CHARS,
    LAYOUTS,
    MAX_BROLL_RUN,
    MAX_CARD_RUN,
    MAX_CONCEPT,
    SAFE_LAYOUT,
    SPLIT_MIN_SECONDS,
    STILL_MOTIONS,
    SUBTITLE_LINE_CHARS,
    SYSTEM,
    USER,
    XFADE,
    XFADE_KINDS,
    _ENC,
    _log,
)
from .text import (  # noqa: E402,F401
    STAT_NOTE_CHARS,
    _BAD_TOPICS,
    _ECHO_MIN_CLAUSE,
    _ECHO_MIN_KEY,
    _UNITS,
    _as_panel_overlay,
    _fact_label,
    _fact_value,
    _hint,
    _norm_words,
    _panel_from_rows,
    _stat_overlay,
    _unit_after,
    echoes_narration,
    forbidden_on_screen,
    ground_keywords,
    ground_note,
    ground_stat,
)
from .concepts import (  # noqa: E402,F401
    concept_guide,
    concept_min,
    concept_scene_for,
    promote_concepts,
)
from .planning_a import (  # noqa: E402,F401
    _beat_line,
    _cap_rows,
    _ground_beat,
    _headline_value,
    auto_panel,
    enforce_broll_cap,
    facts_for,
    rescue_broll,
    sync_narration,
)
from .planning_b import (  # noqa: E402,F401
    KEYWORD_BEAT_MIN,
    KEYWORD_SYSTEM,
    QUIET_CPS,
    QUIET_SECONDS,
    _demote,
    _names_car,
    add_keyword_beats,
    assign_broll_styles,
    beat_subject,
    broll_style,
    enforce_rhythm,
    fallback_beats,
    validate_beats,
)
from .timing import (  # noqa: E402,F401
    beat_durations,
    mark_reveal_times,
    time_beats,
)
from .ffmpeg import (  # noqa: E402,F401
    CONCEPT_FOOTAGE_HINT,
    _BARS_MIN_RATIO,
    _clip_luma,
    _ff,
    _fit_filter,
    _framed,
    _piece_clip,
    _solid,
    _xfade,
    background_clip,
    band_frame,
    concept_background,
    footage_size,
    letterbox_crop,
    motion_for,
    split_clip,
    split_filter,
    xfade_offsets,
)
from .beat_render import (  # noqa: E402,F401
    _broll_base,
    _frames_listing,
    _stat_listing,
    beat_role,
    chapters_of,
    concept_card,
    concept_inputs,
    concept_style_of,
    hook_background,
    layout_filter,
    portrait_concept_style,
    render_beat,
    sting_for,
    with_sting,
)
from .footage import (  # noqa: E402,F401
    footage_mix,
)


#: 横评时追加到分镜 SYSTEM 末尾的规则
#: 逐拍掉人回灌(`team.beat_retention.install`):返回一段「本账号最掉人 / 最留人的版式」。
#: None / 返回空 = 提示词不变
layout_hint: Any = None


def _layout_hint() -> str:
    if layout_hint is None:
        return ""
    try:
        return (layout_hint() or "").strip()
    except Exception:  # noqa: BLE001 - 经验只是锦上添花
        _log.warning("读逐拍经验失败,这次不带", exc_info=True)
        return ""


ROUNDUP_BEAT_RULE = ("\n8. 这是多车横评:拍号后〔〕里是这一拍讲的车。panel / card / stat / concept 的每一行"
                     "只能用**这台车**名下的事实(事实前的 [车名]);没有〔〕的开场 / 结论拍可以用任何一台车的,"
                     "对比卡、柱状图的行名写车名。")


def build_beats(*, subject: str, profile: CategoryProfile, segments: Sequence[ScriptSegment],
                facts: Sequence[Fact], llm, card_log: list[dict] | None = None,
                subjects: Sequence[dict[str, str]] | None = None) -> list[ShotSpec]:
    """一段脚本 = 一拍。LLM 挑版式、填叠加数据,这里逐项核对,核不过就降级成 broll。

    `subjects`:多车横评车单。给了就按车标注事实与拍,核对时每拍只认本车 + 共用事实。"""
    usable = [f for f in facts if f.enabled and f.confidence in ("high", "medium")]
    if subjects:
        facts_payload = "\n".join(f"- {f.id} [{f.subject or '共用'}]"
                                  + (f" [对比:{f.compare_with}]" if f.compare_with else "")
                                  + f": {f.claim}"
                                  for f in usable)[:9000] or "(无)"
    else:
        facts_payload = "\n".join(
            f"- {f.id}" + (f" [对比:{f.compare_with}]" if f.compare_with else "")
            + f": {f.claim}" for f in usable)[:5000] or "(无)"
    vocab = profile.vocab
    from ..script_gen import roster_line
    user = USER.format(
        subject_label=vocab.subject_label if not subjects else "横评车单",
        subject=subject if not subjects else roster_line(subjects), count=len(segments),
        beats="\n".join(_beat_line(i, s, profile, usable) for i, s in enumerate(segments)),
        facts=facts_payload,
    )
    try:
        system = SYSTEM.format(
            kw=KEYWORD_CHARS, domain=vocab.domain, subject_noun=vocab.subject_noun,
            keyword_examples=vocab.keyword_examples, stat_examples=vocab.stat_examples,
            view_enum=vocab.view_enum, view_guide=vocab.view_guide,
            count=len(segments), note_chars=STAT_NOTE_CHARS,
            broll_cap=max(1, int(len(segments) * rhythm.BROLL_RATIO_MAX)),
            concept_guide=concept_guide(vocab, len(segments)))
        if subjects:
            system = system.replace("\n\n必需键: beats", ROUNDUP_BEAT_RULE + "\n\n必需键: beats")
        if hint := _layout_hint():
            system = system.replace("\n\n必需键: beats",
                                    f"\n\n本账号已发作品的逐拍数据(挑版式时参考):{hint}\n\n必需键: beats")
        data = llm.structured(system, user,
                              required_keys=["beats"], temperature=0.5)
        raw = data.get("beats") or []
    except Exception:  # noqa: BLE001 - 版式选不出来也能出片:全部 broll
        raw = []
    by_idx: dict[int, dict] = {}
    for n, item in enumerate(raw):
        if isinstance(item, dict):
            try:
                by_idx[int(item.get("idx", n + 1)) - 1] = item
            except (TypeError, ValueError):
                by_idx[n] = item

    specs = [_ground_beat(i, seg, by_idx.get(i, {}), subject, profile,
                          facts_for(facts, seg.subject, profile.section(seg.section).name),
                          card_log)
             for i, seg in enumerate(segments)]
    # broll 兜底:这一拍讲的数字还有事实支撑,就把它放上屏(见 rescue_broll)
    specs = rescue_broll(specs, segments, facts, profile)
    specs = enforce_rhythm(specs, profile)
    # broll 仍超三成:对超出的拍强制上屏它引用过的事实(见 enforce_broll_cap)
    specs = enforce_broll_cap(specs, segments, facts, profile)
    # 概念动画是全片视觉质量最高的部分,太少了就补(见 promote_concepts)
    specs = promote_concepts(specs, profile)
    return assign_broll_styles(specs)


def measured_features(project_id: str, specs: Sequence[ShotSpec],
                      narration: dict | None = None) -> dict[str, Any]:
    """成片**实际**做成了什么样(复盘按它分档,而不是按旋钮当初想要的值):
    平均拍长、每分钟切几刀、第一拍版式、概念动画占比、素材构成、实际语速、时长。

    旋钮只是「想要」,素材不够 / 版式降级时实际会偏 —— 学习要学实际。"""
    beats = [s for s in specs if s.duration > 0]
    total = sum(s.duration for s in beats)
    if not beats or not total:
        return {}
    cuts = official = 0.0
    for s in beats:
        pieces = db.read_json(project_id, f"beats/{s.idx:03d}.json", None) or []
        cuts += max(1, len(pieces))
        official += sum(float(p.get("length") or 0.0) for p in pieces
                        if p.get("kind") != "still" and not p.get("third_party"))
    concept = sum(1 for s in beats if s.layout == "concept")
    nar = narration or {}
    chars = int(nar.get("total_chars") or 0)
    mix = footage_mix(project_id, specs)
    out = {
        "duration": round(total, 1),
        "beats": len(beats),
        "avg_beat_seconds": round(total / len(beats), 2),
        "cuts_per_minute": round(cuts * 60 / total, 1),
        "first_layout": beats[0].layout or "",
        "concept_share": round(concept / len(beats), 3),
        "official_video_ratio": round(official / mix["seconds"], 3) if mix.get("seconds") else None,
        "still_ratio": mix.get("still_ratio"),
        "third_party_ratio": mix.get("third_party_ratio"),
        "chars_per_second": round(chars / total, 2) if chars else None,
        "bgm": bool(nar.get("bgm_path")),
    }
    return out


def beats_dir(project_id: str) -> Path:
    d = config.project_dir(project_id) / "beats"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: 一拍的素材池最多等多久(秒)。超时就先出卡片并留痕 —— 素材池建不出来时
#: 流水线不能永远卡住,但"这台车一个画面都没有"必须让门禁看见。
POOL_WAIT_SECONDS = 20 * 60


def _load_used(project_id: str) -> dict[str, int]:
    return db.read_json(project_id, "beats_used.json", {}) or {}


def _load_recent(project_id: str) -> list[str]:
    return list(db.read_json(project_id, "beats_recent.json", []) or [])


def card_backgrounds(subject: str) -> list[Path]:
    """数据卡的底图候选:池子里干净的主视角官图(汽车是外观;虚化压暗后做底),
    按视角散开排 —— 各张卡按拍号轮换,不再全片共用一张。

    图库官图的车牌上印着别家 logo(懂车帝的黄牌),铺满全屏时太显眼,
    这里换成抹过水印的版本(抹不动就用原图)。"""
    lib = library.Library.load(subject)
    stills = [i for i in lib.items if i.kind == "still" and i.usable and Path(i.path).is_file()]
    primary = library.vocab_of(lib).primary_views
    stills.sort(key=lambda i: (i.view not in primary, library._dirt(i)))
    groups = library.near_dup_groups(stills)
    seen: set[str] = set()
    out: list[Path] = []
    for i in stills:  # 同一簇(几乎同一张)只留一张
        if groups[i.id] not in seen:
            seen.add(groups[i.id])
            out.append(Path(library.clean_still(i)))
    return out


def card_background(subject: str) -> Path | None:
    bgs = card_backgrounds(subject)
    return bgs[0] if bgs else None


def init_beats(project_id: str, specs: Sequence[ShotSpec]) -> None:
    """门禁 A 之后:清掉两档旧记录,按当前分镜重建 shots 行(全部 pending)。"""
    for stage in ("draft", "final"):
        db.delete_shots(project_id, stage)
        db.upsert_shots(project_id, [
            {"idx": s.idx, "stage": stage, "model": "explainer", "provider_task_id": None,
             "submitted_at": None, "last_polled_at": None, "draft_path": None,
             "final_path": None, "error": None}
            for s in specs
        ])


def _shot_status(project_id: str, idx: int) -> str:
    """取某一拍在草稿档的当前状态(找不到就是空串)。"""
    for r in db.get_shots(project_id, "draft"):
        if r["idx"] == idx:
            return str(r.get("status") or "")
    return ""


def render_pending(project_id: str, specs: Sequence[ShotSpec], *, subject: str,
                   profile: CategoryProfile, limit: int = 3,
                   subjects: Sequence[str] | None = None,
                   should_stop: Any = None) -> dict[str, Any]:
    """渲染还没出片的拍(每次最多 `limit` 拍,后台循环反复调用)。

    产物写成 draft 档 succeeded —— 门禁 B 的审片页按草稿档展示。
    `subjects`:多车横评车单 —— 每拍按 `beat_subject` 取自己那台车的素材,绝不借别的车的。
    `should_stop`:可选的回调,返回 True 就**在拍与拍之间收手**。服务停机
    时用它把手头这一拍做完就停下 —— 剩下的拍留到下次,而不是被砍在
    半截(渲染到一半的 mp4 是坏的)。
    """
    subjects = list(subjects or [])
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    todo = [s for s in specs if s.duration > 0
            and rows.get(s.idx, {}).get("status") != "succeeded"][:limit]
    chapters, where = chapters_of(specs)
    used = _load_used(project_id)
    recent = _load_recent(project_id)
    beats_dir(project_id)  # 每拍的背景片段记录放这里
    # 成品放 drafts/:审片页按 /media/{项目}/drafts/{文件} 取草稿预览
    out_dir = config.project_dir(project_id) / "drafts"
    bg_cache: dict[str, list[Path]] = {}
    style = concept_style_of(project_id)
    # 画幅是项目属性(创建时定),不是品类属性
    project = db.get_project(project_id) or {}
    size = aspect.frame_size(project)
    # 竖屏标题区常驻的大标题:脚本阶段 LLM 起的片名
    headline = str(project.get("title") or "") if aspect.is_portrait(project) else ""
    done, errors = [], []
    notes: list[str] = []
    waiting: list[str] = []
    #: 每拍等素材池的起始时间(拍号 → 时间戳):素材池建得太慢时不无限等
    waits = dict(db.read_json(project_id, "beats_waiting.json", {}) or {})
    #: 每台车的素材池状态查一次就够(读 library.json 不便宜)
    pool_cache: dict[str, str] = {}
    for s in todo:
        # 停机信号:在**拍与拍之间**收手。手头这一拍做完就停,剩下的
        # 留到下次 —— 正在渲的 mp4 被砍在中间是坏的。
        if should_stop is not None and should_stop():
            break
        # 逐拍重读一次状态。todo 是开头那份快照 —— 若这一拍在快照之后
        # 已经被渲好了(succeeded),再渲一遍会与它抢同一个输出文件,
        # 产出半截 mp4。重拍(置回 pending)不受影响:那样本来就该重渲。
        if _shot_status(project_id, s.idx) == "succeeded":
            continue
        pos = next(n for n, x in enumerate(specs) if x.idx == s.idx)
        car = beat_subject(s, subjects, subject)
        # 这台车的素材池还没建好就**先别渲这一拍**:渲出来只有一张卡片,
        # 观众看到的是「这台车一个画面都没有」。等下一轮(池子建完自动补上)。
        status = pool_cache.get(car)
        if status is None:
            status = pool_cache[car] = library.pool_status(car)
        if status in ("building", "tagging"):
            # 首次遇到这一拍:记下开始等的时间。**必须用记下来的这个时间**,
            # 拿 0 当起点会让「等了多久」变成 epoch 差值(实跑踩过:记成 29884999 分钟),
            # 于是这一拍等都没等就直接降级成卡片了。
            first = float(waits.get(str(s.idx)) or 0.0)
            if not first:
                first = time.time()
                waits[str(s.idx)] = first
                db.write_json(project_id, "beats_waiting.json", waits)
            if time.time() - first <= POOL_WAIT_SECONDS:
                waiting.append(f"第 {s.idx} 拍({car}):等素材池"
                               f"{'建好' if status == 'building' else '打好标签'}")
                continue
            notes.append(f"第 {s.idx} 拍({car}):素材池 {int((time.time() - first) // 60)} 分钟还没建好,"
                         "这拍只能出卡片")
            waits.pop(str(s.idx), None)
            db.write_json(project_id, "beats_waiting.json", waits)
        elif status == "empty":
            notes.append(f"第 {s.idx} 拍({car}):素材池里没有可用素材,这拍只有卡片")
        if car not in bg_cache:
            bg_cache[car] = card_backgrounds(car)
        bgs = bg_cache[car]
        try:
            path, pieces = render_beat(s, subject=car, size=size,
                                       out_dir=out_dir, chapters=chapters,
                                       chapter_idx=where[pos], used=used,
                                       card_bg=bgs[s.idx % len(bgs)] if bgs else None,
                                       recent=recent,
                                       sting=sting_for(specs, where, chapters, s.idx),
                                       style=style, headline=headline, notes=notes,
                                       ranges=profile.vocab.concept_ranges,
                                       role=beat_role(where, pos, len(chapters)))
        except (render_mod.RenderError, RuntimeError, OSError) as exc:
            errors.append(f"第 {s.idx} 拍:{exc}")
            db.update_shot(project_id, s.idx, "draft", status="failed", error=str(exc)[:500])
            continue
        db.write_json(project_id, f"beats/{s.idx:03d}.json", pieces)
        db.update_shot(project_id, s.idx, "draft", status="succeeded", draft_path=str(path),
                       provider_task_id=None, error=None, est_cost=0.0,
                       consistency="card" if s.layout in ("card", "concept") else "real")
        done.append(s.idx)
    db.write_json(project_id, "beats_used.json", used)
    db.write_json(project_id, "beats_recent.json", recent)
    for note in notes:
        # 降级要留痕:不然「概念动画全变成数据卡」看起来像正常产出
        db.add_event(project_id, "concept:degraded", reason=note)
    for note in waiting:
        # 素材池还没建好,这一拍先不渲:渲了就是没有这台车画面的卡片
        db.add_event(project_id, "assets:waiting", reason=note)
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    remaining = [s.idx for s in specs if s.duration > 0
                 and rows.get(s.idx, {}).get("status") != "succeeded"]
    return {"rendered": done, "errors": errors, "remaining": remaining,
            "waiting": waiting,
            "stage_complete": not remaining}


def asset_coverage(project_id: str, specs: Sequence[ShotSpec], subjects: Sequence[str],
                   subject: str = "") -> dict[str, Any]:
    """每台车在片子里有没有真的露过脸(用到了它素材池里的画面)。

    素材池采集与逐拍渲染是并行的:池子没建好的车,拍出来就是一张纯卡片 ——
    观众看到的是「这台车一个镜头都没有」。这里按车点出实际用到的素材秒数,
    门禁 B 据此拦下,不让这种片子进发布队列。

    返回 {"cars": [{"name", "beats", "seconds"}...], "missing": [没露脸的车]}。
    """
    cars = [c for c in (subjects or []) if c]
    if not cars and subject:
        cars = [subject]
    if not cars:
        return {"cars": [], "missing": []}
    beats_of: dict[str, list[int]] = {c: [] for c in cars}
    seconds_of: dict[str, float] = {c: 0.0 for c in cars}
    for s in specs:
        pieces = db.read_json(project_id, f"beats/{s.idx:03d}.json", None) or []
        for p in pieces:
            parts = Path(str(p.get("path") or "")).parts
            for car in cars:
                if library.slug(car) in parts:
                    beats_of[car].append(s.idx)
                    seconds_of[car] += float(p.get("length") or 0.0)
                    break
    rows = [{"name": c, "beats": sorted(set(beats_of[c])), "seconds": round(seconds_of[c], 1),
             "pool": library.Library.dir_for(c).is_dir()}
            for c in cars]
    # 只拦「有素材池却一个画面都没用上」的车:没建池子的车(品类不用素材池、
    # 或采集彻底失败)不在这里硬拦,否则整个品类都会被卡住。
    return {"cars": rows, "missing": [r["name"] for r in rows if r["pool"] and not r["beats"]],
            "no_pool": [r["name"] for r in rows if not r["pool"]]}


def rerender(project_id: str, idx: int) -> None:
    """门禁 B「重拍」:换一组背景重渲这一拍。刚用过的片段记成多用几次,排到后面去。"""
    pieces = db.read_json(project_id, f"beats/{idx:03d}.json", []) or []
    used = _load_used(project_id)
    for p in pieces:
        used[p["item_id"]] = used.get(p["item_id"], 0) + 3
    db.write_json(project_id, "beats_used.json", used)
    db.update_shot(project_id, idx, "draft", status="pending", draft_path=None, error=None)
    db.update_shot(project_id, idx, "final", status="pending", final_path=None, error=None)


def promote_all(project_id: str, specs: Sequence[ShotSpec]) -> int:
    """门禁 B 通过:草稿就是成片(画面不再重新生成),写进成片档。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "draft")}
    n = 0
    for s in specs:
        r = rows.get(s.idx)
        if r and r.get("status") == "succeeded" and r.get("draft_path"):
            db.update_shot(project_id, s.idx, "final", status="succeeded",
                           final_path=r["draft_path"], consistency=r.get("consistency") or "real",
                           est_cost=0.0, error=None)
            n += 1
    return n


def _price_of(facts: Sequence[Any]) -> tuple[str, str]:
    """从事实里取入门价。返回 (价格文案, 事实 id)。

    封面上的价格和脚本里的数字一样,必须来自事实层 —— 不能手写"23.35万"。
    取**最低**的那一条:入门价就是最低售价,封面报最高价是误导。
    口径(basis)记在 render.json 里留痕;但**有条件的价**必须带上口径前缀:
    「限时先享价 8.98 万」写成「8.98万元起」,观众会当成常规售价 —— 红线是数字要带口径。

    `facts` 是 facts.json 里的原始字典(不是 Fact 模型):落盘的是 model_dump,
    早期版本的 enabled 存的是字符串,所以取值都做兼容。
    """
    def get(f: Any, key: str, default: Any = "") -> Any:
        if isinstance(f, dict):
            return f.get(key, default)
        return getattr(f, key, default)

    # 事实里说价格的词,不止「售价/价格」:官方口径常写「厂商指导价」「限时先享价」。
    # 只认前两个词,这些事实会被整条丢掉,入门价就只剩某一条新闻里的中段价。
    _PRICE_WORDS = ("售价", "价格", "起售", "定价", "官降", "落地",
                    "指导价", "先享价", "元起")
    # 「9.99 万至 14.19 万元」这种区间:上限那句匹配不上「数字+元」,下限要单独认
    _RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*万\s*(?:至|到|[-~—])\s*\d")
    # 有条件的价:封面得写出来,不然等于报了个假常规价
    _CONDITIONAL = (("限时", ("限时", "先享价")), ("优惠", ("优惠", "促销", "活动价")))

    best: tuple[float, str, str, str, str] | None = None  # (值, 文案, fact id, basis, claim)
    for f in facts or []:
        if get(f, "enabled", True) in (False, "False", "false", 0, "0"):
            continue
        if get(f, "scope", "") != "product":
            continue
        if get(f, "confidence", "") == "low":
            continue
        claim = str(get(f, "claim", "") or "")
        if not any(w in claim for w in _PRICE_WORDS):
            continue
        candidates: list[tuple[float, str]] = []
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万)?元", claim):
            value = float(m.group(1)) * (10000 if m.group(2) else 1)
            candidates.append((value, f"{m.group(1)}{'万' if m.group(2) else ''}元"))
        for m in _RANGE_RE.finditer(claim):
            candidates.append((float(m.group(1)) * 10000, f"{m.group(1)}万元"))
        for value, text in candidates:
            if best is None or value < best[0]:
                best = (value, text, str(get(f, "id", "")), str(get(f, "basis", "") or ""), claim)
    if best is None:
        return "", ""
    value, text, fact_id, basis, claim = best
    hay = f"{basis} {claim}"
    qualifier = next((prefix for prefix, marks in _CONDITIONAL
                      if any(m in hay for m in marks)), "")
    return f"{qualifier}{text}起", fact_id


def cover_meta(project_id: str, profile: CategoryProfile, total: float) -> dict[str, Any]:
    """封面上的两行文字:角标(主体 + 入门价)和副标题(主论点)。

    副标题用主论点而不是"X分X秒 · 品类名":主论点是全片那句判断,
    放在封面上就是一句完整的钩子;时长信息对观众没有意义。
    """
    project = db.get_project(project_id) or {}
    subject = str(project.get("subject") or "")
    thesis = str(project.get("thesis") or "").strip()
    facts = db.read_json(project_id, "facts.json", []) or []
    price, fact_id = _price_of(facts)
    kicker = " · ".join(x for x in (subject, price) if x)
    subtitle = thesis or f"{int(total // 60)}分{int(total % 60)}秒 · {profile.label}"
    return {"kicker": kicker, "subtitle": subtitle, "price_fact": fact_id}


def compose(project_id: str, specs: Sequence[ShotSpec], profile: CategoryProfile,
            title: str, llm: Any = None) -> dict[str, Any]:
    """成片:拼各拍 + 旁白/BGM 混音 + 字幕。旁白在门禁 A 之后已经生成好了。"""
    rows = {r["idx"]: r for r in db.get_shots(project_id, "final")}
    beats = [s for s in specs if s.duration > 0]
    missing = [s.idx for s in beats if not (rows.get(s.idx) or {}).get("final_path")]
    if missing:
        raise RuntimeError(f"还有 {len(missing)} 拍没有成片:{missing[:12]}")
    nar = db.read_json(project_id, "narration.json", {}) or {}
    project_dir = config.project_dir(project_id)
    project = db.get_project(project_id) or {}
    size = aspect.frame_size(project)
    # 拼接缓存按序号命名,重拍过的拍必须重新归一化
    shutil.rmtree(project_dir / "_render", ignore_errors=True)
    result = render_mod.compose(
        project_id, [Path(rows[s.idx]["final_path"]) for s in beats],
        ass_path=Path(nar["ass_path"]) if nar.get("ass_path") else None,
        narration_path=Path(nar["narration_path"]) if nar.get("narration_path") else None,
        bgm_path=Path(nar["bgm_path"]) if nar.get("bgm_path") else None,
        size=size, durations=[s.duration for s in beats],
        output_name=aspect.output_name(project),
    )
    if not result.ok or not result.output_path:
        raise RuntimeError("合成失败:" + "; ".join(result.warnings or []))
    total = sum(s.duration for s in beats)
    meta = cover_meta(project_id, profile, total)
    # 封面底图优先用素材池里的干净官图(白底棚拍图没有第三方水印);
    # 没有官图才从成片抽帧。
    subject = str(project.get("subject") or "")
    still = library.cover_still(subject) if subject else None
    # 包装先出:统一风格封面的大标题用包装里选中的那条
    _make_packaging(project_id, profile, llm)
    from .. import cover_art, packaging

    dest_a = project_dir / aspect.cover_name(project)
    title_a = cover_art.pick_title(packaging.load(project_id), title)
    art = cover_art.build(project_id, dest_a, size=size, title=title_a, subject=subject,
                          kicker=meta["kicker"])
    cover = Path(art["path"]) if art else render_mod.build_cover(
        result.output_path, title, dest_a, subtitle=meta["subtitle"], size=size,
        still=still, kicker=meta["kicker"])
    source_note = ("画面素材:品牌官方宣传片与官方图片,仅作"
                   f"{profile.vocab.subject_label}展示")
    if art and art.get("car_source") == "ai_generated":
        # 封面那台车是生图出来的:说清楚,别让人以为是官方实拍
        source_note += ";封面车型图为 AI 生成示意,以官方图为准"
    payload = {**result.to_dict(), "cover": str(cover) if cover else None, "narration": nar,
               "cover_meta": meta, "cover_still": str(still) if still else None,
               "cover_style": "art" if art else "legacy",
               "cover_car": (art or {}).get("car_source", "legacy"),
               "source_note": source_note}
    if art:
        payload["cover_art"] = {"a": art}
    db.write_json(project_id, "render.json", payload)
    try:
        db.write_json(project_id, "features.json", measured_features(project_id, specs, nar))
    except Exception:  # noqa: BLE001 —— 只是复盘用的记录,不能让成片失败
        _log.warning("features.json 写入失败", exc_info=True)
    cover_b = _make_cover_b(project_id, project, title, meta, size, result.output_path, still,
                            art=art, title_a=title_a)
    if cover_b:
        payload["cover_b"] = cover_b
        db.write_json(project_id, "render.json", payload)
    return payload


def _make_cover_b(project_id: str, project: dict, title: str, meta: dict, size: tuple[int, int],
                  video_path: Any, still_a: Any, art: dict | None = None,
                  title_a: str = "") -> dict | None:
    """封面 B:大字版。大字取自已校验的数字型标题 / 入门价 / 主论点(取不到就不出 B)。

    底图尽量换一张**看起来不同**的官图;池子里没有第二张就用同一张,并在记录里标 same_still。
    `art`:A 版用了统一风格封面,B 也先试统一风格(两版不混风格;A 是老封面 B 也走老封面)。
    只是辅助物料:任何失败都不能让成片失败。"""
    from .. import cover_art, packaging

    try:
        pkg = packaging.load(project_id)
        price, _ = _price_of(db.read_json(project_id, "facts.json", []) or [])
        hook = packaging.cover_hook(pkg.titles if pkg else [], price=price,
                                    thesis=str(project.get("thesis") or ""))
        if not hook:
            return None
        subject = str(project.get("subject") or "")
        still = library.cover_still(subject, exclude=[still_a] if still_a else ()) if subject else None
        same = still is None and bool(still_a)
        if same:
            still = still_a
        # 标题:B 版说明文字用「选中的标题」,没有包装就用项目标题
        caption = title
        if pkg and pkg.titles:
            caption = (packaging.hook_title(pkg.titles, hook)  # 大字出自哪条,说明文字就用哪条
                       or pkg.titles[min(max(int(pkg.picked.get("title", 0)), 0), len(pkg.titles) - 1)].text)
        dest = config.project_dir(project_id) / aspect.cover_name(project, "b")
        made = cover_art.build(project_id, dest, size=size,
                               title=cover_art.b_title(pkg, title_a or caption, hook),
                               subject=subject, kicker=meta.get("kicker", ""), variant="b",
                               hook=hook,
                               exclude=[art.get("car_reference") or art["still"]]) if art else None
        if made:
            return {"path": made["path"], "hook": cover_art.usable_hook(hook), "style": "art",
                    "tone": made["tone"], "source": made["source"], "still": made["still"],
                    "car_source": made.get("car_source", "official_cutout")}
        cover = render_mod.build_cover(video_path, caption, dest, subtitle=meta.get("subtitle", ""),
                                       size=size, still=still, kicker=meta.get("kicker", ""),
                                       layout="hook", hook=hook)
        if not cover:
            return None
        return {"path": str(cover), "hook": hook, "still": str(still) if still else None,
                "same_still": same}
    except Exception as exc:  # noqa: BLE001
        db.add_event(project_id, "packaging", reason=f"封面 B 生成失败:{exc}")
        return None


def _make_packaging(project_id: str, profile: CategoryProfile, llm: Any) -> None:
    """成片后备好发布包装(3 标题 + 置顶评论)。已有就不重做,免得冲掉用户的选择。

    只是辅助物料:任何失败都只记一条事件,不能让成片失败。
    """
    from .. import packaging

    try:
        if packaging.load(project_id) is None:
            made = packaging.ensure(project_id, profile, llm)
            db.add_event(project_id, "packaging",
                         reason=f"发布包装已生成({'LLM' if made.source == 'llm' else '模板兜底'})")
    except Exception as exc:  # noqa: BLE001
        db.add_event(project_id, "packaging", reason=f"发布包装生成失败:{exc}")
