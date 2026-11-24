"""A programme question with no admission round uses the newest one instead of
asking (`dynamic_web.default_newest_term`).

The defaulted term is not something the student said, so it never reaches
memory: `applied_admission_term` stays empty and the term travels separately as
`defaulted_admission_term`, which the displayed body (`numbered_body`) names.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import student_bot.bot.pipeline as pipeline
import student_bot.bot.web_retrieval as wr
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import answer, remember_turn, turn_priors
from student_bot.bot.retrieval import RetrievalResult, RetrievedChunk
from student_bot.bot.web_retrieval import (
    AdmissionHints,
    WebFetchResult,
    _select_programme_urls,
    parse_program_admission_hints,
)
from student_bot.config import get_config

_ROOT = "https://www.kth.se/student/kurser/program/CTFYS"
_TERMS = ["20252", "20242", "20241", "20232"]
_PAGE = (Path(__file__).parent / "fixtures" / "program_CTFYS_20242_arskurs3.html").read_text(
    encoding="utf-8"
)


# --- the router ----------------------------------------------------------


def test_no_hint_picks_the_newest_term():
    r = _select_programme_urls("CTFYS", _TERMS, AdmissionHints(), default_newest=True)
    assert r.queue_urls == [f"{_ROOT}/20252"]
    assert r.defaulted_term == "20252" and not r.clarification_sv


def test_a_year_with_several_terms_picks_the_newest_of_them():
    r = _select_programme_urls(
        "CTFYS", _TERMS, AdmissionHints(year_prefix="2024"), default_newest=True
    )
    assert r.queue_urls == [f"{_ROOT}/20242"]
    assert r.defaulted_term == "20242" and not r.clarification_sv


@pytest.mark.parametrize(
    "hints", [AdmissionHints(exact_term="20241"), AdmissionHints(year_prefix="2023")]
)
def test_a_term_the_student_pinned_down_is_not_defaulted(hints):
    r = _select_programme_urls("CTFYS", _TERMS, hints, default_newest=True)
    assert r.queue_urls and r.defaulted_term is None


@pytest.mark.parametrize(
    "stated, term",
    [
        ("antagen VT2024", "20241"),
        ("VT24", "20241"),
        ("våren 2024", "20241"),
        ("spring 2024", "20241"),
        ("hösten 2024", "20242"),
        ("autumn 2024", "20242"),
    ],
)
def test_a_stated_season_picks_its_round_of_the_year(stated, term):
    hints = parse_program_admission_hints(f"CTFYS årskurs 3, {stated}")
    r = _select_programme_urls("CTFYS", _TERMS, hints, default_newest=True)
    assert r.queue_urls == [f"{_ROOT}/{term}"]
    assert r.stated_term == term and r.defaulted_term is None


@pytest.mark.parametrize("text", ["Mitt antagningsår var 2024", "Som svar 2024 fick jag"])
def test_the_verb_var_is_not_the_spring_term(text):
    hints = parse_program_admission_hints(f"{text}. Vad gäller i årskurs 3 på CTFYS?")
    assert (hints.year_prefix, hints.season) == ("2024", None)


# --- through maybe_fetch_dynamic_web, with the flag from config -----------


class _NoCache:
    def put(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return None


@pytest.fixture
def kth(monkeypatch):
    """Every KTH programme page is the CTFYS fixture; the root lists `_TERMS`.
    Autumn 2026, so the students in årskurs 3 were admitted HT2024."""
    monkeypatch.setattr(wr.time, "localtime", lambda: time.strptime("2026-11-26", "%Y-%m-%d"))
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {"ctfys": "CTFYS"})
    monkeypatch.setattr(wr, "_fetch_html", lambda url, _cfg, **_kw: (url, _PAGE))
    monkeypatch.setattr(wr, "_normalized_programme_terms_from_store", lambda _s: list(_TERMS))
    monkeypatch.setattr(wr, "WebCache", lambda _cfg: _NoCache())


def _cfg(default_newest_term: bool):
    cfg = get_config().model_copy(deep=True)
    cfg.dynamic_web.enabled = True
    cfg.dynamic_web.default_newest_term = default_newest_term
    return cfg


def test_the_cohort_now_in_that_year_is_used_but_not_reported_as_applied(kth):
    r = wr.maybe_fetch_dynamic_web(_cfg(True), "Vad gäller i årskurs 3 på CTFYS?", "sv")
    assert r is not None and r.chunks and r.clarification is None
    assert any("/CTFYS/20242/arskurs3" in u for u in r.source_urls)
    assert r.defaulted_admission_term == "20242"
    assert r.applied_admission_term is None and r.applied_admission_year_prefix is None


def test_a_stated_season_is_reported_as_the_exact_term(kth):
    r = wr.maybe_fetch_dynamic_web(_cfg(True), "Vad gäller i årskurs 3 på CTFYS, VT2024?", "sv")
    assert r is not None and any("/CTFYS/20241/arskurs3" in u for u in r.source_urls)
    assert (r.applied_admission_term, r.applied_admission_year_prefix) == ("20241", None)
    assert r.defaulted_admission_term is None


def test_with_the_flag_off_it_asks_as_before(kth):
    assert get_config().dynamic_web.default_newest_term is True  # on as shipped
    r = wr.maybe_fetch_dynamic_web(_cfg(False), "Vad gäller i årskurs 3 på CTFYS?", "sv")
    assert r is not None and r.clarification and "antagningsomgång" in r.clarification[0]
    assert r.defaulted_admission_term is None


# --- the rendered answer ---------------------------------------------------


@pytest.fixture
def web_answer(monkeypatch):
    """A study-plan answer from the dynamic-web path. Returns the router's
    state: the `term` it defaults to, the `year` it applies, the `calls` it got."""
    url = f"{_ROOT}/20242/arskurs3"
    chunk = RetrievedChunk(
        chunk_id=f"web:{url}",
        text="Obligatoriska kurser i årskurs 3.",
        rel_source=url,
        doc_title="CTFYS årskurs 3",
        doc_type="html",
        language="sv",
        section_path="",
        chunk_index=0,
        chroma_distance=0.0,
        rerank_score=3.5,
        source_url=url,
    )
    defaulted: dict = {"term": None, "year": None, "calls": []}

    def _router(_cfg, question, *_a, **kw):
        defaulted["calls"].append((question, kw.get("admission_hints_override")))
        return WebFetchResult(
            chunks=[chunk],
            source_urls=[url],
            resolved_program_code="CTFYS",
            applied_admission_year_prefix=defaulted["year"],
            defaulted_admission_term=defaulted["term"],
        )

    monkeypatch.setattr(pipeline, "maybe_fetch_dynamic_web", _router)
    monkeypatch.setattr(pipeline, "_jargon", lambda _cfg: None)
    monkeypatch.setattr(pipeline, "_rerank_web_chunks", lambda _cfg, _q, _l, cands: list(cands))
    monkeypatch.setattr(pipeline, "retrieve", lambda _cfg, q, **_kw: RetrievalResult(query=q))
    defaulted["body"] = "Svar."

    def _llm(_cfg, messages, **_kw):
        defaulted["prompt"] = messages
        return iter([defaulted["body"]])

    monkeypatch.setattr(pipeline, "_stream_answer", _llm)
    monkeypatch.setattr(pipeline, "literacy_footer", lambda *_a, **_kw: "_Tips._")
    return defaulted


_Q = "Vilka kurser är obligatoriska i årskurs 3 på CTFYS?"
_LINE = (
    "Svaret utgår från utbildningsplanen för antagna HT2024. "
    'Antogs du en annan termin, skriv den, till exempel "HT2023".'
)


def test_a_defaulted_term_is_named_in_the_body_and_kept_out_of_memory(web_answer):
    web_answer["term"] = "20242"
    memory = ConversationMemory(get_config())
    streamed: list[str] = []

    result = answer(_Q, cfg=get_config(), on_token=streamed.append, **turn_priors(memory, "u", "t"))
    remember_turn(memory, "u", "t", _Q, result)

    # In the body every frontend displays, streamed and rendered alike.
    assert result.numbered_body == f"Svar.\n\n{_LINE}"
    assert result.numbered_body in result.rendered and result.numbered_body in "".join(streamed)
    assert _LINE not in result.answer
    assert memory.get_admission_hints("u", "t") == (None, None)


def _turn(memory, question):
    result = answer(question, cfg=get_config(), **turn_priors(memory, "u", "t"))
    remember_turn(memory, "u", "t", question, result)
    return result


def test_a_reply_stating_the_round_asks_the_defaulted_question_again(web_answer):
    web_answer["term"] = "20242"
    memory = ConversationMemory(get_config())
    _turn(memory, _Q)

    web_answer.update(term=None, year="2023")
    _turn(memory, "Jag antogs HT2023")

    # The router gets the question again (jargon-expanded) with the reply's year.
    question, override = web_answer["calls"][-1]
    assert question.startswith("Vilka kurser") and question.endswith("\n\nJag antogs HT2023")
    assert override == AdmissionHints(year_prefix="2023", season="2")
    # The answer for the assumed round is not shown to the model again.
    assert [m["role"] for m in web_answer["prompt"]].count("assistant") == 0
    # One stored pair for the question, and the stated year is kept.
    stored = [m["content"] for m in memory.get("u", "t")]
    assert stored == [f"{_Q}\n\nJag antogs HT2023", "Svar."]
    assert memory.get_admission_hints("u", "t") == (None, "2023")
    # Nothing is pending after an answer for a stated round.
    _turn(memory, "2022")
    assert web_answer["calls"][-1][0] == "2022"


@pytest.mark.parametrize(
    "followup",
    [
        "Vilka kurser lästes 2023 i årskurs 1?",
        "Kan jag plugga utomlands HT2026?",
        "Tack! Jag tog SF1625 2024 men fick underkänt.",
    ],
)
@pytest.mark.parametrize("first_answer", ["Svar.", "Din antagningsomgång avgör."])
def test_a_followup_that_mentions_a_term_stands_alone(web_answer, followup, first_answer):
    """Only a message that is nothing but a round is a reply. The second
    answer text looks like the admission-round clarification to the text
    detectors, which must not run after a defaulted answer."""
    web_answer.update(term="20242", body=first_answer)
    memory = ConversationMemory(get_config())
    _turn(memory, _Q)
    _turn(memory, followup)
    assert web_answer["calls"][-1] == (followup, None)
    assert len(memory.get("u", "t")) == 4


@pytest.mark.parametrize("term, body", [("20252", "Svar."), ("20242", "")])
def test_no_note_without_that_study_plan_or_without_an_answer(web_answer, monkeypatch, term, body):
    """The fetched page is 20242: another term's note would name a plan the
    model never saw, and an empty answer has nothing to qualify."""
    web_answer["term"] = term
    monkeypatch.setattr(pipeline, "_stream_answer", lambda *_a, **_kw: iter([body]))
    result = answer(_Q, cfg=get_config())
    assert "utbildningsplanen för antagna" not in result.rendered


@pytest.mark.parametrize("reply", ["HT2023", "2023", "Jag antogs 2023", "I was admitted in 2023"])
def test_a_message_that_is_only_a_round_is_a_reply(reply):
    assert wr.admission_round_only_hints(reply).year_prefix == "2023"


def test_the_english_note_names_the_season():
    assert "admitted spring 2024." in pipeline._defaulted_term_note("en", "20241")


def test_the_default_is_the_newest_started_cohort(monkeypatch):
    """KTH lists the next two autumns ahead of time; nobody is admitted yet."""
    monkeypatch.setattr(wr.time, "localtime", lambda: time.strptime("2026-11-26", "%Y-%m-%d"))
    future = ["20272", "20262", "20252", "20242"]
    r = _select_programme_urls("CTFYS", future, AdmissionHints(), default_newest=True)
    assert r.defaulted_term == "20262"
    # A question about årskurs 3 is about the cohort admitted two years earlier.
    r = _select_programme_urls("CTFYS", future, AdmissionHints(), year_level=3, default_newest=True)
    assert r.queue_urls == [f"{_ROOT}/20242/arskurs3"] and r.defaulted_term == "20242"
    r = _select_programme_urls("CTFYS", ["20282", "20272"], AdmissionHints(), default_newest=True)
    assert r.defaulted_term == "20272"  # only future rounds: the first of them
    r = _select_programme_urls("CTFYS", future, AdmissionHints(), question="Vem är PA för CTFYS?")
    assert r.queue_urls == [f"{_ROOT}/20262"] and r.defaulted_term is None
    # A spring cohort has started from January, the autumn one not before August.
    monkeypatch.setattr(wr.time, "localtime", lambda: time.strptime("2027-03-01", "%Y-%m-%d"))
    spring = ["20272", "20271", "20262"]
    r = _select_programme_urls("CTFYS", spring, AdmissionHints(), default_newest=True)
    assert r.defaulted_term == "20271"
