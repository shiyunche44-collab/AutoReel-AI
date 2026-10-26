"""网络探针:小红书创作者中心的「每篇笔记数据」从哪个接口来、长什么样(手动跑,只读)。

    python tests/probe_xhs_metrics.py [笔记标题] [--out 目录]

只打开页面、看页面自己发的请求,不点任何会改数据的按钮。先在分发台扫码登录小红书。做三件事:
1. 打开「笔记管理」和「数据中心 / 内容分析」两页;
2. 每页里凡是 JSON 响应带笔记计数字样(阅读 / 点赞 / 收藏 / 曝光 / view_count / like …)的,
   打印接口路径和顶层结构,完整响应写进 `--out` 目录;
3. 用 `parse_xhs_notes` 把截到的响应解析一遍,列出认出来的笔记(id / 标题 / 阅读 / 点赞 / 收藏 / 曝光),
   给了标题的再用 `match_xhs_note` 对一次 —— 认不出就照着落盘的响应补 `XHS_NOTE_KEYS`。

实测结论:还没跑过(写探针时分发台没登录小红书)。跑完把接口路径和字段名写在这里。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.metrics.collector import match_xhs_note, parse_xhs_notes  # noqa: E402
from app.publish import browser  # noqa: E402
from app.publish.platforms.xiaohongshu import DATA_URL, NOTES_URL  # noqa: E402

PAGES = [NOTES_URL, DATA_URL, "https://creator.xiaohongshu.com/statistics/data-analysis"]
KEYWORDS = re.compile(r"view_count|read_count|like|collect|imp_count|impression|阅读|曝光|点赞|收藏",
                      re.I)


def _shape(data: object, depth: int = 0) -> object:
    """JSON 的结构:dict 列 key,list 只看第一个元素和长度。"""
    if depth > 3:
        return "…"
    if isinstance(data, dict):
        return {k: _shape(v, depth + 1) for k, v in list(data.items())[:25]}
    if isinstance(data, list):
        return [f"len={len(data)}", _shape(data[0], depth + 1)] if data else []
    return type(data).__name__


def probe(title: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    hits: list[dict] = []
    payloads: list = []
    seen: list[str] = []

    def on_response(resp) -> None:  # noqa: ANN001
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        path = resp.url.split("?")[0]
        seen.append(path)
        try:
            text = resp.text()
        except Exception:  # noqa: BLE001
            return
        if not text.startswith("{") or not KEYWORDS.search(text):
            return
        name = f"{len(hits):02d}_{path.rsplit('/', 1)[-1][:40]}.json"
        (out / name).write_text(text, encoding="utf-8")
        hits.append({"url": resp.url[:200], "file": name, "bytes": len(text)})
        try:
            data = json.loads(text)
        except ValueError:
            return
        payloads.append(data)
        print(f"\n-- 命中 {path}({len(text)} 字节)→ {name}")
        print(json.dumps(_shape(data), ensure_ascii=False)[:1500])

    try:
        with browser.BrowserSession("xiaohongshu", headless=True, lock_timeout=90) as session:
            session.page.on("response", on_response)
            for url in PAGES:
                print(f"\n===== {url} =====")
                session.goto(url, wait_ms=9000)
                print("落地:", session.page.url[:160])
                text = session.page_text()
                if "login" in session.page.url or "扫码登录" in text[:500]:
                    print("登录失效:先去分发台扫码登录小红书")
                    return 1
                print("页面字样:", re.sub(r"\s+", " ", text)[:400])
    except browser.BrowserUnavailable as exc:
        print("浏览器起不来:", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"探针失败:{type(exc).__name__}: {exc}")
        return 1
    (out / "_seen.txt").write_text("\n".join(dict.fromkeys(seen)), encoding="utf-8")
    notes = parse_xhs_notes(payloads)
    print(f"\n===== 认出 {len(notes)} 篇笔记 =====")
    for n in notes[:20]:
        print(n["id"], n["title"][:24], n["metrics"], "曝光", n["impressions"])
    if title:
        print("\n对上:", match_xhs_note(notes, "", title))
    print(f"\n共 {len(hits)} 个疑似笔记数据接口,所有 XHR 路径见 {out / '_seen.txt'}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    target = Path(args[args.index("--out") + 1]) if "--out" in args else Path("probe_xhs_out")
    rest = [a for i, a in enumerate(args) if a != "--out" and (i == 0 or args[i - 1] != "--out")]
    sys.exit(probe(rest[0] if rest else "", target))
