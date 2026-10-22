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
        # 「零百」是 0-100km/h 加速的说法,不是 100(和 arabicize 同一条规则);
        # 不挡的话「零百2.92秒」会读成 100292,和事实里的 2.92 永远对不上
        if m.group(0)[0] == "零" and len(m.group(0)) > 1:
            return m.group(0)
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


# --------------------------------------------------------------------------
# 同一参数的两条事实:版本、维度、取值
# --------------------------------------------------------------------------
#: 年款写法("2026款""2025 款")
YEAR_RE = re.compile(r"(20\d{2})\s*款")
#: 版本写法("标准版""GT版""Pro版"),从去掉年款后的 basis 里取
_VERSION_RE = re.compile(r"([A-Za-z一-鿿]{1,4}?版)")
#: 数字后面的单位。长单位在前,免得 kWh 被认成 kW
_MEASURE_UNITS = ("万元", "公里", "分钟", "毫米", "马力", "千瓦时", "kWh", "TOPS", "rpm", "km/h",
                  "N·m", "MPa", "kW", "mm", "km", "kg", "匹", "秒", "米", "元", "L", "V", "C", "%")
#: 同一单位的不同写法("835km"和"643公里"是同一维度)
_UNIT_ALIAS = {"km": "公里", "mm": "毫米", "千瓦时": "kWh", "马力": "匹"}
#: 单位本身就说明了是哪个参数(电压平台、电池容量、算力、转速…),不用再看前面的字。
#: 其余单位(公里、秒、米、元…)一种单位对应很多参数,得靠维度名区分
#: kW / N·m / rpm / MPa 不算:单电机和双电机合计、不同部位的钢,同一单位讲的是不同的东西
_SELF_EXPLAINING = {"V", "kWh", "TOPS", "C"}
#: 单位后面不能紧跟字母:「0.245Cd」的 C 不是充电倍率
_MEASURE_RE = re.compile(r"(\d[\d.]*)\s*(" + "|".join(re.escape(u) for u in _MEASURE_UNITS)
                         + r")(?![A-Za-z])(/)?")
#: 数字前面的连接词,取维度名时跳过("制动距离为 33.9 米" 的维度是"距离")
_LEAD_WORD = re.compile(r"[一-鿿A-Za-z]+$")
_LEAD_FILLER = re.compile(r"(?:为|达到|达|是|约|仅|最高|最大|最长|最短|最快|超过|\s)+$")


#: basis 里单独写的英文版本名(GT / Pro / Max / Ultra)。全大写三个字母以上的是工况
#: (CLTC / WLTP / EPA),不是版本
_LATIN_TRIM_RE = re.compile(r"(?<![A-Za-z])([A-Za-z][a-z]*|[A-Z]{1,2})(?![A-Za-z])")


def versions_of(basis: str) -> set[str]:
    """basis 里写的版本;空 = 全系 / 没分版本。

    「四驱GT」没有「版」字也是一个版本 —— 认不出来就会被当成全系,
    拿去和每个版本比价,把在售的其它版本误判成"数字被改了"。
    """
    text = YEAR_RE.sub(" ", basis or "")
    out = set(_VERSION_RE.findall(text))
    out |= {m for m in _LATIN_TRIM_RE.findall(text) if not any(m in v for v in out)}
    return out


def versions_compatible(a: set[str], b: set[str]) -> bool:
    """任一方没写版本(全系),或两边有一个版本名互相包含(「标准版」/「后驱标准版」)。"""
    if not a or not b:
        return True
    return any(x in y or y in x for x in a for y in b)


def measures(claim: str) -> dict[tuple[str, str], set[str]]:
    """(维度名, 单位) → 取值。维度名 = 数字前两个字("续航 643 公里" → 续航)。

    只按单位分维度太粗:"转弯半径 5.7 米"和"制动距离 32.9 米"会被当成同一件事。
    """
    out: dict[tuple[str, str], set[str]] = {}
    for m in _MEASURE_RE.finditer(claim or ""):
        unit = _UNIT_ALIAS.get(m.group(2), m.group(2))
        if m.group(3):
            unit += "/"  # 「12.9kWh/100km」是比率(电耗),和电池容量「73kWh」不是一个维度
        # "搭载 800V"和"采用 752V"是同一件事:单位自带含义时不看前面的字
        label = ""
        if unit not in _SELF_EXPLAINING:
            lead = _LEAD_WORD.search(_LEAD_FILLER.sub("", claim[:m.start()]))
            if not lead:
                continue  # 数字前面是标点:看不出是哪个参数,宁可不比
            label = lead.group(0)[-2:]
        out.setdefault((label, unit), set()).add(m.group(1))
    return out


def differing_measures(claim_a: str, basis_a: str, claim_b: str,
                       basis_b: str) -> list[tuple[str, str, set[str], set[str]]]:
    """两条事实讲同一参数却给了不同的数:同版本或任一方是全系、同维度、取值不同。

    返回 [(维度名, 单位, a 的取值, b 的取值)]。
    """
    if not versions_compatible(versions_of(basis_a), versions_of(basis_b)):
        return []
    mb = measures(claim_b)
    # 取值有交集不算冲突(「2200MPa」和「1500/2200MPa 两种钢」说的是同一个数)
    return [(k[0], k[1], vals, mb[k]) for k, vals in measures(claim_a).items()
            if k in mb and not vals & mb[k]]
