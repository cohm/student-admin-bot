from __future__ import annotations

from student_bot.bot import prereq_data
from student_bot.config import get_config


def test_known_program_codes_lists_all_ten():
    cfg = get_config()
    codes = prereq_data.known_program_codes(cfg)
    assert set(codes) == {
        "CDATE",
        "CELTE",
        "CFATE",
        "CINEK",
        "CMAST",
        "CMATD",
        "COPEN",
        "CTFYS",
        "CTMAT",
        "TIEMM",
    }


def test_ctfys_course_with_no_prereqs_reports_required_for():
    # SF1673 has no prerequisites of its own in CTFYS, but several later
    # courses require it — this is the reverse ("required for") lookup the
    # web-scraped course page can't answer, per review/CTFYS.md.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "SF1673")
    assert info is not None
    assert info.prerequisites_completed == []
    assert info.prerequisites_participation == []
    # SG1112 only needs concurrent participation in SF1673, not completion —
    # must land in required_for_participation, not required_for_completed.
    assert "SG1112" in info.required_for_participation
    assert "SG1112" not in info.required_for_completed


def test_required_for_completed_states_absence_explicitly_when_empty():
    # Regression found via exploratory testing: SF1673's
    # required_for_completed is empty but required_for_participation is
    # not, so the chunk previously said nothing at all about the
    # "completed" direction — an LLM asked "which courses can't I take if
    # I haven't passed SF1673?" then refused ("not in the provided
    # context") instead of concluding "none" from that silence. Each
    # direction now gets its own explicit sentence, even when empty.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "SF1673")
    assert info is not None
    assert info.required_for_completed == []
    assert info.required_for_participation  # non-empty, the other direction

    sv_text = prereq_data.build_chunks([info], "sv")[0].text
    assert "Krävs inte som avklarad förkunskap för någon kurs" in sv_text

    en_text = prereq_data.build_chunks([info], "en")[0].text
    assert "Not required as a completed prerequisite for any course" in en_text


def test_required_for_participation_states_absence_explicitly_when_empty():
    # Symmetric case: a course required as a completed (hard) prerequisite
    # elsewhere (DD1327, SF1544), but never merely as concurrent study.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "DD1331")
    assert info is not None
    assert info.required_for_completed == ["DD1327", "SF1544"]
    assert info.required_for_participation == []

    sv_text = prereq_data.build_chunks([info], "sv")[0].text
    assert "Krävs inte som samtidig läsning för någon kurs" in sv_text

    en_text = prereq_data.build_chunks([info], "en")[0].text
    assert "Not required as concurrent study for any course" in en_text


def test_required_for_both_empty_keeps_single_combined_sentence():
    # A course that is not required, in either direction, for anything —
    # the pre-existing combined sentence should still fire once, not be
    # duplicated by the two new per-direction sentences.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "EL1000")
    assert info is not None
    assert info.required_for_completed == []
    assert info.required_for_participation == []

    sv_text = prereq_data.build_chunks([info], "sv")[0].text
    assert sv_text.count("Krävs inte för någon senare kurs i CTFYS") == 1
    assert "Krävs inte som avklarad förkunskap" not in sv_text
    assert "Krävs inte som samtidig läsning" not in sv_text


def test_ctfys_sg1112_requires_sf1673_participation():
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "SG1112")
    assert info is not None
    assert info.prerequisites_participation == ["SF1673"]
    assert info.prerequisites_completed == []


def test_participation_prereq_text_states_it_does_not_block():
    # Regression: an LLM answering "I haven't passed SF1674, can I take
    # SK1104?" concluded no, even though a participation-only ("aktivt
    # deltagande", may be taken in parallel) requirement does not require
    # prior completion — SK1104's own course page says these three courses
    # "are studied in parallel with this course". The chunk text must say so
    # explicitly rather than rely on the model inferring it from "deltagande".
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "SK1104")
    assert info is not None
    assert info.prerequisites_completed == []
    assert info.prerequisites_participation == ["SF1672", "SF1673", "SF1674"]

    sv_text = prereq_data.build_chunks([info], "sv")[0].text
    assert "INTE att man avklarat" in sv_text
    assert "inget hinder att ännu inte ha klarat dem" in sv_text

    en_text = prereq_data.build_chunks([info], "en")[0].text
    assert "NOT completion of" in en_text
    assert "does NOT block enrollment" in en_text

    roster = prereq_data.roster_chunk(cfg, "CTFYS", "sv", year=1)
    assert "hindrar inte" in roster.text
    assert "hindrar INTE" in roster.text  # header legend


def test_required_for_split_by_downstream_requirement_type():
    # Regression: SF1674 is required COMPLETED by SF1683/SI1146, but SK1104
    # only needs concurrent PARTICIPATION in SF1674 — lumping all three
    # under one "required for (completed course)" label (as an earlier
    # version did) falsely told a student SF1674 had to be finished before
    # SK1104, when it does not.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTFYS", "SF1674")
    assert info is not None
    assert set(info.required_for_completed) == {"SF1683", "SI1146"}
    assert info.required_for_participation == ["SK1104"]

    text = prereq_data.build_chunks([info], "sv")[0].text
    # SK1104 must appear only in the non-blocking clause, never claimed as
    # something SF1674 must be completed before.
    assert "SK1104" not in text.split("Måste vara avklarad innan")[1].split(".")[0]
    assert "SK1104" in text
    assert "SF1683" in text and "SI1146" in text


