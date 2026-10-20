"""事实来源层的回归测试。

重点锁两件事:
1. 空载荷不能被当成成功素材(这曾让整条流水线"4 来源全绿"却 0 条产品事实);
2. 产品参数检索必须用产品向查询串,不能和公司财经混在一起。
"""
from __future__ import annotations

import json

import pytest

from app.providers import facts


# --------------------------------------------------------------------------
# _flatten:各种返回形态
# --------------------------------------------------------------------------
def test_flatten_double_nested_data():
    """同花顺的典型双层信封。"""
    raw = {
        "code": 1, "msg": "success",
        "data": {"data": '[{"资讯标题":"标题","资讯内容":"正文内容足够长用于通过阈值"}]'},
    }
    out = facts._flatten(raw)
    assert "资讯标题" in out
    assert "正文内容" in out
    assert "code" not in out


def test_flatten_empty_payload_returns_empty_not_key_dump():
    """data 键存在但值为空 → 返回空串。

    回归点:旧实现会退化成 "data: ",让 invoke() 误判为成功。
    """
    assert facts._flatten({"code": 1, "data": None}) == ""
    assert facts._flatten({"code": 1, "data": {"data": ""}}) == ""
    assert facts._flatten({"data": []}) == ""


def test_flatten_deep_news_envelope_is_not_truncated():
    """回归:同花顺新闻的真实嵌套有 7 层,曾被深度守卫整段截成空串。

    完整形态: list[{"type":"text","text":"<json>"}] → 信封 → {"data":"<json数组>"} → 文章
    旧的深度上限 6 会让每篇文章都返回 "" → 空串 → 退化成 "data: " →
    来源"成功"但零素材。这条测试直接照抄该形态。
    """
    articles = json.dumps([
        {"资讯标题": "2026 款比亚迪汉上市", "资讯内容": "续航 705 公里", "日期": "2026-05-01",
         "URL": "https://example.invalid/a"},
    ], ensure_ascii=False)
    inner = json.dumps({"code": 1, "msg": "success", "subCode": None, "subMsg": None,
                        "data": {"data": articles}}, ensure_ascii=False)
    payload = [{"type": "text", "text": inner}]

    out = facts._flatten(payload)
    assert "2026 款比亚迪汉上市" in out, "深度守卫把文章内容截断了"
    assert "705" in out
    assert "2026-05-01" in out


def test_flatten_survives_pathological_self_nesting():
    """防病态嵌套的守卫本身不能被去掉。"""
    obj: dict = {}
    cur = obj
    for _ in range(200):
        cur["data"] = {}
        cur = cur["data"]
    facts._flatten(obj)  # 不抛异常即通过


def test_flatten_unknown_keys_still_dumps_pairs():
    """没有已知正文键时,键值对拼接是正常行为(不能一并砍掉)。"""
    out = facts._flatten({"注册资本": "917000 万元", "法定代表人": "王某"})
    assert "注册资本" in out and "917000" in out


# --------------------------------------------------------------------------
# invoke:空正文要记成失败
# --------------------------------------------------------------------------
def test_invoke_marks_empty_response_as_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        facts.cli, "call",
        lambda *a, **k: {"code": 1, "msg": "success", "data": None},
    )
    doc = facts.invoke("connector__x__y", {}, query_label="q", project_dir=tmp_path)
    assert doc.ok is False
    assert "空正文" in (doc.error or "")
    assert doc.text == ""


def test_invoke_accepts_real_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(
        facts.cli, "call",
        lambda *a, **k: {"code": 1, "data": {"data": "x" * 200}},
    )
    doc = facts.invoke("connector__x__y", {}, query_label="q", project_dir=tmp_path)
    assert doc.ok is True
    assert len(doc.text) == 200


# --------------------------------------------------------------------------
# 产品参数检索
# --------------------------------------------------------------------------
def test_product_news_query_is_product_oriented(monkeypatch, tmp_path):
    """产品向查询串必须带参数关键词,且不能混入公司财经词。

    混入"销量 财报"会把检索结果整体拉回公司新闻,车型参数被挤掉 ——
    这正是之前产品级事实只有 2 条的直接原因。
    """
    seen: dict = {}

    def fake_call(tool, args, **k):
        seen.update(args)
        return {"code": 1, "data": {"data": "y" * 300}}

    monkeypatch.setattr(facts.cli, "call", fake_call)
    facts.ifind_product_news("比亚迪汉 EV", project_dir=tmp_path)

    q = seen["query"]
    for kw in ("续航", "加速", "售价", "配置"):
        assert kw in q, f"产品向查询缺少 {kw}"
    for kw in ("财报", "重大事件"):
        assert kw not in q, f"产品向查询混入了公司财经词 {kw}"


