"""平台指标解析测试。

用样例 JSON 测解析,不联网、不用账号。**缺失即 None** 是这里最要紧的一条:
0 和"没权限"在看板上完全是两回事,拿 0 顶替会把"没权限"画成"零播放"。
"""
from __future__ import annotations

from app.metrics.collector import (
    parse_bilibili_stat,
    parse_count,
    parse_douyin_statistics,
    parse_metric_text,
    parse_youtube_channel,
    parse_youtube_statistics,
)


# --------------------------------------------------------------------------
# B站
# --------------------------------------------------------------------------
BILI = {"code": 0, "data": {"view": 12345, "danmaku": 67, "reply": 89,
                             "favorite": 12, "coin": 34, "share": 5, "like": 210}}


def test_bilibili_stat_maps_every_field():
    out = parse_bilibili_stat(BILI)
    assert out["views"] == 12345
    assert out["danmaku"] == 67
    assert out["comments"] == 89
    assert out["favorites"] == 12
    assert out["coins"] == 34
    assert out["shares"] == 5
    assert out["likes"] == 210


def test_bilibili_stat_missing_field_is_none():
    out = parse_bilibili_stat({"code": 0, "data": {"view": 10}})
    assert out["views"] == 10
    assert out["likes"] is None
    assert out["coins"] is None


def test_bilibili_stat_empty_payload():
    out = parse_bilibili_stat({})
    assert out["views"] is None


# --------------------------------------------------------------------------
# YouTube
# --------------------------------------------------------------------------
YT = {"items": [{"statistics": {"viewCount": "900", "likeCount": "45",
                                "commentCount": "6"}}]}


def test_youtube_statistics_maps_counts():
    out = parse_youtube_statistics(YT)
    assert out["views"] == 900
    assert out["likes"] == 45
    assert out["comments"] == 6


def test_youtube_private_metrics_are_none_not_zero():
    """收藏 / 转发 / 投币在 YouTube 不公开 —— None,不是 0。"""
    out = parse_youtube_statistics(YT)
    assert out["favorites"] is None
    assert out["shares"] is None
    assert out["coins"] is None


def test_youtube_channel_subscribers():
    out = parse_youtube_channel({"items": [{"statistics": {"subscriberCount": "1234"}}]})
    assert out["followers"] == 1234


def test_youtube_empty_items():
    assert parse_youtube_statistics({"items": []}) == {}


# --------------------------------------------------------------------------
# 抖音
# --------------------------------------------------------------------------
DOUYIN = {"data": {"list": [{"statistics": {"digg_count": 200, "comment_count": 100,
                                            "play_count": 300, "share_count": 10,
                                            "forward_count": 10, "download_count": 3}}]}}


def test_douyin_statistics_maps_counts():
    out = parse_douyin_statistics(DOUYIN)
    assert out["views"] == 300
    assert out["likes"] == 200
    assert out["comments"] == 100
    assert out["shares"] == 10


def test_douyin_favorites_is_none():
    """抖音开放接口不给收藏数。"""
    assert parse_douyin_statistics(DOUYIN)["favorites"] is None


def test_douyin_empty_list():
    assert parse_douyin_statistics({"data": {"list": []}}) == {}


# --------------------------------------------------------------------------
# 数量文字(给只能看后台页面的平台用)
# --------------------------------------------------------------------------
def test_parse_count_plain():
    assert parse_count("1234") == 1234


def test_parse_count_units():
    assert parse_count("1.2万") == 12000
    assert parse_count("3亿") == 300_000_000
    assert parse_count("5k") == 5000


def test_parse_count_unrecognisable():
    assert parse_count("暂无") is None
    assert parse_count("") is None


def test_metric_text_extracts_labelled_numbers():
    text = "作品数据\n播放量 1.2万\n点赞 356\n评论 42\n收藏 18\n分享 7\n粉丝 2.3万"
    out = parse_metric_text(text)
    assert out["views"] == 12000
    assert out["likes"] == 356
    assert out["comments"] == 42
    assert out["favorites"] == 18
    assert out["shares"] == 7
    assert out["followers"] == 23000


def test_metric_text_missing_labels_are_none():
    out = parse_metric_text("这是一段没有指标的文字")
    assert all(v is None for v in out.values())


def test_metric_text_handles_colon_form():
    out = parse_metric_text("播放量：888")
    assert out["views"] == 888
