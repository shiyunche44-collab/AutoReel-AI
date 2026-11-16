"""发布包装的纯函数:标题 / 评论 / 回复的校验与兜底。不需要网络与额度。"""
from __future__ import annotations

from app.pipeline import packaging as pk

ALLOWED = pk.allowed_figures(
    ["小米 YU7 标准版售价 23.35 万元起", "15 分钟补能 405 公里"],
    ["这车第一眼就是大"],
)


def test_figures_ignore_year_and_model_code():
    assert pk.figures_in("2026款YU7 Model Y") == set()
    assert pk.figures_in("23.35万,15分钟") == {"2335", "15"}


def test_title_ok_when_numbers_grounded():
    assert pk.check_title("YU7补能15分钟,够不够用", ALLOWED) == []


def test_title_rejects_invented_number():
    probs = pk.check_title("YU7续航900公里", ALLOWED)
    assert any("找不到" in p for p in probs)


def test_title_rejects_too_long_and_short():
    assert any("字" in p for p in pk.check_title("这是一个明显超过十八个字的特别特别长的标题啊", ALLOWED))
    assert pk.check_title("买", ALLOWED)


def test_title_rejects_ad_terms_but_not_benign_first():
    assert any("禁用词" in p for p in pk.check_title("全网最强的纯电SUV", ALLOWED))
    assert pk.check_title("第一眼就想买", ALLOWED) == []
    assert any("禁用词" in p for p in pk.check_title("销量第一的SUV", ALLOWED))


def test_title_rejects_empty_search_style():
    # 空泛词仍压;「值不值得买」这种带车型的搜索式写法放行(吃抖音搜索流量)
    assert any("搜索词" in p for p in pk.check_title("YU7全面解析", ALLOWED))
    assert pk.check_title("YU7值不值得买", ALLOWED) == []


def test_title_must_name_subject():
    assert any("车型名" in p for p in pk.check_title("补能15分钟,够不够用", ALLOWED,
                                                       name="小米YU7"))
    assert pk.check_title("YU7补能15分钟,够不够用", ALLOWED, name="小米YU7") == []
    # 只带品牌不算(小米有好几台车)
    assert pk.check_title("小米这台补能15分钟", ALLOWED, name="小米YU7")
    assert pk.check_title("补能15分钟,够不够用", ALLOWED) == []  # 合集 / 没车型不查


def test_subject_names():
    names = pk.subject_names("小米YU7")
    assert {"小米yu7", "yu7"} <= names
    assert pk.mentions_subject("i6到底香不香", "理想i6")
    assert not pk.mentions_subject("到底香不香", "理想i6")


def test_title_extra_category_terms():
    assert pk.check_title("碳陶卡钳真香", ALLOWED, terms=("碳陶卡钳",))


def test_comment_structure():
    ok = pk.check_comment("你选标准版还是GT?评论区投一票", ["标准版", "GT"], ALLOWED)
    assert ok == []
    assert pk.check_comment("你怎么看?", ["标准版"], ALLOWED)
    assert pk.check_comment("你选标准版还是GT", ["标准版", "GT"], ALLOWED)  # 没有提问
    assert pk.check_comment("你选A还是B?", ["标准版", "GT"], ALLOWED)  # 选项没写进正文
    assert pk.check_comment("你选甲还是甲?", ["甲", "甲"], ALLOWED)
    long_text = "你会选标准版还是GT?" + "字" * 40
    assert any("超过" in p for p in pk.check_comment(long_text, ["标准版", "GT"], ALLOWED))


def test_reply_grounding():
    assert pk.check_reply("补能15分钟是官方口径", ALLOWED) == []
    assert pk.check_reply("补能5分钟就满了", ALLOWED)


def test_fallback_titles_cover_all_kinds_and_pass_checks():
    for kind in pk.KINDS:
        text = pk.fallback_title(kind, "小米YU7", price="23.35万元起", allowed=ALLOWED)
        assert pk.check_title(text, ALLOWED) == [], (kind, text)


def test_fallback_title_long_subject_still_fits():
    text = pk.fallback_title(pk.KIND_NUMBER, "吉利银河银河战舰700旗舰版", price="19.98万元起",
                             allowed=ALLOWED)
    assert pk.spoken_len(text) <= pk.TITLE_MAX_CHARS


def test_fallback_number_without_price_has_no_number():
    text = pk.fallback_title(pk.KIND_NUMBER, "小米YU7", allowed=ALLOWED)
    assert pk.figures_in(text) == set()


