"""人工过验证码:抓取兜底撞墙的站点,打开有头浏览器手动过一次,登录态保存复用。

用法:
    python -m scripts.browser_unblock            # 列出被拦站点,逐个打开
    python -m scripts.browser_unblock <url>      # 直接打开某个页面(比如先登录某站)
    python -m scripts.browser_unblock --list     # 只列出,不打开
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv: list[str]) -> int:
    from app.providers import browser_fetch

    urls = [a for a in argv if a.startswith(("http://", "https://"))]
    if not urls:
        items = browser_fetch.blocked_list()
        if not items:
            print("没有被拦的站点。")
            return 0
        for b in items:
            when = time.strftime("%m-%d %H:%M", time.localtime(b.get("ts", 0)))
            print(f"[{when}] {b.get('host')}  {b.get('reason')}\n    {b.get('url')}")
        if "--list" in argv:
            return 0
        urls = [b["url"] for b in items if b.get("url")]

    for url in urls:
        print(f"\n打开 {url}\n手动过完验证 / 登录后,关掉浏览器窗口继续。")
        path = browser_fetch.unblock(url)
        print(f"登录态已保存:{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
