"""Tests for `pipeline._resolve_prereq_context` — the v2 prereq-data
implementation plan's Designbeslut punkt 1-4, wired into `pipeline.answer()`.
Exercises the resolver directly rather than the full `answer()` flow, so
these don't need a live LLM or network access (see tests/test_eligibility_
fallback.py for the same rationale applied elsewhere in this suite).
"""

from __future__ import annotations

from student_bot.bot import pipeline
from student_bot.config import get_config


def test_no_program_known_asks_for_both_program_and_year():
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa SF1673?", "sv", None, None, None
    )
    assert res.clarification is not None
    sv, en = res.clarification
    assert "program" in sv.lower()
    assert "år" in sv.lower()
    assert not res.chunks


def test_ambiguous_program_asks_which_one():
    cfg = get_config()
    # "teknisk fysik" scores highly for both CTFYS (civilingenjör) and TTFYM
    # (the master's programme sharing the exact same name) — see the v2
    # plan's investigation of the "teknisk fysik" ambiguity.
    res = pipeline._resolve_prereq_context(
        cfg,
        "vilka kurser krävs för teknisk fysik, HT2024?",
        "sv",
        None,
        None,
        None,
    )
    assert res.clarification is not None
    sv, _en = res.clarification
    assert "inte entydigt" in sv.lower()


def test_program_known_year_unknown_asks_for_admission_round():
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa SF1673?", "sv", "CTFYS", None, None
    )
    assert res.clarification is not None
    sv, en = res.clarification
    assert "antagningsomgång" in sv.lower()
    assert "admission round" in en.lower()
    assert res.resolved_program == "CTFYS"


def test_program_and_year_known_builds_course_chunk_for_named_course():
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa SF1673?", "sv", "CTFYS", None, "2023"
    )
    assert res.clarification is None
    assert res.resolved_program == "CTFYS"
    assert len(res.chunks) == 1
    assert res.chunks[0].chunk_id == "studieplan:CTFYS:SF1673"


def test_program_and_year_known_builds_roster_chunk_without_named_course():
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg,
        "jag klarade inte flervarren, vilka kurser kan jag inte läsa i åk2?",
        "sv",
        "CTFYS",
        None,
        "2023",
    )
    assert res.clarification is None
    assert len(res.chunks) == 1
    assert res.chunks[0].doc_type == "studieplan"
    assert "SF1683" in res.chunks[0].text


def test_missing_cohort_data_refers_to_counselor_instead_of_guessing():
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa SF1673?", "sv", "CTFYS", None, "1999"
    )
    assert res.clarification is not None
    sv, en = res.clarification
    assert "1999" in sv
    assert not res.chunks


def test_program_outside_curated_data_falls_through_unchanged():
    # ARKIT is a real KTH programme but not one of the 10 with studieplan
    # data — asking it for an admission year would be pointless.
    cfg = get_config()
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa AF1301?", "sv", "ARKIT", None, None
    )
    assert res.clarification is None
    assert res.chunks == []


def test_admission_term_prior_is_used_when_question_has_no_hint():
    cfg = get_config()
    # admission_term_prior mirrors the 5-digit KTH term code carried over
    # from a prior turn's dynamic-web resolution (see maybe_fetch_dynamic_web).
    res = pipeline._resolve_prereq_context(
        cfg, "vad krävs för att läsa SF1673?", "sv", "CTFYS", "20232", None
    )
    assert res.clarification is None
    assert len(res.chunks) == 1


def test_disabled_via_config_never_asks_or_builds():
    cfg = get_config()
    disabled = cfg.model_copy(deep=True)
    disabled.prereq_data.enabled = False
    res = pipeline._resolve_prereq_context(
        disabled, "vad krävs för att läsa SF1673?", "sv", None, None, None
    )
    assert res.clarification is None
    assert res.chunks == []


def test_two_clarifications_in_a_row_do_not_lose_the_original_question():
    """Regression: a real two-hop conversation (no program known -> program
    ambiguous ("teknisk fysik" scores both CTFYS and TTFYM) -> pick "ctfys")
    used to lose the original "diff och trans" question after the second
    hop, because `ConversationMemory` stored each turn's raw text and
    `merge_programme_clarification_followup` only ever looks one turn back.
    The fix: `AnswerResult.merged_question` carries the already-merged text,
    which callers now persist into memory instead of the raw reply — see
    `AnswerResult.merged_question`'s docstring. This test drives
    `pipeline.answer()` through the same three turns a real caller
    (web/app.py, mattermost_client.py, pipeline._repl) would, replicating
    their exact memory.append call, and checks the FINAL clarification still
    names the original course context.
    """
    from student_bot.bot.memory import ConversationMemory

    cfg = get_config()
    memory = ConversationMemory(cfg)
    user_id, root_id = "test-user", "default"

    def turn(q: str):
        history = memory.get(user_id, root_id)
        program_prior = memory.get_program_code(user_id, root_id)
        r = pipeline.answer(q, history=history, cfg=cfg, program_prior=program_prior)
        if (
            r.answered
            or r.meta_fallback
            or r.gate.reason in ("programme_clarification", "prereq_clarification")
        ):
            memory.append(user_id, root_id, "user", r.merged_question or q)
            memory.append(user_id, root_id, "assistant", r.answer)
        if r.program_code:
            memory.set_program_code(user_id, root_id, r.program_code)
        return r

    r1 = turn("Jag klarade inte diff och trans, vilka kurser kan jag inte läsa nu?")
    assert r1.gate.reason == "prereq_clarification"

    r2 = turn("teknisk fysik, 2024")
    assert r2.gate.reason == "prereq_clarification"
    assert "inte entydigt" in r2.answer.lower()

    r3 = turn("ctfys")
    # "ctfys" alone unambiguously resolves the programme regardless of any
    # lost context, so that alone would not prove anything. The real tell:
    # before the fix, the merged text at this point was just "teknisk
    # fysik, 2024\n\nctfys" — no course code, no eligibility wording left
    # from turn 1 — which made `_question_is_prereq_eligibility_shaped`
    # False, so it fell through to the OLD, generic web-fetch admission-
    # round clarification (gate_reason "programme_clarification"), unaware
    # this was ever an eligibility question. With the fix, turn 2's memory
    # entry already carries turn 1 folded in, so turn 3's own one-hop merge
    # still sees "vilka kurser kan jag inte läsa" and correctly stays on
    # *this* module's clarification path.
    assert r3.gate.reason == "prereq_clarification"
    assert "CTFYS" in r3.answer
