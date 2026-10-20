"""`images.search()` 测试。

这个函数历史上是整条素材链路的**静默断点**:payload 发错形状,工具返回空,
上层只看到 `searched=0`,于是参考图全部退化成 AI 编的虚构车型 —— 文案讲比亚迪汉、
画面拍一台不存在的车,一路走到成片都没人发现。

所以这里对**两种东西**都做了断言:
1. 发出去的 payload 形状对不对(畸形 payload 是本次要修的根因);
2. 返回结构能不能被正确解析(实测形态是"每查询一个对象"的数组)。
"""
from __future__ import annotations

import pytest

from app.categories import load_profile
from app.providers import cli, images

#: 实测的真实返回形态:每个查询一个对象,图片挂在各自的 images 上
REAL_SHAPE = [
    {
        "query": "BYD Han EV exterior",
        "images": [
            {
                "title": "BYD HAN",
                "image_url": "https://cdn.example.com/han-1.jpg",
                "source": "BYD",
                "link": "https://www.byd.com/us/car/han-ev",
            },
            {
                "title": "实拍比亚迪汉L",
                "image_url": "https://s1.xchuxing.com/xchuxing/carousel/han-2.jpg",
                "source": "新出行",
                "link": "https://www.xchuxing.com/article/1",
            },
        ],
    },
    {
        "query": "BYD Han EV rear",
        "images": [
            {
                "title": "BYD Han L rear",
                "image_url": "https://cdn.example.com/han-rear.jpg",
                "source": "新出行",
                "link": "https://www.xchuxing.com/article/2",
            }
        ],
    },
]


@pytest.fixture
def captured(monkeypatch):
    """截获发出去的 payload,返回 {'tool':..., 'args':..., 'calls': n}。"""
    box: dict = {"calls": 0}

    def _fake_call(tool, args, timeout=120):
        box["tool"] = tool
        box["args"] = args
        box["calls"] += 1
        return REAL_SHAPE

    monkeypatch.setattr(images.cli, "call", _fake_call)
    return box


# --------------------------------------------------------------------------
# payload 形状
# --------------------------------------------------------------------------
def test_queries_is_array_of_objects_not_strings(captured):
    """根因:过去发的是 {"queries": ["q"]},schema 要的是对象数组。"""
    images.search(["q1", "q2"])
    queries = captured["args"]["queries"]
    assert isinstance(queries, list) and len(queries) == 2
    for item in queries:
        assert isinstance(item, dict), f"queries 每一项必须是对象,实际 {type(item)}"
        assert set(item) == {"query", "prompt", "task_name"}


def test_every_query_carries_the_three_required_fields(captured):
    """`prompt` 是必填的相关性过滤器,给空串会让整个请求被 schema 拒掉。"""
    images.search(["q1"])
    item = captured["args"]["queries"][0]
    assert item["query"] == "q1"
    assert item["prompt"].strip()
    assert item["task_name"].strip()


def test_relevance_prompt_is_per_query(captured):
    images.search(
        ["q1", "q2"],
        relevance_prompts={"q1": "professional automotive press photo", "q2": "macro detail"},
    )
    prompts = {i["query"]: i["prompt"] for i in captured["args"]["queries"]}
    assert prompts["q1"] == "professional automotive press photo"
    assert prompts["q2"] == "macro detail"


def test_missing_relevance_prompt_falls_back_to_generic(captured):
    """没给描述不能发空串 —— 宁可给通用描述,也不能让整批查询被拒。"""
    images.search(["q1"])
    assert captured["args"]["queries"][0]["prompt"] == images.DEFAULT_RELEVANCE_PROMPT


def test_task_name_is_threaded_through(captured):
    images.search(["q1"], task_name="car_ref_exterior")
    assert captured["args"]["queries"][0]["task_name"] == "car_ref_exterior"


def test_single_call_for_all_queries(captured):
    """schema 本来就收数组,逐条调用是畸形 payload 时代的遗留写法。"""
    images.search(["q1", "q2", "q3"])
    assert captured["calls"] == 1


def test_providers_passthrough_kept(captured):
    images.search(["q1"], providers=["serper"])
    assert captured["args"]["providers"] == ["serper"]


def test_duplicate_and_blank_queries_dropped(captured):
    images.search(["q1", "q1", "  ", ""])
    assert [i["query"] for i in captured["args"]["queries"]] == ["q1"]


