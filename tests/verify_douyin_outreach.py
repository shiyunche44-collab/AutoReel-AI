"""真实验证:用真实登录态把四个动作各跑一遍(手动跑,会真的在抖音上发生)。

    python tests/verify_douyin_outreach.py [作品id] [作者sec_uid]

**这个脚本会真的评论 / 点赞 / 分享 / 关注**,不是模拟。每跑一次都在抖音上留痕,
所以默认只跑一次、只针对一条作品。跑完请到抖音 App 里核对。

评论区文案会先过 `app/team/activator.py` 里的硬规则(禁数字、禁引流、禁绝对化、
必须说到对方作品里的词),不合格就不发 —— 和角色用的是同一套规则。

注意:**点赞是开关**。如果这条作品我们已经点过赞,脚本会跳过(不会点成取消)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.publish import browser  # noqa: E402
from app.publish.platforms import douyin_outreach as act  # noqa: E402

DEFAULT_VIDEO = "7676325036050877746"
DEFAULT_SEC_UID = "MS4wLjABAAAAwJmCgs5ML5aHHo_rqnifHrmu2Sl0cvaOIhAQkmRJA9o"
#: 抖音弹层里拿不到标题(标题在「详情」页签里),这里手写 —— 生产环境标题来自
#: 对标分析员抓到的 `benchmark_posts`。
DEFAULT_TITLE = "「万字说明书」车长超5米25，续航超千公里，比亚迪大汉来了"


def main() -> int:
    aweme_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VIDEO
    sec_uid = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SEC_UID
    title = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_TITLE
    from app.team import activator

    with browser.BrowserSession("douyin", headless=True, lock_timeout=90) as session:
        info = act.open_video(session, aweme_id)
        print("作品:", aweme_id, "| 抓到的信息:", info)

        # 1) 点赞(页面已经开好了)
        try:
            print("点赞:", act.like_open(session))
        except Exception as exc:  # noqa: BLE001
            print("点赞失败:", type(exc).__name__, exc)

        # 2) 评论:文案先过硬规则,合格才发
        cars = [w for w in ("小米YU7", "比亚迪汉", "比亚迪", "YU7", "大汉", "大唐") if w in title]
        llm = None
        try:
            from app.pipeline import orchestrator

            llm = orchestrator._llm()
        except Exception as exc:  # noqa: BLE001
            print("没有 LLM,用模板兜底:", exc)
        text = activator.pick_text(title, llm, cars)
        problems = activator.check_text(text, title) if text else ["没挑出文案"]
        print("候选文案:", text or "(无)", "| 校验:", problems or "通过")
        if text and not problems:
            try:
                act.comment_open(session, text)
                print("评论:已发出")
            except Exception as exc:  # noqa: BLE001
                print("评论失败:", type(exc).__name__, exc)
        else:
            print("评论:跳过(没有合格的文案)")

        # 3) 分享(复制链接)
        try:
            print("分享:", act.share_open(session))
        except Exception as exc:  # noqa: BLE001
            print("分享失败:", type(exc).__name__, exc)

        # 4) 关注(去作者主页点)
        try:
            print("关注:", act.follow_author(session, sec_uid))
        except Exception as exc:  # noqa: BLE001
            print("关注失败:", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
