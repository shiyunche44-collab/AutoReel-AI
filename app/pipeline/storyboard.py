"""③ 分镜设计:脚本段落 → 镜头表。

一个镜头 = 一段旁白 = 一段固定时长的画面。镜头数由 6 秒栅格唯一确定,
因此这里只需要决定**每个镜头拍什么**,不涉及时间分配。

画面描述由 LLM 依据"段落定位 + 该段落节拍方向 + 这段旁白在说什么"生成,
而运镜、参考图视角、合规要求来自品类 Profile —— 结构由配置保证,
内容由模型填充,与提示词编译器采用同一套分工哲学。
"""
from __future__ import annotations

from typing import Sequence

from .. import config
from ..categories import CategoryProfile
from ..models import Fact, ScriptSegment, ShotSpec
from . import cards

SYSTEM = """你是中文短视频的分镜导演,为每个镜头写一段**画面描述**。

硬性要求:
1. 只输出一个 JSON 对象,不要 Markdown 围栏或解释文字。
2. `shots` 数组,每项: {{"idx": 序号, "visual": "画面描述", "on_screen_text": ["可见文字"]}}
   - 数量必须与给定镜头数一致,idx 从 1 开始连续。
   - visual:40-80 字,写清楚 主体 / 动作 / 场景 / 光线 / 景别。
     只写**看得见**的东西,不要写旁白内容、不要写镜头运动(运镜由配置给定)。
   - on_screen_text:画面中真实出现的文字(仪表读数、车型标识等)。
     **不确定具体文案时用占位符,如 "[续航读数]"、""[车型标识]""**,
     严禁臆造品牌名、型号或广告语。没有则给空数组。
3. 必须与该镜头对应的旁白内容一致 —— 画面要能支撑这句话。
4. 逐条遵守镜头清单里的「节拍方向」;若标注了「画面硬要求」,那是**不可协商**的,
   必须在画面里出现,即使旁白只提到了其中一台车。
5. 竖屏 9:16 构图,主体尽量置于画面中部。
6. 标注了「数据卡片」的镜头,画面由程序渲染成图表,你要额外给出 `card` 对象:
   {{"title": "卡片标题(12 字内)", "columns": [对比对象名,仅对比卡需要],
     "rows": [{{"label": "...", "values": ["..."], "fact_id": "f_xxx"}}]}}
   - 每一行**只能**取自下面「可用事实」里的某一条,fact_id 填那条事实的 id;
     values 里的数字必须与事实原文一字不差(含单位),不得换算、取整或估算。
   - 优先挑与该镜头旁白相关的数据;同一视频里不同卡片不要重复同一行。
   - 找不到合适事实时 rows 给空数组,不要编。
   - 这类镜头的 visual 仍要写,作为卡片作废时的备用画面。

必需键: shots"""

#: 有素材池时追加到 SYSTEM 末尾(不过 format,花括号原样)
POOL_RULES = """

## 实拍素材池
下面「可用实拍素材」列出了这台车**已经有的真实画面**(编号 / 视角 / 描述)。
真实画面比 AI 生成可信得多,所以:
- 先看旁白在讲什么,池子里有画面能撑住这句话的(讲外观给外观、讲内饰给内饰、
  讲驾驶给行驶画面、讲配置给对应细节或座舱),就在该镜头给 `"pool": "素材编号"`,
  visual 照抄那条素材的描述即可(程序以素材描述为准);
- 不要求逐字对上:旁白讲"续航 800 公里",给一段行驶画面就够了;
- 每条素材全片只用一次;有「字幕或主持人」标记的,只在没有干净素材可选时才用;
- 池子撑不住的才自己写 visual 交给 AI 生成,`pool` 给空字符串:比如画面硬要求
  两车同框、要特定人物、要池子里没有的部件;
- 数据卡片镜头不选素材。
每项输出格式变为 {"idx", "visual", "on_screen_text", "pool"}。"""

