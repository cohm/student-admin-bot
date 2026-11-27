"""The programme / admission-year picker: static/programs.json feeds the
category and programme selects, and /api/chat carries the picked values
into the turn's priors.

What the picker shows replaces what memory holds, as if the student had
written it, so a student who picks their year is not asked for it; a hint
written in the question still wins further down, inside maybe_fetch_dynamic_web, and
the client moves the picker to it. An empty picker (null) changes nothing; ""
is the student emptying it.

A pick that answers the bot's last message is sent as the student's reply
("CTFYS", "2023"), which the pipeline joins with the waiting question.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from student_bot.bot.memory import ConversationMemory
from student_bot.config import get_config
from student_bot.web import app as web_app


@pytest.fixture
def cfg(tmp_path):
    c = get_config().model_copy(deep=True)
    c.paths.logs_db = tmp_path / "logs.sqlite"
    c.web.auth_enabled = False
    return c


@pytest.fixture
def memory(cfg, monkeypatch):
    """The app's conversation memory, so a test can see what the picker did to it."""
    mem = ConversationMemory(cfg)
    monkeypatch.setattr(web_app, "ConversationMemory", lambda _cfg: mem)
    return mem


@pytest.fixture
def client(cfg, memory, chat_turn_inline):
    return TestClient(web_app.create_app(cfg))


def test_the_programme_list_is_three_categories_of_valid_codes(cfg):
    """static/programs.json: KTH's "Hitta utbytesuniversitet" page gives the
    categories, the order and the Swedish names; the codes and English names
    are the programme catalogue's. Off the page: COPEN is added, "Hållbar
    samhällsplanering och anläggningsteknik" (no such programme) is left out,
    and "Datateknik 180 hp" is both TIDAB and TIDAA."""
    cats = json.loads((web_app.STATIC_DIR / "programs.json").read_text(encoding="utf-8"))
    assert [c["key"] for c in cats] == ["civilingenjor", "hogskoleingenjor_kandidat", "master"]
    assert [len(c["programs"]) for c in cats] == [20, 12, 48]
    codes = [code for c in cats for code, _sv, _en in c["programs"]]
    assert len(codes) == len(set(codes))
    assert all(re.fullmatch(r"[A-Z]{5}", code) for code in codes)  # what the router reads
    assert set(cfg.study_plan_cache.programmes) <= set(codes)


_YEAR = time.localtime().tm_year
_PICKED = str(_YEAR - 2)  # a year the picker offers


def _no_answer(*_a, **_kw):
    raise AssertionError("not validated")


@pytest.mark.parametrize(
    "field",
    [
        {"admission_year": "24"},
        {"admission_year": "0000"},
        {"admission_year": "0" + _PICKED},
        {"admission_year": str(_YEAR + 2)},
        {"admission_year": str(_YEAR - 9)},
    ],
)
def test_chat_rejects_a_malformed_picker_value(client, monkeypatch, field):
    monkeypatch.setattr(web_app, "answer", _no_answer)
    resp = client.post("/api/chat", json={"question": "Vilka kurser läser jag?", **field})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        {"program_code": "CDATE"},
        {"program_code": ""},
        {"admission_year": str(_YEAR + 1)},  # a browser clock ahead of the server's
        {"admission_year": str(_YEAR - 8)},
        {"admission_year": ""},
    ],
)
def test_chat_accepts_what_the_picker_offers(client, monkeypatch, field):
    monkeypatch.setattr(web_app, "answer", _no_answer)
    resp = client.post("/api/chat", json={"question": "Vilka kurser läser jag?", **field})
    assert resp.status_code == 200


_USER = web_app._web_user_id(web_app.ChatRequest(question="q"), None)
_PRIORS = ("program_prior", "admission_term_prior", "admission_year_prefix_prior")


@pytest.fixture
def captured(monkeypatch, memory):
    """The priors /api/chat passes to `answer`, one tuple per turn, with memory
    holding CTFYS and HT2021 from earlier turns."""
    memory.set_program_code(_USER, "default", "CTFYS")
    memory.set_admission_hints(_USER, "default", exact_term="20212")
    calls: list[tuple] = []

    def fake_answer(question, **kwargs):
        calls.append(tuple(kwargs[k] for k in _PRIORS))
        raise RuntimeError("captured")

    monkeypatch.setattr(web_app, "answer", fake_answer)
    return calls


