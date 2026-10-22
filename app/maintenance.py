"""磁盘与缓存的生命周期。

`data/` 已经长到 3 GB 出头,而且**只会涨**:

| 目录 | 实测 | 内容 |
|---|---|---|
| `data/projects` | 1.74 GB | 23 个项目,每个约 76 MB(草稿、成片、音频、facts_raw) |
| `data/library` | 787 MB | 素材池,按车型共享 |
| `data/_cache` | 342 MB | 逐句 TTS 17 MB + 概念动画渲染 314 MB |
| `data/_probe`、`data/_tmp` | 43 MB | 纯临时物,**没有任何清理入口** |

并行渲染会成倍放大这一切,所以这一层要在放开并发**之前**落地。

**原则:只清可重建的,不动不可再生的。**

| 目录 | 能不能自动清 | 为什么 |
|---|---|---|
| `_cache/tts` | 能(按龄) | 内容寻址(文本+音色+语速的哈希)。重建要花钱,但不影响正确性 |
| `_cache/motion` | 能(按龄) | 内容寻址(模板+风格+数据+时长+画幅) |
| `_cache/trends`、`_cache/webassets` | 能(按龄) | 重新抓一次就好 |
| `data/bgm`、`_cache/bgm` | **不自动清** | 共享曲库,重建要花钱 |
| `library/<车型>` | **只能显式驱逐** | 重建要重新下载 + 视觉打标签,很贵 |
| `projects/<id>` 的中间产物 | 能(显式) | 成片、封面、脚本、分镜**永不自动删** |
| `_probe`、`_tmp` | 能 | 纯临时物 |

命令行:

```bash
python -m app.maintenance report          # 看体积分布
python -m app.maintenance prune           # 按策略清理(缓存按龄 + 临时物)
python -m app.maintenance prune --all     # 连共享曲库也清(要重新攒曲库)
python -m app.maintenance prune --dry-run # 只报告不删
```
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from . import config

#: 缓存默认保留天数。TTS 句子与概念动画渲染都是内容寻址的,过期了
#: 重新生成即可 —— 只是要花钱,所以给一个保守的窗口。
DEFAULT_CACHE_MAX_AGE_DAYS = 30

#: 可以按龄清理的缓存子目录(相对 data/_cache)。
#:
#: **不含 bgm** —— 那是共享曲库,重建要花钱,只在显式 `--all` 时才动。
PRUNABLE_CACHE_DIRS = ("tts", "motion", "trends", "webassets")

#: 纯临时目录(相对 data/)。里面没有需要留的东西。
TEMP_DIRS = ("_probe", "_tmp")

#: 项目目录里**永不自动删**的东西 —— 用户的创作内容与成片。
PROJECT_KEEP = ("output.mp4", "cover.jpg", "script.json", "storyboard.json",
                "facts.json", "lint.json", "topic.json", "review_state.json",
                "concept_style.json", "rhythm.json", "render.json", "cards.json",
                "narration.json", "beats")

#: 项目目录里可以清的中间产物。
PROJECT_PRUNABLE_DIRS = ("drafts", "shots", "refs", "audio", "facts_raw", "_render")


# --------------------------------------------------------------------------
# 体积统计
# --------------------------------------------------------------------------
def dir_size(path: Path) -> int:
    """目录(或文件)的总字节数。读不到就当 0。"""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def human(size: float) -> str:
    """字节数转成人看得懂的字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def report() -> dict[str, Any]:
    """data/ 的体积分布。"""
    data = config.DATA_DIR
    parts: dict[str, int] = {}
    if data.is_dir():
        for child in sorted(data.iterdir()):
            parts[child.name] = dir_size(child) if child.is_dir() else child.stat().st_size
    cache = data / "_cache"
    cache_parts: dict[str, int] = {}
    if cache.is_dir():
        for child in sorted(cache.iterdir()):
            cache_parts[child.name] = dir_size(child) if child.is_dir() else child.stat().st_size
    total = sum(parts.values())
    return {
        "data_dir": str(data),
        "total_bytes": total,
        "total": human(total),
        "parts": {k: human(v) for k, v in sorted(parts.items(), key=lambda kv: -kv[1])},
        "parts_bytes": parts,
        "cache": {k: human(v) for k, v in sorted(cache_parts.items(), key=lambda kv: -kv[1])},
        "cache_bytes": cache_parts,
    }


def data_size() -> int:
    """data/ 的总字节数(健康检查用)。"""
    return dir_size(config.DATA_DIR)


# --------------------------------------------------------------------------
# 缓存清理
# --------------------------------------------------------------------------
def _age_days(path: Path) -> float:
    try:
        return (time.time() - path.stat().st_mtime) / 86400.0
    except OSError:
        return 0.0


def prune_caches(max_age_days: int = DEFAULT_CACHE_MAX_AGE_DAYS, *,
                 dry_run: bool = False, include_bgm: bool = False) -> dict[str, Any]:
    """按龄清理可重建的缓存。

    `include_bgm=True` 才会动共享曲库 —— 默认不动(重建曲库要花钱)。
    """
    cache = config.DATA_DIR / "_cache"
    dirs = list(PRUNABLE_CACHE_DIRS) + (["bgm"] if include_bgm else [])
    freed = 0
    removed = 0
    kept = 0
    for name in dirs:
        target = cache / name
        if not target.is_dir():
            continue
        for p in target.rglob("*"):
            if not p.is_file():
                continue
            if _age_days(p) < max_age_days:
                kept += 1
                continue
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if not dry_run:
                try:
                    p.unlink()
                except OSError:
                    continue
            freed += size
            removed += 1
    return {"removed": removed, "kept": kept, "freed": freed, "freed_human": human(freed),
            "dry_run": dry_run, "max_age_days": max_age_days}


