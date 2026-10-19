"""④ 素材解析:构建**全片共享**的参考图集(计划 §4.4 第一层)。

为什么是"全片共享"而不是"每镜头各找各的":
10-30 个镜头若各用各的图,同一辆车在不同镜头里颜色、轮毂、内饰必然漂移。
共享一套图(3-5 张,覆盖不同视角)再按镜头视角排序,能让 H3 在全片维持同一辆车。

⚠️ 检索图**只作为参考图/首帧进入生成流程**,成片画面始终是 H3 的生成产物 ——
既拿到真实外观,又不直接搬运他人素材。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ..categories import CategoryProfile
from ..models import ShotSpec
from ..providers import images, webassets

#: 用户手动上传参考图的目录。人工挑过的照片是唯一能"保证是这台车"的来源,
#: 因此优先级高于一切自动检索 —— 自动检索再灵,也不知道你要哪一台年款。
USER_REF_DIRNAME = "user_refs"

#: 允许的手动上传图片类型
_USER_REF_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")

#: 明显不是编辑级产品照的站点。检索最高频的两类误命中是**电商白底图**
#: (压根不是这台车)和**图库水印图**,两者都不该进参考图集。
#:
#: 这里用硬匹配而不是交给模型判断:一次 `describe` 要花钱,而这些站点
#: 的图连问都不用问 —— 看域名就知道。
_JUNK_HOST_PATTERNS = (
    "alibaba", "aliexpress", "ebay", "dhgate", "1688", "taobao", "jd.com",
    "facebook.com", "lookaside.fbsbx", "instagram", "pinterest",
    "shutterstock", "gettyimages", "alamy", "dreamstime", "123rf",
)
_JUNK_PATH_PATTERNS = ("/product/", "/listing/", "/shop/", "/item/", "/cart")

#: 校验时直接弃用的理由。`text_overlay` 不在列 —— 带水印的图虽然不干净,
#: 但作为"这台车长什么样"的锚点仍然成立,丢掉反而更可惜。
_BLOCKING_PROBLEMS = {"not_target_model", "e-commerce"}


def _junk_reason(ref: images.ImageRef) -> str:
    """返回该图被硬过滤的原因;保留则返回空串。"""
    url = ref.url.lower()
    for host in _JUNK_HOST_PATTERNS:
        if host in url:
            return f"电商/图库站点({host})"
    for path in _JUNK_PATH_PATTERNS:
        if path in url:
            return f"商品页路径({path})"
    return ""


def verify_prompt(subject: str) -> str:
    """构造参考图校验提问。

    必须把**目标车型**写进提示词。过去这一层只问"是不是汽车产品照片",
    从没告诉模型目标是谁 —— 于是它只能答"这是一台轿车",校验通过,但
    车画出来照样不是这台。这正是"文案讲汉、画面不是汉"的另一条来路。

    ⚠️ 实测:视觉问答工具**恒返回散文描述**,不理会"只输出 JSON 数组"的
    要求 —— 提示词里写死输出形状、要求首尾是 `[` `]` 也一样,返回的仍是
    几百字的图文描述。所以这套结构化过滤在当前工具下**不会真的剔掉东西**,
    `parse_verdicts` 失败一律保留。

    这是刻意的保守选择,不是没做完:用启发式去猜散文里"讲的是不是这台车",
    会把明明正确、只是没被点名的好参考图删掉,最后退化成 AI 编的假车 ——
    那比不过滤糟糕得多。工具哪天开始认 JSON,这套解析会自动生效,不用改代码。

    真正解决"车型不对"的是上一层的检索(拿到真实车照),不是这一层校验。
    """
    return (
        f"下面每张图片需要你判定一次。目标车型是「{subject}」。"
        "只依据图片内容回答,不确定就说 unknown,不要猜。\n"
        "对每张图片输出一个 JSON 对象,字段:\n"
        '  "index": 第几张图(从 1 开始,与给出顺序一致)\n'
        f'  "is_target_model": true / false / "unknown" —— 画面里的车是不是「{subject}」\n'
        '  "view": exterior / interior / rear / detail / other\n'
        '  "problem": 空字符串表示可用;否则填下面之一 —— '
        "not_target_model(不是这台车) / e-commerce(电商白底图或画面带价格文字) / "
        "text_overlay(带大段文字或水印) / low_quality(糊、过暗或纯截图) / unknown\n"
        "只输出 JSON 数组,不要任何解释文字。"
    )


def parse_verdicts(text: str) -> list[dict[str, Any]]:
    """从自由文本里抠出 JSON 数组。解析不出来就返回空列表。

    故意保守:抠不出来就返回空,让调用方按"未校验"处理。**绝不猜** ——
    把解析失败当成"全部不合格"会把素材层变成新的失败源,而这一层
    的第一原则是降级而不是阻断。

    公开出来是因为 `clip_resolver` 的片段质检要用同一套抠取逻辑:
    视觉问答工具的返回形态不统一,两边各写一份必然漂移。
    """
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _prepare_verify_targets(picked: list[images.ImageRef], project_dir: Path,
                           report: dict[str, Any]
                           ) -> tuple[list[str], list[int], dict[str, Path]]:
    """把待校验的图变成视觉问答工具能吃的地址。

    返回 (提交地址, 对应的 picked 下标, url → 本地副本)。

    ⚠️ 视觉问答工具**拒收外部直链**,实测返回:
    `image_info.url could not be used as a media source; download the resource in
    your local or sandbox workspace first, then call upload_temp_url`。

    所以外部检索图必须先落地再上传。本地副本按 url 哈希命名并返回,
    下面的预览下载直接复用 —— 否则同一张图会被下两遍,慢一倍还多一份磁盘。

    只有 AI 生成的图是例外:它的 URL 本来就是 `get_asset_url()` 给的临时地址,
    可以直接提交。落地或上传失败的图**不丢弃**,只是这一张没参与校验:
    它仍然是可用的 H3 参考图(生成侧自己去取,不走本机)。
    """
    import hashlib

    targets: list[str] = []
    slots: list[int] = []
    local_of: dict[str, Path] = {}
    verify_dir = project_dir / "_verify"
    for pos, ref in enumerate(picked):
        if ref.origin == "generate":
            targets.append(ref.url)
            slots.append(pos)
            continue
        try:
            digest = hashlib.sha1(ref.url.encode("utf-8")).hexdigest()[:12]
            local = images.download(
                ref.url, verify_dir / f"v{digest}.jpg",
                headers={"User-Agent": webassets.USER_AGENT},
            )
            local_of[ref.url] = local
            targets.append(images.cli.upload_temp_url(str(local)))
            slots.append(pos)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(
                f"参考图未能上传校验(该图保留但未校验):{str(exc)[:120]}"
            )
    report["verify_uploaded"] = len(targets)
    return targets, slots, local_of


def _apply_verdicts(refs: list[images.ImageRef], text: str, slots: list[int],
                     report: dict[str, Any]) -> list[images.ImageRef]:
    """按校验结论剔除不合格的图。解析失败则**原样保留**并记录。

    `slots` 是实际提交给模型的那几张在 `refs` 里的下标 —— 没提交的图
    (落地/上传失败)不参与判定,直接保留。
    """
    verdicts = parse_verdicts(text)
    report["verify_text"] = text[:600]
    if not verdicts:
        report["errors"].append(
            "参考图校验返回无法解析的结构,已按未校验处理(全部保留);"
            f"原始返回:{text[:160]}"
        )
        report["verify_parsed"] = False
        report["verified_ok"] = len(refs)
        return refs

    report["verify_parsed"] = True
    by_index: dict[int, dict[str, Any]] = {}
    for v in verdicts:
        try:
            by_index[int(v.get("index", 0))] = v
        except (TypeError, ValueError):
            continue

    drop: set[int] = set()
    for n, pos in enumerate(slots, start=1):
        verdict = by_index.get(n)
        if not verdict:
            # 模型漏掉这张 → 保留,别因为少一条就把好图丢了
            continue
        ref = refs[pos]
        problem = str(verdict.get("problem") or "").strip().lower()
        if verdict.get("is_target_model") is False and problem not in _BLOCKING_PROBLEMS:
            problem = "not_target_model"
        if problem in _BLOCKING_PROBLEMS:
            report["verified_rejected"] += 1
            report["rejected_reasons"].append(f"{ref.author or ref.url[:40]}:{problem}")
            drop.add(pos)
        elif problem:
            report.setdefault("verify_notes", []).append(
                f"{ref.author or ref.url[:40]}:{problem}"
            )
    kept = [r for i, r in enumerate(refs) if i not in drop]
    report["verified_ok"] = len(kept)
    return kept


def _user_refs(project_dir: Path) -> list[images.ImageRef]:
    """读取用户手动放进 `user_refs/` 的参考图。

    手动图需要先转成可被生成接口访问的 URL,所以走一次上传;
    上传失败就跳过,不让它阻断整条流水线。
    """
    folder = project_dir / USER_REF_DIRNAME
    if not folder.is_dir():
        return []
    refs: list[images.ImageRef] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _USER_REF_SUFFIXES:
            continue
        try:
            url = images.cli.upload_temp_url(str(path))
        except Exception:  # noqa: BLE001
            continue
        refs.append(images.ImageRef(
            url=url, origin="user", note=f"用户上传:{path.name}",
            license="用户提供",
        ))
    return refs


def _angles_for(specs: Sequence[ShotSpec]) -> list[str]:
    """按镜头顺序统计需要的视角,保持出现频率高的优先。"""
    order: list[str] = []
    counts: dict[str, int] = {}
    for spec in specs:
        angle = spec.angle or "exterior_45"
        if angle not in counts:
            order.append(angle)
            counts[angle] = 0
        counts[angle] += 1
    return sorted(order, key=lambda a: counts[a], reverse=True)


def build_reference_set(
    *,
    subject: str,
    profile: CategoryProfile,
    specs: Sequence[ShotSpec],
    project_dir: Path,
    limit: int = 5,
    verify: bool = True,
) -> tuple[list[images.ImageRef], dict]:
    """构建并落库全片共享参考图集。返回 (参考图列表, 报告)。

    任一环节失败都不抛异常 —— 素材层必须降级而不是阻断流水线:
    检索失败 → AI 生图兜底 → 仍失败 → 空集合(生成阶段走纯文生视频)。
    """
    angles = _angles_for(specs)
    picked: list[images.ImageRef] = []
    report: dict = {
        "angles_requested": angles,
        "searched": 0,
        "verified_ok": 0,
        "verified_rejected": 0,
        "rejected_by_filter": 0,
        "rejected_reasons": [],
        "generated_fallback": 0,
        "limit": limit,
        "errors": [],
        "sources_tried": [],
    }

    # ---- 0. 用户手动上传的参考图优先级最高 ----
    # 人工挑过的照片是唯一能"保证是这台车"的来源,永远排在自动检索之前。
    for ref in _user_refs(project_dir):
        if len(picked) >= limit:
            break
        picked.append(ref)
    if picked:
        report["sources_tried"].append(f"user:{len(picked)}")

    # ---- 1. 按视角检索 ----
    # 收集时**多收一些**再截断到 limit:校验环节会剔掉一部分,如果边收边截断,
    # 前几条若恰好全是电商图或错车型图,真正的可用图永远轮不到,而报告还会
    # 显示"已取满 N 张"。
    collect_cap = max(limit * 2, limit + 5)
    for angle in angles:
        if len(picked) >= collect_cap:
            break
        query = profile.reference_query(subject, angle)
        try:
            found = images.search(
                [query],
                # 相关性描述与检索串必须分开给:检索串用中文命中率更高,
                # 而检索工具的 prompt 字段是拿来做**英文语义过滤**的。
                relevance_prompts={query: profile.reference_prompt(subject, angle)},
                task_name=profile.name,
                errors=report["errors"],
            )
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"检索失败({angle}):{exc}")
            continue
        report["searched"] += len(found)
        report["sources_tried"].append(f"search:{len(found)}")

        for ref in found:
            if len(picked) >= collect_cap:
                break
            # 硬过滤放在计数之前 —— 域名就能判定的图不值得占掉配额
            junk = _junk_reason(ref)
            if junk:
                report["rejected_by_filter"] += 1
                report["rejected_reasons"].append(f"{ref.url[:60]}:{junk}")
                continue
            if any(r.url == ref.url for r in picked):
                continue
            ref.angle = angle
            picked.append(ref)

    # ---- 1b. 维基共享资源 / 维基百科:真实照片,免密钥 ----
    # 放在 search 之后:search 的视角覆盖更贴合分镜,而且现在确实能用。
    # 但它命中得少、命名规整,作为第二梯队补位仍然值得。
    if len(picked) < collect_cap:
        try:
            refs, web_errors = webassets.collect(subject, limit=collect_cap)
            report["errors"].extend(web_errors)
            if refs:
                report["sources_tried"].append(f"wikimedia:{len(refs)}")
                for ref in refs:
                    if len(picked) >= collect_cap:
                        break
                    junk = _junk_reason(ref)
                    if junk:
                        report["rejected_by_filter"] += 1
                        report["rejected_reasons"].append(f"{ref.url[:60]}:{junk}")
                        continue
                    if any(r.url == ref.url for r in picked):
                        continue
                    ref.angle = angles[(len(picked)) % len(angles)] if angles else "exterior_45"
                    picked.append(ref)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"维基抓取失败:{exc}")

    # ---- 2. 校验 ----
    # ⚠️ 这里过去是反的:只在"全部是 AI 图"时才校验,理由是"维基/用户上传的
    # 都是真实照片,让模型去判断反而可能把对的否掉"。但真实来源一多,这个
    # 前提就不成立了 —— 检索会混进电商图和别的年款,和 AI 图要解决的是
    # 同一个问题。**来源多不等于可信度自动成立。**
    # 仍然豁免的只有 user:人工挑过的图不该再被模型翻一遍。
    verifiable = [i for i, r in enumerate(picked) if r.origin != "user"]
    local_of: dict[str, Path] = {}
    if verify and verifiable:
        try:
            targets, slots, local_of = _prepare_verify_targets(picked, project_dir, report)
            if targets:
                verdict = images.describe(targets, verify_prompt(subject))
                picked = _apply_verdicts(picked, verdict, slots, report)
            else:
                report["errors"].append("没有可提交的校验图(全部未能落地或上传),已按未校验处理")
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"校验失败:{exc}")

    # 校验可能把图剔没了,此时才允许 AI 生图兜底
    picked = picked[:limit]
    if report.get("rejected_reasons"):
        report["rejected_reasons"] = report["rejected_reasons"][:20]
    if report.get("verify_notes"):
        report["verify_notes"] = report["verify_notes"][:20]

    # ---- 3. AI 生图兜底 ----
    if not picked:
        # 兜底图是 AI **编**出来的,不是这台车的真实照片。用它当参考图,
        # 等于把一个虚构车型锁进全片外观 —— 所以必须明确记录,让人知道
        # 画面里的车并不保证是真的这台车。
        report["warning"] = (
            "联网检索与维基抓取都没拿到真实照片,已退化为 AI 生成参考图。"
            "该图是模型想象的车型,不代表真实车辆外观 —— "
            "可把真实照片放进项目的 user_refs/ 目录后重跑素材层来纠正。"
        )
        try:
            ref = images.generate(
                prompt=(
                    f"A clean product reference photograph of {subject}, "
                    "three-quarter front view, neutral studio lighting, "
                    "plain background, no text overlay"
                ),
                output_file="ref_fallback.png",
                aspect_ratio=profile.aspect_ratio,
            )
            if ref:
                ref.angle = angles[0] if angles else "exterior_45"
                ref.license = "AI 生成(非真实车辆)"
                picked.append(ref)
                report["generated_fallback"] = 1
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"AI 生图兜底失败:{exc}")

    # 参考图来源构成,让人一眼看出画面里的车可不可信
    report["origin_counts"] = {}
    for ref in picked:
        report["origin_counts"][ref.origin] = report["origin_counts"].get(ref.origin, 0) + 1
    real = sum(v for k, v in report["origin_counts"].items() if k != "generate")
    report["has_real_photo"] = real > 0

    # ---- 4. 落盘(仅供 Web 预览与抽帧,生成侧用的是远程 URL)----
    # 注意:下载失败**不阻断生成**。参考图是以 URL 形式交给 H3 的,
    # 由生成侧自己去取,不走本机 IP —— 所以 Wikimedia 限流本机也不影响
    # 成片。这里失败只意味着预览图看不到,必须如实记录而不是静默吞掉。
    saved: list[images.ImageRef] = []
    for i, ref in enumerate(picked[:limit]):
        dest = project_dir / "refs" / f"ref_{i:02d}.jpg"
        try:
            cached = local_of.get(ref.url)
            if cached is not None and cached.is_file():
                # 校验阶段已经下过了,直接复用,别把同一张图下两遍
                dest.write_bytes(cached.read_bytes())
            else:
                # Wikimedia 会 403 掉不带 User-Agent 的下载,这里必须带。
                images.download(ref.url, dest, headers={"User-Agent": webassets.USER_AGENT})
            saved.append(ref)
        except Exception as exc:  # noqa: BLE001
            report.setdefault("download_warnings", []).append(
                f"{ref.origin} 参考图未能下载到本地(仅影响预览,不影响生成):"
                f"{str(exc)[:120]}"
            )
            saved.append(ref)
    if report.get("download_warnings"):
        report["download_warnings"] = report["download_warnings"][:5]

    payload = [asdict(r) for r in saved]
    (project_dir / "refs.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 写到调用方给的目录,而不是反查项目 ID 再走 db.write_json —— 后者在
    # project_dir 不在 PROJECTS_DIR 下时(如测试的 tmp_path)会写进真实数据目录。
    (project_dir / "refs_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return saved, report


def references_for_angle(refs: Sequence[images.ImageRef], angle: str,
                        limit: int) -> list[str]:
    """挑出与该镜头视角最相关的参考图 URL,供生成阶段作为 reference_images。

    策略:同视角的排前面,然后按整体顺序补齐到 limit。
    H3 前 5 张参考图免费,因此 limit 不应超过 5。
    """
    ordered = sorted(
        range(len(refs)),
        key=lambda i: (0 if refs[i].angle == angle else 1, i),
    )
    return [refs[i].url for i in ordered[:limit]]

