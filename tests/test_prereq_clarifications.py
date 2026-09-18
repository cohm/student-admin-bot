from __future__ import annotations

from student_bot.bot.web_retrieval import (
    _parse_programme_year_level,
    _question_is_prereq_eligibility_shaped,
    bilingual_no_program_clarification,
    is_multi_program_clarification_assistant_message,
    is_no_program_clarification_assistant_message,
    is_programme_clarification_assistant_message,
    merge_programme_clarification_followup,
    parse_program_admission_hints,
    question_needs_prereq_context,
)
from student_bot.config import get_config


# Designbeslut punkt 1 in the v2 prereq-data implementation plan: manually
# verified trigger examples.


def test_needs_prereq_context_triggers_on_explicit_course_eligibility():
    assert question_needs_prereq_context("vad krävs för att läsa SF1673?")


def test_needs_prereq_context_triggers_on_unlocks_phrasing():
    assert question_needs_prereq_context("vilka kurser låser SF1672 upp?")


def test_needs_prereq_context_triggers_on_cannot_take_phrasing():
    assert question_needs_prereq_context(
        "jag klarade inte flervarren, vilka kurser kan jag inte läsa?"
    )


def test_needs_prereq_context_does_not_trigger_on_admission_question():
    # No course word — this is a registration_admission question, not a
    # course-prerequisite one.
    assert not question_needs_prereq_context("vad krävs för att bli antagen till CTFYS?")


def test_needs_prereq_context_does_not_trigger_on_course_content_question():
    assert not question_needs_prereq_context("vad är kursmålen för SF1673?")


def test_eligibility_shaped_matches_needs_context_for_trigger_b_cases():
    # For every trigger-B-shaped example above, the narrower "should we run
    # the new prereq_data clarification/build logic" check agrees with the
    # broader plan-level trigger — see `_question_is_prereq_eligibility_shaped`'s
    # docstring for the one case (bare program code, no course angle) where
    # they intentionally differ.
    for q in (
        "vad krävs för att läsa SF1673?",
        "vilka kurser låser SF1672 upp?",
        "jag klarade inte flervarren, vilka kurser kan jag inte läsa?",
    ):
        assert _question_is_prereq_eligibility_shaped(q)
    for q in (
        "vad krävs för att bli antagen till CTFYS?",
        "vad är kursmålen för SF1673?",
    ):
        assert not _question_is_prereq_eligibility_shaped(q)


def test_bare_nickname_with_no_course_code_or_kurs_word_now_triggers():
    # Regression found via exploratory testing: "vad krävs för att läsa
    # <nickname>?" previously fell through entirely when the nickname had
    # no course code and the sentence had no "kurs"-word or "vilka
    # kurser" phrase — the exact same shape of question issue #136 is
    # about, just phrased with "krävs" instead of "vilka kurser kan jag
    # läsa". The verb (läsa/ta/take/study) is itself a course-context
    # signal, distinct from an admission question's verb ("bli antagen"/
    # "be admitted"), which must still be excluded.
    for q in (
        "Vad krävs för att läsa envarren i teknisk matematik?",
        "What is required to take SF1683?",
        "What do I need to take diskmat?",
    ):
        assert _question_is_prereq_eligibility_shaped(q)
    for q in (
        "vad krävs för att bli antagen till CTFYS?",
        "What is required to be admitted to CTFYS?",
    ):
        assert not _question_is_prereq_eligibility_shaped(q)


def test_bare_verb_ga_does_not_overtrigger():
    # Regression found via exploratory testing: "gå" alone is far too
    # generic a verb to serve as a course-context signal the way "läsa"/
    # "ta" do — these are all real, unrelated "vad krävs för att gå..."
    # questions that must NOT enter the prereq-data clarification flow.
    for q in (
        "Vad krävs för att gå ut gymnasiet innan man börjar på KTH?",
        "Vad krävs för att gå med i studentkåren?",
        "Vad krävs för att gå på studievägledning?",
    ):
        assert not _question_is_prereq_eligibility_shaped(q)


