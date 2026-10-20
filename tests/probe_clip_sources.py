"""探测:车媒文章页里到底有没有可直接下载的视频直链。

这条探测决定 `app/providers/clips.py` 的 V2 来源能不能成立。
背景:没有视频检索 connector,真实视频片段只能自建抓取;`search_images`
能拿到车媒文章页地址,但这些页面里嵌的视频往往是 YouTube iframe
(需要签名解密,本机又没有 yt-dlp)。如果页面里另有 mp4 / m3u8 直链,
那 ffmpeg 原生就能拉(它支持 HLS),整条路就不必依赖任何第三方包。

用法:python -m tests.probe_clip_sources [页数]
"""
from __future__ import annotations

import re
import sys

import httpx

from app import categories
from app.providers import images

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

#: 直链候选。只认 mp4 / m3u8 —— webm 在 Windows 上要额外转码,不划算。
#:
#: 刻意写成宽松形态:很多车媒的页面里视频地址是**协议相对**(//cdn/a.mp4)
#: 或**转义过的 JSON 串**(http:\/\/cdn\/a.mp4),只认绝对 https 会漏掉一大半,
#: 然后误判成"这站不给直链"。
DIRECT_LINK_RE = re.compile(
    r"(?:https?:)?(?:\\?/){2}[^\"'\s<>\\]+\.(?:mp4|m3u8)(?:[^\"'\s<>\\]*)",
    re.IGNORECASE,
)

#: 这些站点只有 iframe 或需要签名解密,提前跳过,别浪费请求
_SKIP_HOSTS = ("youtube.com", "youtu.be", "bilibili.com", "zhihu.com", "tiktok.com")

_QUERIES = [
    "比亚迪汉 实拍 评测 视频",
    "BYD Han EV driving video",
    "比亚迪汉 静态体验 视频",
    "比亚迪汉 试驾 视频",
    "BYD Han EV interior video",
]


def _pages(limit: int) -> list[str]:
    profile = categories.load_profile("auto_long")
    refs = images.search(
        _QUERIES,
        relevance_prompts={
            q: profile.reference_prompt("BYD Han EV", "exterior_45") for q in _QUERIES
        },
    )
    out: list[str] = []
    for ref in refs:
        page = ref.source_page
        if not page or page in out:
            continue
        if any(h in page for h in _SKIP_HOSTS):
            continue
        out.append(page)
        if len(out) >= limit:
            break
    return out


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    pages = _pages(limit)
    print(f"候选文章页:{len(pages)}")
    hits = 0
    for url in pages:
        try:
            resp = httpx.get(url, headers=UA, timeout=25, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERR {type(exc).__name__} {url[:70]}")
            continue
        found = DIRECT_LINK_RE.findall(resp.text)
        found = [f for f in found if not f.lower().split("?")[0].endswith(".webm")]
        found = sorted(set(found))
        if found:
            hits += 1
        print(f"  {'HIT ' if found else 'no  '} {resp.status_code} "
              f"{len(found)} 条  {url[:70]}")
        for link in found[:3]:
            print(f"        -> {link[:120]}")
    print(f"\n命中率:{hits}/{len(pages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