def test_fallback_uses_thesis_when_valid():
    text = pk.fallback_title(pk.KIND_STAKE, "小米YU7", thesis="买的是电池不是屏幕", allowed=ALLOWED)
    assert text == "买的是电池不是屏幕"


def test_fallback_skips_bad_thesis():
    text = pk.fallback_title(pk.KIND_STAKE, "小米YU7", thesis="全网最强纯电SUV没有之一的理由", allowed=ALLOWED)
    assert "最强" not in text


def test_assemble_from_nothing_gives_three_titles():
    p = pk.assemble(None, "小米YU7", allowed=ALLOWED, price="23.35万元起")
    assert [t.kind for t in p.titles] == list(pk.KINDS)
    assert p.source == "fallback"
    assert len(p.replies) == pk.REPLY_COUNT
    assert pk.check_comment(p.comment.text, p.comment.options, ALLOWED) == []
    assert p.picked == {"title": 0, "cover": "a"}


def test_assemble_keeps_good_llm_and_replaces_bad():
    raw = {
        "titles": [
            {"text": "23.35万YU7,买屏还是买电?", "kind": "stake"},
            {"text": "YU7续航900公里", "kind": "number"},           # 编造数字 → 兜底
            {"text": "预算25万天天跑高速选哪版", "kind": "person"},  # 25 不在事实里 → 兜底
            {"text": "又一条立场", "kind": "stake"},                 # 同写法第二条被忽略
        ],
        "comment": {"text": "你选标准版还是GT?评论区投一票", "options": ["标准版", "GT"]},
        "replies": ["补能15分钟是官方口径", "补能5分钟就满", "", "补能15分钟是官方口径"],
    }
    p = pk.assemble(raw, "小米YU7", allowed=ALLOWED, price="23.35万元起")
    assert p.source == "llm"
    assert p.titles[0].text == "23.35万YU7,买屏还是买电?"
    assert p.titles[1].note == "模板" and p.titles[2].note == "模板"
    for t in p.titles:
        assert pk.check_title(t.text, ALLOWED) == []
    assert p.comment.options == ["标准版", "GT"]
    assert p.replies[0] == "补能15分钟是官方口径"
    assert "补能5分钟就满" not in p.replies
    assert len(p.replies) == pk.REPLY_COUNT and len(set(p.replies)) == pk.REPLY_COUNT


def test_assemble_bad_comment_falls_back():
    raw = {"comment": {"text": "你怎么看", "options": []}}
    p = pk.assemble(raw, "小米YU7", allowed=ALLOWED)
    assert p.comment.options == ["现在买", "再等等"]
    assert pk.check_comment(p.comment.text, p.comment.options, ALLOWED) == []


def test_assemble_survives_garbage():
    for raw in ("oops", 3, {"titles": "x", "comment": 5, "replies": {"a": 1}}, {"titles": [1, None, {}]}):
        p = pk.assemble(raw, "小米YU7", allowed=ALLOWED)
        assert len(p.titles) == 3 and len(p.replies) == pk.REPLY_COUNT


def test_titles_are_distinct():
    p = pk.assemble({"titles": [{"text": "同一句话怎么选", "kind": k} for k in pk.KINDS]},
                    "小米YU7", allowed=ALLOWED)
    assert len({t.text for t in p.titles}) == 3


def test_packaging_roundtrip():
    p = pk.assemble(None, "小米YU7", allowed=ALLOWED, now=12.0)
    again = pk.Packaging.model_validate(p.model_dump())
    assert again == p


# --------------------------------------------------------------------------
# 生成与读写(假 LLM、临时库)
# --------------------------------------------------------------------------
import pytest  # noqa: E402

from app.categories import load_profile  # noqa: E402
from app.models import ProjectCreate  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    from app import config, db

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


class _LLM:
    def __init__(self, data=None, boom=False):
        self.data, self.boom, self.calls = data, boom, []

    def structured(self, system, user, **kw):
        self.calls.append((system, user, kw))
        if self.boom:
            raise RuntimeError("api down")
        return self.data


GOOD = {
    "titles": [
        {"kind": "stake", "text": "小米YU7,23.35万买屏还是买电?"},
        {"kind": "number", "text": "小米YU7补能15分钟,够用吗"},
        {"kind": "person", "text": "小米YU7天天跑高速选哪版"},
    ],
    "comment": {"text": "你选标准版还是GT?评论区投一票", "options": ["标准版", "GT"]},
    "replies": ["补能15分钟是官方口径", "价格以官方当期为准", "有出入欢迎指出"],
}


