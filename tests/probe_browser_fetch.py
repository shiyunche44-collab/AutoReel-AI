"""网络探针:浏览器兜底真能拿到东西吗(会开本机 Chrome / Edge,手动跑)。

    python tests/probe_browser_fetch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.providers import browser_fetch  # noqa: E402

CASES = [
    # 懂车帝 PC 车系页:直连是 JS 空壳,浏览器拿渲染后的 DOM
    ("dongchedi 车系页", "https://www.dongchedi.com/auto/series/4865"),
    # 汽车之家参数接口:JSON 原样返回
    ("autohome 找车接口", "https://www.autohome.com.cn/ashx/AjaxIndexCarFind.ashx?type=1"),
]


def main() -> int:
    for name, url in CASES:
        try:
            body = browser_fetch.fetch(url, as_text=True)
        except browser_fetch.BrowserFetchError as exc:
            print(f"[{name}] {type(exc).__name__}: {exc}")
            continue
        print(f"[{name}] {len(body)} 字符:{body[:120]!r}")
    try:
        hits = browser_fetch.capture_json(
            "https://www.dongchedi.com/auto/series/4865", r"/motor/")
        print(f"[dongchedi capture_json] 截到 {len(hits)} 个 JSON")
    except browser_fetch.BrowserFetchError as exc:
        print(f"[dongchedi capture_json] {type(exc).__name__}: {exc}")
    print("blocked:", browser_fetch.blocked_list())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
