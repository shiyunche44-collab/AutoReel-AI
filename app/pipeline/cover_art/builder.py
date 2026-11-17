"""合成与出片:底图 → 车 → 字,按现有 render.json 重画 A / B 版。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence
from ... import config, degrade

from .tone import Tone, log, tone_of
from .studio import background
from .car_ai import pick_visual
from .layout import (Frame, model_word, place_art, place_car, question_part, render, short_name,
                     split_title, strip_figure, usable_hook)


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------
def _save(image: Any, dest: Path) -> None:
    tmp = dest.with_name(f"{dest.stem}.part{dest.suffix}")
    try:
        image.save(tmp, quality=93)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def compose(cut: Any, tone: Tone, size: tuple[int, int], *, main: str, sub: str = "",
            name: str = "", brand: str = "", series: str = "",
            art: Any = None) -> tuple[Any, str]:
    """底图 → 型号大字 + 车 → 字。返回 (图, 底图来源)。
    `art`:拆解封面的剖视图(暗底色调才用):图当主角,车缩小放右下角,不画型号大字。"""
    canvas, source = background(tone, size)
    if tone.dark and art is not None:
        place_art(canvas, art, Frame.of(size))
        place_car(canvas, cut, Frame.of(size), tone=tone, small=True)
    else:
        place_car(canvas, cut, Frame.of(size), tone=tone, ghost="" if tone.dark else model_word(name))
    # 车名角标**始终画**:主页一排封面靠它认出同一个号(账号里已发的几条都有这块)。
    # 以前是「主标题已经是车名就不重复」—— 大唐、小米YU7 那几条的角标能出现是因为
    # 大字和车名不是同一个字符串,纯属巧合;蔚来ES8 这种大字就是车名的就整个没了,
    # 一排看过去跟别人的封面不是一套(实跑踩过)。
    # 挂了栏目就并进角标:「车名 | 柒哥帮你挑·第3期」,观众一眼知道这是哪个系列的第几期
    chip = " | ".join(x for x in (name, series) if x)
    return render(canvas, tone, main=main, sub=sub, chip=chip, brand=brand), source


def build(project_id: str, dest: str | Path, *, size: tuple[int, int], title: str, subject: str,
          kicker: str = "", variant: str = "a", hook: str = "",
          exclude: Sequence[str | Path] = ()) -> dict[str, Any] | None:
    """出一版封面,同时按 3:4 和 4:3 各重排一张(小红书 3:4;抖音网页版要横 4:3 + 竖 3:4)。
    成功返回 {"path", "tone", "source", "still"},否则 None。

    车从哪来:素材池的白底官图(抠图)→ 抠不出来就生图生成一台(`ai_car`,过三道质检门)
    → 都没有才退回老封面。两种来源走的是同一套版式,主页一排看过去是同一种封面。

    B 版:有带数字的钩子就钩子当大字、标题当副标;`exclude` 传 A 版用过的官图,尽量换一张车图。"""
    from ..narration import cover_3x4_path, cover_4x3_path

    if not config.COVER_ART or not title:
        return None
    try:
        visual = pick_visual(subject, exclude)
        if visual is None:
            log.info("封面:%s 没有能抠的白底官图,退回老封面", subject)
            degrade.note("cover.to_old", f"{subject} 没有能抠的白底官图")
            return None
        cut, still = visual.cut, visual.still
        hook = usable_hook(hook) if variant == "b" else ""
        main, sub = (hook, title) if hook else split_title(title, short_name(kicker))
        if hook:
            # 大字是那个数字,副标里就别再出现一遍
            sub = strip_figure(sub, hook) or sub
        tone = tone_of(project_id, variant)
        art = None
        if tone.dark and variant == "b":
            # 拆解封面不出 B 版:B 版的大字是「带数字的钩子」(入门价、扭矩),放在问题句封面的
            # 版式里既不是这条片的问题,数字还会被拆成半截(样片出过「340 / N·m怎么做出…」)
            return None
        if tone.dark:
            # 拆解封面:大字是问题句(车名在角标里),配这条片用得最多的那张剖视图
            if not hook:
                main, sub = question_part(title, subject), ""
            art = _mech_art(project_id)
        short = min(size)
        jobs = [(Path(dest), size),
                (cover_3x4_path(dest), (short, short * 4 // 3)),
                (cover_4x3_path(dest), (short * 4 // 3, short))]
        name = short_name(kicker)
        series_chip = _series_chip(project_id)
        rendered = [(path, compose(cut, tone, sz, main=main, sub=sub, name=name,
                                   brand=config.COVER_BRAND, series=series_chip, art=art))
                    for path, sz in jobs]
        for path, (im, _) in rendered:
            _save(im, path)
        return {"path": str(dest), "tone": tone.name, "source": rendered[0][1][1],
                "still": str(still), "main": main, "sub": sub, **visual.trace}
    except Exception as exc:  # noqa: BLE001
        log.warning("统一风格封面失败,退回老封面:%s", exc)
        degrade.note("cover.to_old", f"统一风格封面失败:{exc}")
        return None


def _mech_art(project_id: str) -> Path | None:
    """这条片的机构画面里用得最多的那张剖视图(已经生成过、在缓存里的);没有返回 None。
    只读缓存不出新图:封面不该因为生图失败拦住成片。"""
    from collections import Counter

    from ... import db
    from .. import mechanism_art

    try:
        beats = db.read_json(project_id, "storyboard.json", []) or []
        arts = Counter((str(ov.get("part") or ""), str(ov.get("art") or ""))
                       for b in beats if isinstance(b, dict)
                       for ov in [b.get("overlay") or {}]
                       if b.get("layout") == "mech" and ov.get("art"))
        for (part, art), _ in arts.most_common():
            try:
                return mechanism_art.cutaway(part, art, generate=False)
            except mechanism_art.MechArtError:
                continue
    except Exception as exc:  # noqa: BLE001
        log.info("封面:取剖视图失败,只放车:%s", exc)
    return None


def _series_chip(project_id: str) -> str:
    """项目挂的栏目角标(pipeline/series.py);读不到 = 空串。"""
    from ... import db
    from .. import series, variety

    try:
        return series.chip(variety.parse((db.get_project(project_id) or {}).get("variant")))
    except Exception:  # noqa: BLE001
        return ""


def pick_title(pkg: Any, fallback: str) -> str:
    """封面大标题:包装里选中的那条(≤18 字、数字已核对);没有包装用片名。"""
    if pkg is not None and getattr(pkg, "titles", None):
        idx = pkg.picked.get("title", 0)
        idx = idx if isinstance(idx, int) and 0 <= idx < len(pkg.titles) else 0
        return pkg.titles[idx].text
    return fallback


def b_title(pkg: Any, title_a: str, hook: str) -> str:
    """B 版的标题:大字出自哪条就用哪条;没有能用的大字就换一条**不同写法**的标题,
    让 A/B 的差别落在文案上,而不是同一句话换个底色。"""
    from .. import packaging

    if pkg is None or not getattr(pkg, "titles", None):
        return title_a
    if usable_hook(hook):
        return packaging.hook_title(pkg.titles, hook) or title_a
    return next((t.text for t in pkg.titles if t.text != title_a), title_a)


# --------------------------------------------------------------------------
# 命令行:给已完成的项目重出封面
# --------------------------------------------------------------------------
def rebuild(project_id: str) -> dict[str, Any]:
    """按现有 render.json / packaging.json 重画 A、B 封面,不重新合成视频。"""
    from ... import db
    from .. import aspect, explainer, packaging

    project = db.get_project(project_id) or {}
    rendered = db.read_json(project_id, "render.json", {}) or {}
    if not project or not rendered:
        return {"ok": False, "reason": "项目不存在或还没成片"}
    meta = rendered.get("cover_meta") or {}
    size = aspect.frame_size(project)
    subject = str(project.get("subject") or "")
    pkg = packaging.load(project_id)
    title_a = pick_title(pkg, str(project.get("title") or ""))
    out: dict[str, Any] = {"ok": True}
    a = build(project_id, config.project_dir(project_id) / aspect.cover_name(project),
              size=size, title=title_a, subject=subject, kicker=meta.get("kicker", ""))
    out["a"] = a
    price, _ = explainer._price_of(db.read_json(project_id, "facts.json", []) or [])
    hook = packaging.cover_hook(pkg.titles if pkg else [], price=price,
                                thesis=str(project.get("thesis") or ""))
    if hook and a:
        # B 版换一张参照图(AI 车用的是官方参照图,真车用的是官图本身)
        out["b"] = build(project_id, config.project_dir(project_id) / aspect.cover_name(project, "b"),
                         size=size, title=b_title(pkg, title_a, hook), subject=subject,
                         kicker=meta.get("kicker", ""), variant="b", hook=hook,
                         exclude=[a.get("car_reference") or a["still"]])
    if a:
        rendered["cover_style"] = "art"
        rendered["cover_art"] = {k: v for k, v in (("a", a), ("b", out.get("b"))) if v}
        rendered["cover_car"] = a.get("car_source", "official_cutout")
        note = str(rendered.get("source_note") or "")
        if a.get("car_source") == "ai_generated" and "AI 生成" not in note:
            # 封面那台车是生图出来的:说清楚,别让人以为是官方实拍
            rendered["source_note"] = (note + ";封面车型图为 AI 生成示意,以官方图为准").strip(";")
        db.write_json(project_id, "render.json", rendered)
    return out
