"""真机探针:逐镜头取材实跑一遍,看每一级来源在你的网络下能不能用。

不走流水线、不写数据库,只在 data/_probe_media/ 下落地素材。用法:

    python -m tests.probe_media_search                 # 默认 "比亚迪汉 EV",4 个镜头
    python -m tests.probe_media_search "小米SU7" --shots 6
    python -m tests.probe_media_search "比亚迪汉 EV" --sources-only   # 只测检索,不下载

输出分两段:
1. 各来源**检索**结果数(官方号 / 汽车之家 / 懂车帝 / 官网 / B 站 / YouTube /
   抖音 / Bing 图片 / 免费图库)—— 0 条的来源就是你这边不通或没配 Key;
2. 逐镜头**落地**结果:每个镜头最后用了哪一级、哪个来源,落地文件在哪。

YouTube 不通先检查 .env 的 YTDLP_PROXY;抖音下载失败多半是缺 YTDLP_COOKIES。
"""
from __future__ import annotations

import argparse
import json
import time

from app import config
from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import clip_resolver, media_query
from app.providers import official, platform_video, stock, webassets

DEFAULT_SHOTS = [
    ("开场钩子", "车辆低机位驶入画面", "exterior_45"),
    ("核心卖点", "中控大屏与方向盘特写", "interior"),
    ("核心卖点", "尾灯点亮特写", "rear"),
    ("行业", "车展现场人流", "industry"),
    ("卖点深挖", "轮毂与刹车卡钳细节", "detail"),
    ("购买建议", "城市夜景车流", "industry"),
]


def _timed(label: str, fn):
    t = time.time()
    try:
        out = fn()
        n = len(out[0]) + len(out[1]) if isinstance(out, tuple) else len(out)
        print(f"  {label:<28} {n:>3} 条   {time.time() - t:5.1f}s")
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<28} 失败:{type(exc).__name__}: {str(exc)[:80]}")
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("subject", nargs="?", default="比亚迪汉 EV")
    ap.add_argument("--shots", type=int, default=4)
    ap.add_argument("--sources-only", action="store_true")
    args = ap.parse_args()

    profile = load_profile("auto")
    subject = args.subject
    brand_key, spec = official.detect_brand(subject, profile.official_sites)
    aliases = official.brand_aliases(brand_key, spec) if brand_key else []
    print(f"主体:{subject}   品牌:{brand_key or '未识别'}   平台:{config.MEDIA_PLATFORMS}")
    print(f"代理:{config.YTDLP_PROXY or '无'}   cookie:{config.YTDLP_COOKIES or '无'}   "
          f"\n图库 Key:Pixabay {'有' if config.PIXABAY_API_KEY else '无'} · "
          f"Pexels {'有' if config.PEXELS_API_KEY else '无'} · "
          f"Coverr {'有' if config.COVERR_API_KEY else '无'} · "
          f"Unsplash {'有' if config.UNSPLASH_ACCESS_KEY else '无'} · "
          f"Openverse {'开' if config.USE_OPENVERSE else '关'}\n")

    print("① 各来源检索")
    errors: list[str] = []
    off = _timed("官方号(平台)", lambda: official.platform_official(subject, aliases,
                                                                   errors=errors))
    for h in off[:3]:
        print(f"      [{h.platform}] {h.uploader} · {h.title[:40]}")
    _timed("汽车之家官图", lambda: official.autohome_images(subject, errors=errors))
    _timed("懂车帝官图", lambda: official.dongchedi_images(subject, errors=errors))
    if spec:
        _timed("车企官网 视频+图片", lambda: official.site_media(subject, spec, errors=errors))
    for plat in ("bilibili", "youtube", "douyin"):
        hits = _timed(f"{plat} 搜索", lambda plat=plat: platform_video._SEARCHERS[plat](
            subject, limit=5))
        for h in hits[:2]:
            print(f"      {h.uploader} · {h.title[:40]} · {h.duration:.0f}s")
    _timed("Bing 图片", lambda: webassets.bing_images(f"{subject} 外观", limit=10))
    _timed("免费图库 视频", lambda: stock.search_videos("auto show crowd", errors=errors))
    _timed("免费图库 图片", lambda: stock.search_photos("city night traffic",
                                                             errors=errors))
    for e in errors[:10]:
        print(f"    ! {e}")
    if args.sources_only:
        return

    print("\n② 逐镜头落地(真实下载 + 转码,每镜头可能要几十秒)")
    specs = [ShotSpec(idx=i, section=sec, visual_zh=vis, angle=ang)
             for i, (sec, vis, ang) in enumerate(DEFAULT_SHOTS[:args.shots])]
    queries, _ = media_query.build(subject, profile, specs)
    out_dir = config.DATA_DIR / "_probe_media"
    t = time.time()
    assignment, report = clip_resolver.collect(
        subject=subject, profile=profile, specs=specs, project_dir=out_dir,
        queries=queries, qc=False,
    )
    for s in specs:
        sel = report["selected"].get(str(s.idx))
        q = next(q for q in queries if q.idx == s.idx)
        if sel:
            print(f"  镜头 {s.idx} [{q.kind}] {q.query_zh:<18} → {sel['tier']:<15} "
                  f"{sel['origin']:<20} {sel['path']}")
        else:
            print(f"  镜头 {s.idx} [{q.kind}] {q.query_zh:<18} → AI 生成(没找到真实素材)")
    print(f"\n  用时 {time.time() - t:.0f}s。{report.get('summary', '')}")
    print("  来源尝试:", json.dumps(report["sources_tried"], ensure_ascii=False))
    for e in report["errors"][:15]:
        print(f"    ! {e}")


if __name__ == "__main__":
    main()
