"""DeepSeek 连通性与结构化输出自检(会发起一次真实的文本 API 调用,成本极低)。

用法:
    python -m tests.verify_llm
"""
from __future__ import annotations

import sys
import time

from app.llm.base import LLMNotConfigured
from app.llm.deepseek import DeepSeekProvider


def main() -> int:
    provider = DeepSeekProvider()
    if not provider.available():
        print("✗ 未配置 DEEPSEEK_API_KEY")
        return 1

    print(f"模型:{provider.model}")
    print(f"端点:{provider.base_url}/chat/completions")

    t0 = time.time()
    try:
        data = provider.structured(
            "你是一个只输出 JSON 的助手。",
            '请输出这个 JSON:{"ok": true, "n": 3, "note": "连通性测试"}',
            required_keys=["ok", "n"],
            temperature=0.1,
            max_retries=1,
        )
    except LLMNotConfigured as exc:
        print(f"✗ 未配置:{exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 调用失败:{type(exc).__name__}: {exc}")
        return 1

    elapsed = time.time() - t0
    print(f"✓ 结构化输出成功({elapsed:.1f}s):{data}")

    # 用真实解说脚本提示词做一次冒烟:确认能处理中文长文本
    #
    # 这里直接调用生产路径 script_gen.generate(),而不是自己拼 SYSTEM.format。
    # 自行拼串的版本在 SYSTEM 新增占位符后会静默炸成 KeyError('theme_intent'),
    # 而真实流水线是好的 —— 冒烟测试反而变成噪声。
    print("\n正在用真实解说脚本提示词做冒烟测试 ...")
    t1 = time.time()
    try:
        from app.categories import load_profile
        from app.models import Fact
        from app.pipeline.script_gen import compliance_lint, generate

        profile_facts = [
            Fact(id="f_1", claim="汉 EV 2026 款 CLTC 续航 702 公里",
                 source_tool="thsifind.get_security_indicators",
                 scope="product", confidence="high"),
            Fact(id="f_2", claim="百公里加速 3.8 秒",
                 source_tool="thsifind.search_news",
                 scope="product", confidence="high"),
        ]
        profile = load_profile("auto")
        theme = profile.theme_spec()
        plan = profile.plan_for(60, theme.name)
        segs, title = generate(
            subject="比亚迪汉 EV 2026",
            profile=profile,
            facts=profile_facts,
            plan=plan,
            duration=60,
            llm=provider,
            theme=theme,
        )
        print(f"✓ 脚本生成成功({time.time() - t1:.1f}s),标题:{title!r},{len(segs)} 段")
        for s in segs:
            print(f"    [{s.section}] {s.text[:60]}  refs={s.fact_refs}")
        issues = compliance_lint(segs, profile, profile_facts)
        errs = [i for i in issues if i.is_error]
        print(f"  合规检查:{'✓ 无 error' if not errs else f'✗ {len(errs)} 条 error'}")
        for e in errs[:3]:
            print(f"    {e}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 脚本冒烟失败:{type(exc).__name__}: {exc}")
        return 1

    print("\n✓ LLM 链路全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
