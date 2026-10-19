"""一致性链测试(计划 §6.1 / §4.4)。

长视频最大的质量风险是跨镜头跳变,这套机制是唯一的防线,
因此"链构建正确"和"尾帧缺失时优雅降级"都必须被锁住。
"""
from __future__ import annotations

import pytest

from app.pipeline.chains import (
    Chain,
    assign_to_shots,
    build_chains,
    chains_for,
    concurrency_limit,
    degraded_shots,
    expected_chain_count,
    plan_first_frames,
    ready_shots,
)


# --------------------------------------------------------------------------
# 链构建
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "shot_count,group_size,expected_chains,expected_first_chain_len",
    [(10, 3, 4, 3), (15, 3, 5, 3), (20, 3, 7, 3), (30, 3, 10, 3)],
)
def test_build_chains_partitions_shots(
    shot_count, group_size, expected_chains, expected_first_chain_len
):
    chains = build_chains(shot_count, group_size)
    assert len(chains) == expected_chains
    assert len(chains[0].members) == expected_first_chain_len
    # 所有镜头恰好出现一次,不重不漏
    flat = [i for c in chains for i in c.members]
    assert sorted(flat) == list(range(shot_count))


def test_build_chains_last_chain_may_be_shorter():
    chains = build_chains(10, 3)
    assert [len(c.members) for c in chains] == [3, 3, 3, 1]


def test_expected_chain_count_matches_build():
    for n in (10, 15, 20, 30):
        assert expected_chain_count(n, 3) == len(build_chains(n, 3))


def test_chains_for_handles_non_contiguous_indices():
    chains = chains_for([0, 1, 5, 6, 7], group_size=3)
    flat = [i for c in chains for i in c.members]
    assert flat == [0, 1, 5, 6, 7]


def test_assign_to_shots_records_chain_and_position():
    chains = build_chains(10, 3)
    mapping = assign_to_shots(chains)
    assert mapping[0] == (0, 0)
    assert mapping[1] == (0, 1)
    assert mapping[2] == (0, 2)
    assert mapping[3] == (1, 0)
    assert mapping[9] == (3, 0)


# --------------------------------------------------------------------------
# 调度:链内串行,链间并行
# --------------------------------------------------------------------------
def test_only_chain_heads_are_ready_initially():
    chains = build_chains(10, 3)
    ready = ready_shots(chains, done=set())
    # 每条链只放行链首
    assert sorted(ready) == [0, 3, 6, 9]


def test_next_shot_unlocks_only_after_predecessor_done():
    chains = build_chains(10, 3)
    ready = ready_shots(chains, done={0})
    assert 1 in ready
    assert 0 not in ready


def test_chain_serialises_beyond_one_concurrent_shot():
    chains = build_chains(10, 3)
    ready = ready_shots(chains, done={0, 3})
    # 链 0、1 的后继(1、4)解锁;链 2、3 的链首(6、9)本就可用。
    # 每条链仍然只放行一个 —— 这就是"链内串行"的体现。
    assert sorted(ready) == [1, 4, 6, 9]
    assert len(ready) == len(chains)


def test_ready_shots_respects_approval_set():
    chains = build_chains(10, 3)
    approved = {0, 1, 2, 5}
    ready = ready_shots(chains, done=set(), approved=approved)
    # 链 0 只放行 0;链 1 的链首 3 不在批准列表
    assert 0 in ready
    assert 3 not in ready


def test_completed_shots_are_not_requeued_but_unlock_successors():
    chains = build_chains(10, 3)
    ready = ready_shots(chains, done={0, 3, 6, 9})
    # 已完成的 0/3/6/9 绝不能被重新提交;
    # 它们各自解锁链内下一个镜头 1/4/7(9 是尾链唯一成员,无后继)。
    assert sorted(ready) == [1, 4, 7]
    assert not ({0, 3, 6, 9} & set(ready))


def test_concurrency_capped_by_chain_count():
    # 2 条链时,并发上限不应超过 2
    assert concurrency_limit(2, configured=8) == 2
    # 链数很多时,用配置上限
    assert concurrency_limit(20, configured=8) == 8