def _ask(client, program_code=None, admission_year=None):
    body = {"question": "Vilka kurser?", "program_code": program_code}
    resp = client.post("/api/chat", json={**body, "admission_year": admission_year})
    assert resp.status_code == 200


@pytest.mark.parametrize("code", ["CDATE", "TCSCM"])
def test_the_picker_replaces_the_programme_and_year_from_memory(client, captured, code):
    _ask(client, code, _PICKED)
    assert captured == [(code, None, _PICKED)]


@pytest.mark.parametrize("code", ["AAAAA", "cdate", "CDATE1"])
def test_a_code_the_list_does_not_have_counts_as_no_pick(client, captured, code):
    """Anything but a listed code: a browser that still holds an older
    programs.json keeps working."""
    _ask(client, code)
    assert captured == [("CTFYS", "20212", None)]


def test_an_empty_picker_leaves_the_memory_priors_alone(client, captured):
    _ask(client)
    assert captured == [("CTFYS", "20212", None)]


def test_emptied_pickers_clear_the_memory_priors(client, captured):
    _ask(client, "", "")
    assert captured == [(None, None, None)]


def test_a_pick_back_to_the_value_sent_before_counts(memory):
    """The question named another programme and year, the pickers followed it,
    and the student puts them back before the next question."""
    memory.apply_picker("u", "t", "CDATE", "2024")
    memory.set_program_code("u", "t", "CTFYS")  # named in the question
    memory.set_admission_hints("u", "t", exact_term="20232", replace=True)
    memory.apply_picker("u", "t", "CDATE", "2024")
    assert memory.get_program_code("u", "t") == "CDATE"
    assert memory.get_admission_hints("u", "t") == (None, "2024")


def test_emptying_a_picker_that_followed_the_question_clears_it(memory):
    memory.apply_picker("u", "t", None, None)
    memory.set_program_code("u", "t", "CTFYS")  # named in the question
    memory.set_admission_hints("u", "t", year_prefix="2023", replace=True)
    memory.apply_picker("u", "t", "", "")
    assert memory.get_program_code("u", "t") is None
    assert memory.get_admission_hints("u", "t") == (None, None)


def _meta(cfg, client, monkeypatch, *, reason="programme_clarification", **fields):
    """The meta event of a /api/chat turn whose `answer` returned these fields."""
    from student_bot.bot.gate import GateDecision
    from student_bot.bot.pipeline import AnswerResult
    from student_bot.bot.retrieval import RetrievalResult

    cfg.topics.enabled = False
    result = AnswerResult(
        question="Vilka kurser?",
        lang="sv",
        answered=reason == "web_live",
        answer=fields.pop("answer", "Svar"),
        rendered="Svar",
        gate=GateDecision(reason == "web_live", reason, 0.0, 0.0, 0),
        retrieval=RetrievalResult(query="q"),
        latency_ms=0,
        **fields,
    )
    monkeypatch.setattr(web_app, "answer", lambda *_a, **_k: result)
    stream = client.post("/api/chat", json={"question": "Vilka kurser?"}).text
    return json.loads(stream.split("event: meta\ndata: ", 1)[1].split("\n", 1)[0])


@pytest.mark.parametrize("term,kind", [("20242", "year"), (None, "")])
def test_a_defaulted_answer_waits_for_the_year_picker(cfg, client, monkeypatch, term, kind):
    """The answer assumed the admission round: a picked year is the reply."""
    meta = _meta(cfg, client, monkeypatch, reason="web_live", defaulted_admission_term=term)
    assert meta["pending_kind"] == kind


def test_an_answer_that_only_mentions_a_round_waits_for_nothing(cfg, client, monkeypatch):
    meta = _meta(cfg, client, monkeypatch, reason="web_live", answer="Din antagningsomgång avgör.")
    assert meta["pending_kind"] == ""


def test_a_pick_does_not_revive_an_expired_conversation(memory, monkeypatch):
    memory.append("u", "t", "user", "En fråga")
    later = time.time() + memory.ttl + 1
    monkeypatch.setattr("student_bot.bot.memory.time.time", lambda: later)
    memory.apply_picker("u", "t", "CDATE", None)
    assert memory.get("u", "t") == []
    assert memory.take_expired_flag("u", "t")