def _project(fresh_db):
    from app.pipeline import orchestrator

    pid = orchestrator.create_project(ProjectCreate(subject="小米YU7"))
    fresh_db.update_project(pid, thesis="YU7买的是补能速度")
    fresh_db.write_json(pid, "facts.json", [
        {"id": "f_1", "claim": "YU7 标准版售价 23.35 万元起", "scope": "product",
         "confidence": "high", "enabled": True},
        {"id": "f_2", "claim": "15 分钟补能 405 公里", "scope": "product",
         "confidence": "high", "enabled": True, "basis": "官方"},
        {"id": "f_3", "claim": "续航 999 公里", "scope": "product",
         "confidence": "high", "enabled": False},
        {"id": "f_4", "claim": "零百 1.2 秒", "scope": "product", "confidence": "low"},
    ])
    fresh_db.write_json(pid, "script.json", [{"text": "这车第一眼就是大"},
                                             {"text": "23.35万起,15分钟补能"}])
    return pid


def test_generate_uses_llm_and_grounds_numbers(fresh_db):
    pid = _project(fresh_db)
    llm = _LLM(GOOD)
    p = pk.generate(pid, load_profile("auto_long"), llm)
    assert p.source == "llm"
    assert [t.text for t in p.titles] == [x["text"] for x in GOOD["titles"]]
    system, user, _ = llm.calls[0]
    assert "23.35 万元" in user and "15 分钟补能 405 公里(官方)" in user
    assert "999" not in user and "1.2 秒" not in user  # 被禁用 / 低置信的事实不给 LLM
    assert "入门价:23.35万元起" in user


def test_generate_rejects_numbers_from_disabled_facts(fresh_db):
    pid = _project(fresh_db)
    bad = {**GOOD, "titles": [{"kind": "number", "text": "YU7续航999公里"}, *GOOD["titles"][:1]]}
    p = pk.generate(pid, load_profile("auto_long"), _LLM(bad))
    assert "999" not in "".join(t.text for t in p.titles)


@pytest.mark.parametrize("llm", [None, _LLM(boom=True), _LLM("garbage")])
def test_generate_falls_back_without_a_working_llm(fresh_db, llm):
    pid = _project(fresh_db)
    p = pk.generate(pid, load_profile("auto_long"), llm)
    assert p.source == "fallback" and len(p.titles) == 3


def test_ensure_persists_and_does_not_overwrite(fresh_db):
    pid = _project(fresh_db)
    profile = load_profile("auto_long")
    first = pk.ensure(pid, profile, _LLM(GOOD))
    assert pk.load(pid) == first
    first.picked = {"title": 2, "cover": "b"}
    pk.save(pid, first)
    llm2 = _LLM(GOOD)
    again = pk.ensure(pid, profile, llm2)
    assert again.picked == {"title": 2, "cover": "b"} and not llm2.calls
    forced = pk.ensure(pid, profile, _LLM(GOOD), force=True)
    assert forced.picked["cover"] == "b"  # 重新生成保留封面选择


def test_load_missing_or_corrupt(fresh_db):
    pid = _project(fresh_db)
    assert pk.load(pid) is None
    fresh_db.write_json(pid, pk.FILE_NAME, {"titles": "nope"})
    assert pk.load(pid) is None


