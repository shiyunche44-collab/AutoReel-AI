"""一致性链编排(计划 §4.4)。

10-30 个镜头各自独立生成时,最容易崩的是跨镜头一致性:同一辆车在不同镜头里
颜色变了、场景从展台跳到街头。解法是把镜头串成链:

    镜头 N 的尾帧  ──►  作为镜头 N+1 的首帧(input_image)

这样 N+1 天然承接 N 的构图与运动状态。

⚠️ 由此产生的**调度约束**:链内镜头有数据依赖,必须串行;链与链之间无关,可并行。
因此实际并行度 = 链数,而非镜头数。这是长视频墙钟时间的主要决定因素。

降级原则:尾帧抽取失败**不阻断流水线**,该镜头标记 `consistency=degraded`,
仅依赖全片共享参考图集,并在 UI 上明示。
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .. import config


@dataclass
class Chain:
    chain_id: int
    #: 链内镜头序号(从 0 开始),按生成顺序排列
    members: list[int] = field(default_factory=list)

    @property
    def head(self) -> int:
        return self.members[0]

    @property
    def tail(self) -> int:
        return self.members[-1]

    def next_after(self, shot_index: int) -> int | None:
        pos = self.members.index(shot_index)
        return self.members[pos + 1] if pos + 1 < len(self.members) else None

    def prev_before(self, shot_index: int) -> int | None:
        pos = self.members.index(shot_index)
        return self.members[pos - 1] if pos > 0 else None


def build_chains(shot_count: int, group_size: int | None = None) -> list[Chain]:
    """把 N 个镜头切成若干条链,每条链 group_size 个镜头。

    链内串行、链间并行。示例:10 镜头 / 每链 3 个 → 4 条链(3+3+3+1)。
    """
    size = max(1, group_size or config.CHAIN_GROUP_SIZE)
    chains: list[Chain] = []
    for start in range(0, shot_count, size):
        members = list(range(start, min(start + size, shot_count)))
        chains.append(Chain(chain_id=len(chains), members=members))
    return chains


def chains_for(shot_indices: Sequence[int], group_size: int | None = None) -> list[Chain]:
    """按实际镜头序号(可能不连续,例如被跳过的镜头)建链。"""
    size = max(1, group_size or config.CHAIN_GROUP_SIZE)
    ordered = sorted(shot_indices)
    return [
        Chain(chain_id=i // size, members=ordered[i : i + size])
        for i in range(0, len(ordered), size)
    ]


def assign_to_shots(
    chains: Sequence[Chain],
) -> dict[int, tuple[int, int]]:
    """返回 {镜头序号: (chain_id, 链内位置)}。"""
    mapping: dict[int, tuple[int, int]] = {}
    for chain in chains:
        for pos, idx in enumerate(chain.members):
            mapping[idx] = (chain.chain_id, pos)
    return mapping


def ready_shots(
    chains: Sequence[Chain],
    done: set[int],
    approved: set[int] | None = None,
) -> list[int]:
    """返回当前**可以立即提交**的镜头序号。

    一个镜头可提交,当且仅当:
    - 它已被用户批准(GATE_B),且
    - 它是链首(没有前驱),或它的前驱已经生成完成。
    这条规则由调度器直接消费,避免"并行提交整条链"导致一致性被破坏。
    """
    eligible = approved if approved is not None else set()
    result: list[int] = []
    for chain in chains:
        for pos, idx in enumerate(chain.members):
            if eligible and idx not in eligible:
                continue
            if idx in done:
                continue
            if pos == 0 or chain.members[pos - 1] in done:
                result.append(idx)
                break  # 每条链同时只放行一个,保持串行
    return result


def concurrency_limit(num_chains: int, configured: int | None = None) -> int:
    """有效并发 = min(配置上限, 链数)。

    链内串行意味着实际并行度受链数限制,配置再高也不会更快。
    """
    limit = configured or config.GEN_MAX_CONCURRENCY
    return max(1, min(limit, max(1, num_chains)))


# --------------------------------------------------------------------------
# 尾帧抽取
# --------------------------------------------------------------------------
def extract_last_frame(
    video_path: str | Path,
    dest_path: str | Path,
    *,
    from_end: float = 0.08,
) -> Path | None:
    """抽取视频尾帧作为下一个镜头的首帧。

    用 `-sseof` 从结尾倒数若干秒取一帧,避免正好落在最后一行编码残片上。
    任何失败都返回 None,由调用方降级处理。
    """
    if not config.FFMPEG_PATH:
        return None
    src = Path(video_path)
    dst = Path(dest_path)
    if not src.is_file():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(config.FFMPEG_PATH), "-y", "-v", "error",
        "-sseof", f"-{from_end}",
        "-i", str(src),
        "-frames:v", "1",
        "-q:v", "2",
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        return None
    return dst


def plan_first_frames(
    chains: Sequence[Chain],
    stage: str,
    project_dir: Path,
    *,
    ref_limit: int = 0,
) -> dict[int, dict[str, object]]:
    """为每个镜头规划首帧来源。

    返回 {镜头序号: {"path": Path|None, "source": "chain_last_frame"|"reference_set"|"none"}}

    - 链首:没有前驱可用,首帧由参考图集决定(通常为 None,走纯文生视频 + 参考图)。
    - 链中非首:首帧 = 前一镜头的尾帧路径(该文件在生成时按需抽取)。
    """
    plan: dict[int, dict[str, object]] = {}
    frames_dir = Path(project_dir) / "frames"
    for chain in chains:
        for pos, idx in enumerate(chain.members):
            if pos == 0:
                plan[idx] = {
                    "path": None,
                    "source": "reference_set" if ref_limit else "none",
                }
            else:
                prev = chain.members[pos - 1]
                plan[idx] = {
                    "path": frames_dir / f"{stage}_{prev:03d}_last.jpg",
                    "source": "chain_last_frame",
                    "from_shot": prev,
                }
    return plan


def materialize_first_frames(
    plan: dict[int, dict[str, object]],
    chains: Sequence[Chain],
    completed: dict[int, Path],
    stage: str,
) -> dict[int, Path]:
    """在生成过程中真正落盘尾帧,返回 {镜头序号: 首帧路径}。

    `completed` 是 {已生成镜头序号: 视频路径}。对链中镜头调用:先抽前驱尾帧,
    抽不出来就标记降级(返回结果里不含该镜头,上层据此设 consistency=degraded)。
    """
    resolved: dict[int, Path] = {}
    for chain in chains:
        for pos, idx in enumerate(chain.members):
            if pos == 0:
                continue
            prev = chain.members[pos - 1]
            source_video = completed.get(prev)
            if not source_video:
                continue
            target = Path(str(plan[idx]["path"]))
            frame = extract_last_frame(source_video, target)
            if frame:
                resolved[idx] = frame
    return resolved


def degraded_shots(plan: dict[int, dict[str, object]], resolved: dict[int, Path]) -> list[int]:
    """需要标记为 consistency=degraded 的镜头(期望有首帧但实际没有)。"""
    expected = {i for i, p in plan.items() if p.get("source") == "chain_last_frame"}
    return sorted(expected - set(resolved))


def expected_chain_count(shot_count: int, group_size: int | None = None) -> int:
    size = max(1, group_size or config.CHAIN_GROUP_SIZE)
    return math.ceil(shot_count / size)