# --------------------------------------------------------------------------
# 首帧规划
# --------------------------------------------------------------------------
def test_plan_first_frames_marks_chain_heads_without_source():
    chains = build_chains(10, 3)
    plan = plan_first_frames(chains, "final", "/tmp/proj")
    # 链首没有前驱可继承
    for head in (0, 3, 6, 9):
        assert plan[head]["source"] != "chain_last_frame"
        assert plan[head]["path"] is None
    # 链中镜头指向前一镜的尾帧
    assert plan[1]["source"] == "chain_last_frame"
    assert plan[1]["from_shot"] == 0
    assert "final_000_last" in str(plan[1]["path"])


def test_plan_first_frames_uses_reference_set_when_available():
    chains = build_chains(10, 3)
    plan = plan_first_frames(chains, "final", "/tmp/proj", ref_limit=5)
    assert plan[0]["source"] == "reference_set"


def test_degraded_shots_identify_missing_last_frames():
    chains = build_chains(10, 3)
    plan = plan_first_frames(chains, "final", "/tmp/proj")
    # 假设 1、2 的首帧抽取成功,其余失败
    resolved = {1: "x.jpg", 2: "y.jpg"}
    degraded = degraded_shots(plan, resolved)
    expected = sorted({i for i, p in plan.items() if p["source"] == "chain_last_frame"} - {1, 2})
    assert degraded == expected
    # 链首永远不该被标记为 degraded
    assert 0 not in degraded and 3 not in degraded


def test_degraded_empty_when_all_first_frames_resolved():
    chains = build_chains(10, 3)
    plan = plan_first_frames(chains, "final", "/tmp/proj")
    resolved = {i: "f.jpg" for i, p in plan.items() if p["source"] == "chain_last_frame"}
    assert degraded_shots(plan, resolved) == []


# --------------------------------------------------------------------------
# Chain 自身的导航
# --------------------------------------------------------------------------
def test_chain_next_after_and_prev_before():
    chain = Chain(chain_id=0, members=[0, 1, 2])
    assert chain.head == 0
    assert chain.tail == 2
    assert chain.next_after(0) == 1
    assert chain.next_after(2) is None
    assert chain.prev_before(1) == 0
    assert chain.prev_before(0) is None


def test_extract_last_frame_returns_none_for_missing_file():
    """ffmpeg 不可用或文件缺失时必须优雅降级,而不是抛异常阻断流水线。"""
    from app.pipeline.chains import extract_last_frame

    assert extract_last_frame("/nonexistent/video.mp4", "/tmp/out.jpg") is None


# --------------------------------------------------------------------------
# SHOT_LIMIT:只生成前 N 个镜头的冒烟测试模式
# --------------------------------------------------------------------------
def test_stage_complete_respects_shot_limit(monkeypatch):
    """SHOT_LIMIT 之外的镜头不参与阶段完成判定。

    否则冒烟测试跑到最后会永远卡在 DRAFT_RENDERING:limit 内的镜头都成了,
    limit 外的还停在 pending,而 stage_complete 一直返回 False。
    """
    from app import config
    from app.pipeline import generate

    rows = [
        {"idx": 0, "status": "succeeded"},
        {"idx": 1, "status": "succeeded"},
        {"idx": 2, "status": "succeeded"},
        {"idx": 3, "status": "pending"},
        {"idx": 4, "status": "pending"},
    ]
    monkeypatch.setattr(generate.db, "get_shots", lambda *a, **k: rows)

    monkeypatch.setattr(config, "SHOT_LIMIT", 0)
    assert generate.stage_complete("p", "draft") is False, "不限时应要求全部完成"

    monkeypatch.setattr(config, "SHOT_LIMIT", 3)
    assert generate.stage_complete("p", "draft") is True, "限 3 个时前 3 个完成即算完成"

    rows[1]["status"] = "running"
    assert generate.stage_complete("p", "draft") is False, "限内未完成仍应判未完成"


