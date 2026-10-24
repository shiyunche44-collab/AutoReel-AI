"""给「技术参数」上线前建的素材池补宽、高、帧率、码率、编码。

只读本地文件(ffprobe / PIL),不重新下载、不调视觉模型。补完后
`Item.low_res`(视频短边 < 720)直接生效,对应的段不再进背景。
顺带给每个视频段做一次本地三帧体检(`media_check.frame_verdict`),黑场 / 淡场标成跳过;
给每个已下载的源做一次静态角标检测(`library.check_logos`);第三方搬运的官方画面标成重复。

用法:
    python -m scripts.backfill_media_info              # 补全部素材池
    python -m scripts.backfill_media_info 小米YU7       # 只补指定车型
    python -m scripts.backfill_media_info --relogo     # 角标检测规则改了:全部源重测

幂等:已有宽高的条目不重读。跑之前先停掉正在重建素材池的服务。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def backfill(subject: str, *, relogo: bool = False) -> str:
    from app.pipeline import library, media_check

    lib = library.Library.load(subject)
    probed: dict[str, dict] = {}
    changed = 0
    for item in lib.items:
        if not Path(item.path).is_file():
            continue
        if item.kind == "video" and item.sharp < 0 and item.length >= library.MIN_TAG_SECONDS:
            # 本地三帧体检:黑场 / 纯色淡场标成跳过(视觉模型打过标签的也一样,它常放过黑场)
            stats = media_check.frame_stats(item.path, item.start, item.end)
            if stats:
                item.sharp = media_check.median_sharpness(stats)
                item.skipped = item.skipped or media_check.frame_verdict(stats)
                changed += 1
        if item.width and item.height:
            continue
        if item.kind == "video":
            # 同一个窗文件切出来的段共用一次 ffprobe
            if item.path not in probed:
                probed[item.path] = media_check.probe_video(item.path)
            info = probed[item.path]
            if not info:
                continue
            for k, v in info.items():
                setattr(item, k, v)
        else:
            item.width, item.height = media_check.image_size(item.path)
            if not item.width:
                continue
        changed += 1
    for src in lib.sources:
        if src.status == "done" and (relogo or not src.logo_checked):
            library.check_logos(lib, src)
            changed += 1
    changed += library.mark_official_duplicates(lib)
    if changed:
        lib.save()
    return f"{subject}: 补了 {changed} 条 —— {summary(lib)}"


def summary(lib) -> str:
    videos = [i for i in lib.items if i.kind == "video"]
    stills = [i for i in lib.items if i.kind == "still"]
    codecs = Counter(i.vcodec or "?" for i in videos)
    low_res = [i for i in videos if i.low_res]
    low_br = [i for i in videos if i.low_bitrate]
    small = [i for i in stills if i.small_still]
    bad = Counter(i.skipped for i in videos if i.skipped.startswith(("frame_", "logo_", "dup_")))
    logos = sum(1 for s in lib.sources if s.logo_boxes)
    return (f"视频 {len(videos)} 段(编码 {dict(codecs)};低清 {len(low_res)} 段 "
            f"{sum(i.length for i in low_res):.0f} 秒,码率偏低 {len(low_br)} 段,"
            f"跳过 {dict(bad)},带角标的源 {logos}/{len(lib.sources)});"
            f"静图 {len(stills)} 张(长边 < 1280 的 {len(small)} 张)")


def all_subjects() -> list[str]:
    from app import config

    root = config.DATA_DIR / "library"
    out = []
    for f in sorted(root.glob("*/library.json")):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8"))["subject"])
        except (OSError, ValueError, KeyError):
            continue
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for s in args or all_subjects():
        print(backfill(s, relogo="--relogo" in sys.argv))
