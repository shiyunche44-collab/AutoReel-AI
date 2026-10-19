"""⑧ 质检门禁。

两道检查:
1. **画面质检**:`videos_understand` 对每个成片镜头提问 —— 是否符合分镜描述、
   有无畸变、有无错误文字。
2. **旁白质检**:native 模式下用 ASR 复核发音;tts 模式抽查数字读法。

质检不追求全自动判定(视频模型的自我评估不可靠),而是给出**结构化的风险提示**,
让门禁 B 的人工确认有据可依。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .. import config, db
from ..models import ShotSpec
from ..providers import audio, cli

VIDEO_QA_TOOL = "connector__matrix__videos_understand"

SYSTEM_PROMPT = (
    "You are a strict visual QA reviewer for short-form vertical video. "
    "Answer only from what is visible in the clip."
)

VIDEO_PROMPT = """Review this clip and answer in English:
1. Does the subject (a car) stay visually consistent in body colour and styling throughout?
2. Is there any visible distortion: warped body panels, extra or missing wheels, melted text, deformed hands, duplicated objects?
3. Is any readable text garbled or misspelled?
4. Does the camera movement look natural, or is there jitter or morphing?
Answer each point in one short sentence, then end with a line "VERDICT: PASS" or "VERDICT: FAIL"."""


@dataclass
class ShotQA:
    idx: int
    verdict: str = "unknown"          # pass / fail / unknown
    findings: list[str] = field(default_factory=list)
    raw: str = ""
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "verdict": self.verdict,
            "findings": self.findings,
            "raw": self.raw[:800],
            "checked_at": self.checked_at,
        }


def _flatten(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return "\n".join(_flatten(i) for i in result)
    if isinstance(result, dict):
        for key in ("content", "text", "result", "answer", "message"):
            if key in result:
                return _flatten(result[key])
        return "\n".join(f"{k}: {v}" for k, v in result.items() if isinstance(v, str))
    return str(result)


def review_shot(spec: ShotSpec, video_path: str | Path) -> ShotQA:
    """对单个成片镜头做视觉质检。失败返回 unknown,不抛异常。"""
    qa = ShotQA(idx=spec.idx)
    path = Path(video_path)
    if not path.is_file():
        qa.verdict = "unknown"
        qa.findings.append("文件不存在")
        return qa
    try:
        url = cli.upload_temp_url(path)
    except Exception as exc:  # noqa: BLE001
        qa.verdict = "unknown"
        qa.findings.append(f"上传失败:{exc}")
        return qa

    try:
        result = cli.call(
            VIDEO_QA_TOOL,
            {"video_info": [{"url": url}], "prompt": f"{SYSTEM_PROMPT}\n\n{VIDEO_PROMPT}"},
            timeout=240,
        )
        text = _flatten(result)
    except Exception as exc:  # noqa: BLE001
        qa.verdict = "unknown"
        qa.findings.append(f"质检调用失败:{exc}")
        return qa

    qa.raw = text
    upper = text.upper()
    if "VERDICT: PASS" in upper:
        qa.verdict = "pass"
    elif "VERDICT: FAIL" in upper:
        qa.verdict = "fail"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and "." in stripped[:3]:
            qa.findings.append(stripped)
    return qa


def review_cross_shot_consistency(
    shot_paths: Sequence[tuple[int, str]], sample: int = 5
) -> dict[str, Any]:
    """跨镜头一致性抽检:抽取若干镜头同框比较车身颜色与场景。"""
    picked = list(shot_paths)[:sample]
    if len(picked) < 2:
        return {"checked": 0, "note": "镜头不足,跳过跨镜头一致性检查"}
    try:
        urls = [cli.upload_temp_url(p) for _, p in picked if Path(p).is_file()]
    except Exception as exc:  # noqa: BLE001
        return {"checked": 0, "note": f"上传失败:{exc}"}
    if len(urls) < 2:
        return {"checked": 0, "note": "可用镜头不足"}

    try:
        result = cli.call(
            VIDEO_QA_TOOL,
            {
                "video_info": [{"url": u} for u in urls],
                "prompt": (
                    "These clips come from ONE video and must show the same car. "
                    "Compare them: is the body colour, wheel design and interior "
                    "consistent across all clips? Answer in one sentence, then "
                    '"VERDICT: CONSISTENT" or "VERDICT: INCONSISTENT".'
                ),
            },
            timeout=300,
        )
        text = _flatten(result)
    except Exception as exc:  # noqa: BLE001
        return {"checked": len(urls), "note": f"调用失败:{exc}"}

    upper = text.upper()
    return {
        "checked": len(urls),
        "shots": [i for i, _ in picked],
        "consistent": "VERDICT: CONSISTENT" in upper,
        "verdict": "consistent" if "VERDICT: CONSISTENT" in upper else "inconsistent",
        "raw": text[:600],
    }


def audit_narration(audio_path: str | Path, expected_text: str) -> dict[str, Any]:
    """旁白抽检:ASR 结果与原文比对,重点看数字。"""
    transcript = audio.transcribe(audio_path)
    if not transcript:
        return {"ok": False, "note": "ASR 未返回内容"}
    expected_digits = [c for c in expected_text if c.isdigit()]
    transcript_digits = [c for c in transcript if c.isdigit()]
    return {
        "ok": True,
        "expected": expected_text[:120],
        "transcript": transcript[:200],
        "digit_match": expected_digits == transcript_digits,
        "expected_digits": "".join(expected_digits),
        "transcript_digits": "".join(transcript_digits),
    }


def run(project_id: str, specs: Sequence[ShotSpec]) -> dict[str, Any]:
    """对成片档的全部镜头执行质检,产出 qa.json。"""
    # 数据卡片不进视觉质检:画面里本来就没有车,问"是不是目标车型"只会误判;
    # 它的数字在分镜阶段已经逐行核对过事实。
    rows = [
        r for r in db.get_shots(project_id, "final")
        if r["status"] == "succeeded" and r.get("final_path") and r.get("consistency") != "card"
    ]
    spec_map = {s.idx: s for s in specs}
    results: list[ShotQA] = []
    for row in rows:
        spec = spec_map.get(row["idx"])
        if spec is None:
            continue
        results.append(review_shot(spec, row["final_path"]))

    consistency = review_cross_shot_consistency(
        [(r["idx"], r["final_path"]) for r in rows]
    )

    report = {
        "project_id": project_id,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "shots": [r.to_dict() for r in results],
        "pass_count": sum(1 for r in results if r.verdict == "pass"),
        "fail_count": sum(1 for r in results if r.verdict == "fail"),
        "unknown_count": sum(1 for r in results if r.verdict == "unknown"),
        "cross_shot_consistency": consistency,
    }
    db.write_json(project_id, "qa.json", report)
    return report


def blocking_failures(report: dict[str, Any]) -> list[str]:
    """列出应当阻止进入合成阶段的镜头问题。"""
    reasons: list[str] = []
    for shot in report.get("shots", []):
        if shot.get("verdict") == "fail":
            reasons.append(f"镜头 {shot['idx']} 质检未通过")
    consistency = report.get("cross_shot_consistency") or {}
    if consistency.get("verdict") == "inconsistent":
        reasons.append("跨镜头一致性抽检未通过(车身颜色/内饰在不同镜头间不一致)")
    return reasons