def test_empty_pickers_start_no_conversation(memory, monkeypatch):
    """Or a student with nothing picked whose first question was refused
    would later be told that a conversation expired."""
    memory.apply_picker("u", "t", None, "")
    later = time.time() + memory.ttl + 1
    monkeypatch.setattr("student_bot.bot.memory.time.time", lambda: later)
    assert not memory.take_expired_flag("u", "t")


def test_meta_carries_what_the_turn_settled_on_not_what_memory_holds(
    cfg, client, monkeypatch, memory
):
    """The pickers follow the turn. A turn about nothing in particular must
    not move them back to what an earlier turn was about."""
    memory.set_program_code(_USER, "default", "CTFYS")
    meta = _meta(cfg, client, monkeypatch, reason="web_live")
    assert (meta["program_code"], meta["admission_year"]) == ("", "")


def test_meta_carries_a_year_stated_without_its_term(cfg, client, monkeypatch):
    meta = _meta(cfg, client, monkeypatch, reason="web_live", admission_year_prefix="2023")
    assert meta["admission_year"] == "2023"


def test_an_emptied_programme_picker_clears_the_programme(memory):
    """A "" after a pick is the student un-choosing it."""
    memory.apply_picker("u", "t", "CDATE", None)
    assert memory.get_program_code("u", "t") == "CDATE"
    memory.apply_picker("u", "t", "", None)
    assert memory.get_program_code("u", "t") is None


def test_an_emptied_year_picker_brings_back_the_newest_round(memory):
    memory.apply_picker("u", "t", None, "2024")
    assert memory.get_admission_hints("u", "t") == (None, "2024")
    memory.set_admission_hints("u", "t", exact_term="20222", replace=True)  # typed later
    memory.apply_picker("u", "t", None, "")
    assert memory.get_admission_hints("u", "t") == (None, None)


@pytest.mark.parametrize("program_code", [None, "CTFYS"])
def test_an_empty_picker_never_clears_anything(memory, program_code):
    """Signed in, memory is per account: another device's empty pickers, or a
    stored code the programme list no longer has, send null."""
    memory.apply_picker("u", "t", "CDATE", "2024")
    memory.set_program_code("u", "t", program_code or "CDATE")  # typed, or not
    memory.apply_picker("u", "t", None, None)
    assert memory.get_program_code("u", "t") == (program_code or "CDATE")
    assert memory.get_admission_hints("u", "t") == (None, "2024")


def test_a_picker_that_agrees_with_memory_changes_nothing(memory):
    """The client syncs the pickers to what the answer settled on."""
    memory.set_program_code("u", "t", "CDATE")
    memory.set_admission_hints("u", "t", exact_term="20231")
    memory.apply_picker("u", "t", "CDATE", "2023")
    assert memory.get_program_code("u", "t") == "CDATE"
    assert memory.get_admission_hints("u", "t") == ("20231", None)


@pytest.mark.parametrize("other", ["CDATE", ""])
def test_switching_programme_keeps_the_year_but_not_the_term(memory, other):
    """To another programme, or to none."""
    memory.apply_picker("u", "t", "CTFYS", None)
    memory.set_admission_hints("u", "t", exact_term="20212")
    memory.apply_picker("u", "t", other, None)
    assert memory.get_program_code("u", "t") == (other or None)
    assert memory.get_admission_hints("u", "t") == (None, "2021")


@pytest.mark.parametrize(
    "answer_text,kind",
    [
        ("Vilket program läser du? Skriv programkoden.", "programme"),
        ("Ditt program är inte entydigt. Vilket menar du?", "programme"),
        (
            "För att visa rätt utbildningsplan behöver jag veta vilken antagningsomgång som gäller.",
            "year",
        ),
        ("Du nämnde inte någon kurskod. Vilken menar du?", ""),
    ],
)
def test_meta_says_which_picker_answers_the_clarification(
    cfg, client, monkeypatch, answer_text, kind
):
    meta = _meta(
        cfg, client, monkeypatch, answer=answer_text, program_code="CTFYS", admission_term="20242"
    )
    assert meta["pending_kind"] == kind
    assert (meta["program_code"], meta["admission_year"]) == ("CTFYS", "2024")


# --- a pick that answers the bot is sent as the student's reply -------------

_COURSE_Q = "Jag klarade inte tentan i differentialekvationer, kan jag läsa reglerteknik?"
_PLAN_Q = "Vilka kurser är obligatoriska i årskurs 3?"
_URL = "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3"


