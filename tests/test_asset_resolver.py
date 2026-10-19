"""参考图集构建测试。

这一层过去有两个方向相反的错误,都只有在这里能钉住:

1. 检索 payload 畸形 → `searched=0` → 参考图全退化成 AI 编的虚构车型
   (`images.py` 侧的测试见 `test_images_search.py`);
2. 校验条件反了 —— 过去是"**只有全是 AI 图才校验**",理由是"真实照片不用验"。
   真实来源一多这个前提就塌了:检索会混进电商白底图和别的年款。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.categories import load_profile
from app.pipeline import asset_resolver as ar
from app.providers import images


def _ref(url: str, origin: str = "search", **kw) -> images.ImageRef:
    return images.ImageRef(url=url, origin=origin, **kw)


@pytest.fixture
def no_network(monkeypatch):
    """掐掉维基与 AI 生图,并把下载/上传打桩成"成功"。

    下载必须打桩成**成功**而不是抛异常:视觉问答工具拒收外部直链,校验层
    必须先把图落地再上传,下载一失败整条校验链路就不会发起调用。
    这样打桩才贴近真实路径 —— 报错路径由 `download_fails` 单独覆盖。
    """

    origin_of: dict[str, str] = {}   # 上传地址 → 原始检索地址
    local_of: dict[str, str] = {}

    def _download(url, dest, **kw):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-image")
        local_of[dest.name] = url
        return dest

    def _upload(path):
        up = f"https://up/{Path(path).name}"
        origin_of[up] = local_of.get(Path(path).name, up)
        return up

    monkeypatch.setattr(ar.images, "download", _download)
    monkeypatch.setattr(ar.images.cli, "upload_temp_url", _upload)
    monkeypatch.setattr(ar.webassets, "collect", lambda *a, **k: ([], []))
    monkeypatch.setattr(ar.images, "generate", lambda *a, **k: None)
    return origin_of


@pytest.fixture
def download_fails(monkeypatch):
    """下载失败(CDN 热链保护之类)。图必须保留,只是不参与校验。"""
    monkeypatch.setattr(ar.images, "download",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("403")))
    monkeypatch.setattr(ar.webassets, "collect", lambda *a, **k: ([], []))
    monkeypatch.setattr(ar.images, "generate", lambda *a, **k: None)


@pytest.fixture
def search_stub(monkeypatch):
    """装一个可控的检索桩,并记录调用参数。"""
    box: dict = {"result": [], "calls": []}

    def _search(queries, **kw):
        box["calls"].append({"queries": queries, **kw})
        return box["result"]

    monkeypatch.setattr(ar.images, "search", _search)
    return box


@pytest.fixture
def describe_stub(monkeypatch):
    box: dict = {"calls": [], "result": ""}

    def _describe(urls, prompt, **kw):
        box["calls"].append({"urls": list(urls), "prompt": prompt})
        result = box["result"]
        return result(urls) if callable(result) else result

    monkeypatch.setattr(ar.images, "describe", _describe)
    return box

def _per_image(no_network, answers: dict[str, str]):
    """按原始检索地址给每张图一个回答。校验是并发逐张问的,不能靠调用顺序。"""
    return lambda urls: answers.get(no_network[urls[0]], "")


class _FakeLLM:
    """记录 structured 调用;`result` 可以是返回值或异常。"""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def structured(self, system, user, **kw):
        self.calls.append({"system": system, "user": user, **kw})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _build(tmp_path, search_stub, describe_stub, **kw):
    from app.models import ShotSpec

    profile = load_profile("auto")
    # 必须给真分镜:视角列表是从分镜统计出来的,空分镜会让检索循环一次都不跑,
    # 那样测出来的"检索没被调用"全是假象。
    specs = [ShotSpec(idx=0, duration=6.0, section="开场钩子",
                      visual_zh="整车 45 度前侧", angle="exterior_45")]
    return ar.build_reference_set(
        subject="BYD Han EV",
        profile=profile,
        specs=specs,
        project_dir=tmp_path,
        **kw,
    )


# --------------------------------------------------------------------------
# 检索接线
# --------------------------------------------------------------------------
def test_search_gets_relevance_prompt_and_task_name(tmp_path, search_stub, describe_stub,
                                                    no_network):
    """相关性描述与 task_name 必须一起发出去 —— `prompt` 是 schema 必填项。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = ""
    _build(tmp_path, search_stub, describe_stub)

    assert search_stub["calls"], "检索根本没被调用"
    for call in search_stub["calls"]:
        q = call["queries"][0]
        assert call["relevance_prompts"][q].strip(), "相关性描述不能为空"
        assert call["task_name"]


