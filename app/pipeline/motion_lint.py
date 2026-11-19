"""概念动画片段的硬校验:Claude 为某一拍现写的 HyperFrames 片段,渲染前先过这里。

片段格式同 `motion/scenes/*.html`:说明与 meta、`<style>`、`<script>` 三块,由
`motion.compose_html` 套进 `motion/_shell.html`。规则:

- **只许 ASCII**:HyperFrames 按页面上出现过的字符裁内嵌字体子集,连注释里的中文也算;
  中文一律经 `window.__DATA__` 注入(CLAUDE.md 的 motion 规则);
- **不许联网**:http/https 链接、`//cdn...` 协议相对地址一律报错(SVG 命名空间 w3.org 除外);
- **只能引用 shell 提供的文件**:gsap、`_lib.js`、`_viz.js`、three.js、工作目录里的底 / 车图,
  以及 `motion/registry/compositions/` 里现有的组件;
- **时间轴不能明显超过拍长**:静态估算 `tl.to/from/fromTo/set(..., 位置)` 的 位置 + duration
  和 `data-duration`,只认数字字面量和 `S`(拍长)组成的算式,算不出来的不猜;
- **画面数字必须对上 Fact**:`__DATA__` 里出现的每个数字(字符串里的、数值型字段)都要
  在某条启用的事实原文里出现过,比对复用 `grounding.numbers_in`。版式用的数值字段
  (`at`、`ratio`、`share`、`seconds`……)不算上屏数字,不查。

分级沿用 `script_gen` 的 lint:error 必须改完才能渲染,warning 提示人看一眼。
"""
from __future__ import annotations

import ast
import operator
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .. import config
from ..models import Fact
from .grounding import YEAR_RE, numbers_in


@dataclass(frozen=True)
class Issue:
    """一条校验结果。`where` 是出问题的位置(行号、数据路径),给人定位用。"""
    severity: str
    message: str
    where: str = ""

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "message": self.message, "where": self.where}


def errors(issues: Sequence[Issue]) -> list[Issue]:
    return [i for i in issues if i.is_error]


# --------------------------------------------------------------------------
# 引用的文件
# --------------------------------------------------------------------------
#: shell 和渲染工作目录提供的文件(`motion._prepare` 拷进去的)
ALLOWED_FILES = frozenset({"gsap.min.js", "_lib.js", "_viz.js", "three.min.js",
                           "bg.mp4", "car.png", "car_edges.png", "part.png"})
#: 组件库目录(片段里写 `compositions/<名>.html` 或 `components/<名>.html`)
REGISTRY_DIR = "registry/compositions"

