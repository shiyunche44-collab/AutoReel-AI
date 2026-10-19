"""④ 素材解析:构建**全片共享**的参考图集(计划 §4.4 第一层)。

为什么是"全片共享"而不是"每镜头各找各的":
10-30 个镜头若各用各的图,同一辆车在不同镜头里颜色、轮毂、内饰必然漂移。
共享一套图(3-5 张,覆盖不同视角)再按镜头视角排序,能让 H3 在全片维持同一辆车。

这里的参考图用来给 AI 生成的镜头锁定外观;能直接用真实素材的镜头由
`clip_resolver.py` 逐镜头取材,两条通路互不依赖。
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
    """构造**单张**参考图的校验提问。

    必须把**目标车型**写进提示词。过去这一层只问"是不是汽车产品照片",
    从没告诉模型目标是谁 —— 于是它只能答"这是一台轿车",校验通过,但
    车画出来照样不是这台。这正是"文案讲汉、画面不是汉"的另一条来路。

    ⚠️ 实测两件事,决定了现在的形状:

    1. 视觉问答工具**恒返回散文描述**,不理会"只输出 JSON"的要求;
    2. 一次塞 10 张图,它**只描述第一张**(实测 p_d1851382e779:10 张进,
       一段讲海豹 07 的散文出)—— 批量校验其余 9 张从来没被看过。

    所以改成每张图问一次,并且提示词里点名要它说出**品牌、车型和依据**
    (车标、车尾字样、车牌位文字)。哪怕它照旧回散文,散文里也有足够的
    信息交给 LLM 转成结构化判定(见 `_structure_with_llm`)。工具哪天开始
    认 JSON,`_parse_one` 会直接吃下,不用改代码。
    """
    return (
        f"判定这张图片能否作为「{subject}」的参考图。只依据图片内容回答,不要猜。\n"
        "请务必说明:\n"
        "1. 画面里车的品牌和车型,以及你的依据(车标、车尾字样、车牌位文字);"
        "看不出来就直说看不出来;\n"
        "2. 拍摄视角:外观正侧 / 车尾 / 内饰 / 局部细节 / 其他;\n"
        "3. 是否是电商白底图、画面里有没有价格文字、大段文字或水印、是否模糊过暗。\n"
        "如果可以,只输出一个 JSON 对象,字段:\n"
        f'  "is_target_model": true / false / "unknown" —— 是不是「{subject}」\n'
        '  "view": exterior / interior / rear / detail / other\n'
        '  "problem": 空字符串表示可用;否则填 not_target_model / e-commerce / '
        "text_overlay / low_quality / unknown 之一"
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


#: 每张图单独问一次后,并发数。视觉问答走 CLI 子进程,开太多会被限流
_VERIFY_WORKERS = 4

STRUCTURE_SYSTEM = """你把视觉模型对若干张图片的描述转成参考图校验结论。目标车型是「{subject}」。

只依据描述里**明确写出**的内容判定,保守,不要推理补全:
- is_target_model:描述明确认出的就是目标车型(同品牌同车系;年款、动力版本不同仍算是)→ true;
  描述明确认出是**别的品牌或别的车系** → false;描述没点名车型、只说"一台白色 SUV"之类 → "unknown"。
- view:exterior / interior / rear / detail / other。
- problem:描述明确说是电商白底图或画面有价格文字 → "e-commerce";
  is_target_model 为 false → "not_target_model";有大段文字或水印 → "text_overlay";
  明确说模糊、过暗、截图 → "low_quality";都没有 → ""。
- 描述是报错、拒答或为空 → is_target_model 填 "unknown",problem 填 ""。