# --------------------------------------------------------------------------
# 硬过滤(不依赖模型)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://www.alibaba.com/product/detail/123.html",
    "https://www.ebay.com/itm/12345",
    "https://www.dhgate.com/wholesale/han-ev.html",
    "https://detail.1688.com/offer/123.html",
    "https://scontent.facebook.com/lookaside/c_abc.jpg",
    "https://www.shutterstock.com/image-photo/car.jpg",
    "https://site.example.com/listing/abc",
])
def test_junk_sites_are_rejected_before_the_model_ever_sees_them(url):
    assert ar._junk_reason(_ref(url)) != ""


@pytest.mark.parametrize("url", [
    "https://s1.xchuxing.com/xchuxing/carousel/han.jpg",
    "https://cjh2.autoimg.cn/chejiahao/han.jpg",
    "https://electrek.co/wp-content/uploads/han.jpeg",
    "https://www.byd.com/material/han.jpg",
])
def test_editorial_sites_are_kept(url):
    assert ar._junk_reason(_ref(url)) == ""


def test_junk_filter_runs_before_the_limit(tmp_path, search_stub, describe_stub, no_network):
    """前 3 条是电商图时,真正的车照必须能补进来,而不是被前 3 条挤掉配额。"""
    search_stub["result"] = [
        _ref("https://www.alibaba.com/product/detail/1.jpg"),
        _ref("https://www.ebay.com/itm/2.jpg"),
        _ref("https://www.dhgate.com/wholesale/3.jpg"),
        _ref("https://s1.xchuxing.com/a.jpg"),
        _ref("https://cjh2.autoimg.cn/b.jpg"),
    ]
    describe_stub["result"] = ""
    refs, report = _build(tmp_path, search_stub, describe_stub, limit=2, verify=False)

    assert [r.url for r in refs] == [
        "https://s1.xchuxing.com/a.jpg", "https://cjh2.autoimg.cn/b.jpg",
    ]
    assert report["rejected_by_filter"] == 3
    assert report["has_real_photo"] is True


def test_filtered_urls_are_recorded_with_reason(tmp_path, search_stub, describe_stub,
                                               no_network):
    search_stub["result"] = [_ref("https://www.ebay.com/itm/9.jpg")]
    _, report = _build(tmp_path, search_stub, describe_stub, verify=False)
    assert report["rejected_reasons"]
    assert any("ebay" in r for r in report["rejected_reasons"])


# --------------------------------------------------------------------------
# 校验方向(这是本轮修的核心错误)
# --------------------------------------------------------------------------
def test_real_search_results_are_verified(tmp_path, search_stub, describe_stub, no_network):
    """过去 `all(origin == "generate")` 才校验,真实检索结果反而不验。"""
    search_stub["result"] = [_ref("https://s1.xchuxing.com/a.jpg", origin="search")]
    describe_stub["result"] = ""
    _build(tmp_path, search_stub, describe_stub)
    assert describe_stub["calls"], "真实检索图必须送校验"


def test_external_urls_are_uploaded_before_verifying(tmp_path, search_stub, describe_stub,
                                                     no_network):
    """视觉问答工具拒收外部直链,实测要求先落地再 upload_temp_url。

    不做这一步,describe 会对每张图返回一句英文错误、描述为空 —— 校验看起来
    "跑了",实际什么都没验。
    """
    search_stub["result"] = [_ref("https://s1.xchuxing.com/a.jpg", origin="search")]
    describe_stub["result"] = ""
    _build(tmp_path, search_stub, describe_stub)
    submitted = describe_stub["calls"][0]["urls"]
    assert submitted and all(u.startswith("https://up/") for u in submitted), submitted