def prune_temp(*, dry_run: bool = False) -> dict[str, Any]:
    """清空纯临时目录与散落的临时文件。

    `data/_probe`、`data/_tmp` 里只有探测与下载的中间物;还有几个
    `data/_tmp_*.mp4` 这种直接躺在 data 根下的残留。
    """
    freed = 0
    removed = 0
    data = config.DATA_DIR
    for name in TEMP_DIRS:
        target = data / name
        if not target.is_dir():
            continue
        size = dir_size(target)
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
        freed += size
        removed += 1
    for p in data.glob("_tmp_*"):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if not dry_run:
            try:
                p.unlink()
            except OSError:
                continue
        freed += size
        removed += 1
    return {"removed": removed, "freed": freed, "freed_human": human(freed), "dry_run": dry_run}


# --------------------------------------------------------------------------
# 项目中间产物
# --------------------------------------------------------------------------
def prune_project(project_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """清掉一个项目的中间产物,**保留**成片、封面与用户的创作内容。

    删的是 `drafts/`、`shots/`、`audio/`(分句音频)、`facts_raw/` 这些
    可重建的东西;`script.json` / `storyboard.json` / `output.mp4` 这些
    留着 —— 那是用户手改的稿与成片,不能因为"省磁盘"被清掉。
    """
    root = config.project_dir(project_id)
    freed = 0
    removed: list[str] = []
    for name in PROJECT_PRUNABLE_DIRS:
        target = root / name
        if not target.is_dir():
            continue
        size = dir_size(target)
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
        freed += size
        removed.append(name)
    return {"project": project_id, "removed": removed, "freed": freed,
            "freed_human": human(freed), "dry_run": dry_run}


# --------------------------------------------------------------------------
# 素材池
# --------------------------------------------------------------------------
def pool_dir(subject: str) -> Path:
    """素材池目录。slug 规则与 `library.slug` 一致。"""
    from .pipeline import library

    return config.DATA_DIR / library.LIB_ROOT_NAME / library.slug(subject)


def pools_in_use() -> dict[str, list[str]]:
    """哪些项目在用哪些素材池。{车型: [项目 id, ...]}。

    素材池是**跨项目共享**的:两个项目讲同一台车,用的是同一个池子。
    驱逐之前必须先看还有谁在用。
    """
    from . import db
    from .pipeline import library

    out: dict[str, list[str]] = {}
    try:
        rows = db.list_projects(limit=1000)
    except Exception:  # noqa: BLE001 - 库还没建时不该让清理工具崩掉
        return out
    for row in rows:
        names = [row.get("subject") or ""]
        raw = row.get("subjects") or "[]"
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            items = []
        for item in items or []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
        for name in names:
            if not name:
                continue
            out.setdefault(library.slug(name), []).append(str(row.get("id")))
    return out


def evict_pool(subject: str, *, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """驱逐一个素材池(删掉 `data/library/<slug>/`)。

    还有项目在用这个池时会被拒绝 —— 除非 `force=True`。素材池重建要
    重新下载 + 视觉打标签,很贵,不该在别的项目正用着它的时候删掉。
    """
    from .pipeline import library

    slug = library.slug(subject)
    target = config.DATA_DIR / library.LIB_ROOT_NAME / slug
    if not target.is_dir():
        return {"ok": False, "reason": "没有这个池", "slug": slug}
    users = [pid for pid in pools_in_use().get(slug, []) if pid]
    if users and not force:
        return {"ok": False, "reason": f"还有 {len(users)} 个项目在用这个池",
                "slug": slug, "used_by": users}
    size = dir_size(target)
    if not dry_run:
        shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "slug": slug, "freed": size, "freed_human": human(size),
            "dry_run": dry_run, "forced": bool(users and force)}


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def _main() -> None:
    ap = argparse.ArgumentParser(description="aimovice 磁盘与缓存维护")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report", help="看 data/ 的体积分布")
    p = sub.add_parser("prune", help="按策略清理")
    p.add_argument("--all", action="store_true", help="连共享 BGM 曲库一起清(要重新攒)")
    p.add_argument("--dry-run", action="store_true", help="只报告不删")
    p.add_argument("--age-days", type=int, default=DEFAULT_CACHE_MAX_AGE_DAYS,
                   help=f"缓存保留天数(默认 {DEFAULT_CACHE_MAX_AGE_DAYS})")
    p.add_argument("--pool", default="", help="额外驱逐某个车型的素材池")
    args = ap.parse_args()

    if args.cmd == "report":
        info = report()
        print(f"data/ 合计 {info['total']}")
        for name, size in info["parts"].items():
            print(f"  {name:<14} {size}")
        if info["cache"]:
            print("  _cache/ 明细:")
            for name, size in info["cache"].items():
                print(f"    {name:<12} {size}")
        return

    if args.cmd == "prune":
        tmp = prune_temp(dry_run=args.dry_run)
        caches = prune_caches(args.age_days, dry_run=args.dry_run, include_bgm=args.all)
        print(f"临时物:删 {tmp['removed']} 项,腾出 {tmp['freed_human']}")
        print(f"缓存:删 {caches['removed']} 项,留 {caches['kept']} 项,腾出 {caches['freed_human']}")
        if args.pool:
            out = evict_pool(args.pool, dry_run=args.dry_run)
            print(f"素材池 {out.get('slug')}:{'已删 ' + out.get('freed_human', '') if out.get('ok') else out.get('reason')}")
        if args.dry_run:
            print("(dry-run,没有真的删)")


if __name__ == "__main__":
    _main()
