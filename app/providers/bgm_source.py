"""BGM 自己收集:从 Mixkit 免费曲库按风格抓无版权纯音乐存到本地。

方舟套餐没有音乐模型,BGM 改成「收一次、反复用」。
Mixkit 许可(https://mixkit.co/license/):可免费商用,不要求署名。
每首的来源、标题、许可记在目录里的 `_licenses.json`,以后换来源或被追问时有据可查。

失败一律不抛:BGM 是锦上添花,网络不通就是没有 BGM,不能拖垮流水线。

情绪(`mood`):每首曲子在台账里带一个 `MOODS` 里的情绪,配乐规划(`pipeline/edit/music.plan`)
按它挑曲。`LIBRARY` 是给解说片挑好的 29 首(本地已有的 + 待下载的),`collect_library` 把缺的
下到 `<品类>/pool/`(和旧的按项目轮换曲库分开,旧流程不会挑到这些曲子);`tag_moods` 给已有台账补
情绪,不下载。节拍信息由 `pipeline/beat_track` 离线算好写回同一份台账。
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from pathlib import Path

import httpx

BASE = "https://mixkit.co"
LICENSE_URL = BASE + "/license/"
UA = {"User-Agent": "Mozilla/5.0"}
#: 太短的曲子循环起来太明显;成片 2–8 分钟,至少要 1.5 分钟
MIN_SECONDS = 90
LICENSES = "_licenses.json"
LICENSE_TEXT = "Mixkit License(免费商用,无需署名)"
#: 情绪曲库放在品类目录下的这个子目录
POOL = "pool"

#: 情绪词表 → Mixkit 列表页(风格页或 mood 页)
MOODS: dict[str, tuple[str, ...]] = {
    "热血": ("mood/energetic", "mood/driving", "mood/powerful"),
    "科技": ("tech", "mood/futuristic"),
    "舒缓": ("mood/calm",),
    "悬念": ("mood/suspenseful", "mood/tension"),
    "大气": ("mood/epic", "mood/uplifting"),
}
#: 老台账只有 genre:按风格推一个默认情绪
GENRE_MOOD = {
    "tech": "科技", "electronic": "科技", "ambient": "舒缓", "cinematic": "悬念",
    "documentary": "舒缓", "corporate-music": "大气", "children": "舒缓", "acoustic": "舒缓",
}

#: 解说片曲库(Mixkit id → 标题、情绪)。前 18 首本地已有,后 11 首要下载(约 46 MB,先问用户)。
LIBRARY: tuple[dict[str, object], ...] = (
    # 本地已有
    {"id": 123, "title": "Slow Trance Morning", "mood": "科技", "genre": "tech"},
    {"id": 130, "title": "Tech House vibes", "mood": "科技", "genre": "tech"},
    {"id": 162, "title": "Minimal Techno 01", "mood": "科技", "genre": "tech"},
    {"id": 201, "title": "Swish Swed", "mood": "科技", "genre": "tech"},
    {"id": 623, "title": "Deep Urban", "mood": "科技", "genre": "tech"},
    {"id": 1167, "title": "Close Up", "mood": "科技", "genre": "corporate-music"},
    {"id": 127, "title": "Valley Sunset", "mood": "舒缓", "genre": "ambient"},
    {"id": 292, "title": "Relax Beat", "mood": "舒缓", "genre": "ambient"},
    {"id": 580, "title": "Sun and His Daughter", "mood": "舒缓", "genre": "documentary"},
    {"id": 682, "title": "Classical vibes 2", "mood": "舒缓", "genre": "documentary"},
    {"id": 684, "title": "Classical vibes 4", "mood": "舒缓", "genre": "documentary"},
    {"id": 188, "title": "Echoes", "mood": "悬念", "genre": "documentary"},
    {"id": 614, "title": "Silent Descent", "mood": "悬念", "genre": "cinematic"},
    {"id": 671, "title": "Piano Horror", "mood": "悬念", "genre": "cinematic"},
    {"id": 871, "title": "Fright Night", "mood": "悬念", "genre": "cinematic"},
    {"id": 34, "title": "Raising Me Higher", "mood": "大气", "genre": "corporate-music"},
    {"id": 184, "title": "Vastness", "mood": "大气", "genre": "ambient"},
    {"id": 587, "title": "Discover", "mood": "大气", "genre": "documentary"},
    # 待下载
    {"id": 51, "title": "Sports Highlights", "mood": "热血", "genre": "mood/energetic"},
    {"id": 80, "title": "Daredevil", "mood": "热血", "genre": "mood/energetic"},
    {"id": 78, "title": "Sports Rock", "mood": "热血", "genre": "mood/energetic"},
    {"id": 1010, "title": "Super Strong", "mood": "热血", "genre": "mood/powerful"},
    {"id": 140, "title": "Cyberpunk City", "mood": "科技", "genre": "mood/futuristic"},
    {"id": 134, "title": "Deep Techno Ambience", "mood": "科技", "genre": "mood/futuristic"},
    {"id": 676, "title": "Epical Drums 01", "mood": "悬念", "genre": "mood/suspenseful"},
    {"id": 678, "title": "Epical Drums 03", "mood": "悬念", "genre": "mood/suspenseful"},
    {"id": 322, "title": "Life's a Movie", "mood": "大气", "genre": "mood/epic"},
    {"id": 187, "title": "Mountains", "mood": "大气", "genre": "mood/uplifting"},
    {"id": 32, "title": "Driving Ambition", "mood": "大气", "genre": "mood/uplifting"},
)
_BY_ID = {str(t["id"]): t for t in LIBRARY}


def _mixkit_id(name: str, meta: dict | None = None) -> str:
    m = (re.search(r"mixkit_(\d+)", name)
         or re.search(r"music/(\d+)/", str((meta or {}).get("source") or "")))
    return m.group(1) if m else ""


def mood_of(name: str, meta: dict | None = None) -> str | None:
    """一首曲子的情绪:台账里写了就用;精选清单里有就按清单;否则按风格 / mood 页推。"""
    meta = meta or {}
    if meta.get("mood") in MOODS:
        return str(meta["mood"])
    picked = _BY_ID.get(_mixkit_id(name, meta))
    if picked:
        return str(picked["mood"])
    genre = str(meta.get("genre") or "")
    if genre in GENRE_MOOD:
        return GENRE_MOOD[genre]
    return next((m for m, pages in MOODS.items() if genre in pages), None)


def _read_ledger(dest: Path) -> dict:
    try:
        data = json.loads((dest / LICENSES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger(dest: Path, ledger: dict) -> None:
    tmp = dest / (LICENSES + ".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest / LICENSES)


def _ledger_dirs(root: Path) -> list[Path]:
    return [root, *sorted(p.parent for p in root.glob(f"*/**/{LICENSES}"))]


def tag_moods(dest: Path) -> int:
    """给目录台账里缺情绪的条目补上 `mood`(不下载、不改别的字段)。返回补了几条。"""
    ledger = _read_ledger(dest)
    n = 0
    for name, meta in ledger.items():
        if isinstance(meta, dict) and meta.get("mood") not in MOODS:
            mood = mood_of(name, meta)
            if mood:
                meta["mood"] = mood
                n += 1
    if n:
        _write_ledger(dest, ledger)
    return n


def library_plan(root: Path, items: Sequence[dict] = LIBRARY) -> list[dict]:
    """精选清单里 `root`(品类目录,含子目录)还没有的曲子 —— 也就是 `collect_library` 要下的。"""
    have = set()
    for folder in _ledger_dirs(root):
        for name, meta in _read_ledger(folder).items():
            if (folder / name).is_file():
                have.add(_mixkit_id(name, meta if isinstance(meta, dict) else None))
    return [t for t in items if str(t["id"]) not in have]


def collect_library(root: Path, items: Sequence[dict] = LIBRARY) -> dict[str, list[str]]:
    """把精选清单里缺的曲子下到 `root/pool/`,台账带情绪;已有的(任一子目录)跳过。
    **会联网下载,调用前要先问过用户。** 返回 `{"done", "failed"}` 文件名。"""
    result: dict[str, list[str]] = {"done": [], "failed": []}
    todo = library_plan(root, items)
    if not todo:
        return result
    dest = root / POOL
    dest.mkdir(parents=True, exist_ok=True)
    ledger = _read_ledger(dest)
    with httpx.Client() as client:
        for t in todo:
            name = f"mixkit_{t['id']}.mp3"
            url = f"https://assets.mixkit.co/music/{t['id']}/{t['id']}.mp3"
            try:
                _download(client, url, dest / name)
            except (httpx.HTTPError, OSError):
                result["failed"].append(name)
                continue
            ledger[name] = {"title": t["title"], "genre": t.get("genre", ""), "mood": t["mood"],
                            "source": url, "license": LICENSE_TEXT, "license_url": LICENSE_URL}
            _write_ledger(dest, ledger)
            result["done"].append(name)
    return result


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
                                        "mood": mood_of(name, t), "seconds": t["seconds"],
                                        "source": t["url"], "license": LICENSE_TEXT,
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


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m app.providers.bgm_source plan|fetch|tag [品类目录]`(默认 data/bgm/auto_long)。
    plan 只列要下的;fetch 真下载(先问过用户);tag 给各级台账补情绪。"""
    import sys

    from .. import config

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "plan"
    base = config.BGM_DIR or config.DATA_DIR / "bgm"
    root = Path(args[1]) if len(args) > 1 else Path(base) / "auto_long"
    if cmd == "plan":
        for t in library_plan(root):
            print(t["id"], t["mood"], t["title"])
    elif cmd == "fetch":
        print(json.dumps(collect_library(root), ensure_ascii=False))
    elif cmd == "tag":
        for folder in _ledger_dirs(root):
            print(folder, tag_moods(folder))
    else:
        print(main.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