def test_download_failure_keeps_the_image_but_skips_verification(
    tmp_path, search_stub, describe_stub, download_fails
):
    """CDN 热链保护之类导致下不下来时:图仍然可用(生成侧自己去取),
    只是这一张没参与校验 —— 不能因为校验链路抖动把好图丢了。"""
    search_stub["result"] = [_ref("https://cdn/hotlink.com/a.jpg", origin="search")]
    describe_stub["result"] = ""
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert [r.url for r in refs] == ["https://cdn/hotlink.com/a.jpg"]
    assert not describe_stub["calls"]
    assert any("未能上传校验" in e for e in report["errors"])


def test_generated_fallback_is_not_verified(tmp_path, search_stub, describe_stub,
                                            no_network, monkeypatch):
    """AI 兜底图在**校验之后**才生成,所以不会参与校验 —— 而且**不该**参与。

    它本身就是模型编出来的车,拿"画面里是不是目标车型"去问它,答案必然是
    "不是",结果就是刚兜底的参考图又被自己剔光,只剩一个空集合。
    """
    search_stub["result"] = []
    monkeypatch.setattr(
        ar.images, "generate",
        lambda *a, **k: _ref("https://temp/asset.png", origin="generate"),
    )
    refs, report = _build(tmp_path, search_stub, describe_stub)

    assert [r.origin for r in refs] == ["generate"]
    assert not describe_stub["calls"]
    assert report["generated_fallback"] == 1
    assert report["has_real_photo"] is False


def test_user_uploads_are_exempt_from_verification(tmp_path, monkeypatch, search_stub,
                                                   describe_stub, no_network):
    """人工挑过的图不该再被模型翻一遍 —— 那是唯一的确定性来源。"""
    folder = tmp_path / ar.USER_REF_DIRNAME
    folder.mkdir(parents=True)
    (folder / "mine.jpg").write_bytes(b"fake")
    monkeypatch.setattr(ar.images.cli, "upload_temp_url", lambda p: "https://up/1.jpg")

    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert [r.origin for r in refs] == ["user"]
    assert not describe_stub["calls"]
    assert report["has_real_photo"] is True


def test_verify_prompt_names_the_target_subject(tmp_path, search_stub, describe_stub,
                                               no_network):
    """过去只问"是不是汽车",从没告诉模型目标车型是谁 —— 校验形同虚设。"""
    search_stub["result"] = [_ref("https://s1.xchuxing.com/a.jpg")]
    describe_stub["result"] = ""
    _build(tmp_path, search_stub, describe_stub)
    assert "BYD Han EV" in describe_stub["calls"][0]["prompt"]


# --------------------------------------------------------------------------
# 校验结论的解析与保守策略
# --------------------------------------------------------------------------
def test_each_image_is_asked_separately(tmp_path, search_stub, describe_stub, no_network):
    """实测一次塞 10 张图,视觉模型只描述第一张 —— 其余 9 张从没被看过。"""
    search_stub["result"] = [_ref(f"https://xchuxing.com/{c}.jpg") for c in "abc"]
    describe_stub["result"] = ""
    _build(tmp_path, search_stub, describe_stub)
    assert len(describe_stub["calls"]) == 3
    assert all(len(c["urls"]) == 1 for c in describe_stub["calls"])


def test_verdict_rejects_wrong_model_and_ecommerce(tmp_path, search_stub, describe_stub,
                                                   no_network):
    search_stub["result"] = [
        _ref("https://xchuxing.com/good.jpg", author="新出行"),
        _ref("https://xchuxing.com/wrong.jpg", author="车讯"),
        _ref("https://xchuxing.com/shop.jpg", author="某电商"),
    ]
    describe_stub["result"] = _per_image(no_network, {
        "https://xchuxing.com/good.jpg": '{"is_target_model": true, "problem": ""}',
        "https://xchuxing.com/wrong.jpg":
            '{"is_target_model": false, "problem": "not_target_model"}',
        "https://xchuxing.com/shop.jpg": '{"is_target_model": true, "problem": "e-commerce"}',
    })
    refs, report = _build(tmp_path, search_stub, describe_stub)

    assert [r.url for r in refs] == ["https://xchuxing.com/good.jpg"]
    assert report["verified_rejected"] == 2
    assert report["verified_ok"] == 1
    assert report["verify_parsed"] is True
    assert report["verify_sources"]["json"] == 3
    assert any("not_target_model" in r for r in report["rejected_reasons"])
    assert any("e-commerce" in r for r in report["rejected_reasons"])