def test_empty_query_list_never_calls_the_tool(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return REAL_SHAPE

    monkeypatch.setattr(images.cli, "call", _boom)
    assert images.search([]) == []
    assert called["n"] == 0


# --------------------------------------------------------------------------
# 返回解析
# --------------------------------------------------------------------------
def test_parses_real_per_query_array_shape(captured):
    """实测形态:顶层是数组,每项才是带 images 的对象。"""
    refs = images.search(["a", "b"])
    assert len(refs) == 3
    assert {r.url for r in refs} == {
        "https://cdn.example.com/han-1.jpg",
        "https://s1.xchuxing.com/xchuxing/carousel/han-2.jpg",
        "https://cdn.example.com/han-rear.jpg",
    }


def test_still_parses_legacy_flat_shapes():
    """保留旧的顶层 dict 形态解析 —— 不同 provider 的返回并不统一。"""
    assert len(list(images._iter_images({"images": [{"url": "a"}]}))) == 1
    assert len(list(images._iter_images([{"url": "a"}, {"url": "b"}]))) == 2
    assert len(list(images._iter_images(["a", "b"]))) == 2


def test_link_is_never_taken_as_image_url(captured):
    """真 bug:`link` 是文章页地址,拿它兜底等于把 HTML 页面交给 H3 当参考图。"""
    refs = images.search(["q1"])
    assert all("article" not in r.url and not r.url.startswith("https://www.") for r in refs)
    assert all(r.url.endswith((".jpg", ".jpeg", ".png", ".webp")) for r in refs)


def test_source_page_is_recorded(captured):
    """图片直链通常是 CDN 临时地址,记下文章页才追溯得到出处。"""
    refs = {r.url: r for r in images.search(["q1", "q2"])}
    first = refs["https://cdn.example.com/han-1.jpg"]
    assert first.source_page == "https://www.byd.com/us/car/han-ev"
    assert first.author == "BYD"
    assert first.note == "BYD HAN"
    assert first.origin == "search"


def test_duplicates_across_queries_collapse(monkeypatch):
    monkeypatch.setattr(images.cli, "call", lambda *a, **k: [
        {"query": "a", "images": [{"image_url": "u1"}, {"image_url": "u2"}]},
        {"query": "b", "images": [{"image_url": "u1"}]},
    ])
    assert [r.url for r in images.search(["a", "b"])] == ["u1", "u2"]


# --------------------------------------------------------------------------
# 失败处理
# --------------------------------------------------------------------------
def test_connector_error_is_surfaced_not_swallowed(monkeypatch):
    """报告里必须能区分"工具挂了"和"没搜到",否则上层无从判断该修什么。"""
    def _fail(tool, args, timeout=120):
        raise cli.ConnectorError("search_images", "connector_tool_error: bad payload")

    monkeypatch.setattr(images.cli, "call", _fail)
    errors: list[str] = []
    refs = images.search(["q1", "q2"], errors=errors)
    assert refs == []
    assert errors and "检索失败" in errors[0]
    assert "2 条查询" in errors[0]


def test_error_list_is_optional(monkeypatch):
    def _fail(tool, args, timeout=120):
        raise cli.ConnectorError("search_images", "boom")

    monkeypatch.setattr(images.cli, "call", _fail)
    assert images.search(["q1"]) == []


# --------------------------------------------------------------------------
# 与品类 profile 的接线
# --------------------------------------------------------------------------
def test_reference_prompt_is_english_and_names_the_view():
    profile = load_profile("auto_long")
    prompt = profile.reference_prompt("BYD Han EV", "exterior_45")
    assert "BYD Han EV" in prompt
    assert "three-quarter" in prompt
    # 相关性描述必须全英文 —— 检索工具按英文语义做过滤
    assert not any("\u4e00" <= c <= "\u9fff" for c in prompt)
    assert "no e-commerce" in prompt


def test_reference_prompt_handles_unknown_angle():
    profile = load_profile("auto_long")
    prompt = profile.reference_prompt("BYD Han EV", "一个没收录的视角")
    assert "BYD Han EV" in prompt
    assert "一个没收录的视角" in prompt


def test_every_auto_angle_has_a_prompt():
    """auto profile 里用到的每个视角都要能生成相关性描述,不能退回原始标签。"""
    profile = load_profile("auto_long")
    for angle in profile.angle_queries:
        prompt = profile.reference_prompt("BYD Han EV", angle)
        assert not any("\u4e00" <= c <= "\u9fff" for c in prompt), angle
