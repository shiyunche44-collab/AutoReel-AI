"""实测批量 TTS 工具对 emotion 与 SSML 的支持(开发期用,联网且烧少量额度)。

文档查不到这两项,所以直接试:每个候选各合成一句短文本,看成功与否、时长、转写。
结果写 data/_probe/tts_prosody.json。运行:python tests/probe_tts_prosody.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.pipeline import timeline, voice_qa  # noqa: E402
from app.providers import audio  # noqa: E402

OUT = ROOT / "data" / "_probe"
SENTENCE = "这台车的续航真的很扎实"
EMOTIONS = ["happy", "sad", "angry", "neutral", "excited", "storytelling", "news",
            "advertising", "tension", "comfort"]
SSML = [
    ("ssml_speak", "<speak>续航<break time=\"300ms\"/>六百四十三公里</speak>"),
    ("ssml_sayas", "<speak>续航<say-as interpret-as=\"cardinal\">643</say-as>公里</speak>"),
    ("tag_break", "续航<break time=\"300ms\"/>六百四十三公里"),
]
NUMBERS = [("raw", "续航643公里,2024年上市,800V平台"),
           ("normalized", timeline.normalize_for_tts("续航643公里,2024年上市,800V平台"))]


def run_one(name: str, text: str, emotion: str | None, out_dir: Path, asr) -> dict:
    row: dict = {"name": name, "text": text, "emotion": emotion or ""}
    try:
        assets = audio.synthesize_batch([text], emotion=emotion, output_dir=out_dir, file_prefix=name)
    except Exception as exc:  # noqa: BLE001 - 探针要记下失败原因
        row["error"] = str(exc)[:200]
        return row
    if not assets:
        row["error"] = "无成功项"
        return row
    path = Path(assets[0].local_path)
    row["seconds"] = round(timeline.probe_safe(path, default=0.0), 2)
    if asr:
        try:
            row["heard"] = asr(path)
        except Exception as exc:  # noqa: BLE001
            row["heard_error"] = str(exc)[:100]
    return row


def main() -> None:
    out_dir = OUT / "tts_prosody"
    out_dir.mkdir(parents=True, exist_ok=True)
    asr = voice_qa.load_asr()
    rows = [run_one("base", SENTENCE, None, out_dir, asr)]
    rows += [run_one(f"emo_{e}", SENTENCE, e, out_dir, asr) for e in EMOTIONS]
    rows += [run_one(n, t, None, out_dir, asr) for n, t in SSML]
    rows += [run_one(f"num_{n}", t, None, out_dir, asr) for n, t in NUMBERS]
    (OUT / "tts_prosody.json").write_text(
        json.dumps({"voice": audio.DEFAULT_VOICE_ID, "tag": config.tts_tag(), "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(r["name"], "ERR " + r["error"] if "error" in r else f'{r["seconds"]}s {r.get("heard", "")}')


if __name__ == "__main__":
    main()