def test_news_window_is_wide_enough(monkeypatch, tmp_path):
    """发布当天的报道才是参数来源,180 天窗口会漏掉改款参数。"""
    seen: dict = {}
    monkeypatch.setattr(
        facts.cli, "call",
        lambda tool, args, **k: (seen.update(args), {"code": 1, "data": {"data": "z" * 300}})[1],
    )
    facts.ifind_news("比亚迪", project_dir=tmp_path)
    start = seen["time_start"]
    assert start <= "2026-04-19", f"新闻窗口过窄:{start}"


def test_product_source_registered():
    assert "ifind.product_news" in facts.SOURCES


def test_auto_profile_uses_product_news():
    from app import categories

    profile = categories.load_profile("auto_long")
    assert "ifind.product_news" in profile.fact_sources


def test_unknown_source_marked_failed(tmp_path):
    res = facts.collect(["ifind.does_not_exist"], "测试", project_dir=tmp_path)
    assert len(res.docs) == 1
    assert res.docs[0].ok is False
    assert "未知事实来源" in (res.docs[0].error or "")


# ---------------------------------------------------------------------------
# 内容方向专属来源
# ---------------------------------------------------------------------------
THEME_SOURCES = [
    "ifind.iteration_news",
    "ifind.compare_news",
    "ifind.cost_news",
    "ifind.tech_news",
    "ifind.market_news",
]


@pytest.mark.parametrize("name", THEME_SOURCES)
def test_theme_source_registered(name):
    assert name in facts.SOURCES


def _capture_query(monkeypatch, name: str) -> str:
    """调用一个来源并截获它实际发出的查询串(不真的发请求)。"""
    captured: dict = {}
    monkeypatch.setattr(
        facts.cli, "call",
        lambda tool, args, **k: (
            captured.update(args), {"code": 1, "data": {"data": "z" * 300}}
        )[1],
    )
    facts.SOURCES[name]("比亚迪汉")
    return captured["query"]


def test_all_theme_source_queries_unique(monkeypatch):
    """5 条方向查询必须两两不同。

    把几条查询写成一样的东西,检索引擎就会当成同一个意图 ——
    于是"迭代史"方向拿回一堆市场新闻、"市场观察"方向拿回一堆改款通稿。
    """
    queries = [_capture_query(monkeypatch, n) for n in THEME_SOURCES]
    assert len(set(queries)) == len(THEME_SOURCES), f"方向查询串出现重复:{queries}"


def test_theme_queries_stay_away_from_company_news(monkeypatch):
    """方向专属查询不得混入公司向词汇。

    "销量 财报"这类词混进一条查询,检索结果会被整体拉回财经新闻,
    方向真正要的素材被挤掉 —— 实测拆分后产品级事实从 2 条升到 19 条。
    """
    banned = ("销量", "财报", "营业收入", "净利润")
    for name in ("ifind.iteration_news", "ifind.compare_news",
                 "ifind.cost_news", "ifind.tech_news"):
        query = _capture_query(monkeypatch, name)
        hit = [w for w in banned if w in query]
        assert not hit, f"{name} 的查询串混入了公司向词汇 {hit}:{query}"


def test_iteration_window_is_widest(monkeypatch):
    """迭代史天然跨多年,540 天只够捞回最近一次改款的通稿。"""
    def _start_for(name: str) -> str:
        captured: dict = {}
        monkeypatch.setattr(
            facts.cli, "call",
            lambda tool, args, **k: (
                captured.update(args), {"code": 1, "data": {"data": "z" * 300}}
            )[1],
        )
        facts.SOURCES[name]("比亚迪汉")
        return captured["time_start"]

    iteration = _start_for("ifind.iteration_news")
    product = _start_for("ifind.product_news")
    assert iteration < product, (
        f"迭代史窗口({iteration})应比车型参数窗口({product})更宽"
    )


def test_auto_themes_reference_registered_sources():
    """auto 的每个方向声明的来源都必须真实存在,否则采集阶段会静默失败。"""
    from app import categories

    profile = categories.load_profile("auto_long")
    for name, theme in profile.themes.items():
        for src in theme.fact_sources or profile.fact_sources:
            assert src in facts.SOURCES, f"方向 {name} 引用了未注册的来源 {src}"