def test_prose_is_structured_by_llm(tmp_path, search_stub, describe_stub, no_network):
    """视觉工具恒回散文:散文交给 LLM 转成判定,校验才真正生效。"""
    search_stub["result"] = [_ref("https://xchuxing.com/han.jpg", author="新出行"),
                             _ref("https://xchuxing.com/seal.jpg", author="车讯")]
    describe_stub["result"] = _per_image(no_network, {
        "https://xchuxing.com/han.jpg": "这是一台比亚迪汉 EV,车尾有 HAN 字样。",
        "https://xchuxing.com/seal.jpg": "这是一台白色比亚迪海豹 07 DM-i。",
    })
    llm = _FakeLLM({"verdicts": [
        {"index": 1, "is_target_model": True, "problem": "", "reason": "车尾 HAN"},
        {"index": 2, "is_target_model": False, "problem": "not_target_model",
         "reason": "海豹 07"},
    ]})
    refs, report = _build(tmp_path, search_stub, describe_stub, llm=llm)

    assert [r.url for r in refs] == ["https://xchuxing.com/han.jpg"]
    assert len(llm.calls) == 1, "散文要合在一次 LLM 调用里转"
    assert "BYD Han EV" in llm.calls[0]["system"]
    assert "海豹 07" in llm.calls[0]["user"]
    assert report["verify_sources"] == {"json": 0, "llm": 2, "none": 0}
    assert any("海豹 07" in r for r in report["rejected_reasons"])


def test_only_prose_answers_go_to_llm(tmp_path, search_stub, describe_stub, no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg"),
                             _ref("https://xchuxing.com/b.jpg")]
    describe_stub["result"] = _per_image(no_network, {
        "https://xchuxing.com/a.jpg": '{"is_target_model": true, "problem": ""}',
        "https://xchuxing.com/b.jpg": "一台白色 SUV,看不出车标。",
    })
    llm = _FakeLLM({"verdicts": [{"index": 2, "is_target_model": "unknown", "problem": ""}]})
    refs, report = _build(tmp_path, search_stub, describe_stub, llm=llm)
    assert len(refs) == 2
    assert "### 图 2" in llm.calls[0]["user"]
    assert "### 图 1" not in llm.calls[0]["user"]


