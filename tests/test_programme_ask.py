"""A course question with no code, no programme known and nothing in the
corpus asks for the programme instead of answering that nothing was found.
A typed code, or the web picker's pick sent as the reply, is merged with the
waiting question like the other programme picks."""

import pytest

import student_bot.bot.course_resolver as course_resolver
import student_bot.bot.pipeline as pipeline
import student_bot.bot.web_retrieval as web_retrieval
from student_bot.bot.gate import GateDecision
from student_bot.bot.pipeline import answer
from student_bot.bot.retrieval import RetrievalResult
from student_bot.bot.web_retrieval import (
    _extract_targets_with_cfg,
    merge_programme_clarification_followup,
)
from student_bot.config import get_config

Q = "Jag klarade inte tentan i differentialekvationer, kan jag fortfarande läsa reglerteknik?"
ASK_SV = "Vilket program läser du?"


NAMES = frozenset(
    {"Reglerteknik, allmän kurs", "Differentialekvationer och transformmetoder", "Finansiering"}
)


@pytest.fixture
def programmes(monkeypatch):
    """What the programme alias resolution knows, without the network."""
    aliases = {"ctfys": "CTFYS", "civilingenjörsutbildning i datateknik": "CDATE"}
    monkeypatch.setattr(web_retrieval, "_get_program_aliases", lambda _cfg: dict(aliases))
    monkeypatch.setattr(
        web_retrieval, "_load_program_nicknames", lambda _cfg: {"teknisk fysik": ["CTFYS", "TTFYM"]}
    )
    monkeypatch.setattr(web_retrieval, "_cached_terms_for_code", lambda _cfg, _code: [])


@pytest.fixture
def empty_corpus(monkeypatch, programmes):
    """No web route, no corpus hit, an LLM that would answer 'Svar.', and
    three course names in the study-plan cache."""
    monkeypatch.setattr(course_resolver, "cached_course_names", lambda _cfg: NAMES)
    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", lambda *_a, **_kw: None)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda _cfg, _r: GateDecision(False, "top1<-0.5", -4.0, -4.0, 0)
    )
    monkeypatch.setattr(pipeline, "_stream_answer", lambda *_a, **_kw: iter(["Svar."]))
    monkeypatch.setattr(pipeline, "literacy_footer", lambda *_a, **_kw: "_Tips._")


@pytest.mark.parametrize(
    "channel,how",
    [
        ("web", "Välj det ovanför frågan eller skriv programkoden"),
        ("mattermost", "Skriv programkoden"),
        ("cli", "Skriv programkoden"),
    ],
)
def test_asks_for_the_programme_when_the_corpus_has_nothing(empty_corpus, channel, how):
    streamed: list[str] = []
    result = answer(Q, cfg=get_config(), channel=channel, on_token=streamed.append)
    assert result.gate.reason == "programme_clarification"
    assert not result.answered
    assert result.answer.startswith(ASK_SV) and how in result.answer
    assert result.rendered == result.answer
    assert streamed == [result.answer]


@pytest.mark.parametrize(
    "q,how",
    [
        (Q, "Välj det ovanför frågan eller skriv programkoden"),
        (
            "I failed the exam, can I still take the course in reglerteknik next year?",
            "Pick it above the question or write the programme code",
        ),
    ],
)
def test_the_web_ui_points_at_the_programme_picker(
    empty_corpus, chat_turn_inline, tmp_path, q, how
):
    """Through /api/chat, which answers as the web channel."""
    from fastapi.testclient import TestClient

    from student_bot.web import app as web_app

    cfg = get_config().model_copy(deep=True)
    cfg.paths.logs_db = tmp_path / "logs.sqlite"
    cfg.web.auth_enabled = False
    cfg.topics.enabled = False
    stream = TestClient(web_app.create_app(cfg)).post("/api/chat", json={"question": q}).text
    assert how in stream


