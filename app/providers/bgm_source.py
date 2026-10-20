"""BGM 自己收集:从 Mixkit 免费曲库按风格抓无版权纯音乐存到本地。

MiniMax 音乐接口已对新用户关闭(HTTP 410),BGM 改成「收一次、反复用」。
Mixkit 许可(https://mixkit.co/license/):可免费商用,不要求署名。
每首的来源、标题、许可记在目录里的 `_licenses.json`,以后换来源或被追问时有据可查。

失败一律不抛:BGM 是锦上添花,网络不通就是没有 BGM,不能拖垮流水线。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Sequence

import httpx

BASE = "https://mixkit.co"
LICENSE_URL = BASE + "/license/"
UA = {"User-Agent": "Mozilla/5.0"}
#: 太短的曲子循环起来太明显;成片 2–8 分钟,至少要 1.5 分钟
MIN_SECONDS = 90
LICENSES = "_licenses.json"


def _seconds(text: str) -> int:
    parts = [int(p) for p in re.findall(r"\d+", text)]
    return parts[0] * 60 + parts[1] if len(parts) == 2 else 0


def list_genre(genre: str, client: httpx.Client) -> list[dict]:
    """一个风格页上的曲目:[{id, title, seconds, url}]。"""
    resp = client.get(f"{BASE}/free-stock-music/{genre}/", headers=UA, timeout=30,
                      follow_redirects=True)
    resp.raise_for_status()
    tracks: list[dict] = []
    for card in resp.text.split('class="item-grid__item"')[1:]:
        m = re.search(r"music/(\d+)/\1\.mp3", card)
        if not m:
            continue
        title = re.search(r'item-grid-card__title[^>]*>\s*(?:<[^>]+>\s*)*([^<]+)', card)
        dur = re.search(r'data-test-id="duration">\s*([\d:]+)', card)
        tracks.append({
            "id": m.group(1),
            "title": html.unescape(title.group(1).strip()) if title else "",
            "seconds": _seconds(dur.group(1)) if dur else 0,
            "url": f"https://assets.mixkit.co/music/{m.group(1)}/{m.group(1)}.mp3",
            "genre": genre,
        })
    return tracks


def collect(genres: Sequence[str], dest: Path, *, want: int = 6) -> list[Path]:
    """往 `dest` 收最多 `want` 首(已有的算在内),各风格轮流取、够长的优先。
    返回目录里现有的曲子。"""
    dest.mkdir(parents=True, exist_ok=True)
    have = sorted(p for p in dest.glob("mixkit_*.mp3") if p.stat().st_size > 0)
    if len(have) >= want or not genres:
        return have
    try:
        ledger = json.loads((dest / LICENSES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ledger = {}
    try:
        with httpx.Client() as client:
            pools = []
            for g in genres:
                try:
                    pools.append([t for t in list_genre(g, client) if t["seconds"] >= MIN_SECONDS])
                except httpx.HTTPError:
                    continue
            # 风格轮流取一首,直到够数
            n = len(have)
            while n < want and any(pools):
                for pool in pools:
                    if n >= want:
                        break
                    while pool:
                        t = pool.pop(0)
                        name = f"mixkit_{t['id']}.mp3"
                        if (dest / name).is_file():
                            continue
                        try:
                            _download(client, t["url"], dest / name)
                        except (httpx.HTTPError, OSError):
                            continue
                        ledger[name] = {"title": t["title"], "genre": t["genre"],
                                        "seconds": t["seconds"], "source": t["url"],
                                        "license": "Mixkit License(免费商用,无需署名)",
                                        "license_url": LICENSE_URL}
                        n += 1
                        break
    except httpx.HTTPError:
        pass
    (dest / LICENSES).write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    return sorted(p for p in dest.glob("mixkit_*.mp3") if p.stat().st_size > 0)


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".part")
    with client.stream("GET", url, headers=UA, timeout=120, follow_redirects=True) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 16):
                fh.write(chunk)
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise OSError("下载到空文件")
    tmp.replace(dest)