输出 JSON 对象 {{"verdicts": [...]}},每张图一项,字段 index(与输入编号一致)、
is_target_model、view、problem、reason(一句中文依据,引用描述原话)。"""


def _parse_one(text: str) -> dict[str, Any] | None:
    """从单张图的回答里抠出判定对象。数组取第一项,对象直接用;抠不出返回 None。"""
    found = parse_verdicts(text)
    if found:
        return found[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) and "problem" in data else None


def _describe_each(targets: list[str], subject: str,
                   report: dict[str, Any]) -> list[str]:
    """每张图单独问一次视觉模型。单张失败只记一条,返回空串(= 该图未校验)。"""
    from concurrent.futures import ThreadPoolExecutor

    prompt = verify_prompt(subject)

    def _one(url: str) -> str:
        try:
            return images.describe([url], prompt)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"参考图校验调用失败(该图保留):{str(exc)[:120]}")
            return ""

    with ThreadPoolExecutor(max_workers=min(_VERIFY_WORKERS, len(targets))) as pool:
        return list(pool.map(_one, targets))


def _structure_with_llm(texts: dict[int, str], subject: str, llm: Any,
                        report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """把散文描述一次性交给 LLM 转成判定。`texts` 的键是 1 起的图片编号。

    一次调用转完所有图,而不是每张一次:散文已经是文本,LLM 不需要看图,
    合在一起问更便宜也更快。失败时返回空 —— 等于这些图未校验,全部保留。
    """
    if not texts or llm is None:
        return {}
    user = "\n\n".join(f"### 图 {n}\n{t[:1500]}" for n, t in sorted(texts.items()))
    try:
        data = llm.structured(STRUCTURE_SYSTEM.format(subject=subject), user,
                              required_keys=["verdicts"], temperature=0.0)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(f"校验描述结构化失败,这些图按未校验保留:{str(exc)[:120]}")
        return {}
    out: dict[int, dict[str, Any]] = {}
    for v in data.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        try:
            n = int(v.get("index", 0))
        except (TypeError, ValueError):
            continue
        if n in texts:
            out[n] = v
    return out


def _collect_verdicts(texts: list[str], subject: str, llm: Any,
                      report: dict[str, Any]) -> list[dict[str, Any] | None]:
    """逐张得出判定:模型直接给了 JSON 就用,否则交给 LLM 转;都不成则 None。"""
    verdicts: list[dict[str, Any] | None] = [_parse_one(t) if t else None for t in texts]
    prose = {n: t for n, (t, v) in enumerate(zip(texts, verdicts), start=1)
             if t and v is None}
    structured = _structure_with_llm(prose, subject, llm, report)
    for n, v in structured.items():
        verdicts[n - 1] = v
    report["verify_sources"] = {
        "json": sum(1 for t in texts if t and _parse_one(t) is not None),
        "llm": len(structured),
        "none": sum(1 for v in verdicts if v is None),
    }
    return verdicts


def _apply_verdicts(refs: list[images.ImageRef], verdicts: list[dict[str, Any] | None],
                    slots: list[int], report: dict[str, Any]) -> list[images.ImageRef]:
    """按逐张校验结论剔除不合格的图。没有结论的图**原样保留**。

    `slots[i]` 是第 i 张提交图在 `refs` 里的下标 —— 没提交的图
    (落地/上传失败)不参与判定,直接保留。
    """
    if not any(verdicts):
        report["errors"].append("参考图校验没有得出任何可用结论,已按未校验处理(全部保留)")
        report["verify_parsed"] = False
        report["verified_ok"] = len(refs)
        return refs

    report["verify_parsed"] = True
    drop: set[int] = set()
    for verdict, pos in zip(verdicts, slots):
        if not verdict:
            # 这张没得出结论 → 保留,别因为少一条就把好图丢了
            continue
        ref = refs[pos]
        label = ref.author or ref.url[:40]
        reason = str(verdict.get("reason") or "").strip()
        problem = str(verdict.get("problem") or "").strip().lower()
        if verdict.get("is_target_model") is False and problem not in _BLOCKING_PROBLEMS:
            problem = "not_target_model"
        if problem in _BLOCKING_PROBLEMS:
            report["verified_rejected"] += 1
            report["rejected_reasons"].append(
                f"{label}:{problem}" + (f"({reason[:60]})" if reason else ""))
            drop.add(pos)
        elif problem:
            report.setdefault("verify_notes", []).append(f"{label}:{problem}")
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
    llm: Any = None,
) -> tuple[list[images.ImageRef], dict]:
    """构建并落库全片共享参考图集。返回 (参考图列表, 报告)。

    任一环节失败都不抛异常 —— 素材层必须降级而不是阻断流水线:
    检索失败 → AI 生图兜底 → 仍失败 → 空集合(生成阶段走纯文生视频)。

    `llm` 用来把视觉模型的散文描述转成结构化判定;不给则只认模型直接
    返回的 JSON,其余图按未校验保留。
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
                texts = _describe_each(targets, subject, report)
                report["verify_texts"] = [t[:300] for t in texts]
                verdicts = _collect_verdicts(texts, subject, llm, report)
                picked = _apply_verdicts(picked, verdicts, slots, report)
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