def test_master_eligibility_question_is_excluded_from_trigger_b():
    # Regression found via the sample50 eval sweep (el-3): a master-
    # eligibility question must keep going through the existing, more
    # specific `_question_is_master_eligibility` clarification (which asks
    # for the student's civilingenjör programme and routes to KTH's
    # "mappade masterprogram" data) rather than this trigger's generic
    # "which programme/year" ask — prereq_data has no mapped-masters data
    # at all, so answering from it would be actively worse.
    # (`question_needs_prereq_context`, the OR of trigger A and B, still
    # returns True here — "masterprogrammet" trips trigger A's "program"
    # substring check on its own. That's fine: the pipeline gate that
    # actually drives this new behaviour is `_question_is_prereq_eligibility_
    # shaped` alone, asserted below, not the OR'd function.)
    q = "Vilka kurser behöver jag för att bli behörig till masterprogrammet i matematik?"
    assert not _question_is_prereq_eligibility_shaped(q)


def test_parse_programme_year_level_recognizes_ak_abbreviation():
    # "ÅK2"/"åk 2" is the common student shorthand for "årskurs 2".
    assert _parse_programme_year_level("vilka kurser i ÅK2 kan jag läsa") == 2
    assert _parse_programme_year_level("kurser i åk 3") == 3
    # Must not fire on a course code that happens to start with AK (e.g.
    # AK2014) — no word boundary between the captured digit and what follows.
    assert _parse_programme_year_level("AK2014 Beslutsteori") is None


def test_parse_admission_hints_recognizes_antagningsomgang():
    # This is the exact term the bot's own clarification question uses
    # ("vilken antagningsomgång som gäller"), so it's worth recognizing on
    # its own even before the bot ever asks.
    h = parse_program_admission_hints("Jag läser ctfys, antagningsomgång 2024, ...")
    assert h.year_prefix == "2024"


def test_no_program_clarification_message_is_recognized():
    sv, en = bilingual_no_program_clarification()
    assert is_no_program_clarification_assistant_message(sv)
    assert is_no_program_clarification_assistant_message(en)
    assert not is_programme_clarification_assistant_message(sv)
    assert not is_multi_program_clarification_assistant_message(sv)


def test_no_program_clarification_fuses_with_explicit_code_reply():
    cfg = get_config()
    sv, _en = bilingual_no_program_clarification()
    history = [
        {"role": "user", "content": "Vad krävs för att läsa SF1673?"},
        {"role": "assistant", "content": sv},
    ]
    merged = merge_programme_clarification_followup("CTFYS", history, cfg)
    assert merged.startswith("Vad krävs för att läsa SF1673?")
    assert merged.rstrip().endswith("CTFYS")


def test_no_program_clarification_fuses_with_colloquial_name_reply():
    # No candidate list was shown (unlike the ambiguous-program pick), so a
    # colloquial reply ("teknisk fysik") must still fuse — not just a bare
    # 5-letter code.
    cfg = get_config()
    sv, _en = bilingual_no_program_clarification()
    history = [
        {"role": "user", "content": "Vad krävs för att läsa SF1673?"},
        {"role": "assistant", "content": sv},
    ]
    merged = merge_programme_clarification_followup("teknisk fysik, HT2024", history, cfg)
    assert merged.startswith("Vad krävs för att läsa SF1673?")
    assert "teknisk fysik" in merged


def test_no_program_clarification_does_not_fuse_an_unrelated_followup():
    cfg = get_config()
    sv, _en = bilingual_no_program_clarification()
    history = [
        {"role": "user", "content": "Vad krävs för att läsa SF1673?"},
        {"role": "assistant", "content": sv},
    ]
    merged = merge_programme_clarification_followup(
        "nej förlåt, glöm det, hur öppnar jag ett ärende hos studievägledaren?",
        history,
        cfg,
    )
    assert merged == "nej förlåt, glöm det, hur öppnar jag ett ärende hos studievägledaren?"