USER = """Subject: {subject}
Category: {category}
Shot duration: {duration} seconds each
Total shots: {count}
Aspect ratio: 9:16 (vertical)

镜头清单(逐条对应,visual 要呼应 narration):
{shots}

可用事实(数据卡片只能取自这里;普通镜头仅在画面确需展示数据时可作为 on_screen_text 的依据):
{facts}

输出 JSON。"""


def build(
    *,
    subject: str,
    profile: CategoryProfile,
    segments: Sequence[ScriptSegment],
    facts: Sequence[Fact],
    duration: int,
    llm,
    card_log: list[dict] | None = None,
    pool: Sequence[dict] | None = None,
) -> list[ShotSpec]:
    """生成分镜表。

    `card_log` 非空时收集每个卡片镜头的核对结果(哪些行因数字无据被丢掉),
    供门禁 A 复核。

    `pool` 是素材池清单(`library.catalog()`)。给了就让模型先看池子里有什么、
    能用实拍的镜头直接点名素材 —— 反过来"先写画面再去找",写出来的往往是
    "车门夹层玻璃特写""两车并排"这种池子里压根没有的画面。
    """
    shot_count = config.shots_for_duration(duration)
    grid = config.SHOT_GRID_SECONDS

    plan = [s.section for s in segments] or ["" for _ in range(shot_count)]
    while len(plan) < shot_count:
        plan.append(plan[-1] if plan else "")

    shot_lines = []
    for i in range(shot_count):
        seg = segments[i] if i < len(segments) else None
        sec = profile.section(plan[i] if i < len(plan) else "")
        narration = seg.text if seg else ""
        # multi_subject 段落的节拍(两代并排 / 新旧款对照)必须落到画面里,
        # 所以要显式写成硬要求 —— 只放在"节拍方向"里会被旁白压过去。
        # 实测:旁白讲"底盘是重头戏",模型就画底盘特写,两台车全没了。
        hard = (
            " | 画面硬要求:必须出现第二台车(不同年款或不同配置),两台同框"
            if sec.multi_subject else ""
        )
        card = (
            f" | 数据卡片:{cards.KIND_GUIDE[sec.card]}" if sec.card in cards.KIND_GUIDE else ""
        )
        shot_lines.append(
            f"[镜头 {i + 1}] 段落:{sec.name or '未分类'} | "
            f"节拍方向:{sec.beat or '无'}{hard}{card} | "
            f"旁白:{narration}"
        )

    # 卡片要能取到中置信度的参数(新闻源里大部分产品参数是 medium);
    # 低置信度(超期旧参数)一律不给,ground() 也会再拦一次。
    has_cards = any(profile.section(p).card for p in plan[:shot_count])
    usable = [f for f in facts if f.enabled and
              (f.confidence == "high" or (has_cards and f.confidence == "medium"))]
    facts_payload = "\n".join(
        f"- {f.id}: {f.claim}" for f in usable
    )[:4000 if has_cards else 2000] or "(无)"

    pool_by_id = {str(p["id"]): p for p in pool or [] if p.get("id")}
    system = SYSTEM.format(category=profile.label)
    user = USER.format(
        subject=subject,
        category=profile.label,
        duration=grid,
        count=shot_count,
        shots="\n".join(shot_lines),
        facts=facts_payload,
    )
    if pool_by_id:
        system += POOL_RULES
        user += "\n\n可用实拍素材:\n" + "\n".join(_pool_line(p) for p in pool_by_id.values())

    data = llm.structured(
        system,
        user,
        required_keys=["shots"],
        temperature=0.65,
    )

    raw = data.get("shots") or []
    specs: list[ShotSpec] = []
    used_pool: set[str] = set()
    for i in range(shot_count):
        item = raw[i] if i < len(raw) and isinstance(raw[i], dict) else {}
        sec = profile.section(plan[i] if i < len(plan) else "")
        seg = segments[i] if i < len(segments) else None

        visible = item.get("on_screen_text") or []
        if isinstance(visible, str):
            visible = [visible]
        # 占位符是"内容未知"的诚实标记,保留;但要去掉明显的臆造风险:
        # 若模型给出了具体品牌/型号而事实层没有,这里不做判断,交由 QA 与人工门禁把关。
        on_screen = [str(v).strip() for v in visible if str(v).strip()]

        card, dropped = (None, [])
        if sec.card:
            card, dropped = cards.ground(cards.normalise(item.get("card"), sec.card), facts)
        if card_log is not None and sec.card:
            card_log.append({"idx": i, "section": sec.name, "kind": sec.card,
                             "rendered": card is not None, "dropped": dropped})

        # 点名的素材必须真在清单里、没被前面的镜头用过;卡片镜头不取材
        pool_id = str(item.get("pool") or "").strip()
        if card or pool_id not in pool_by_id or pool_id in used_pool:
            pool_id = ""
        if pool_id:
            used_pool.add(pool_id)
        angle = (pool_by_id[pool_id].get("angle") if pool_id else "") or sec.angle
        visual = str(item.get("visual", "")).strip()
        # 点了素材,画面描述就是素材本身的描述:模型照着改写时总会添油加醋
        # (静态车灯特写写成"从画面深处驶来"、雪山写成"黄昏余晖"),
        # 门禁 A 审的是这段文字,必须是那段实拍里真有的东西
        if pool_id and pool_by_id[pool_id].get("desc"):
            visual = str(pool_by_id[pool_id]["desc"]).strip()

        specs.append(
            ShotSpec(
                idx=i,
                duration=float(grid),
                section=sec.name,
                visual_zh=visual
                or (sec.beat or f"{subject} 的第 {i + 1} 个展示镜头"),
                camera_zh=sec.camera or "Static Shot",
                ambience_zh=sec.beat or "",
                narration_zh=seg.text if seg else "",
                angle=angle,
                on_screen_text=[] if card else on_screen,
                card=card,
                pool_id=pool_id,
            )
        )
    return specs


