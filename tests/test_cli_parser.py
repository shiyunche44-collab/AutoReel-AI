"""CLI 封装与视频契约测试(计划 §6.1)。

这一层是平台与生成通道之间唯一的接口,所以错误归一化、超时处理、
模型参数校验必须可靠 —— 参数非法却照样提交,是会真金白银烧额度的。
"""
from __future__ import annotations

import json

import pytest

from app.llm.base import LLMOutputInvalid, extract_json, require_keys
from app.providers.cli import ConnectorError, _extract_json, _unwrap


# --------------------------------------------------------------------------
# JSON 解析稳健性
# --------------------------------------------------------------------------
def test_extract_json_plain():
    assert _extract_json('{"a":1}') == {"a": 1}


def test_extract_json_with_surrounding_noise():
    raw = '[info] connecting...\n{"a": 1}\ndone'
    assert _extract_json(raw) == {"a": 1}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ConnectorError):
        _extract_json("not json at all")


def test_extract_json_handles_arrays():
    assert _extract_json("[1, 2, 3]") == [1, 2, 3]


# --------------------------------------------------------------------------
# 响应信封归一化
# --------------------------------------------------------------------------
def test_unwrap_strips_code_and_message():
    out = _unwrap("tool", {"code": 0, "message": "ok", "data": {"x": 1}})
    assert "code" not in out
    assert out == {"data": {"x": 1}}


def test_unwrap_unwraps_single_key_list():
    out = _unwrap("tool", {"code": 0, "message": "ok", "success_items": [{"a": 1}]})
    assert out == [{"a": 1}]


def test_unwrap_raises_on_error_code():
    with pytest.raises(ConnectorError) as exc:
        _unwrap("tool", {"code": 2056, "message": "insufficient balance"})
    assert "2056" in str(exc.value)


def test_unwrap_accepts_200_success_code():
    assert _unwrap("tool", {"code": 200, "message": "ok", "x": 1}) == {"x": 1}


def test_unwrap_passes_through_non_envelope():
    assert _unwrap("tool", ["a", "b"]) == ["a", "b"]


# --------------------------------------------------------------------------
# LLM JSON 提取
# --------------------------------------------------------------------------
def test_extract_json_from_markdown_fence():
    text = '```json\n{"segments": [], "title": "t"}\n```'
    assert extract_json(text) == {"segments": [], "title": "t"}


def test_extract_json_from_fence_without_language_tag():
    assert extract_json('```\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_from_prose():
    text = '好的,结果如下:\n{"a": 1}\n希望有帮助。'
    assert extract_json(text) == {"a": 1}


def test_extract_json_with_nested_braces():
    text = '前缀 {"a": {"b": {"c": 1}}, "d": [1, 2]} 后缀'
    assert extract_json(text)["a"]["b"]["c"] == 1


def test_extract_json_handles_escaped_quotes():
    # 构造出真正的 JSON 转义序列:{\"claim\": \"他说\\\"这是真的\\\"\"}
    text = json.dumps({"claim": '他说"这是真的"'})
    assert extract_json(text)["claim"] == '他说"这是真的"'


def test_extract_json_raises_on_empty():
    with pytest.raises(LLMOutputInvalid):
        extract_json("")


def test_require_keys_detects_missing():
    with pytest.raises(LLMOutputInvalid):
        require_keys({"a": 1}, ["a", "b"])


def test_require_keys_passes_when_complete():
    require_keys({"a": 1, "b": 2}, ["a", "b"])


# --------------------------------------------------------------------------
# 事实层信封剥离(实测同花顺/企查查都带业务信封,不清洗会浪费 token
# 并让模型把 "code":1 误读成失败)
# --------------------------------------------------------------------------
def test_flatten_strips_business_envelope():
    from app.providers.facts import _flatten

    out = _flatten({"code": 1, "msg": "success", "subCode": None,
                    "subMsg": None, "data": "# A股公司基本信息\n|证券代码|"})
    assert out.startswith("# A股公司基本信息")
    assert "success" not in out


def test_flatten_digs_into_nested_payload():
    from app.providers.facts import _flatten

    out = _flatten({"code": 1, "msg": "success",
                    "data": {"answer": "|证券代码|证券简称|营业收入|"}})
    assert out == "|证券代码|证券简称|营业收入|"


def test_flatten_parses_stringified_json():
    from app.providers.facts import _flatten

    out = _flatten({"code": 1, "data": '[{"标题": "比亚迪9月销量增长"}]'})
    assert "比亚迪9月销量增长" in out


def test_flatten_passes_through_plain_text():
    from app.providers.facts import _flatten

    assert _flatten("纯文本内容") == "纯文本内容"
    assert _flatten({"无匹配项": "未匹配到搜索关键词。"}) == "无匹配项: 未匹配到搜索关键词。"


def test_flatten_survives_deeply_nested_payload():
    from app.providers.facts import _flatten

    payload = {"code": 1, "data": {"result": {"content": "正文"}}}
    assert _flatten(payload) == "正文"