def test_compose_hook_is_best_effort(fresh_db, monkeypatch):
    from app.pipeline import explainer

    pid = _project(fresh_db)
    profile = load_profile("auto_long")
    explainer._make_packaging(pid, profile, _LLM(GOOD))
    assert pk.load(pid) is not None

    other = _project(fresh_db)
    monkeypatch.setattr(pk, "ensure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    explainer._make_packaging(other, profile, None)  # 不抛
    assert pk.load(other) is None


def test_idioms_with_numerals_are_not_figures():
    assert pk.figures_in("你选标准版还是GT?评论区投一票") == set()
    assert pk.check_comment("你选标准版还是GT?评论区投一票", ["标准版", "GT"], ALLOWED) == []
    assert pk.figures_in("一起聊聊") == set()


def test_profile_title_avoid_rejects_and_falls_back():
    avoid = (*pk.TITLE_AVOID, "炸裂")
    assert pk.check_title("YU7补能炸裂,够不够用", ALLOWED, avoid=avoid)
    assert pk.check_title("YU7补能炸裂,够不够用", ALLOWED) == []
    raw = {"titles": [{"kind": "stake", "text": "买屏还是买电炸裂"}]}
    p = pk.assemble(raw, "小米YU7", allowed=ALLOWED, avoid=avoid)
    assert "炸裂" not in p.titles[0].text and p.titles[0].note == "模板"


def test_profiles_declare_title_avoid():
    for name in ("auto_long", "baby_long"):
        assert load_profile(name).title_avoid, name


def test_title_rejects_model_absent_from_script():
    known = "哈弗长城H10 H10五座六座 续航15公里 第2期"
    bad = pk.check_title("H10和钛7怎么选", {"5", "6", "15"}, name="哈弗长城H10", known=known)
    assert any("钛7" in p for p in bad)
    assert not pk.check_title("H10选五座还是六座,天天通勤带家人", {"5", "6", "15"}, name="哈弗长城H10", known=known)
    assert not pk.check_title("H10续航15公里够不够", {"5", "6", "15"}, name="哈弗长城H10", known=known)


def test_single_car_title_must_lead_with_name():
    probs = pk.check_title("天天通勤带家人,H10选五座还是六座", ALLOWED, name="哈弗长城H10")
    assert any("开头" in p for p in probs)
    assert not pk.check_title("哈弗长城H10选五座还是六座", {"5", "6"}, name="哈弗长城H10")
    assert not pk.check_title("小鹏 MONA L03 值不值", ALLOWED, name="小鹏MONA L03")


def test_lead_full_name_expands_short_name():
    assert pk.lead_full_name("荣放等新款还是抄底现款", "丰田RAV4荣放") == "丰田RAV4荣放等新款还是抄底现款"
    assert pk.lead_full_name("L03,先看你是哪类人", "小鹏MONA L03") == "小鹏MONA L03,先看你是哪类人"
    assert pk.lead_full_name("丰田RAV4荣放值不值", "丰田RAV4荣放") == "丰田RAV4荣放值不值"
    assert pk.lead_full_name("买荣放别盯新款", "丰田RAV4荣放") == "买荣放别盯新款"  # 不是简称开头不动
    long = "荣放" + "很" * 27
    assert pk.lead_full_name(long, "丰田RAV4荣放") == long  # 换了超长就不换


def test_build_titles_upgrades_short_name_and_drops_unled():
    raw = [{"kind": "stake", "text": "荣放等新款还是抄底现款"},
           {"kind": "person", "text": "预算有限,荣放选哪版"}]
    titles, _ = pk.build_titles(raw, "丰田RAV4荣放", allowed=ALLOWED, name="丰田RAV4荣放")
    assert titles[0].text == "丰田RAV4荣放等新款还是抄底现款"
    for t in titles:
        assert pk.leads_with_subject(t.text, "丰田RAV4荣放"), t.text


def test_fallback_titles_lead_with_name():
    for kind in pk.KINDS:
        text = pk.fallback_title(kind, "小米YU7", thesis="买的是电池不是屏幕", allowed=ALLOWED,
                                 name="小米YU7")
        assert text.startswith("小米YU7"), (kind, text)


def test_roundup_title_cannot_borrow_another_cars_number():
    script = [{"subject": "", "text": "10万级新能源汽车"},
              {"subject": "比亚迪元UP", "text": "CLTC续航301、401、501公里"},
              {"subject": "比亚迪海豚", "text": "520版冬天能跑约350到380公里"}]
    owners = pk.figure_owners(script)
    allowed = {"10", "301", "401", "501", "520", "350", "380"}
    bad = pk.check_title("比亚迪元UP 501公里版,冬季实测约350公里", allowed, owners=owners)
    assert any("350" in p and "比亚迪海豚" in p for p in bad)
    assert not pk.check_title("比亚迪元UP和海豚对比,501还是520", allowed, owners=owners)
    assert not pk.check_title("10万级纯电,续航350够不够", allowed, owners=owners)  # 没点名车不查


def test_title_figures_must_be_spoken():
    probs = pk.check_title("小米YU7补能15分钟", ALLOWED, spoken={"2335"})
    assert any("片里没说" in p for p in probs)
    assert not pk.check_title("小米YU7补能15分钟", ALLOWED, spoken={"15"})
