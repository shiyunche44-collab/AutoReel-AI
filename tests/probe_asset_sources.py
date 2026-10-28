"""实测"从公开渠道取真实车型照片"的可行性(开发期用)。

问题背景:matrix 的 search_images 工具整体挂了(任何查询都返回
latency_ms=0),所以平台拿不到真实车型照片,只能退化成 AI 编的虚构车。

这里逐条验证几条候选替代渠道是否真的可用,而不是凭印象推荐。
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

UA = "AutoReel-AI/1.0 (local automation tool; contact: local user)"


def http_get(url: str, timeout: int = 25) -> tuple[int, bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, resp.read(), resp.headers.get("Content-Type", "")


def probe(name: str, url: str) -> bool:
    try:
        status, body, ctype = http_get(url)
        print(f"  ✓ {name}: HTTP {status}, {len(body)} bytes, {ctype[:40]}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {name}: {type(exc).__name__}: {str(exc)[:120]}")
        return False


def wikimedia_commons(query: str, limit: int = 8) -> None:
    """维基共享资源(CC 授权)按分类搜车型照片。"""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6",       # File:
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": "1280",
    }
    url = f"{api}?{urllib.parse.urlencode(params)}"
    print(f"\n=== Commons 检索:{query} ===")
    try:
        status, body, _ = http_get(url, timeout=40)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 失败:{type(exc).__name__}: {str(exc)[:160]}")
        return
    data = json.loads(body.decode("utf-8"))
    pages = (data.get("query") or {}).get("pages") or {}
    if not pages:
        print("  (无结果)")
        return
    for p in list(pages.values())[:limit]:
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value", "?")
        print(f"  · {p.get('title','')[:70]}")
        print(f"    {lic:<16} {ii.get('width')}x{ii.get('height')}")
        print(f"    {str(ii.get('url',''))[:110]}")


def wikipedia_page_images(title: str) -> None:
    """维基百科条目里配的车型照片(通常是官方图,比 Commons 搜索更准)。"""
    api = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "titles": title,
        "prop": "pageimages|images", "piprop": "original", "imlimit": "30",
    }
    url = f"{api}?{urllib.parse.urlencode(params)}"
    print(f"\n=== 维基百科条目:{title} ===")
    try:
        _, body, _ = http_get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 失败:{type(exc).__name__}: {str(exc)[:160]}")
        return
    data = json.loads(body.decode("utf-8"))
    for p in ((data.get("query") or {}).get("pages") or {}).values():
        if p.get("missing"):
            print("  (条目不存在)")
            return
        orig = p.get("original")
        if orig:
            print(f"  主图: {orig.get('source')}  ({orig.get('width')}x{orig.get('height')})")
        for im in (p.get("images") or [])[:10]:
            print(f"  图: {im.get('title')}")


def main() -> int:
    print("=== 1. 出网连通性 ===")
    ok = True
    ok &= probe("维基共享资源 API", "https://commons.wikimedia.org/w/api.php?action=query&format=json&meta=siteinfo")
    ok &= probe("维基百科 API", "https://en.wikipedia.org/w/api.php?action=query&format=json&meta=siteinfo")
    ok &= probe("通用 HTTPS(example.com)", "https://example.com")
    ok &= probe("汽车之家(国内站)", "https://www.autohome.com.cn/")
    ok &= probe("比亚迪官网", "https://www.bydauto.com.cn/")

    print("\n=== 2. 真实车型照片来源 ===")
    wikimedia_commons("BYD Han")
    wikimedia_commons("BYD Han EV")
    wikipedia_page_images("BYD Han")

    print("\n=== 3. matrix reverse_image_search 是否可用 ===")
    try:
        from app.providers import cli, images
        result = cli.call("connector__matrix__reverse_image_search",
                          {"image_url": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/9c/BYD_Han_001.jpg/800px-BYD_Han_001.jpg"},
                          timeout=90, retries=0)
        items = list(images._iter_images(result))
        print(f"  ✓ 返回 {len(items)} 条")
        for it in items[:3]:
            print(f"    {str(it.get('url',''))[:110]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {type(exc).__name__}: {str(exc)[:160]}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
