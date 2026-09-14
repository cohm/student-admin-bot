"""Role questions must not be routed into the study-plan fetch (#84 follow-up).

"Vem är programansvarig för teknisk fysik?" answered correctly while
"Vem är ansvarig för CTFYS?" did not, which looked like a name-vs-code problem.
It was not: the working phrasing contains a role NOUN that
`_CONTACT_INTENT_RE` matched, so the fetch was skipped. The failing phrasings
used "PA" or a bare "ansvarig för", reached the study-plan fetch, and its
sections outscored the page that actually names the PA — measured on the live
index, rank 8 behind three syllabus chunks.

A syllabus never names its programansvarig, so there is nothing for the fetch
to contribute to these.
"""

from __future__ import annotations

import pytest

from student_bot.bot.web_retrieval import _is_contact_intent_question


@pytest.mark.parametrize(
    "question",
    [
        # The two reported failures.
        "Vem är ansvarig för CTFYS?",
        "Vem är PA för CTFYS?",
        # And the ones that already worked — these must not regress.
        "Vem är programansvarig för teknisk fysik?",
        "Who is the programme director for CFATE?",
        "Vem är ansvarig för COPEN?",
        "Vem är studievägledare för CTFYS?",
        "Vem är studierektor på SCI?",
        "Who is the director of studies for CTFYS?",
    ],
)
def test_role_questions_skip_the_study_plan_fetch(question):
    assert _is_contact_intent_question(question)


@pytest.mark.parametrize(
    "question",
    [
        # These genuinely need the study plan; skipping the fetch would break them.
        "Vilka kurser ingår i CTFYS?",
        "Vilka kurser är obligatoriska i CTFYS årskurs 2?",
        "Vilka masterprogram är mappade till CTFYS?",
        "Hur många hp är CTFYS?",
        "Vad krävs för behörighet till TTFYM?",
        "Vilka valbara kurser finns i årskurs 3?",
    ],
)
def test_study_plan_questions_still_fetch(question):
    assert not _is_contact_intent_question(question)


def test_lowercase_pa_is_not_treated_as_a_role_question():
    """Swedish "på" is everywhere; only the verbatim uppercase abbreviation
    counts. `_PA_ABBREV_RE` exists precisely for this."""
    assert not _is_contact_intent_question("Vilka kurser läser jag pa CTFYS?")


def test_an_empty_question_is_not_a_role_question():
    assert not _is_contact_intent_question("")
    assert not _is_contact_intent_question(None)