def _answered(**fields):
    from student_bot.bot.retrieval import RetrievedChunk
    from student_bot.bot.web_retrieval import WebFetchResult

    chunk = RetrievedChunk(
        chunk_id=f"web:{_URL}",
        text="Obligatoriska kurser i årskurs 3.",
        rel_source=_URL,
        doc_title="CTFYS årskurs 3",
        doc_type="html",
        language="sv",
        section_path="",
        chunk_index=0,
        chroma_distance=0.0,
        rerank_score=3.5,
        source_url=_URL,
    )
    return WebFetchResult(
        chunks=[chunk], source_urls=[_URL], resolved_program_code="CTFYS", **fields
    )


def _clarified(sv):
    from student_bot.bot.web_retrieval import WebFetchResult

    return WebFetchResult(clarification=(sv, sv))


@pytest.fixture
def router(cfg, monkeypatch):
    """The real pipeline behind /api/chat, with the dynamic-web router's
    results queued in `replies` (None: no web route) and what it got in `calls`.
    The corpus finds nothing and the model answers "Svar."."""
    import student_bot.bot.pipeline as pipeline
    from student_bot.bot import course_resolver
    from student_bot.bot import web_retrieval as wr
    from student_bot.bot.gate import GateDecision
    from student_bot.bot.retrieval import RetrievalResult

    class _Router:
        def __init__(self):
            self.replies: list = []
            self.calls: list = []

        def __call__(self, _cfg, question, *_a, **kw):
            self.calls.append((question, kw))
            return self.replies.pop(0)

    r = _Router()
    cfg.topics.enabled = False
    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", r)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(pipeline, "_rerank_web_chunks", lambda _c, _q, _l, cands: list(cands))
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda *_a, **_kw: GateDecision(False, "top1<-0.5", -5, -5, 0)
    )
    monkeypatch.setattr(pipeline, "_stream_answer", lambda *_a, **_kw: iter(["Svar."]))
    monkeypatch.setattr(pipeline, "literacy_footer", lambda *_a, **_kw: "_Tips._")
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"ctfys": "CTFYS"})
    monkeypatch.setattr(wr, "_load_program_nicknames", lambda _cfg: {})
    monkeypatch.setattr(
        course_resolver, "cached_course_names", lambda _cfg: frozenset({"Reglerteknik"})
    )
    return r


@pytest.mark.parametrize(
    "question,first,reply,picks,earlier",
    [
        # An answer for the newest round; the student picks their year.
        (
            _PLAN_Q,
            _answered(defaulted_admission_term="20242"),
            _PICKED,
            {"admission_year": _PICKED},
            {},
        ),
        # The admission-round question (dynamic_web.default_newest_term off).
        (
            _PLAN_Q,
            _clarified("Vilken antagningsomgång gäller för dig? Ange t.ex. HT2022."),
            _PICKED,
            {"admission_year": _PICKED},
            {"admission_year": _PICKED},  # picked before: memory already holds it
        ),
        # "Vilket program läser du?" (pipeline._programme_ask).
        (_COURSE_Q, None, "CTFYS", {"program_code": "CTFYS"}, {}),
        # The ambiguous-programme list, with the picked programme already in memory.
        (
            _PLAN_Q,
            _clarified("Ditt program är inte entydigt. Vilket menar du? CTFYS eller TTFYM?"),
            "CTFYS",
            {"program_code": "CTFYS"},
            {"program_code": "CTFYS"},
        ),
    ],
)
def test_a_picked_reply_is_joined_with_the_waiting_question(
    client, memory, router, question, first, reply, picks, earlier
):
    router.replies = [first, _answered()]
    stream = client.post("/api/chat", json={"question": question, **earlier}).text
    meta = json.loads(stream.split("event: meta\ndata: ", 1)[1].split("\n", 1)[0])
    assert meta["pending_kind"] == ("year" if "admission_year" in picks else "programme")
    assert client.post("/api/chat", json={"question": reply, **picks}).status_code == 200

    routed, kw = router.calls[-1]
    assert routed.startswith(question) and f"\n\n{reply}" in routed
    if "admission_year" in picks:
        assert kw["admission_year_prefix_prior"] == _PICKED
    else:
        assert kw["program_prior"] == "CTFYS"
    stored = [m["content"] for m in memory.get(_USER, "default")]
    assert stored == [f"{question}\n\n{reply}", "Svar."]
    assert memory.get_pending_question(_USER, "default") is None
