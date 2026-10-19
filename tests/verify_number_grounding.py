"""核对脚本里的数字是否真的出现在它引用的那条事实里(开发期验证用)。

lint 目前只保证"有引用",不保证"引用的那条事实支持这个数字"。
参数讲错正是这套系统要防的头号翻车方式,所以这里单独查一遍。

注意:中文数字必须真解析,不能逐字替换。
"三十一" 逐字替换会变成 "3"+"10"+"1" = "3101",而事实里写的是 31 ——
早期版本就是这么把 5 段正常脚本全判成"数字无据"的,纯假阳性。

用法:
    python -m tests.verify_number_grounding
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
        return f"{v * 10}%" if v is not None else m.group(0)

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


def main() -> int:
    from app import db

    project_id = (ROOT / "data" / "_current_project.txt").read_text(
        encoding="utf-8-sig").strip()

    facts = {f["id"]: f for f in (db.read_json(project_id, "facts.json", []) or [])}
    segments = db.read_json(project_id, "script.json", []) or []

    ok, manual, bad = 0, 0, 0
    print("--- 逐段核对:脚本数字是否被引用的事实支持 ---\n")
    for seg in segments:
        text = seg.get("text", "")
        refs = seg.get("fact_refs", []) or []
        script_nums = numbers_in(text)
        if not script_nums:
            continue
        cited_claims = [facts.get(r, {}).get("claim", "") for r in refs]
        cited_nums = numbers_in(" ".join(cited_claims))
        unsupported = script_nums - cited_nums

        if not unsupported:
            ok += 1
            print(f"✓ [{seg['idx']}] {sorted(script_nums)} 全部见于引用事实")
            continue

        # 脚本里有"五米一"这类单位表达时,数字必然对不齐,交人工判断
        if has_unit_expression(text) or has_unit_expression(" ".join(cited_claims)):
            manual += 1
            print(f"⚠ [{seg['idx']}] {seg.get('section','')} 含单位表达,需人工确认")
            print(f"    脚本   : {text}")
            for c in cited_claims:
                print(f"    依据   : {c}")
            print(f"    未直接命中: {sorted(unsupported)}")
            print()
            continue

        bad += 1
        print(f"✗ [{seg['idx']}] {seg.get('section','')} 数字与引用事实对不上")
        print(f"    脚本   : {text}")
        for c in cited_claims:
            print(f"    依据   : {c}")
        print(f"    未被支持: {sorted(unsupported)}")
        print()

    print()
    print(f"直接命中 {ok} 段 · 需人工确认 {manual} 段 · 确实无据 {bad} 段")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
