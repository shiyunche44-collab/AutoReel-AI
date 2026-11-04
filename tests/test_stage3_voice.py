"""人设、口碑抽取和账号主人亲历:转述要有来源,第一人称只属于亲历事实。"""
from __future__ import annotations

import pytest

from app import config, db
from app.categories import load_profile
from app.db.core import connect
from app.models import Fact, ScriptSegment
from app.pipeline import derive, intake, script_gen
from app.pipeline.script_gen.lint_checks import (
    firsthand_lint,
    ledger_use_lint,
    review_use_lint,
    stock_phrase_lint,
)
from app.pipeline.script_gen.prompt import reviewer_identity
from app.pipeline.script_gen.spoken_pass import rewrite_spoken
from app.providers.facts import RawDoc


@pytest.fixture
def profile():
    return load_profile("auto_long")


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "data" / "projects")
    monkeypatch.setattr(db, "_INITIALISED", False)
    return db


def _doc(text: str, query: str = "星愿 试驾 评测 实测 体验 车主 口碑") -> RawDoc:
    return RawDoc(tool="ark.web_search", query=query, retrieved_at="2026-11-04", text=text)


class _Llm:
    def __init__(self, *claims: str, error: Exception | None = None) -> None:
        self.claims, self.error, self.user = claims, error, ""

    def structured(self, system, user, required_keys=None, temperature=0.0):
        self.user = user
        if self.error:
            raise self.error
        return {"impressions": [{"claim": c, "basis": "车主口碑"} for c in self.claims]}


def _reviews(profile, llm, material="过减速带颠,电耗8.8kWh/100km", **kw):
    return intake.extract_reviews([_doc(material)], subject="星愿", llm=llm, vocab=profile.vocab, **kw)


def test_persona_reaches_identity_without_invented_experience(profile):
    who = reviewer_identity(profile.vocab)
    assert "柒哥" in who and "说白了" in who and "不假装自己开过" in who
    assert not any(term in profile.vocab.persona for term in profile.vocab.firsthand_terms)


def test_reviews_become_attributed_product_facts(profile):
    got = _reviews(profile, _Llm("有车主反映过减速带挺颠"))
    assert [f.claim for f in got] == ["有车主反映过减速带挺颠"]
    fact = got[0]
    assert (fact.topic, fact.scope, fact.source_kind) == (script_gen.REVIEW_TOPIC, "product", "media")


def test_reviews_only_read_review_searches(profile):
    llm = _Llm("有车主反映过减速带挺颠")
    other = _doc("参数", query="星愿 参数")
    assert intake.extract_reviews([other], subject="星愿", llm=llm, vocab=profile.vocab) == []
    assert llm.user == ""


@pytest.mark.parametrize("claim", [
    "有车主实测电耗9.9kWh/100km",   # 素材里没有这个数
    "过减速带挺颠",                  # 没说是谁说的
])
def test_reviews_drop_unsourced_claims(profile, claim):
    assert _reviews(profile, _Llm(claim)) == []


def test_reviews_skip_what_was_already_extracted(profile):
    old = Fact(claim="有车主实测电耗8.8kWh/100km", source_tool="t", topic=script_gen.REVIEW_TOPIC)
    got = _reviews(profile, _Llm("有车主实测电耗8.8kWh/100km左右", "有车主反映过减速带挺颠"), existing=[old])
    assert [f.claim for f in got] == ["有车主反映过减速带挺颠"]


def test_review_failure_does_not_block(profile):
    assert _reviews(profile, _Llm(error=RuntimeError("超时"))) == []


def _seg(text: str, refs: list[str]) -> ScriptSegment:
    return ScriptSegment(idx=0, section="论点一", text=text, fact_refs=refs)


def test_firsthand_allowed_only_with_owner_fact(profile):
    mine = intake.owner_facts("开了两周通勤,方向很轻")[0]
    assert mine.source_kind == "owner" and mine.basis == "个人体验"
    text = "我开过两周通勤,方向很轻。"
    assert not firsthand_lint([_seg(text, [mine.id])], profile, [mine])
    assert firsthand_lint([_seg(text, ["f_other"])], profile, [mine])[0].is_error


def test_owner_facts_split_by_line_and_skip_blanks():
    got = intake.owner_facts("第一条\n\n  第二条  \n")
    assert [f.claim for f in got] == ["第一条", "第二条"]
    assert intake.owner_facts("") == []


