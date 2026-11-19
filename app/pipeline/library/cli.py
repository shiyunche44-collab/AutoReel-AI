"""命令行子命令。"""
from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence

from ...categories import load_profile
from .builder import build
from .licensing import STATUS_LABEL, STATUSES, license_of_source, set_license
from .licensing import summary as license_summary
from .model import MAX_AUTO_SOURCES, Library, normalise_url, slug
from .sheet import contact_sheet
from .sources import add_images, add_links


def _license_cmd(args: argparse.Namespace) -> int:
    lib = Library.load(args.subject)
    if args.source:
        ids = set()
        for ref in args.source:
            hit = next((s for s in lib.sources if s.id == ref or s.url in (ref, normalise_url(ref))), None)
            if hit is None:
                print(f"没有这个源:{ref}")
                return 1
            ids.add(hit.id)
        expires = None
        if args.expires is not None:
            expires = 0.0 if args.expires.lower() == "none" else time.mktime(
                time.strptime(args.expires, "%Y-%m-%d"))
        n = set_license(lib, ids, status=args.status, scope=args.scope, expires=expires,
                        proof=args.proof, review=args.review, reviewer=args.reviewer, event=args.event)
        lib.save()
        print(f"已更新 {n} 个源")
    for s in lib.sources:
        lic = license_of_source(s)
        exp = time.strftime("%Y-%m-%d", time.localtime(lic.expires)) if lic.expires else "无期限"
        flag = "已驳回" if lic.rejected else (s.review_status and "已通过") or "未审"
        print(f"{s.id}  {STATUS_LABEL[lic.status]:<5} {flag:<4} 到期 {exp}  {s.platform}  {s.title[:30]}  {s.url}")
    print(json.dumps(license_summary(lib), ensure_ascii=False))
    return 0


def _cookies_check_cmd(args: argparse.Namespace) -> int:
    """探一条官方号投稿(只解析不下载),报 B 站 / YouTube 肯给的最高清晰度。"""
    from . import footage_health

    urls = {k: v for k, v in (("bilibili", args.bilibili), ("youtube", args.youtube)) if v}
    data = footage_health.check(urls)
    print(f"cookies:{data.get('config_note') or '未知'}")
    for platform in footage_health.PLATFORMS:
        row = (data.get("platforms") or {}).get(platform) or {}
        height, err = int(row.get("height") or 0), str(row.get("error") or "")
        print(f"  {platform:<9} 最高 {f'{height}p' if height else '—':<6} "
              f"{err[:120] if err else row.get('url', '')}")
    eff = data.get("effective")
    print("结论:" + ("cookies 已生效,能拿到高清" if eff is True else
                    "只拿得到低清:cookies 未配置或未生效" if eff is False else "没探成功,不知道"))
    share = footage_health.low_res_share()
    if share["video_segments"]:
        print(f"素材池低清段:{share['low_res_segments']} / {share['video_segments']}"
              f"({share['low_res_ratio'] * 100:.0f}%)")
    return 0