def test_old_exams_are_a_course_word_too(empty_corpus):
    q = "Var hittar jag gamla tentor i reglerteknik?"
    assert answer(q, cfg=get_config()).gate.reason == "programme_clarification"


def test_an_english_question_is_asked_in_english(empty_corpus):
    q = "I failed the exam, can I still take the course in reglerteknik next year?"
    result = answer(q, cfg=get_config())
    assert result.answer == "Which programme do you study? Write the programme code, e.g. CTFYS."


def test_no_ask_when_the_corpus_answers(empty_corpus, monkeypatch):
    monkeypatch.setattr(
        pipeline, "evaluate_gate", lambda _cfg, _r: GateDecision(True, "ok", 4.0, 4.0, 1)
    )
    assert answer(Q, cfg=get_config()).gate.reason != "programme_clarification"


def test_with_a_programme_known_it_falls_back_as_before(empty_corpus):
    result = answer(Q, cfg=get_config(), program_prior="CTFYS")
    assert result.gate.reason != "programme_clarification"
    assert "Svar." in result.answer


@pytest.mark.parametrize(
    "q",
    [
        "Hur överklagar jag ett beslut om tillgodoräknande?",  # no course named
        "Hur anmäler jag mig till tentamen?",  # course intent, but no course named
        "När är tentan i reglerteknik, EL1000?",  # a code: the course page is fetched instead
        "kan jag läsa reglerteknik?",  # a course named, but no course word
        "Var finns kursmaterialet i reglerteknik?",  # nor one the web route knows
        # "Finansiering" is a course, but not what the course word is about here.
        "Är det svårt att få finansiering för en kurs utomlands?",
    ],
)
def test_no_ask_without_a_course_named(empty_corpus, q):
    assert answer(q, cfg=get_config()).gate.reason != "programme_clarification"


@pytest.mark.parametrize(
    "q",
    [
        "Vem är examinator för kursen reglerteknik?",  # the web route skips contact questions
        "Kan jag läsa kursen reglerteknik på CTFYS?",  # a programme named by code
    ],
)
def test_no_ask_where_a_programme_cannot_help(empty_corpus, q):
    assert answer(q, cfg=get_config()).gate.reason != "programme_clarification"


def test_a_programme_name_the_router_does_not_route_on_still_asks(empty_corpus):
    # The router looks for a programme only in a question about one (a code,
    # "program", a year), and "teknisk fysik" is two programmes anyway.
    q = "När är tentan i reglerteknik för teknisk fysik?"
    assert answer(q, cfg=get_config()).gate.reason == "programme_clarification"


def test_no_ask_with_the_web_route_off(empty_corpus):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.enabled = False
    assert answer(Q, cfg=cfg).gate.reason != "programme_clarification"


def test_no_ask_with_the_question_switched_off(empty_corpus):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.ask_programme_for_course = False
    result = answer(Q, cfg=cfg)
    assert result.gate.reason != "programme_clarification"
    assert "Svar." in result.answer


def _after_the_ask(reply: str, pending: str = Q):
    history = [
        {"role": "user", "content": pending},
        {"role": "assistant", "content": f"{ASK_SV} Skriv programkoden, t.ex. CTFYS."},
    ]
    return answer(reply, history=history, cfg=get_config(), pending_question_prior=pending)


def test_a_reply_naming_the_programme_is_not_asked_again(empty_corpus):
    pending = "När är tentan i reglerteknik?"
    result = _after_the_ask("CTFYS, kursen reglerteknik", pending)
    assert result.merged_question == f"{pending}\n\nCTFYS, kursen reglerteknik"
    assert result.gate.reason != "programme_clarification"


def test_a_merged_reply_is_not_asked_again(empty_corpus):
    # A course question on its own, with no programme: still not asked twice.
    reply = "Jag menar kursen reglerteknik"
    result = _after_the_ask(reply)
    assert result.merged_question == f"{Q}\n\n{reply}"
    assert result.gate.reason != "programme_clarification"