def test_legacy_untyped_prerequisites_field_is_read_as_completed():
    # CTMAT's SF1681 still carries the old untyped `prerequisites` field
    # rather than the typed prerequisitesCompleted/Participation pair.
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTMAT", "SF1681")
    assert info is not None
    assert info.prerequisites_completed == ["SF1672"]


def test_course_and_program_not_found_returns_none_not_error():
    cfg = get_config()
    assert prereq_data.course_data(cfg, "ZZZZZ", "XX0000") is None
    assert prereq_data.course_data(cfg, "CTFYS", "XX0000") is None


def test_course_data_is_scoped_to_the_given_programme():
    # SF1674 (Flervariabelanalys) exists in both CTFYS and CTMAT with
    # different "required for" lists — the disambiguation between the two
    # happens above this module (see pipeline._resolve_prereq_context and
    # its "ditt program är inte entydigt" clarification); course_data itself
    # must never blend or guess between programmes.
    cfg = get_config()
    ctfys = prereq_data.course_data(cfg, "CTFYS", "SF1674")
    ctmat = prereq_data.course_data(cfg, "CTMAT", "SF1674")
    assert ctfys is not None and ctmat is not None
    assert ctfys.required_for_completed != ctmat.required_for_completed


def test_cohort_only_program_falls_back_to_latest_cohort_file():
    # CDATE has no curated top-level CDATE.json, only cohort archives.
    cfg = get_config()
    infos = prereq_data._load_program_data(cfg, "CDATE")
    assert infos
    assert infos[0].source_file.startswith("cohorts/CDATE-HT")


def test_cohort_data_exists_checks_the_specific_admission_year():
    cfg = get_config()
    assert prereq_data.cohort_data_exists(cfg, "CTFYS", "2023")
    assert not prereq_data.cohort_data_exists(cfg, "CTFYS", "1999")


def test_build_chunks_sv_and_en_render_expected_shape():
    cfg = get_config()
    info = prereq_data.course_data(cfg, "CTMAT", "SF1681")
    sv_chunks = prereq_data.build_chunks([info], "sv")
    en_chunks = prereq_data.build_chunks([info], "en")
    assert len(sv_chunks) == 1
    assert "Kräver avklarad kurs: SF1672" in sv_chunks[0].text
    assert "not yet been verified" in en_chunks[0].text
    chunk = sv_chunks[0]
    assert chunk.doc_type == "studieplan"
    assert chunk.rel_source == "studieplan:CTMAT"
    assert chunk.source_url == "https://www.kth.se/student/kurser/kurs/SF1681"
    assert chunk.chroma_distance == 0.0


def test_disabled_via_config_returns_nothing():
    cfg = get_config()
    disabled = cfg.model_copy(deep=True)
    disabled.prereq_data.enabled = False
    assert prereq_data.course_data(disabled, "CTFYS", "SF1673") is None
    assert prereq_data.roster_chunk(disabled, "CTFYS", "sv") is None


def test_roster_chunk_filters_by_year():
    cfg = get_config()
    chunk = prereq_data.roster_chunk(cfg, "CTFYS", "sv", year=2)
    assert chunk is not None
    for code in ("DD1327", "SF1544", "SF1681", "SF1683", "SI1146"):
        assert code in chunk.text


def test_roster_chunk_flags_unmet_prereq_course():
    # This is the reproduction of the reported bug: a student who hasn't
    # passed SF1674 needs to see that SF1683 and SI1146 require it.
    cfg = get_config()
    chunk = prereq_data.roster_chunk(cfg, "CTFYS", "sv", year=2)
    assert chunk is not None
    assert "SF1683" in chunk.text and "SF1674" in chunk.text
    assert "SI1146" in chunk.text
    assert chunk.doc_type == "studieplan"


def test_roster_chunk_none_when_no_courses():
    cfg = get_config()
    assert prereq_data.roster_chunk(cfg, "CTFYS", "sv", year=99) is None


def test_max_roster_courses_bounds_result_size():
    cfg = get_config()
    # TIEMM's year-1 elective pool alone is far larger than any real
    # question needs in one chunk — result must stay bounded.
    chunk = prereq_data.roster_chunk(cfg, "TIEMM", "sv", year=1)
    assert chunk is not None
    assert chunk.text.count("\n") <= prereq_data._MAX_ROSTER_COURSES + 3


def test_diff_och_trans_nickname_regression():
    # Historik punkt 4 in the v2 implementation plan: v1 could not answer
    # "diff och trans" (a nickname for SF1683, never in any curated
    # dictionary) because it relied on server-side nickname matching. v2's
    # fix is to hand the LLM the whole, already-correct roster instead of
    # guessing the course itself — this only tests that SF1683's entry, with
    # its correctly split prerequisites, is actually present in that roster.
    # Whether an LLM then maps "diff och trans" to it is out of scope for a
    # unit test.
    cfg = get_config()
    chunk = prereq_data.roster_chunk(cfg, "CTFYS", "sv", year=2)
    assert chunk is not None
    assert "SF1683" in chunk.text
    info = prereq_data.course_data(cfg, "CTFYS", "SF1683")
    assert info is not None
    assert "SF1674" in info.prerequisites_completed