#: 属性 / CSS 里的引用:src、href、data-composition-src、url(...)、@import、ES import
_REF_RES = (
    re.compile(r"""\b(?:src|href|data-composition-src|data-composition-file|poster)\s*=\s*["']([^"']*)["']""",
               re.I),
    re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""", re.I),
    re.compile(r"""@import\s+["']([^"']+)["']""", re.I),
    re.compile(r"""\bimport\s*(?:[\w{}\s,*]+\s+from\s*)?["']([^"']+)["']"""),
    re.compile(r"""\bimport\(\s*["']([^"']+)["']\s*\)"""),
)
#: 绝对链接;SVG / XHTML 命名空间(createElementNS、xmlns)不是网络请求
_URL_RE = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>)]*", re.I)
_NAMESPACE_RE = re.compile(r"^https?://www\.w3\.org/", re.I)
#: 协议相对地址:引号或括号后紧跟 //域名
_PROTO_REL_RE = re.compile(r"""["'(]\s*(//[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+[^\s"')]*)""")
#: 运行时拉资源的 API:片段在离线工作目录里跑,这些只会失败或卡住
_FETCH_RE = re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts)\s*\(|"
                       r"\bnew\s+(XMLHttpRequest|WebSocket|EventSource|Worker)\b")


def _line_of(text: str, pos: int) -> str:
    return f"第 {text.count(chr(10), 0, pos) + 1} 行"


def registry_files() -> set[str]:
    """组件库里能引用的组件(相对 `motion/registry/compositions/` 的路径)。"""
    root = config.MOTION_DIR / REGISTRY_DIR
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def references(fragment: str) -> Iterator[tuple[int, str]]:
    """片段里引用的文件 / 地址:(位置, 原文)。data: / blob: / 页内锚点 / SVG 引用(#id)不算。"""
    for rx in _REF_RES:
        for m in rx.finditer(fragment):
            ref = m.group(1).strip()
            if not ref or ref.startswith(("#", "data:", "blob:", "about:")):
                continue
            yield m.start(1), ref


def _ref_issue(ref: str, where: str, registry: set[str]) -> Issue | None:
    if _URL_RE.match(ref) or ref.startswith("//"):
        return None  # 外链由 _network_issues 统一报
    path = ref.split("?", 1)[0].split("#", 1)[0].lstrip("./")
    if path in ALLOWED_FILES:
        return None
    for prefix in ("compositions/", "registry/compositions/", "motion/registry/compositions/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if path in registry:
        return None
    return Issue("error", f"引用了 shell 不提供的文件「{ref}」:只能用 gsap / _lib.js / _viz.js / "
                          f"three.min.js 和 motion/{REGISTRY_DIR}/ 里的组件", where)


def _ascii_issues(fragment: str) -> list[Issue]:
    bad = [(m.start(), m.group(0)) for m in re.finditer(r"[^\x00-\x7f]+", fragment)]
    if not bad:
        return []
    lines = sorted({fragment.count("\n", 0, pos) + 1 for pos, _ in bad})
    sample = "、".join(f"「{s[:8]}」" for _, s in bad[:3])
    return [Issue("error", f"片段里有非 ASCII 字符 {sample}(共 {len(bad)} 处):中文只能经 "
                           "window.__DATA__ 注入,注释里也不许写(会改内嵌字体子集)",
                  "第 " + "、".join(str(n) for n in lines[:6]) + " 行")]


def _network_issues(fragment: str) -> list[Issue]:
    out: list[Issue] = []
    for m in _URL_RE.finditer(fragment):
        if _NAMESPACE_RE.match(m.group(0)):
            continue
        out.append(Issue("error", f"引用了外部网络资源「{m.group(0)[:60]}」:渲染离线进行,"
                                  "外链要么失败要么卡住", _line_of(fragment, m.start())))
    for m in _PROTO_REL_RE.finditer(fragment):
        out.append(Issue("error", f"引用了外部网络资源「{m.group(1)[:60]}」(协议相对地址)",
                         _line_of(fragment, m.start(1))))
    for m in _FETCH_RE.finditer(fragment):
        out.append(Issue("error", f"片段在运行时拉资源({m.group(1) or m.group(2)}):数据只能经 "
                                  "window.__DATA__ 注入", _line_of(fragment, m.start())))
    return out


def _structure_issues(fragment: str) -> list[Issue]:
    missing = [t for t in ("<style>", "</style>", "<script>", "</script>") if t not in fragment]
    if missing:
        return [Issue("error", f"不是合法片段:缺少 {' '.join(missing)}(片段要有 <style> 和 <script>)")]
    out: list[Issue] = []
    if "HF.boot(" not in fragment and "gsap.timeline(" not in fragment:
        out.append(Issue("warning", "片段没有调用 HF.boot() 也没建 gsap 时间轴,画面可能是静止的"))
    if "HF.register(" not in fragment and "__timelines" not in fragment:
        out.append(Issue("warning", "片段没有 HF.register(tl):HyperFrames 找不到时间轴,动画不会动"))
    if re.search(r"<\s*(html|head|body)\b", fragment, re.I):
        out.append(Issue("error", "片段里不要写 html / head / body:外壳由 motion/_shell.html 提供"))
    return out


# --------------------------------------------------------------------------
# 时间轴长度(静态估算)
# --------------------------------------------------------------------------
#: 时间轴上的 tween(`gsap.to(...)` 是独立 tween,不进时间轴,不算)
_TWEEN_RE = re.compile(r"\b(?!gsap\.)\w+\.(to|from|fromTo|set)\s*\(")
_DUR_RE = re.compile(r"\bduration\s*:\s*([^,}\n]+)")
_ATTR_DUR_RE = re.compile(r"""data-duration\s*=\s*["']([\d.]+)["']""")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval(expr: str, seconds: float) -> float | None:
    """只算数字字面量和 S(拍长)组成的四则算式;别的(变量、函数调用、"+=0.3")返回 None。"""
    expr = expr.strip()
    if not expr or not re.fullmatch(r"[\d.\sS+\-*/()]+", expr):
        return None
    try:
        tree = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return None

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "S":
            return seconds
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -ev(node.operand)
        raise ValueError(expr)

    try:
        return ev(tree)
    except (ValueError, ZeroDivisionError):
        return None


def _call_args(text: str, start: int) -> list[str]:
    """从左括号后开始,按顶层逗号切出一次调用的参数(跳过字符串和嵌套括号)。"""
    args, depth, buf, quote = [], 0, [], ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            if depth == 0:
                args.append("".join(buf))
                return args
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    return args


def timeline_end(fragment: str, seconds: float) -> float:
    """静态估算的时间轴终点(秒):各 tween 的 位置 + duration 取最大,再看 data-duration。
    没写位置的 tween 按 gsap 的规矩接在当前末尾;位置或时长算不出来的那一条跳过。"""
    end = 0.0
    for m in _TWEEN_RE.finditer(fragment):
        args = _call_args(fragment, m.end())
        n_vars = 2 if m.group(1) == "fromTo" else 1
        if len(args) < 1 + n_vars:
            continue
        at = _eval(args[1 + n_vars], seconds) if len(args) > 1 + n_vars else end
        if at is None:
            continue
        dur = 0.0
        if m.group(1) != "set":
            dm = _DUR_RE.search(args[n_vars])
            got = _eval(dm.group(1), seconds) if dm else 0.5  # gsap 默认 0.5 秒
            if got is None:
                continue
            dur = got
        end = max(end, at + dur)
    for m in _ATTR_DUR_RE.finditer(fragment):
        end = max(end, float(m.group(1)))
    return end


#: 时间轴可以超过拍长多少(秒,或拍长的比例,取大的)才算"明显超过"
OVERRUN_SECONDS = 0.5
OVERRUN_RATIO = 0.1


def _timing_issues(fragment: str, seconds: float) -> list[Issue]:
    if seconds <= 0:
        return []
    end = timeline_end(fragment, seconds)
    slack = max(OVERRUN_SECONDS, seconds * OVERRUN_RATIO)
    if end > seconds + slack:
        return [Issue("error", f"时间轴约 {end:.2f} 秒,明显超过拍长 {seconds:.2f} 秒:"
                               "超出的部分会被裁掉,收尾动画看不到")]
    if end > seconds:
        return [Issue("warning", f"时间轴约 {end:.2f} 秒,略超拍长 {seconds:.2f} 秒")]
    return []


# --------------------------------------------------------------------------
# 数据里的数字
# --------------------------------------------------------------------------
#: 版式 / 时序用的数值字段:不上屏,不查事实
LAYOUT_KEYS = frozenset({
    "at", "ratio", "share", "seconds", "variant", "idx", "index", "order", "delay", "dur",
    "duration", "x", "y", "w", "h", "width", "height", "scale", "opacity", "size", "angle",
    "rotate", "zoom", "focus", "seed", "dec", "step", "level", "weight", "sha", "src",
})
#: 整棵子树都不是上屏内容(车图几何、额外图片的指纹)
SKIP_TREES = frozenset({"photo", "image", "edges"})
#: 名字不是参数:主体名("小米 SU7")、栏目名
NAME_KEYS = frozenset({"subject", "kicker", "scene"})
#: 年款("2026款""2025 年")和型号代号(SU7、X5、V6s)里的数字是名字,不查(同脚本 lint)
_YEAR_WORD_RE = re.compile(r"(?:19|20)\d{2}\s*(?:款|年)")
_MODEL_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}\d{1,2}(?![\d.])[A-Za-z]*")


def _walk(data: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """(路径, 叶子值)。路径形如 `rows[0].values[1]`。"""
    if isinstance(data, Mapping):
        for k, v in data.items():
            if str(k) in SKIP_TREES:
                continue
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(data, (list, tuple)):
        for i, v in enumerate(data):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, data


def _leaf_key(path: str) -> str:
    """`rows[0].values[1]` -> `values`;`rows[0].num` -> `num`。"""
    return re.sub(r"\[\d+\]", "", path).rsplit(".", 1)[-1]


def _numbers_of(value: Any, key: str) -> set[str]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float)):
        if key in LAYOUT_KEYS:
            return set()
        return numbers_in(f"{value:g}")
    if isinstance(value, str):
        if key in NAME_KEYS:
            return set()
        text = _MODEL_CODE_RE.sub(" ", _YEAR_WORD_RE.sub(" ", YEAR_RE.sub(" ", value)))
        return numbers_in(text)
    return set()


