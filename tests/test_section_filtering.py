"""Within-page section filtering for scraped HTML (scripts/fetch_url_corpus).

`exclude_patterns` drops whole URLs; this drops parts of a page. It exists for
the outward-facing programme pages, which carry good descriptive prose
alongside master-programme and eligibility claims — and those must only ever
come from the study plans, which are the authoritative steering documents.

Filtering at ingest rather than trusting the reranker is the point: a claim
that never enters the index cannot outrank the authoritative one, and needs no
re-verification when the embedding or reranker model changes.
"""

from __future__ import annotations

from scripts.fetch_url_corpus import drop_excluded_sections

_PAGE = [
    "Intro line before any heading.",
    "## Om utbildningen",
    "Descriptive prose.",
    "## Masterprogram",
    "Mapping table row A",
    "Mapping table row B",
    "### Mappade masterprogram",
    "Nested mapping row",
    "## Jobb och framtid",
    "Career prose.",
]


def test_drops_the_matching_section_and_its_body():
    out = drop_excluded_sections(_PAGE, ["masterprogram"])
    assert "## Masterprogram" not in out
    assert "Mapping table row A" not in out
    assert "Mapping table row B" not in out


def test_drops_nested_subsections_too():
    # A ### under an excluded ## must go with it, not survive as an orphan.
    out = drop_excluded_sections(_PAGE, ["masterprogram"])
    assert "### Mappade masterprogram" not in out
    assert "Nested mapping row" not in out


def test_resumes_at_the_next_same_level_heading():
    out = drop_excluded_sections(_PAGE, ["masterprogram"])
    assert "## Jobb och framtid" in out
    assert "Career prose." in out


def test_keeps_everything_else_including_the_preamble():
    out = drop_excluded_sections(_PAGE, ["masterprogram"])
    assert out[0] == "Intro line before any heading."
    assert "## Om utbildningen" in out
    assert "Descriptive prose." in out


def test_matching_is_case_insensitive_and_substring():
    assert "## Masterprogram" not in drop_excluded_sections(_PAGE, ["MASTERPROGRAM"])
    assert "## Masterprogram" not in drop_excluded_sections(_PAGE, ["masterprog"])


def test_no_patterns_is_a_passthrough():
    assert drop_excluded_sections(_PAGE, None) == _PAGE
    assert drop_excluded_sections(_PAGE, []) == _PAGE


def test_a_deeper_heading_does_not_end_an_excluded_section():
    # ### is deeper than ##, so it continues the dropped region rather than
    # ending it. Getting this backwards would leak the nested rows.
    lines = ["## Behörighet", "req", "### Särskild behörighet", "more req", "## Annat", "keep"]
    out = drop_excluded_sections(lines, ["behörighet"])
    assert out == ["## Annat", "keep"]


def test_unmatched_patterns_change_nothing():
    assert drop_excluded_sections(_PAGE, ["nonexistent-heading"]) == _PAGE