def test_llm_failure_keeps_everything(tmp_path, search_stub, describe_stub, no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = "一台别的车。"
    llm = _FakeLLM(RuntimeError("deepseek down"))
    refs, report = _build(tmp_path, search_stub, describe_stub, llm=llm)
    assert len(refs) == 1
    assert report["verify_parsed"] is False
    assert any("结构化失败" in e for e in report["errors"])


def test_llm_index_out_of_range_is_ignored(tmp_path, search_stub, describe_stub, no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = "一台别的车。"
    llm = _FakeLLM({"verdicts": [{"index": 7, "is_target_model": False}]})
    refs, _ = _build(tmp_path, search_stub, describe_stub, llm=llm)
    assert len(refs) == 1


def test_unparseable_verdict_without_llm_keeps_everything(tmp_path, search_stub,
                                                           describe_stub, no_network):
    """解析失败必须**保守保留** —— 把"解析不了"当成"全不合格",
    会让校验层变成素材层新的失败源。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg"),
                             _ref("https://xchuxing.com/b.jpg")]
    describe_stub["result"] = "抱歉,我无法完成这项分析。"
    refs, report = _build(tmp_path, search_stub, describe_stub)

    assert len(refs) == 2
    assert report["verify_parsed"] is False
    assert any("可用结论" in e for e in report["errors"])


def test_empty_verdict_keeps_everything(tmp_path, search_stub, describe_stub, no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = ""
    llm = _FakeLLM({"verdicts": []})
    refs, report = _build(tmp_path, search_stub, describe_stub, llm=llm)
    assert len(refs) == 1
    assert report["verify_parsed"] is False
    assert not llm.calls, "空回答没有可转的内容,不该花一次 LLM 调用"


def test_describe_error_on_one_image_keeps_it(tmp_path, search_stub, describe_stub,
                                              no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg"),
                             _ref("https://xchuxing.com/b.jpg")]

    def _answer(urls):
        if no_network[urls[0]].endswith("b.jpg"):
            raise RuntimeError("timeout")
        return '{"is_target_model": false, "problem": "not_target_model"}'

    describe_stub["result"] = _answer
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert [r.url for r in refs] == ["https://xchuxing.com/b.jpg"]
    assert any("校验调用失败" in e for e in report["errors"])


def test_nonblocking_problem_only_warns(tmp_path, search_stub, describe_stub, no_network):
    """带水印的图作为"这台车长什么样"的锚点仍然成立,不该丢。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = '{"is_target_model": true, "problem": "text_overlay"}'
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert len(refs) == 1
    assert report.get("verify_notes")


def test_image_without_verdict_is_kept(tmp_path, search_stub, describe_stub, no_network):
    """一张没得出结论就丢掉好图,代价太大。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg"),
                             _ref("https://xchuxing.com/b.jpg")]
    describe_stub["result"] = _per_image(no_network, {
        "https://xchuxing.com/a.jpg": '{"is_target_model": true, "problem": ""}',
    })
    refs, _ = _build(tmp_path, search_stub, describe_stub)
    assert len(refs) == 2


def test_false_target_model_without_problem_is_still_rejected(tmp_path, search_stub,
                                                              describe_stub, no_network):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = '{"is_target_model": false, "problem": ""}'
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert refs == []
    assert report["verified_rejected"] == 1


def test_verdict_wrapped_in_prose_still_parses(tmp_path, search_stub, describe_stub,
                                               no_network):
    describe_stub["result"] = (
        "好的,以下是分析结果:\n"
        '[{"index": 1, "is_target_model": true, "problem": ""}]\n'
        "希望有帮助。"
    )
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert len(refs) == 1
    assert report["verify_parsed"] is True


# --------------------------------------------------------------------------
# 兜底与报告
# --------------------------------------------------------------------------
def test_all_rejected_falls_back_to_ai_generation(tmp_path, search_stub, describe_stub,
                                                   no_network, monkeypatch):
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = '{"is_target_model": false, "problem": "not_target_model"}'

    def _gen(*a, **k):
        r = _ref("https://ai/generated.png", origin="generate")
        return r

    monkeypatch.setattr(ar.images, "generate", _gen)
    refs, report = _build(tmp_path, search_stub, describe_stub)

    assert [r.origin for r in refs] == ["generate"]
    assert report["has_real_photo"] is False
    assert report["generated_fallback"] == 1
    assert "虚构" in report["warning"] or "想象" in report["warning"]


def test_report_keeps_legacy_keys(tmp_path, search_stub, describe_stub, no_network):
    """`has_real_photo` / `origin_counts` 是 `project.html` 依赖的既有键。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = ""
    _, report = _build(tmp_path, search_stub, describe_stub)
    for key in ("has_real_photo", "origin_counts", "searched", "errors", "sources_tried"):
        assert key in report, key


def test_download_failure_is_recorded_not_fatal(tmp_path, search_stub, describe_stub,
                                                 no_network):
    """参考图是以 URL 交给生成侧取的,本机下不下来不影响成片。"""
    search_stub["result"] = [_ref("https://xchuxing.com/a.jpg")]
    describe_stub["result"] = ""
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert len(refs) == 1
    assert report["download_warnings"]
    assert Path(tmp_path / "refs.json").is_file()


def test_search_failure_degrades_to_empty_set(tmp_path, search_stub, describe_stub,
                                               no_network, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("检索炸了")

    monkeypatch.setattr(ar.images, "search", _boom)
    monkeypatch.setattr(ar.images, "generate", lambda *a, **k: None)
    refs, report = _build(tmp_path, search_stub, describe_stub)
    assert refs == []
    assert report["searched"] == 0
    assert any("检索失败" in e for e in report["errors"])