def _data_issues(data: Mapping[str, Any], facts: Sequence[Fact]) -> list[Issue]:
    enabled = [f for f in facts if getattr(f, "enabled", True)]
    solid: set[str] = set()
    weak: set[str] = set()
    for f in enabled:
        (weak if f.confidence == "low" else solid).update(numbers_in(f.claim))
    out: list[Issue] = []
    for path, value in _walk(data):
        key = _leaf_key(path)
        nums = _numbers_of(value, key)
        missing = sorted(nums - solid)
        if not missing:
            continue
        unbacked = [n for n in missing if n not in weak]
        shown = str(value)[:40]
        if unbacked:
            out.append(Issue("error", f"画面数字 {unbacked} 在事实里找不到(「{shown}」):"
                                      "每个上屏数字都要引用一条 Fact", path))
        else:
            out.append(Issue("warning", f"画面数字 {missing} 只见于低置信度事实(「{shown}」),"
                                        "上屏前再核对一下", path))
    return out


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def lint(fragment_text: str, data: Mapping[str, Any] | None, facts: Sequence[Fact],
         seconds: float, size: tuple[int, int] | None = None) -> list[Issue]:
    """校验一段项目内概念动画片段。error 必须改完才能渲染。

    fragment_text:片段原文(`<项目目录>/motion/<beat>.html`);
    data:要注入 `window.__DATA__` 的数据;facts:项目的事实;seconds:拍长;
    size:画幅,(1920, 1080) 或 (1080, 1920),给了别的尺寸也报 error(渲染器只认这两种)。"""
    issues: list[Issue] = []
    issues += _structure_issues(fragment_text)
    issues += _ascii_issues(fragment_text)
    issues += _network_issues(fragment_text)
    registry = registry_files()
    for pos, ref in references(fragment_text):
        got = _ref_issue(ref, _line_of(fragment_text, pos), registry)
        if got is not None:
            issues.append(got)
    issues += _timing_issues(fragment_text, float(seconds or 0))
    issues += _data_issues(data or {}, facts)
    if size is not None:
        ok = {tuple(getattr(config, "LANDSCAPE_SIZE", (1920, 1080))),
              tuple(getattr(config, "PORTRAIT_SIZE", (1080, 1920)))}
        if tuple(size) not in ok:
            issues.append(Issue("error", f"画幅 {size[0]}x{size[1]} 不支持:只能横屏 1920x1080 "
                                         "或竖屏 1080x1920"))
        elif size[1] > size[0] and "@portrait" not in fragment_text:
            issues.append(Issue("warning", "竖屏项目的片段文件头没写 @portrait yes:"
                                           "确认版式按竖屏排过"))
    return issues