def _upgrade_cmd(args: argparse.Namespace) -> int:
    """重拉高清:默认排后台任务(服务的 worker 跑);--now 在本进程里跑。"""
    from ... import jobs
    from .builder import BUILD_JOB
    from .upgrade import all_subjects, candidates, job_key, submit_upgrade, upgrade

    if not args.all and not args.subject:
        print("要给车型,或者 --all")
        return 2
    subjects = all_subjects() if args.all else [args.subject]
    if args.dry_run:
        for name in subjects:
            picked = candidates(Library.load(name))
            if picked:
                print(f"{name}:{len(picked)} 个源要重拉")
                for s in picked[:args.limit or None]:
                    print(f"  {s.id} {'官方' if s.official else '    '} {s.platform:<8} {s.title[:40]}")
        return 0
    if not args.now:
        job = submit_upgrade(args.subject, all_=args.all, limit=args.limit, no_llm=args.no_llm)
        print(f"已排任务 #{job}(在服务的后台跑,进度看 /api/health 的 jobs)" if job else
              f"{job_key(args.subject, args.all)} 已经在排 / 在跑")
        return 0
    # 本进程直接跑:和服务里的建池 / 重拉不互斥(池锁只管进程内),撞上就别跑
    busy = [n for n in subjects if jobs.active(BUILD_JOB, slug(n))]
    if busy:
        print(f"这些车的素材池正在建,等建完再 --now:{', '.join(busy)}")
        return 1
    llm = None
    if not args.no_llm:
        from ..orchestrator import _llm

        llm = _llm()
    errors: list[str] = []
    out = upgrade(subjects, llm=llm, limit=args.limit, errors=errors,
                  log_fn=lambda m: print(m, flush=True))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def _backfill_cmd(args: argparse.Namespace) -> int:
    from .upgrade import all_subjects, backfill, job_key, submit_backfill

    if not args.all and not args.subject:
        print("要给车型,或者 --all")
        return 2
    if not args.now:
        job = submit_backfill(args.subject, all_=args.all, shots=args.shots)
        print(f"已排任务 #{job}" if job else f"{job_key(args.subject, args.all)} 已经在排 / 在跑")
        return 0
    llm = None
    if args.shots:
        from ..orchestrator import _llm

        llm = _llm()
    errors: list[str] = []
    for name in (all_subjects() if args.all else [args.subject]):
        got = backfill(name, shots=args.shots, llm=llm, errors=errors)
        print(f"{name}:动感 {got['motion']} 段,景别 {got['shots']} 段", flush=True)
    for e in errors[:15]:
        print(" -", e)
    return 0


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------
def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.pipeline.library",
                                     description="素材池:构建、加链接、查看")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="增量构建(发现源、下载、切段、打标签、抓官图)")
    b.add_argument("subject")
    b.add_argument("--brand", default="")
    b.add_argument("--category", default="", help="品类 profile 名,默认 DEFAULT_CATEGORY")
    b.add_argument("--budget", type=float, default=None, help="时长预算(秒),0 不限")
    b.add_argument("--max-sources", type=int, default=MAX_AUTO_SOURCES)
    b.add_argument("--no-llm", action="store_true", help="不调 LLM(只认视觉模型自己的 JSON)")
    a = sub.add_parser("add", help="手动加视频链接")
    a.add_argument("subject")
    a.add_argument("urls", nargs="+")
    ai = sub.add_parser("add-images", help="手动加图:图片地址、本地图片,或商品详情页链接")
    ai.add_argument("subject")
    ai.add_argument("refs", nargs="+")
    ai.add_argument("--category", default="", help="品类 profile 名(新池子要给)")
    rt = sub.add_parser("retag", help="清掉已有标签,下次 build 重新打(改了打标签规则之后用)")
    rt.add_argument("subject")
    s = sub.add_parser("show", help="看统计")
    s.add_argument("subject")
    lc = sub.add_parser("license", help="核验授权:列出各源的授权状态,或给源写授权 / 审核")
    lc.add_argument("subject")
    lc.add_argument("--source", action="append", default=[], metavar="ID_OR_URL",
                    help="源 id 或地址,可重复;不给就只列出")
    lc.add_argument("--status", choices=STATUSES)
    lc.add_argument("--scope", help="允许的用途,逗号分隔(background);空串 = 不限")
    lc.add_argument("--expires", help="到期日 YYYY-MM-DD;none = 无期限")
    lc.add_argument("--proof", help="凭证引用:合同 / 邮件 / 授权页面地址")
    lc.add_argument("--review", choices=("approved", "rejected"))
    lc.add_argument("--reviewer", default="")
    lc.add_argument("--event", help="所属发布会(名称 / 日期)")
    sh = sub.add_parser("sheet", help="导出联系表图片")
    sh.add_argument("subject")
    sh.add_argument("out", nargs="?", default="")
    sh.add_argument("--usable", action="store_true")
    cc = sub.add_parser("cookies-check",
                        help="cookies 体检:探一条官方号投稿(不下载),报 B 站 / YouTube 能给的最高清晰度")
    cc.add_argument("--bilibili", default="", help="指定探哪条 B 站视频(默认取池子里的官方号投稿)")
    cc.add_argument("--youtube", default="", help="指定探哪条 YouTube 视频")
    up = sub.add_parser("upgrade", help="重拉高清:低清段占多数的源按 1080p 重下、切段、打标签,换掉老段")
    up.add_argument("subject", nargs="?", default="")
    up.add_argument("--all", action="store_true", help="所有池子")
    up.add_argument("--limit", type=int, default=0, help="最多重拉几个源,0 不限")
    up.add_argument("--dry-run", action="store_true", help="只列出要重拉的源")
    up.add_argument("--now", action="store_true", help="在本进程里跑(默认排后台任务)")
    up.add_argument("--no-llm", action="store_true", help="新段只继承老段的标签,不调视觉模型")
    bf = sub.add_parser("backfill", help="回填:补测动感(不调模型);--shots 再补打景别 / 大镜头分")
    bf.add_argument("subject", nargs="?", default="")
    bf.add_argument("--all", action="store_true", help="所有池子")
    bf.add_argument("--shots", action="store_true", help="补打景别 / 大镜头分(花视觉调用)")
    bf.add_argument("--now", action="store_true", help="在本进程里跑(默认排后台任务)")
    args = parser.parse_args(argv)

    if args.cmd == "cookies-check":
        return _cookies_check_cmd(args)
    if args.cmd == "upgrade":
        return _upgrade_cmd(args)
    if args.cmd == "backfill":
        return _backfill_cmd(args)

    if args.cmd == "add":
        added = add_links(args.subject, args.urls)
        print(f"新加 {len(added)} 个:" + ", ".join(added))
        return 0
    if args.cmd == "add-images":
        errs: list[str] = []
        n = add_images(args.subject, args.refs, category=args.category, errors=errs)
        print(f"新加 {n} 张图,下次 build 时打标签")
        for e in errs:
            print(" -", e)
        return 0
    if args.cmd == "retag":
        lib = Library.load(args.subject)
        n = 0
        for i in lib.items:
            if i.tagged:
                i.tagged, n = False, n + 1
        lib.save()
        print(f"已清掉 {n} 个标签,运行 build 重新打")
        return 0
    if args.cmd == "show":
        lib = Library.load(args.subject)
        print(json.dumps({**lib.stats(), "license": license_summary(lib)}, ensure_ascii=False, indent=1))
        return 0
    if args.cmd == "license":
        return _license_cmd(args)
    if args.cmd == "sheet":
        out = args.out or str(Library.dir_for(args.subject) / "sheet.png")
        got = contact_sheet(args.subject, out, usable_only=args.usable)
        print(got or "池子里还没有帧")
        return 0

    from ...categories import default_category

    llm = None
    if not args.no_llm:
        from ..orchestrator import _llm

        llm = _llm()
    errors: list[str] = []
    profile = load_profile(args.category or default_category())
    stats = build(args.subject, profile=profile, brand=args.brand, llm=llm,
                  budget=args.budget, max_sources=args.max_sources, errors=errors,
                  log=lambda m: print(m, flush=True))
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    if errors:
        print(f"\n{len(errors)} 条错误(前 15 条):")
        for e in errors[:15]:
            print(" -", e)
    return 0