def test_sync_owner_facts_follows_the_notes(fresh_db, monkeypatch):
    store = {"facts.json": [Fact(id="f1", claim="老事实", source_tool="t").model_dump()]}
    monkeypatch.setattr(db, "read_json", lambda pid, name, default=None: store.get(name, default))
    monkeypatch.setattr(db, "write_json", lambda pid, name, data: store.__setitem__(name, data))
    intake.sync_owner_facts("p", "方向很轻")
    assert [f["source_kind"] for f in store["facts.json"]] == ["", "owner"]
    intake.sync_owner_facts("p", "方向很轻\n过减速带颠")
    assert [f["claim"] for f in store["facts.json"]] == ["老事实", "方向很轻", "过减速带颠"]
    intake.sync_owner_facts("p", "")
    assert [f["claim"] for f in store["facts.json"]] == ["老事实"]


def test_owner_notes_column_exists_and_defaults_empty(fresh_db):
    db.init_db()
    with connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    assert "owner_notes" in cols


def _review(fid: str) -> Fact:
    return Fact(id=fid, claim="有车主反映很颠", source_tool="t", topic=script_gen.REVIEW_TOPIC,
                scope="product")


def test_review_use_warns_when_reviews_are_ignored():
    facts = [_review("r1"), _review("r2"), _review("r3")]
    assert review_use_lint([_seg("没用转述。", ["r1"])], facts)[0].severity == "warning"
    assert not review_use_lint([_seg("用了转述。", ["r1", "r2"])], facts)
    assert not review_use_lint([_seg("只有一条可用。", [])], facts[:1])


def test_catchphrase_limit(profile):
    word = profile.vocab.catchphrases[0]
    assert not stock_phrase_lint([_seg(f"{word},贵。{word},不值。", [])], profile)
    assert "口头禅" in stock_phrase_lint([_seg(f"{word}。" * 3, [])], profile)[0].message


def _price(claim: str) -> Fact:
    return Fact(id="f_p", claim=claim, source_tool="t", topic="售价", scope="product", confidence="high")


def _ledger_of(claim: str) -> list[str]:
    return [f.claim.split("(推算")[0] for f in derive.derive([_price(claim)], ["trim_ledger"])]


def test_ledger_compares_same_trim_then_cheapest_step():
    got = _ledger_of("权益价:310km向往版6.18万元,310km乘风版6.88万元,410km向往版6.88万元,"
                     "410km乘风版7.58万元,480km探索版8.38万元")
    assert got == ["310km向往版和410km向往版差价 7000 元,续航多 100km,每多 1 公里续航约 70 元",
                   "410km乘风版和480km探索版差价 8000 元,续航多 70km,每多 1 公里续航约 114 元"]


def test_ledger_reads_a_stated_gap():
    got = _ledger_of("410km版型提供310km续航反选权益,反选后价格减7000元")
    assert got == ["310km和410km差价 7000 元,续航多 100km,每多 1 公里续航约 70 元"]


@pytest.mark.parametrize("claim", [
    "全系售价6.18万元至9.18万元,CLTC续航410km",   # 没有版本对应关系
    "410km向往版售价6.88万元",                     # 只有一档
    "480km探索版售价8.38万元,410km乘风版售价8.38万元",  # 没有差价
])
def test_ledger_needs_a_real_step(claim):
    assert _ledger_of(claim) == []


def _ledger_facts() -> list[Fact]:
    """差价账和它的来源:来源停用的推算事实不算可用。"""
    price = _price("410km版型反选310km,价格减7000元")
    return [price, *derive.derive([price], ["trim_ledger"])]


def test_ledger_lint_wants_both_amounts_said():
    facts = _ledger_facts()
    ref = facts[1].id
    assert ledger_use_lint([_seg("这笔差价不值。", [])], facts)[0].severity == "warning"
    assert ledger_use_lint([_seg("差7000元,不值。", [ref])], facts)
    assert not ledger_use_lint([_seg("差7000元,每多1公里续航约70元。", [ref])], facts)
    assert not ledger_use_lint([_seg("没有账可算。", [])], [])


def test_spoken_pass_keeps_ledger_amounts(profile):
    fact = _ledger_facts()[1]
    old = "这个版本贵7000元,每多1公里续航约70元,城里通勤不值。"

    class Rewriter:
        def structured(self, system, user, required_keys=None, temperature=0.0):
            return {"segments": [{"i": 0, "text": "这个版本贵不少,算下来多出来的续航挺贵,城里通勤真不值这钱。"}]}

    got = rewrite_spoken([_seg(old, [fact.id])], subject="星愿", profile=profile, facts=[fact], llm=Rewriter())
    assert got[0].text == old
