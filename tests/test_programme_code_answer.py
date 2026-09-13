"""Answering "what is the programme code for X?" (#85).

The router already resolved the name to a code in order to pick a URL — that
was never the gap. The gap was that the code stopped at the URL: the model got
the fetched page and the links, never the code, so it answered that the
information was not in the context. It was not. It was in the router.
"""

from __future__ import annotations

import pytest

from student_bot.config import get_config
from student_bot.bot.pipeline import (
    _alias_language,
    _glossary_with_codes,
    _resolve_program_codes,
)
from student_bot.bot.web_retrieval import _extract_program_candidates


@pytest.fixture(scope="module")
def cfg():
    return get_config()


# --- the router already knew ---------------------------------------------


@pytest.mark.parametrize(
    "question,code",
    [
        ("Vad har masterprogrammet i teknisk fysik för programkod?", "TTFYM"),
        ("Vilken är programkoden för civilingenjör teknisk fysik?", "CTFYS"),
    ],
)
def test_the_programme_is_resolved_from_its_name(cfg, question, code):
    candidates, _ = _extract_program_candidates(question, cfg)
    assert candidates, question
    assert candidates[0].code == code


# --- and now it reaches the prompt ---------------------------------------


def test_a_resolved_code_becomes_a_glossary_line(cfg):
    glossary = _glossary_with_codes("", "sv", _resolve_program_codes(cfg, "TTFYM", "sv"))
    assert "- TTFYM = " in glossary
    assert glossary.startswith("Ordlista:")


def test_it_appends_to_an_existing_jargon_glossary(cfg):
    """Jargon runs first; the codes must not replace its block."""
    glossary = _glossary_with_codes(
        "Ordlista:\n- PA = programansvarig", "sv", _resolve_program_codes(cfg, "TTFYM", "sv")
    )
    assert "- PA = programansvarig" in glossary
    assert "- TTFYM = " in glossary
    assert glossary.count("Ordlista:") == 1


def test_english_gets_an_english_heading(cfg):
    assert _glossary_with_codes("", "en", {"TTFYM": "x"}).startswith("Glossary:")


def test_nothing_to_add_leaves_the_glossary_alone():
    assert _glossary_with_codes("Ordlista:\n- PA = x", "sv", {}) == "Ordlista:\n- PA = x"
    assert _glossary_with_codes("", "sv", {"TTFYM": "   "}) == ""


# --- the glossary names the programme in the reader's language ------------


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("civilingenjörsutbildning i teknisk fysik", "sv"),
        ("masterprogram, teknisk fysik", "sv"),
        ("degree programme in vehicle engineering", "en"),
        ("master's programme, engineering physics", "en"),
    ],
)
def test_alias_language_detection(alias, expected):
    assert _alias_language(alias) == expected


def test_the_glossary_name_follows_the_conversation(cfg):
    """Without this the choice is made by string length — a coin flip that gave
    CTFYS a Swedish name and TTFYM an English one."""
    sv = _resolve_program_codes(cfg, "TTFYM CTFYS", "sv")
    en = _resolve_program_codes(cfg, "TTFYM CTFYS", "en")
    assert _alias_language(sv["TTFYM"]) == "sv"
    assert _alias_language(sv["CTFYS"]) == "sv"
    assert _alias_language(en["TTFYM"]) == "en"
    assert _alias_language(en["CTFYS"]) == "en"


def test_the_retrieval_query_is_deliberately_NOT_language_matched(cfg):
    """Load-bearing, and counter-intuitive enough to pin.

    The corpus is 127 Swedish files to 59 English, and the programme-director
    pages are Swedish. Expanding "Who is the programme director for CFATE?"
    with the English programme name moves it away from the only document that
    answers it: measured, recall@5 went 44/45 -> 43/45 when the language
    preference was applied to the query as well as the glossary.
    """
    from student_bot.bot.pipeline import build_retrieval_query

    expanded, _, _ = build_retrieval_query(cfg, "Who is the programme director for CFATE?", "en")
    assert "Civilingenjörsutbildning i farkostteknik" in expanded
    assert "Degree programme in vehicle engineering" not in expanded


def test_a_missing_language_falls_back_rather_than_dropping_the_code(cfg):
    """A code whose aliases give no language signal must still be named."""
    assert _resolve_program_codes(cfg, "CTFYS", "de")["CTFYS"]
