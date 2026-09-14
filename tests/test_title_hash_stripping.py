"""Content hashes out of citation tags and the Sources block.

The URL scraper names files `<slug>-<n>-<10 hex>.md` and the ingest derives
doc_title from the filename, so all 167 web_import titles ended in a content
hash — and web_import is 1980 of 2361 chunks. The model was shown
`[teknisk fysik 1ecfccae57 · …]` on most turns, learned that citations are
hashes, and emitted them bare where they matched nothing and reached students
as raw text.
"""

from __future__ import annotations

import pytest

from student_bot.bot.citations import apply_citation_numbering, strip_title_hash
from student_bot.bot.prompts import format_context
from student_bot.bot.retrieval import RetrievedChunk


def chunk(title, section="Programansvarig", cid="a", rel="web_import/www.kth.se/x.md"):
    return RetrievedChunk(
        chunk_id=cid,
        text="TEXT",
        rel_source=rel,
        doc_title=title,
        doc_type="web",
        language="sv",
        section_path=section,
        chunk_index=0,
        chroma_distance=0.1,
        rerank_score=5.0,
        page_start=None,
        source_url="https://www.kth.se/x",
    )


@pytest.mark.parametrize(
    "raw,clean",
    [
        ("teknisk fysik 1ecfccae57", "teknisk fysik"),
        ("oppen ingang 053d820829", "oppen ingang"),
        ("administrera dina studier 1 bdd4e9464d", "administrera dina studier"),
        ("programansvariga grundutbildning 1 aa0ea3ca62", "programansvariga grundutbildning"),
    ],
)
def test_real_titles_from_the_index_are_cleaned(raw, clean):
    assert strip_title_hash(raw) == clean


@pytest.mark.parametrize(
    "title",
    [
        "Tentamensregler",  # no hash
        "Kurs 2024",  # ends in a number, but not a hash
        "Utbildningsplan HT2026",
        "abc123",  # too short
        "teknisk fysik 1ecfccae5",  # 9 hex, not 10
        "teknisk fysik 1ecfccae57z",  # not hex
    ],
)
def test_ordinary_titles_are_untouched(title):
    assert strip_title_hash(title) == title


def test_a_title_that_is_only_a_hash_is_kept():
    """Better an ugly tag than an empty one."""
    assert strip_title_hash("abcdef1234") == "abcdef1234"
    assert strip_title_hash("") == ""


def test_the_model_is_shown_the_clean_tag():
    assert format_context([chunk("teknisk fysik 1ecfccae57")]).startswith(
        "[teknisk fysik · Programansvarig]"
    )


def test_the_clean_tag_resolves():
    rows = [chunk("teknisk fysik 1ecfccae57")]
    assert apply_citation_numbering("X [teknisk fysik].", rows)[0] == "X [1]."
    assert apply_citation_numbering("X [teknisk fysik · Programansvarig].", rows)[0] == "X [1]."


def test_the_hashed_tag_still_resolves():
    """A conversation started before this change, or a model echoing an earlier
    turn, keeps emitting the old form. Dropping it would trade one rendering
    bug for another."""
    rows = [chunk("teknisk fysik 1ecfccae57")]
    assert apply_citation_numbering("X [teknisk fysik 1ecfccae57].", rows)[0] == "X [1]."
    assert (
        apply_citation_numbering("X [teknisk fysik 1ecfccae57 · Programansvarig].", rows)[0]
        == "X [1]."
    )


def test_both_forms_number_the_same_row():
    """They must not become two entries in the Sources block."""
    rows = [chunk("teknisk fysik 1ecfccae57")]
    out, cited = apply_citation_numbering("A [teknisk fysik]. B [teknisk fysik 1ecfccae57].", rows)
    assert out == "A [1]. B [1]."
    assert len(cited) == 1


def test_it_still_works_with_several_rows_from_one_document():
    """The gap fixed in #125, now with cleaned titles on both sides."""
    rows = [
        chunk("teknisk fysik 1ecfccae57", "Programansvarig", "a"),
        chunk("teknisk fysik 1ecfccae57", "Kontakt", "b"),
    ]
    assert apply_citation_numbering("X [teknisk fysik].", rows)[0] == "X [1]."
