"""命令行子命令。"""
from __future__ import annotations

import argparse
import json
from typing import Sequence
from ...categories import load_profile

import time
from .licensing import STATUSES, STATUS_LABEL, license_of_source, set_license
from .licensing import summary as license_summary
from .model import Library, MAX_AUTO_SOURCES, normalise_url
from .sources import add_images, add_links
from .builder import build
from .sheet import contact_sheet


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
    args = parser.parse_args(argv)

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
