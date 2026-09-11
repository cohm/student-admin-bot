"""Scope of the programme clarification prompts (web_retrieval).

Two clarifications exist: "which admission round?" and "which programme did
you mean?". Both used to fire on questions that had no cohort-specific or
ambiguous answer to give, and the logs show students answering them and being
asked again. These tests pin down when each is still warranted.
"""

from __future__ import annotations

import pytest

from student_bot.bot import web_retrieval as wr

# alias -> code, shaped like data/program_aliases.json
_ALIASES = {
    "ctfys": "CTFYS",
    "civilingenjörsutbildning i teknisk fysik": "CTFYS",
    "ttfym": "TTFYM",
    "masterprogram, teknisk fysik": "TTFYM",
    "cfate": "CFATE",
    "civilingenjörsutbildning i farkostteknik": "CFATE",
}
# Autumn-only intakes, which is the normal KTH shape.
_TERMS = ["20272", "20262", "20252", "20242", "20232"]


@pytest.fixture
def cfg(monkeypatch):
    """Stub the two things that would otherwise reach the network."""
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: _ALIASES)
    monkeypatch.setattr(wr, "_cached_terms_for_code", lambda _cfg, _code: list(_TERMS))

    class _DynWeb:
        historical_program_years = 6
        discriminator_rare_token_max_aliases = 3

    class _Cfg:
        dynamic_web = _DynWeb()

    return _Cfg()


class TestYearIndependentQuestions:
    """Questions whose answer cannot vary by the asker's admission cohort."""

    @pytest.mark.parametrize(
        "q",
        [
            "Vem är PA för CFATE?",
            "Vem är PA för Öppen ingång?",
            "Vem är programansvarig för teknisk fysik?",
            "Vem är studievägledare för CTFYS?",
            "Who's the program director for TTFYM?",
            "Ok, but who is responsible for the engineering physics master program?",
        ],
    )
    def test_role_lookups_never_need_a_cohort(self, q):
        # A programme's PA is the same person whichever year you were admitted.
        assert wr._question_is_year_independent(q)

    @pytest.mark.parametrize(
        "q",
        [
            "Berätta mer om TTFYM",
            "Vilken programkod har teknisk fysik?",
            "Vad är det fullständiga namnet på programmet med kod CFATE?",
            "Vad är det för skillnad på ctfys och cfate?",
            "Sammanfatta kortfattat vad masterprogrammet TTFYM fokuserar på",
            "Vad finns det för program om teknisk fysik?",
        ],
    )
    def test_identity_questions_never_need_a_cohort(self, q):
        assert wr._question_is_year_independent(q)

    @pytest.mark.parametrize(
        "q",
        [
            "Vilka kurser ingår första året på CTFYS?",
            "Vad står det i utbildningsplanen för CTFYS?",
            "Vilka mattekurser ingår under första året på CTFYS?",
            "Vilka spärrkurser finns det i programmet Teknisk fysik?",
        ],
    )
    def test_curriculum_questions_still_depend_on_the_cohort(self, q):
        # These genuinely differ between admission rounds — keep asking.
        assert not wr._question_is_year_independent(q)

    def test_lowercase_pa_is_not_the_abbreviation(self):
        # "pa" would collide with the very common Swedish "på", so only the
        # verbatim uppercase abbreviation counts. Asserted on the pattern
        # rather than on _question_is_year_independent, which has several
        # other pre-existing branches that can return True for the same text.
        assert not wr._PA_ABBREV_RE.search("Hur många hp har en kurs pa KTH?")
        assert wr._PA_ABBREV_RE.search("Vem är PA för CTFYS?")

    def test_regexes_survived_source_editing(self):
        # Regression: a patch script once wrote these with \b interpreted as a
        # backspace character, so every word-boundary silently stopped matching.
        for name in ("_ROLE_LOOKUP_RE", "_PA_ABBREV_RE", "_PROGRAMME_IDENTITY_RE"):
            assert "\x08" not in getattr(wr, name).pattern, name


class TestWhichProgrammeClarification:
    def _resolve(self, cfg, q, codes):
        roots = [f"https://{wr._KTH_HOST}/student/kurser/program/{c}" for c in codes]
        return wr._resolve_multi_program_candidates(cfg, q, roots)

    def test_no_ask_when_the_student_typed_every_code(self, cfg):
        # "Vad är CTFYS och TTFYM?" names both — there is nothing to
        # disambiguate, and the question wants both answered.
        r = self._resolve(cfg, "Vad är CTFYS och TTFYM?", ["CTFYS", "TTFYM"])
        assert not r.clarification_sv
        assert {u.rsplit("/", 1)[-1] for u in r.queue_urls} == {"CTFYS", "TTFYM"}

    def test_no_ask_when_the_question_is_about_the_set(self, cfg):
        # Demanding the student pick one before telling them what exists is
        # circular.
        r = self._resolve(cfg, "Vad finns det för program om teknisk fysik?", ["CTFYS", "TTFYM"])
        assert not r.clarification_sv
        assert len(r.queue_urls) == 2

    def test_still_asks_when_genuinely_ambiguous(self, cfg):
        # No code typed, not a question about the set, and the answer differs
        # between the two programmes — this is what the prompt is for.
        r = self._resolve(
            cfg, "Vilka mastersprogram är mappade till teknisk fysik?", ["CTFYS", "TTFYM"]
        )
        assert r.clarification_sv and r.clarification_en
