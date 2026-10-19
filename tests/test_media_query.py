"""逐镜头检索词:LLM 正常 / 部分缺失 / 挂掉 三种情况都要给出可用的检索词。"""
from __future__ import annotations

from app.categories import load_profile
from app.models import ShotSpec
from app.pipeline import media_query

PROFILE = load_profile("auto")
SPECS = [
    ShotSpec(idx=0, section="开场钩子", visual_zh="汉EV在夜色中驶来", angle="exterior_45"),
    ShotSpec(idx=1, section="核心卖点", visual_zh="中控大屏特写", angle="interior"),
    ShotSpec(idx=2, section="行业", visual_zh="车展现场,人流涌动", angle="industry"),
]


class _LLM:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc = payload, exc

    def structured(self, system, user, **kw):
        if self.exc:
            raise self.exc
        return self.payload


def test_fallback_without_llm():
    queries, brand = media_query.build("比亚迪汉 EV", PROFILE, SPECS)
    assert brand == ""
    assert [q.kind for q in queries] == ["car", "car", "other"]
    assert queries[0].query_zh.startswith("比亚迪汉 EV")
    assert "实拍" not in queries[0].query_zh
    assert queries[2].query_en  # 图库只认英文,兜底也得有


def test_llm_output_is_used():
    llm = _LLM({"brand": "比亚迪", "shots": [
        {"idx": 0, "kind": "car", "query_zh": "比亚迪汉 夜景 行驶", "query_en": "BYD Han night"},
        {"idx": 2, "kind": "other", "query_zh": "车展 人流", "query_en": "auto show crowd"},
    ]})
    queries, brand = media_query.build("比亚迪汉 EV", PROFILE, SPECS, llm=llm)
    assert brand == "比亚迪"
    assert queries[0].query_zh == "比亚迪汉 夜景 行驶"
    # LLM 漏掉的镜头保留模板
    assert queries[1].kind == "car" and queries[1].query_zh.startswith("比亚迪汉")
    assert queries[2].query_en == "auto show crowd"


def test_llm_garbage_is_ignored():
    llm = _LLM({"shots": [{"idx": "x"}, {"idx": 99, "kind": "car"}, "nope",
                          {"idx": 1, "kind": "weird", "query_zh": ""}]})
    queries, _ = media_query.build("比亚迪汉 EV", PROFILE, SPECS, llm=llm)
    assert queries[1].kind == "car"
    assert queries[1].query_zh


def test_llm_failure_falls_back():
    errors: list[str] = []
    queries, brand = media_query.build("比亚迪汉 EV", PROFILE, SPECS,
                                       llm=_LLM(exc=RuntimeError("timeout")), errors=errors)
    assert len(queries) == 3 and brand == ""
    assert errors
