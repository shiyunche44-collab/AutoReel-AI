"""生产旋钮:剪辑 / 素材 / 配音 / 封面上「能改、也能学」的那几个开关。

开头 / 结尾写法(`variety`)、视觉包(`looks`)、标题写法(`packaging`)早就是变体了;
这里补的是**画面和声音**那一侧 —— 以前全是模块常量,复盘官看得到效果却没东西可调:

==============  ===========================  ==========================================
旋钮             取值                          生效点
==============  ===========================  ==========================================
pace            fast / mid / slow            背景素材一刀最长几秒(`library.fill`)
hook_visual     hook / highlight / broll     第一拍版式(分镜提示词里只给这一种)
concept_level   low / mid / high             全片概念动画下限(`explainer.concept_min`)
speed           calm / mid / brisk           TTS 语速;脚本字数预算按同一比例缩放
voice           profile `voices:` 里的音色    TTS 音色(品类没配候选就没有这个旋钮)
cover_tone      封面 4 种底色                 统一风格封面 A 版底色(`cover_art.tone_of`)
==============  ===========================  ==========================================

**默认值 = 旋钮出现之前的常量**,老项目、测试和 golden 里画出来、说出来逐字不变。
建项目时 `pick` 挑一组写进 `projects.variant`:

- 服务里(`team.playbook.install` 挂上了手册)→ 按手册权重挑 + 30% 探索,配对实验点名了就照做;
- 没挂手册(测试、脚本)→ 全取默认值,一次随机数都不多抽。

渲染时用 `use(variant)` 设当前项目的旋钮(contextvar,和 `looks.use` 一样),
各生效点读 `value("pace")`,不必把变体一层层传下去。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping


@dataclass(frozen=True)
class Knob:
    id: str
    label: str
    #: 可选取值(第一个不一定是默认)
    values: tuple[str, ...]
    default: str
    #: 取值的中文名
    names: Mapping[str, str] = field(default_factory=dict)

    def name(self, value: str) -> str:
        return self.names.get(value, value)


#: 背景素材一刀最长几秒。mid = 旧常量 `library.FILL_MAX_PIECE`
PIECE_SECONDS = {"fast": 3.0, "mid": 4.5, "slow": 6.0}
#: 概念动画下限占拍数的比例。mid = 旧常量 0.13
CONCEPT_SHARE = {"low": 0.08, "mid": 0.13, "high": 0.2}
#: 语速倍率,相对 config.TTS_SPEED(mid = 1.0 = 旧行为)
SPEED_FACTOR = {"calm": 0.94, "mid": 1.0, "brisk": 1.07}

KNOBS: dict[str, Knob] = {k.id: k for k in (
    Knob("pace", "剪辑节奏", ("fast", "mid", "slow"), "mid",
         {"fast": "快切(3 秒一刀)", "mid": "常规(4.5 秒)", "slow": "慢镜(6 秒)"}),
    Knob("hook_visual", "开场画面", ("hook", "highlight", "broll"), "",
         {"hook": "大字钩子", "highlight": "关键词高亮", "broll": "纯实拍"}),
    Knob("concept_level", "概念动画量", ("low", "mid", "high"), "mid",
         {"low": "少", "mid": "中", "high": "多"}),
    Knob("speed", "语速", ("calm", "mid", "brisk"), "mid",
         {"calm": "稍慢", "mid": "常规", "brisk": "稍快"}),
    Knob("cover_tone", "封面底色", ("beige", "sage", "mist", "silver"), "",
         {"beige": "米色", "sage": "灰绿", "mist": "雾蓝", "silver": "银灰"}),
)}
#: 品类声明了 `voices:` 才有的旋钮(取值来自 profile)
VOICE = "voice"

_current: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "knobs_variant", default={})


def label(knob: str) -> str:
    if knob == VOICE:
        return "配音音色"
    return KNOBS[knob].label if knob in KNOBS else knob


def value_name(knob: str, value: str) -> str:
    if knob in KNOBS:
        return KNOBS[knob].name(value)
    return value


@contextlib.contextmanager
def use(variant: Mapping[str, Any] | None) -> Iterator[None]:
    token = _current.set(dict(variant or {}))
    try:
        yield
    finally:
        _current.reset(token)


def value(knob: str) -> str:
    """当前项目这个旋钮的取值;没设 / 不认识 → 默认值(空串 = 「不干预」)。"""
    raw = str(_current.get().get(knob) or "")
    spec = KNOBS.get(knob)
    if spec is None:
        return raw
    return raw if raw in spec.values else spec.default


def piece_seconds() -> float:
    return PIECE_SECONDS.get(value("pace"), PIECE_SECONDS["mid"])


def concept_share() -> float:
    return CONCEPT_SHARE.get(value("concept_level"), CONCEPT_SHARE["mid"])


def speed_factor() -> float:
    return SPEED_FACTOR.get(value("speed"), 1.0)


def voice() -> str:
    """当前项目点名的音色;空 = 用品类默认。"""
    return str(_current.get().get(VOICE) or "")


# --------------------------------------------------------------------------
# 建项目时挑
# --------------------------------------------------------------------------
def _rng(project_id: str) -> random.Random:
    digest = hashlib.sha256(f"knobs:{project_id}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def choose(rng: random.Random, allowed: tuple[str, ...], used: Iterable[str],
           weights: Mapping[str, float] | None) -> str:
    """有手册权重时 70% **在全部取值里**按权重挑(旋钮只有三四个取值,只在「最近没用过的」
    里挑的话永远是轮换,学到的东西用不上);其余 / 没权重 → 照 `variety._choose` 轮换。"""
    from . import variety

    if weights and rng.random() >= variety.EXPLORE:
        return rng.choices(allowed, [max(float(weights.get(x, 1.0)), 0.01) for x in allowed])[0]
    return variety._choose(rng, allowed, used)


def universe(profile: Any = None) -> dict[str, tuple[str, ...]]:
    """每个旋钮能取的值。音色只在品类配了两个以上候选时才算旋钮。"""
    out = {k: spec.values for k, spec in KNOBS.items()}
    voices = tuple(getattr(profile, "voices", ()) or ())
    if len(voices) >= 2:
        out[VOICE] = voices
    return out


def pick(project_id: str, recent: Iterable[Mapping[str, Any]] = (), profile: Any = None,
         chosen: Mapping[str, str] | None = None) -> dict[str, str]:
    """给新项目挑一组旋钮。没挂手册 → 空字典(全部默认,变体和以前逐字一样)。

    `chosen`:用户 / 调用方点名的取值,优先。"""
    from . import variety

    chosen = {k: v for k, v in (chosen or {}).items() if v}
    if variety.learned is None and variety.plan is None:
        return dict(chosen)
    rng = _rng(project_id)
    recent = [variety.parse(v) for v in recent][:variety.RECENT_WINDOW]
    out: dict[str, str] = {}
    for knob, allowed in universe(profile).items():
        if knob in chosen and chosen[knob] in allowed:
            out[knob] = chosen[knob]
            continue
        out[knob] = variety._planned(knob, allowed) or choose(
            rng, allowed, [str(v.get(knob) or "") for v in recent], variety._weights(knob))
    return out


def describe(variant: Mapping[str, Any] | None) -> str:
    """项目页一句话:「剪辑节奏:快切 · 开场画面:大字钩子 …」(只列设了的)。"""
    v = variant or {}
    bits = [f"{label(k)}:{value_name(k, str(v[k]))}" for k in (*KNOBS, VOICE) if v.get(k)]
    return " · ".join(bits)
