"""Programme-code resolution and query expansion (pipeline).

Codes are opaque to retrieval: the PA pages for ABE, CBH and SCI list
programmes by name only, so "Vem är PA för CFATE?" has nothing to match until
the code is expanded to "Civilingenjörsutbildning i farkostteknik".

`_resolve_program_codes` is exercised against a stubbed alias table rather
than the real one, which is scraped at runtime and would make these tests
depend on kth.se.
"""

from __future__ import annotations

import pytest

from student_bot.bot import pipeline as pl

# alias -> code, shaped like the real data/program_aliases.json
_ALIASES = {
    "ctfys": "CTFYS",
    "civilingenjörsutbildning i teknisk fysik": "CTFYS",
    "degree programme in engineering physics": "CTFYS",
    "cfate": "CFATE",
    "civilingenjörsutbildning i farkostteknik": "CFATE",
    "copen": "COPEN",
    "civilingenjörsutbildning öppen ingång": "COPEN",
    "media": "MEDIA",
    "civilingenjörsutbildning i medieteknik": "MEDIA",
    "times": "TIMES",
    "degree programme in mechanical engineering and economics": "TIMES",
}


@pytest.fixture
def cfg(monkeypatch):
    """A stand-in config plus a stubbed alias table.

    `_resolve_program_codes` imports `_get_program_aliases` lazily from
    web_retrieval, so the stub is installed on that module.
    """
    from student_bot.bot import web_retrieval

    monkeypatch.setattr(web_retrieval, "_get_program_aliases", lambda _cfg: _ALIASES)
    return object()


class TestResolveProgramCodes:
    def test_uppercase_code_resolves_to_official_name(self, cfg):
        assert pl._resolve_program_codes(cfg, "Vem är PA för CFATE?") == {
            "CFATE": "civilingenjörsutbildning i farkostteknik"
        }

    def test_lowercase_code_resolves_identically(self, cfg):
        # Regression: the original regex was [A-Z]{5}, so half the real
        # traffic ("ctfys") was silently skipped.
        assert pl._resolve_program_codes(cfg, "vem är pa för cfate?") == {
            "CFATE": "civilingenjörsutbildning i farkostteknik"
        }

    def test_picks_the_longest_alias_as_the_official_name(self, cfg):
        # CTFYS has three aliases; the longest is the official Swedish name.
        got = pl._resolve_program_codes(cfg, "CTFYS")
        assert got == {"CTFYS": "civilingenjörsutbildning i teknisk fysik"}

    def test_two_codes_in_one_query(self, cfg):
        got = pl._resolve_program_codes(cfg, "skillnaden på ctfys och cfate?")
        assert set(got) == {"CTFYS", "CFATE"}

    def test_ordinary_five_letter_words_are_not_codes(self, cfg):
        assert pl._resolve_program_codes(cfg, "Vilka kurser finns under andra året?") == {}

    def test_no_codes_returns_empty(self, cfg):
        assert pl._resolve_program_codes(cfg, "Hur överklagar jag ett betyg?") == {}


class TestAmbiguousCodes:
    """MEDIA and TIMES are also words people write, so they need the
    verbatim uppercase form. COPEN is deliberately not guarded — it only
    appeared in web2 (Webster's 1934) as an archaic colour term."""

    def test_lowercase_times_in_prose_is_not_a_code(self, cfg):
        assert pl._resolve_program_codes(cfg, "How many times can I retake an exam?") == {}

    def test_uppercase_times_is_a_code(self, cfg):
        assert pl._resolve_program_codes(cfg, "Vem är PA för TIMES?") == {
            "TIMES": "degree programme in mechanical engineering and economics"
        }

    def test_lowercase_media_in_prose_is_not_a_code(self, cfg):
        assert pl._resolve_program_codes(cfg, "Var hittar jag media om KTH?") == {}

    def test_copen_resolves_in_either_case(self, cfg):
        expected = {"COPEN": "civilingenjörsutbildning öppen ingång"}
        assert pl._resolve_program_codes(cfg, "Ok, men COPEN-programmet då") == expected
        assert pl._resolve_program_codes(cfg, "Ok, men copen-programmet då") == expected


class TestExpandProgramCodes:
    def test_appends_name_and_keeps_the_code(self):
        # The code must survive verbatim: the dynamic-web router keys off it.
        out = pl._expand_program_codes("Vem är PA för CFATE?", {"CFATE": "farkostteknik"})
        assert out == "Vem är PA för CFATE (Farkostteknik)?"

    def test_preserves_the_surface_form_of_a_lowercase_code(self):
        out = pl._expand_program_codes("pa för cfate?", {"CFATE": "farkostteknik"})
        assert out == "pa för cfate (Farkostteknik)?"

    def test_expands_each_code_once_even_if_repeated(self):
        out = pl._expand_program_codes("CTFYS, ja CTFYS", {"CTFYS": "teknisk fysik"})
        assert out.count("Teknisk fysik") == 1

    def test_expands_both_codes_in_a_two_code_query(self):
        out = pl._expand_program_codes(
            "ctfys och cfate", {"CTFYS": "teknisk fysik", "CFATE": "farkostteknik"}
        )
        assert "Teknisk fysik" in out and "Farkostteknik" in out

    def test_empty_mapping_leaves_text_untouched(self):
        assert pl._expand_program_codes("Hur överklagar jag ett betyg?", {}) == (
            "Hur överklagar jag ett betyg?"
        )
