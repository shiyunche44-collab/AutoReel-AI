"""数字核对:文本里的数字是否真的出现在它引用的事实里。

lint 只保证"有引用",不保证"引用的那条事实支持这个数字"。脚本核对
(`tests.verify_number_grounding`)和数据卡片(`cards.ground`)共用这里的解析。

中文数字必须真解析,不能逐字替换:"三十一" 逐字替换会变成 "3"+"10"+"1",
而事实里写的是 31。
"""
from __future__ import annotations

import re

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
       "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(text: str) -> int | None:
    """解析 0–999 的常见中文数词。覆盖 十/十五/二十/三十一/两百/一百零五。"""
    if not text:
        return None
    if not any(c in _CN or c in "十百" for c in text):
        return None
    total, section, number = 0, 0, 0
    for ch in text:
        if ch in _CN:
            number = _CN[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        else:
            return None
    result = total + section + number
    return result or None


def _expand_cn_numerals(text: str) -> str:
    """把文本里的中文数词替换成阿拉伯数字,供后续统一比对。"""
    # 先处理"百分之X"和"X成"(两成 = 20%)
    def pct(m: re.Match) -> str:
        v = cn_to_int(m.group(1))
        return f"{v}%" if v is not None else m.group(0)

    text = re.sub(r"百分之([零一二三四五六七八九十百两]+)", pct, text)

    def cheng(m: re.Match) -> str:
        v = cn_to_int(m.group(1))
        return f"{v * 10}" if v is not None else m.group(0)

    text = re.sub(r"([零一二三四五六七八九十百两]+)成", cheng, text)

    # 再处理普通中文数词(从长到短,避免"三十一"被拆开)
    def repl(m: re.Match) -> str:
        v = cn_to_int(m.group(0))
        return str(v) if v is not None else m.group(0)

    return re.sub(r"[零一二三四五六七八九十百两]+", repl, text)


def numbers_in(text: str) -> set[str]:
    """抽出文本里的所有数字(去掉小数点便于与事实比对)。"""
    expanded = _expand_cn_numerals(text)
    return {m.replace(".", "") for m in re.findall(r"\d+(?:\.\d+)?", expanded)}


#: 量词/单位表达,无法用数字直接比对,单列出来交人工判断
_UNIT_WORDS = ("米", "毫米", "公里", "升", "度", "千瓦", "秒", "分钟", "小时",
               "个", "款", "成", "万", "元", "吨", "毫米")


def has_unit_expression(text: str) -> bool:
    return any(w in text for w in _UNIT_WORDS)


# --------------------------------------------------------------------------
# 中文数量词 -> 阿拉伯数字(脚本落盘前统一)
# --------------------------------------------------------------------------
#: LLM 偶尔整篇写中文数字("二十六万三千五""五百九十三公里")。不统一的话:
#: lint 的"数字必须有引用"只认阿拉伯数字,会被整篇绕过;字幕也全是汉字。
#: 只转**明确是数量**的写法,"第一""一下""千万别""万一""三电""两档"一律不碰。
_DIGITS = "零〇一二三四五六七八九两"
_MAGS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}
_QTY_RE = re.compile(
    rf"(?<![第{_DIGITS}十百千万亿点])"
    rf"([{_DIGITS}十][{_DIGITS}十百千万亿]*(?:点[{_DIGITS}]+)?)"
    rf"(?![{_DIGITS}十百千万亿])"
)
_CN_YEAR_RE = re.compile(rf"([{_DIGITS}]{{4}})(?=年)")
_PCT_RE = re.compile(rf"百分之([{_DIGITS}十百]+(?:点[{_DIGITS}]+)?)")


def _digit(ch: str) -> int:
    return 0 if ch in "零〇" else (2 if ch == "两" else "一二三四五六七八九".index(ch) + 1)


def cn_number(text: str) -> float | None:
    """解析带 十/百/千/万/亿 和"点"的中文数量。

    支持省略尾数位的口语:"三千五" = 3500,"两万五" = 25000,"二十六万三千五" = 263500。
    连续裸数字("一一""四五千")无法确定含义,返回 None。
    """
    if not text:
        return None
    integer, _, frac = text.partition("点")
    if frac and not all(c in _DIGITS for c in frac):
        return None
    total, section, number, last_mag, prev_digit = 0, 0, 0, 1, False
    for ch in integer:
        if ch in _DIGITS:
            if prev_digit and ch not in "零〇":
                return None
            number = _digit(ch)
            prev_digit = ch not in "零〇"
            continue
        prev_digit = False
        mag = _MAGS.get(ch)
        if mag is None:
            return None
        if mag >= 10_000:
            section = (section + number) or 1
            total += section * mag
            section, number = 0, 0
        else:
            section += (number or 1) * mag
            number = 0
        last_mag = mag
    # 口语省略:"三千五" 的五是五百,"两万五" 的五是五千
    if number and last_mag >= 100 and not integer.endswith(tuple("零〇")) and \
            "零" not in integer[-2:]:
        number *= last_mag // 10
    value = float(total + section + number)
    if frac:
        value += float("0." + "".join(str(_digit(c)) for c in frac))
    return value


def _fmt(value: float, had_wan: bool, had_yi: bool) -> str:
    if had_yi and value >= 100_000_000:
        return f"{value / 100_000_000:g}亿"
    if had_wan and value >= 10_000:
        return f"{value / 10_000:g}万"
    return f"{value:g}"


def arabicize(text: str) -> str:
    """把文本里的中文数量词换成阿拉伯数字。不确定的保持原样。"""
    if not text:
        return text
    text = _CN_YEAR_RE.sub(lambda m: "".join(str(_digit(c)) for c in m.group(1)), text)

    def pct(m: re.Match) -> str:
        v = cn_number(m.group(1))
        return f"{v:g}%" if v is not None else m.group(0)

    text = _PCT_RE.sub(pct, text)

    def qty(m: re.Match) -> str:
        s = m.group(1)
        # 单个汉字("一台""三电""两档""十足")不是要核对的数据,不动
        if len(s) < 2:
            return s
        # 数量不会以「零」开头(「零点五」除外);「零百」是 0-100km/h 加速的说法,不是 100
        if s[0] in "零〇" and not s[1:].startswith("点"):
            return s
        v = cn_number(s)
        if v is None:
            return s
        return _fmt(v, "万" in s, "亿" in s)

    return _QTY_RE.sub(qty, text)
