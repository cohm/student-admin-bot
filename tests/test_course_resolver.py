"""The course-without-code path: a course named in the question is found
without cutting a phrase out of the sentence (the programme can be known from
an earlier turn even when the question carries no code)."""

import pytest

from student_bot.bot import course_resolver as cr
from student_bot.bot import web_retrieval
from student_bot.config import get_config

CTFYS_COURSES = [
    ("DD1331", "Grundläggande programmering"),
    ("SF1672", "Linjär algebra"),
    ("SF1673", "Analys i en variabel"),
    ("SF1674", "Flervariabelanalys"),
    ("SF1683", "Differentialekvationer och transformmetoder"),
    ("EL1000", "Reglerteknik, allmän kurs"),
]


@pytest.fixture
def ctfys(monkeypatch):
    monkeypatch.setattr(web_retrieval, "_cached_terms_for_code", lambda cfg, code: ["20252"])
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: CTFYS_COURSES)
    return get_config()


def _codes(res):
    return [u.rsplit("/", 1)[1] for u in res.course_urls]


def test_courses_named_in_the_sentence_are_both_fetched_without_asking(ctfys):
    q = (
        "Jag klarade inte första tentan i differetialekvationer och transformmetoder, "
        "kan jag fortfarande läsa reglerteknik?"
    )
    res = cr.resolve_course_intent(ctfys, q, program_prior="CTFYS")
    assert res is not None
    assert res.clarification_sv == ""
    assert _codes(res) == ["SF1683", "EL1000"]


def test_a_name_inside_a_longer_named_one_is_not_fetched(ctfys, monkeypatch):
    courses = [("HI1024", "Programmering, grundkurs"), ("HI1027", "Objektorienterad programmering")]
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: courses)
    q = "När är tentan i objektorienterad programmering?"
    assert _codes(cr.resolve_course_intent(ctfys, q, program_prior="CMEDT")) == ["HI1027"]


def test_a_shorter_name_named_again_outside_the_longer_one_is_fetched(ctfys, monkeypatch):
    courses = [
        ("DD1338", "Algoritmer och datastrukturer"),
        ("DD2350", "Algoritmer, datastrukturer och komplexitet"),
    ]
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: courses)
    q = (
        "Kan jag läsa kursen Algoritmer och datastrukturer om jag har läst "
        "Algoritmer, datastrukturer och komplexitet?"
    )
    assert _codes(cr.resolve_course_intent(ctfys, q, program_prior="CDATE")) == [
        "DD1338",
        "DD2350",
    ]


def test_the_programme_name_after_its_code_does_not_name_a_course(ctfys, monkeypatch):
    # The pipeline writes a programme code out as "CODE (its name)", and CDEPR
    # has a course named like the programme.
    courses = [("MF1062", "Design och produktframtagning"), ("MJ1112", "Tillämpad termodynamik")]
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: courses)
    q = (
        "Hur går tentan i termodynamik till?\n\n"
        "CDEPR (Civilingenjörsutbildning i design och produktframtagning)"
    )
    assert _codes(cr.resolve_course_intent(ctfys, q, program_now="CDEPR")) == ["MJ1112"]
    # Nor does the programme's name when the student gave it as the reply.
    assert _codes(
        cr.resolve_course_intent(ctfys, f"{q} (design och produktframtagning)", program_now="CDEPR")
    ) == ["MJ1112"]


def test_every_course_word_anchors_the_course_name():
    for word in cr._COURSE_INTENT_KEYWORDS:
        assert cr._COURSE_KEYWORD_RE.fullmatch(word), word


def test_names_that_differ_only_by_generic_words_are_listed(ctfys, monkeypatch):
    courses = CTFYS_COURSES + [("SF1681", "Linjär algebra, fortsättningskurs")]
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: courses)
    res = cr.resolve_course_intent(
        ctfys, "Hur går tentan i linjär algebra till?", program_prior="CTFYS"
    )
    assert res.course_urls == []
    assert "SF1672" in res.clarification_sv and "SF1681" in res.clarification_sv


def test_a_prerequisites_question_is_not_a_course_question(ctfys, monkeypatch):
    # With a programme known it would list every course with "mekanik" in it.
    courses = [("SG1112", "Mekanik I"), ("SG1113", "Mekanik II")]
    monkeypatch.setattr(cr, "_fetch_program_courses", lambda cfg, code, term: courses)
    q = "Vilka förkunskaper krävs för mekanik?"
    assert cr.resolve_course_intent(ctfys, q, program_prior="CTFYS") is None


def test_a_shared_word_alone_does_not_name_a_course():
    tokens = set("hur går tentan i analys till".split())
    assert not cr._named_in_question("Analys i en variabel", tokens)
    assert cr._named_in_question("Analys i en variabel", tokens | {"variabel"})
    # A close spelling counts, a merely similar word does not.
    assert cr._named_in_question("Analys i en variabel", tokens | {"varabel"})
    assert not cr._named_in_question("Analys i en variabel", tokens | {"variant"})
    # Generic words never identify a course on their own.
    assert not cr._named_in_question("Allmän kurs", tokens | {"allmän", "kurs"})


def test_a_one_word_name_needs_an_exact_word_of_eight_letters():
    assert cr._named_in_question("Reglerteknik, allmän kurs", {"läsa", "reglerteknik"})
    assert not cr._named_in_question("Reglerteknik, allmän kurs", {"reglertekniken"})
    assert not cr._named_in_question("Fysik I", {"tentan", "fysik"})
    assert not cr._named_in_question("Mekanik", {"kurs", "mekanik"})
    assert not cr._named_in_question("Datorteknik, grundkurs", {"kurser", "datateknik"})