def test_stage_complete_false_when_no_shots(monkeypatch):
    from app import config
    from app.pipeline import generate

    monkeypatch.setattr(generate.db, "get_shots", lambda *a, **k: [])
    monkeypatch.setattr(config, "SHOT_LIMIT", 3)
    assert generate.stage_complete("p", "draft") is False


def test_shot_limit_keeps_only_in_limit_chain_heads(monkeypatch):
    """5 个镜头分两条链(链首 0 和 3);限 3 个镜头时只有链首 0 能提交。

    这正是 submit_ready 里的过滤逻辑:按镜头序号过滤候选,
    而不是"取前 N 个候选"——后者会把第 3 镜头的链首排除在外,
    又留下永远等不到前驱的镜头。
    """
    from app import config
    from app.pipeline import chains

    heads = chains.ready_shots(chains.chains_for([0, 1, 2, 3, 4]), set(), {0, 1, 2, 3, 4})
    assert heads == [0, 3], "两条链各自的首镜头都应可提交"

    monkeypatch.setattr(config, "SHOT_LIMIT", 3)
    kept = [c for c in heads if c < config.SHOT_LIMIT]
    assert kept == [0]


# --------------------------------------------------------------------------
# 模型能力降级:草稿档模型不吃参考图
# --------------------------------------------------------------------------
def test_hailuo_does_not_support_reference_images():
    """草稿档用 Hailuo-2.3,它不吃多模态参考图 —— 这是既定契约。"""
    from app.providers import video

    assert video.supports_reference_images("MiniMax-Hailuo-2.3") is False
    assert video.supports_reference_images("MiniMax-H3") is True


def test_reference_images_dropped_for_models_without_support():
    """回归:曾无条件把参考图传给草稿档,导致每个链首都以
    "模型不支持参考图"直接失败。正确行为是摘掉参考图继续跑 ——
    草稿档的一致性靠链内尾帧接首帧,不靠参考图集。
    """
    from app.providers import video

    ref_urls = ["http://x/1.jpg", "http://x/2.jpg"]

    def keep(refs, model):
        return refs if video.supports_reference_images(model) else None

    assert keep(ref_urls, "MiniMax-H3") == ref_urls
    assert keep(ref_urls, "MiniMax-Hailuo-2.3") is None

    # 摘掉参考图之后必须真的能通过校验
    video.validate("MiniMax-Hailuo-2.3", duration=6, resolution="768P",
                   ref_count=0, has_last_frame=False)


def test_hailuo_ignores_ratio_h3_honours_it():
    """Hailuo-2.3 不支持画幅(固定 16:9),H3 支持。

    实测:传了 ratio="9:16" 的 Hailuo 任务返回的是 1366x768 横屏。
    因此提交时不能给它传 ratio,否则日志会显示"设了 9:16"而实际不是。
    """
    from app.providers import video

    assert video.supports_ratio("MiniMax-H3") is True
    assert video.supports_ratio("MiniMax-H3-Max") is True
    assert video.supports_ratio("MiniMax-Hailuo-2.3") is False


def test_submit_omits_ratio_for_models_that_ignore_it(monkeypatch):
    """提交时按能力决定要不要带 ratio 参数。"""
    from app.providers import video

    seen: list[dict] = []

    def fake_call(tool, args, **kwargs):
        seen.append(args)
        return {"task_id": "t-1"}

    monkeypatch.setattr(video.cli, "call", fake_call)

    video.submit(model="MiniMax-Hailuo-2.3", prompt="p", duration=6,
                 resolution="768P", ratio="9:16")
    assert "ratio" not in seen[-1], "Hailuo 不支持画幅,不该传 ratio"

    video.submit(model="MiniMax-H3", prompt="p", duration=6,
                 resolution="768P", ratio="9:16")
    assert seen[-1]["ratio"] == "9:16", "H3 必须带上 ratio"


