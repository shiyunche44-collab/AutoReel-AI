"""直连后端真实调用自检(要联网,消耗少量 M Plan 额度)。

默认只跑便宜的几项:音色列表、网络搜索、图片理解、一句 TTS。

用法:
    python -m tests.verify_direct
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.providers import audio, cli, direct, facts, images  # noqa: E402


def step(name: str, fn) -> bool:
    t = time.time()
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - 自检要把每一项的失败都打印出来
        print(f"✗ {name}:{type(exc).__name__}: {getattr(exc, 'message', exc)}")
        return False
    print(f"✓ {name}({time.time() - t:.1f}s):{detail}")
    return True


def main() -> int:
    if not cli.is_direct():
        print(f"当前后端是 {config.CONNECTOR_BACKEND!r}。请在 .env 填 MINIMAX_PLAN_KEY,"
              "并设 CONNECTOR_BACKEND=direct(或留空且不配 MCODE_TOOLS)。")
        return 2
    print(f"后端:direct → {config.MINIMAX_API_HOST}\n")
    ok = True
    def voices():
        n = len(audio.list_voices())  # list_voices 吞掉错误返回 [],0 个就当失败
        if not n:
            raise RuntimeError("没拿到系统音色(Key 无效或未生效?)")
        return f"{n} 个系统音色"
    ok &= step("音色列表", voices)

    def search():
        doc = facts.ifind_product_news("比亚迪汉 EV 2026 款")
        if not doc.ok:
            raise RuntimeError(doc.error)
        return f"{len(doc.text)} 字 · 来源记为 {doc.tool}"
    ok &= step("网络搜索(事实来源)", search)

    def vlm():
        ref = next((p for p in sorted((ROOT / "data" / "projects").glob("*/refs/ref_00.jpg"))), None)
        if ref is None:
            raise RuntimeError("没有现成的参考图可测")
        text = images.describe([cli.upload_temp_url(ref)], "图里是什么品牌什么车型?一句话回答。")
        if not text:
            raise RuntimeError("返回为空")
        return text[:80]
    ok &= step("图片理解", vlm)

    def tts():
        with tempfile.TemporaryDirectory() as tmp:
            assets = audio.synthesize_batch(["这是一条直连配音测试,续航七百零二公里。"],
                                            output_dir=tmp)
            if not assets:
                raise RuntimeError("没有产出音频")
            return f"{assets[0].local_path.stat().st_size} 字节 mp3({config.TTS_MODEL})"
    ok &= step("语音合成", tts)

    print("\n全部通过" if ok else "\n有失败项,见上")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
