"""网络探针:抖音创作者中心有没有「逐秒留存曲线」,接口长什么样(手动跑,只读)。

    python tests/probe_douyin_retention.py <作品id> [--out 目录]

只打开页面、看页面自己发的请求,不点任何会改数据的按钮。做三件事:
1. 打开「作品管理」,把作品卡片上和「数据 / 分析」有关的链接、按钮文字列出来;
2. 依次打开几个候选的作品详情 / 作品分析页(以及卡片上找到的分析链接);
3. 每个页面里,凡是 JSON 响应带「留存 / retention / bounce / watch / 跳出 / 曲线」这类字样的,
   打印接口路径和顶层结构,完整响应写进 `--out` 目录,拿来定 `fetch_work_retention` 的解析。

实测结论(2026-10-26,两条已发作品):
- **没有逐秒留存曲线**。作品详情只给汇总:平均播放占比、5s 完播率、2s 跳出率、平均播放时长
  (`/janus/douyin/creator/data/item_analysis/overview` 等),数据中心同样只有汇总。
- 详情页「进度条分析」接口 `PROGRESS_API` 能用(播放 <100 也返回 200,页面自己只在 ≥100 才调):
  `jump_forward`(跳过率)/ `jump_backward`(回看率)各一串 `{key, value}`,
  key 是播放进度秒数(每段的中点,步长随片长变:231 秒的片 8 秒一段 29 个点,短一些的片 5 秒一段),
  value 是整数比率;
  `video_data.frames` 是同样时间点的截帧 `{cut_time, frame_url}`。
  数据没出来时页面写「数据处理中,请于发布半小时后查看」;没人拖进度条时全 0。
- 所以「逐拍掉人」用跳过率代替:某一拍被拖过去的多 = 这一拍没留住人(`fetch_work_progress`)。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publish import browser  # noqa: E402

CREATOR_MANAGE = "https://creator.douyin.com/creator-micro/content/manage"
CANDIDATES = [
    "https://creator.douyin.com/creator-micro/work-management/work-detail/{id}",
    "https://creator.douyin.com/creator-micro/data/stats/video-detail?item_id={id}",
    "https://creator.douyin.com/creator-micro/data-center/content/detail?item_id={id}",
]
#: 作品详情页「进度条分析」(跳过率 / 回看率随播放进度)的接口,从页面脚本里找到的
PROGRESS_API = "/janus/douyin/creator/bff/data/progress/analysis/v2?item_id={id}"
_FETCH_JS = """async (url) => {
  const r = await fetch(url, {credentials: 'include', headers: {'agw-js-conv': 'str'}});
  return {status: r.status, text: await r.text()};
}"""
KEYWORDS = re.compile(r"retention|bounce|watch|drop|curve|leave|留存|跳出|流失|完播|观看时长", re.I)

#: 作品卡片上和数据有关的入口
_DUMP_LINKS_JS = """() => {
  const out = [];
  document.querySelectorAll('a, button, [role=button], span').forEach((el) => {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 12 || !/数据|分析|详情|表现/.test(t)) return;
    if (out.length < 40) out.push({tag: el.tagName, text: t, href: el.getAttribute('href') || ''});
  });
  return out;
}"""


def _shape(data: object, depth: int = 0) -> object:
    """JSON 的结构:dict 列 key,list 只看第一个元素和长度。"""
    if depth > 3:
        return "…"
    if isinstance(data, dict):
        return {k: _shape(v, depth + 1) for k, v in list(data.items())[:25]}
    if isinstance(data, list):
        return [f"len={len(data)}", _shape(data[0], depth + 1)] if data else []
    return type(data).__name__


def probe(aweme_id: str, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    hits: list[dict] = []
    seen: list[str] = []

    def on_response(resp) -> None:  # noqa: ANN001
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        path = resp.url.split("?")[0]
        seen.append(path)
        try:
            text = resp.text()
        except Exception:  # noqa: BLE001
            return
        if not text.startswith("{") or not KEYWORDS.search(text):
            return
        name = f"{len(hits):02d}_{path.rsplit('/', 1)[-1][:40]}.json"
        (out / name).write_text(text, encoding="utf-8")
        hits.append({"url": resp.url[:200], "file": name, "bytes": len(text)})
        try:
            print(f"\n-- 命中 {path}({len(text)} 字节)→ {name}")
            print(json.dumps(_shape(json.loads(text)), ensure_ascii=False)[:1500])
        except ValueError:
            pass

    try:
        with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
            session.page.on("response", on_response)
            print("===== 作品管理 =====")
            session.goto(CREATOR_MANAGE, wait_ms=8000)
            if "扫码登录" in session.page_text():
                print("登录失效:先去分发台扫码")
                return 1
            links = session.page.evaluate(_DUMP_LINKS_JS)
            print(json.dumps(links, ensure_ascii=False, indent=1)[:2500])
            urls = [u.format(id=aweme_id) for u in CANDIDATES]
            urls += ["https://creator.douyin.com" + x["href"] for x in links
                     if x["href"].startswith("/") and aweme_id in x["href"]]
            for url in urls:
                print(f"\n===== {url} =====")
                session.goto(url, wait_ms=9000)
                print("落地:", session.page.url[:160])
                print("页面字样:", re.sub(r"\s+", " ", session.page_text())[:400])
            # 作品详情页的「进度条分析」接口:页面自己只在播放 ≥100 时才调,这里直接 GET 一次
            session.goto(CANDIDATES[0].format(id=aweme_id), wait_ms=6000)
            got = session.page.evaluate(_FETCH_JS, PROGRESS_API.format(id=aweme_id))
            print(f"\n===== 进度条分析 {got.get('status')} =====")
            text = got.get("text") or ""
            (out / "progress.json").write_text(text, encoding="utf-8")
            try:
                print(json.dumps(_shape(json.loads(text)), ensure_ascii=False)[:2500])
            except ValueError:
                print(text[:800])
    except browser.BrowserUnavailable as exc:
        print("浏览器起不来:", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"探针失败:{type(exc).__name__}: {exc}")
        return 1
    (out / "_seen.txt").write_text("\n".join(dict.fromkeys(seen)), encoding="utf-8")
    print(f"\n共 {len(hits)} 个疑似留存接口,所有 XHR 路径见 {out / '_seen.txt'}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    target = Path(args[args.index("--out") + 1]) if "--out" in args else Path("probe_retention_out")
    sys.exit(probe(args[0], target))