def test_a_typed_code_is_merged_with_the_waiting_question(programmes):
    history = [
        {"role": "user", "content": Q},
        {"role": "assistant", "content": f"{ASK_SV} Skriv programkoden, t.ex. CTFYS."},
    ]
    merged = merge_programme_clarification_followup(
        "CTFYS", history, get_config(), pending_question=Q
    )
    assert merged == f"{Q}\n\nCTFYS"
    assert (
        merge_programme_clarification_followup("tack", history, get_config(), pending_question=Q)
        == "tack"
    )


@pytest.mark.parametrize(
    "reply, merged",
    [
        ("datateknik", f"{Q}\n\nCDATE (datateknik)"),  # names one programme
        ("teknisk fysik", "teknisk fysik"),  # names two: CTFYS and TTFYM
        ("Är tentan svår?", "Är tentan svår?"),
    ],
)
def test_a_programme_name_is_merged_when_it_means_one_programme(programmes, reply, merged):
    history = [
        {"role": "user", "content": Q},
        {"role": "assistant", "content": f"{ASK_SV} Skriv programkoden, t.ex. CTFYS."},
    ]
    got = merge_programme_clarification_followup(reply, history, get_config(), pending_question=Q)
    assert got == merged


def test_a_merged_programme_name_is_routed_like_its_code(programmes):
    merged = f"{Q}\n\nCDATE (datateknik)"
    typed = f"{Q}\n\nCDATE"
    assert _extract_targets_with_cfg(merged, get_config()) == _extract_targets_with_cfg(
        typed, get_config()
    )
    assert _extract_targets_with_cfg(merged, get_config()) == [
        "https://www.kth.se/student/kurser/program/CDATE"
    ]


def test_course_names_come_from_the_cached_study_plans(tmp_path, monkeypatch):
    from student_bot.bot.web_cache import WebCache

    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.cache_db = str(tmp_path / "web_cache.sqlite")
    cache = WebCache(cfg)
    rows = [
        # A malformed row is skipped; the others still count.
        ("https://www.kth.se/student/kurser/program/CDATE/20242/arskurs1#study-plan", "{oops"),
        (
            "https://www.kth.se/student/kurser/program/CTFYS/20242/arskurs3#study-plan",
            '[{"code": "EL1000", "name": "Reglerteknik, allmän kurs"}]',
        ),
        # Not a study plan page: its names are not course names to look for.
        (
            "https://www.kth.se/student/kurser/kurs/EL1000",
            '[{"code": "SF1625", "name": "Envariabelanalys"}]',
        ),
    ]
    with cache._connect() as conn:
        for url, content in rows:
            conn.execute(
                "INSERT INTO web_cache (url, title, content, fetched_at) VALUES (?, '', ?, 0)",
                (url, content),
            )
    monkeypatch.setattr(course_resolver, "_cached_names", None)
    assert course_resolver.cached_course_names(cfg) == {"Reglerteknik, allmän kurs"}
    assert course_resolver.question_names_a_course(cfg, "Vad kräver kursen reglerteknik?")
    assert not course_resolver.question_names_a_course(cfg, "Hur anmäler jag mig till tentamen?")


def test_an_unreadable_cache_is_not_remembered(tmp_path, monkeypatch):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.cache_db = str(tmp_path)  # a directory: sqlite cannot open it
    monkeypatch.setattr(course_resolver, "_cached_names", None)
    assert course_resolver.cached_course_names(cfg) == frozenset()
    assert course_resolver._cached_names is None


def test_an_empty_cache_is_not_remembered(tmp_path, monkeypatch):
    """Before the first warm there are no names; the next question looks again."""
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.cache_db = str(tmp_path / "web_cache.sqlite")
    monkeypatch.setattr(course_resolver, "_cached_names", None)
    assert course_resolver.cached_course_names(cfg) == frozenset()
    assert course_resolver._cached_names is None