def test_first_frame_sends_reference_type(monkeypatch):
    """首帧必须配 reference_type=first_frame。

    schema:input_image 要配合 reference_type 才是关键帧模式;
    漏传会让上游把图当普通参考图,一致性链的首尾衔接悄悄失效。
    """
    from app.providers import video

    seen: list[dict] = []
    monkeypatch.setattr(video.cli, "call",
                        lambda tool, args, **kw: (seen.append(args), {"task_id": "t"})[1])

    video.submit(model="MiniMax-H3", prompt="p", duration=6,
                 resolution="768P", first_frame="https://x/f.jpg")
    assert seen[-1]["input_image"] == "https://x/f.jpg"
    assert seen[-1]["reference_type"] == "first_frame"

    # 无图时不应出现 reference_type
    video.submit(model="MiniMax-H3", prompt="p", duration=6, resolution="768P")
    assert "reference_type" not in seen[-1]
    assert "input_image" not in seen[-1]


def test_first_frame_alone_must_not_require_last_frame_support():
    """回归:链内第二镜传的是**首**帧,却被按"要支持尾帧"校验而失败。

    Hailuo-2.3 只支持首帧、不支持尾帧;一旦把首帧误报成尾帧,
    每个非链首镜头都会以"不支持尾帧输入"直接失败。
    """
    from app.providers import video

    # 只有首帧 → 校验必须通过
    video.validate("MiniMax-Hailuo-2.3", duration=6, resolution="768P",
                   ref_count=0, has_last_frame=False)
    # 真的传了尾帧才应该被拒
    with pytest.raises(video.VideoValidationError):
        video.validate("MiniMax-Hailuo-2.3", duration=6, resolution="768P",
                       ref_count=0, has_last_frame=True)


def test_first_and_last_frame_omits_reference_type():
    """首尾帧同时给出时不再塞 reference_type —— 枚举里没有这个值。"""
    from app.providers import video

    seen: list[dict] = []
    monkeypatch_targets = video.cli
    original = monkeypatch_targets.call
    monkeypatch_targets.call = (  # type: ignore[method-assign]
        lambda tool, args, **kw: (seen.append(args), {"task_id": "t"})[1])
    try:
        video.submit(model="MiniMax-H3", prompt="p", duration=6, resolution="768P",
                     first_frame="https://x/f.jpg", last_frame="https://x/l.jpg")
        assert seen[-1]["input_image"] == "https://x/f.jpg"
        assert seen[-1]["last_frame_image"] == "https://x/l.jpg"
        assert "reference_type" not in seen[-1]
    finally:
        monkeypatch_targets.call = original  # type: ignore[method-assign]


def test_hailuo_rejects_image_input_in_practice():
    """实测:MiniMax-Hailuo-2.3 收不了图生视频。

    schema 写着支持 input_image + reference_type=first_frame,但实际提交
    一律 connector_tool_error(latency_ms=0);纯文生视频正常。
    因此能力位以实测为准,不能照抄文档 —— 否则每个带图的镜头都会白等
    一次失败,而且报错完全看不出是图的问题。
    """
    from app.providers import video

    assert video.supports_image_input("MiniMax-Hailuo-2.3") is False
    assert video.supports_image_input("MiniMax-H3") is True


def test_submit_drops_first_frame_when_model_cannot_use_images(monkeypatch):
    """不支持图生的模型:submit 必须把首帧丢掉,而不是递上去换一个失败。"""
    from app.providers import video

    seen: list[dict] = []
    monkeypatch.setattr(video.cli, "call",
                        lambda tool, args, **kw: (seen.append(args), {"task_id": "t"})[1])

    video.submit(model="MiniMax-Hailuo-2.3", prompt="p", duration=6,
                 resolution="768P", first_frame="https://x/f.jpg")
    assert "input_image" not in seen[-1]
    assert "reference_type" not in seen[-1]

    video.submit(model="MiniMax-H3", prompt="p", duration=6,
                 resolution="768P", first_frame="https://x/f.jpg")
    assert seen[-1]["input_image"] == "https://x/f.jpg"