def _pool_line(p: dict) -> str:
    tags = [p.get("view") or "-", "官方" if p.get("official") else "",
            "图片" if p.get("kind") == "still" else "视频",
            {1: "有台标", 2: "有字幕或主持人"}.get(int(p.get("dirt") or 0), "")]
    return f"- {p['id']} [{' / '.join(t for t in tags if t)}] {p.get('desc') or '(无描述)'}"


def fallback_specs(
    *,
    subject: str,
    profile: CategoryProfile,
    segments: Sequence[ScriptSegment],
    duration: int,
) -> list[ShotSpec]:
    """LLM 不可用时的确定性兜底:直接用 Profile 的节拍方向填表。

    画面质量会明显下降,但结构、时间轴与后续生成链路依然完整,
    便于在没配 Key 的情况下先验证渲染与合成环节。
    """
    shot_count = config.shots_for_duration(duration)
    grid = config.SHOT_GRID_SECONDS
    specs: list[ShotSpec] = []
    for i in range(shot_count):
        seg = segments[i] if i < len(segments) else None
        sec = profile.section(seg.section if seg else "")
        specs.append(
            ShotSpec(
                idx=i,
                duration=float(grid),
                section=sec.name,
                visual_zh=f"{subject}:{sec.beat or '产品展示'}",
                camera_zh=sec.camera or "Static Shot",
                ambience_zh="",
                narration_zh=seg.text if seg else "",
                angle=sec.angle,
                on_screen_text=[],
            )
        )
    return specs


def validate_specs(specs: Sequence[ShotSpec], duration: int) -> list[str]:
    """分镜表自检:数量与时长必须与栅格一致。"""
    errors: list[str] = []
    expected = config.shots_for_duration(duration)
    if len(specs) != expected:
        errors.append(f"镜头数应为 {expected},实际 {len(specs)}")
    for spec in specs:
        if spec.duration <= 0:
            errors.append(f"镜头 {spec.idx} 时长非法:{spec.duration}")
        if not spec.visual_zh.strip():
            errors.append(f"镜头 {spec.idx} 缺少画面描述")
    return errors
