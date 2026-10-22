"""文案生成测试。

文案要同时喂五个平台,规则各不相同(B站标题 80 字、小红书 20 字、抖音话题
内嵌正文、YouTube 标签单独一个字段……)。这些裁剪逻辑是纯函数,所以能直接测。
"""
from __future__ import annotations

from app.publish import metadata as md
from app.publish.registry import LANDSCAPE, PORTRAIT, PLATFORMS, rule_for


def _ctx(**kw) -> md.ContentContext:
    base = dict(
        subject="小米YU7",
        title="小米YU7 值不值得买",
        thesis="同价位里,它的补能效率最扎实。",
        segments=["小米YU7 是今年最热门的 25 万级纯电 SUV。",
                  "续航 835 公里,实际跑高速大概掉到 640。",
                  "结论:如果你家有家充,它是这个价位最稳的选择。"],
        topics=["续航", "补能"],
        disclaimers=["数据来源:公开信息与官方渠道,仅供参考,实际以购车时为准"],
        source_note="画面素材:品牌官方宣传片与官方图片,仅作车型展示",
        category="auto_long",
        subject_label="车型",
    )
    base.update(kw)
    return md.ContentContext(**base)


# --------------------------------------------------------------------------
# 标题裁剪
# --------------------------------------------------------------------------
def test_short_title_untouched():
    assert md.clamp_title("小米YU7", 80) == "小米YU7"


def test_long_title_is_cut_with_ellipsis():
    out = md.clamp_title("很长的标题" * 30, 20)
    assert len(out) <= 20
    assert out.endswith("…")


def test_title_not_cut_inside_a_word():
    """英文标题不能从单词中间切开 —— 切了观众看到的是半句话。"""
    out = md.clamp_title("Xiaomi YU7 electric SUV review", 14)
    assert out.endswith("…")
    assert " " in out
    # 切点落在空格上:最后一个词是完整的
    body = out[:-1].rstrip()
    assert body.split()[-1] in {"Xiaomi", "YU7", "electric", "SUV", "review"}


def test_soft_limit_wins_over_hard_limit():
    """软上限更小就按软上限裁 —— 超长标题反正会被折叠。"""
    out = md.clamp_title("小米YU7 值不值得买 详细评测", 80, 10)
    assert len(out) <= 10


def test_hard_limit_never_exceeded():
    long = "标题" * 200
    for rule in PLATFORMS.values():
        assert len(md.clamp_title(long, rule.title_max, rule.title_soft)) <= rule.title_max


def test_whitespace_is_collapsed():
    assert md.clamp_title("  小米   YU7  ", 80) == "小米 YU7"


# --------------------------------------------------------------------------
# 标签
# --------------------------------------------------------------------------
def test_tags_start_with_subject():
    tags = md.build_tags(_ctx(), 10)
    assert tags[0] == "小米YU7"


def test_tags_respect_limit():
    assert len(md.build_tags(_ctx(), 3)) == 3


def test_tags_have_no_duplicates():
    tags = md.build_tags(_ctx(), 10)
    assert len(tags) == len(set(tags))


def test_tags_are_stripped_of_spaces_and_commas():
    tags = md.build_tags(_ctx(subject="小 米,YU7"), 10)
    assert "小 米,YU7" not in tags
    assert "小米YU7" in tags


def test_category_tags_are_used():
    assert "汽车解说" in md.build_tags(_ctx(category="auto_long"), 10)
    assert "母婴好物" in md.build_tags(_ctx(category="baby_long"), 10)


# --------------------------------------------------------------------------
# 简介
# --------------------------------------------------------------------------
def test_description_contains_disclaimer():
    out = md.build_description(_ctx(), rule_for("bilibili"))
    assert "数据来源" in out


def test_description_contains_source_note():
    out = md.build_description(_ctx(), rule_for("bilibili"))
    assert "品牌官方宣传片" in out


def test_description_uses_only_first_segments():
    ctx = _ctx(segments=[f"第{i}段" for i in range(10)])
    out = md.build_description(ctx, rule_for("bilibili"))
    assert "第0段" in out and "第2段" in out
    assert "第9段" not in out


def test_description_respects_platform_limit():
    ctx = _ctx(segments=["很长的一段说明" * 200])
    for rule in PLATFORMS.values():
        assert len(md.build_description(ctx, rule)) <= rule.desc_max


# --------------------------------------------------------------------------
# 话题
# --------------------------------------------------------------------------
def test_hashtags_format():
    assert md.hashtags(["汽车", "测评"]) == "#汽车 #测评"


def test_hashtags_skip_empty():
    assert md.hashtags(["", "  ", "汽车"]) == "#汽车"


def test_hashtags_limit():
    assert len(md.hashtags(["a", "b", "c", "d"], limit=2).split()) == 2


# --------------------------------------------------------------------------
# 整包
# --------------------------------------------------------------------------
def test_metadata_for_every_platform_has_required_fields():
    ctx = _ctx()
    for key in PLATFORMS:
        out = md.metadata_for(key, ctx)
        assert out["title"], f"{key} 缺标题"
        assert out["description"], f"{key} 缺简介"
        assert isinstance(out["tags"], list)
        assert len(out["tags"]) <= rule_for(key).tag_max


def test_metadata_respects_platform_aspect():
    assert rule_for("bilibili").aspect == LANDSCAPE
    assert rule_for("douyin").aspect == PORTRAIT
    assert rule_for("xiaohongshu").aspect == PORTRAIT
    assert rule_for("channels").aspect == PORTRAIT
    assert rule_for("youtube").aspect == LANDSCAPE
