"""Citation tags that reached students as raw text (v0.2.0 on prod).

Two independent matcher gaps, both reproduced from real answers:

  "Den tidigare PA var Martin Viklund [teknisk fysik 1ecfccae57]."
  "Bioteknik (CBH) [982c61022f, 41318b481d, 9ce33099d8, 1ac333021a8, 90adef82eb]"
"""

from __future__ import annotations

import pytest

from student_bot.bot.citations import apply_citation_numbering
from student_bot.bot.retrieval import RetrievedChunk


def chunk(title, section, cid, rel="web_import/www.kth.se/x.md"):
    return RetrievedChunk(
        chunk_id=cid,
        text="body-" + cid,
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


# --- gap 1: a bare title, several rows from the same document -------------


def test_bare_title_resolves_when_the_document_has_one_row():
    out, cited = apply_citation_numbering(
        "PA var Martin Viklund [teknisk fysik 1ecfccae57].",
        [chunk("teknisk fysik 1ecfccae57", "Programansvarig", "a")],
    )
    assert out.endswith("[1].")
    assert len(cited) == 1


def test_bare_title_resolves_when_the_document_has_several_rows():
    """The reported bug. `_match` required exactly one candidate and gave up
    otherwise — but a long page contributing two sections to the top-K is the
    normal case, not an edge case, so the tag reached the student as text."""
    out, cited = apply_citation_numbering(
        "PA var Martin Viklund [teknisk fysik 1ecfccae57].",
        [
            chunk("teknisk fysik 1ecfccae57", "Programansvarig", "a"),
            chunk("teknisk fysik 1ecfccae57", "Kontakt", "b"),
        ],
    )
    assert "1ecfccae57" not in out, "hash leaked into the answer"
    assert out.endswith("[1].")
    assert len(cited) == 1


def test_the_best_ranked_row_wins_when_ambiguous():
    """Rows arrive in rerank order, so the first is that document's
    best-scoring section."""
    rows = [
        chunk("t 1ecfccae57", "Best", "a"),
        chunk("t 1ecfccae57", "Worse", "b"),
    ]
    _, cited = apply_citation_numbering("X [t 1ecfccae57].", rows)
    assert cited[0].section_path == "Best"


# --- gap 2: several tags in one bracket -----------------------------------


TITLES = [
    "kontakta kth 982c61022f",
    "skolor vid kth 41318b481d",
    "utbildningsutbud 9ce33099d8",
]


def test_comma_separated_hashes_are_split_and_numbered():
    """Asked to list 19 programmes, the model put five tags in every bracket.
    Treated as one tag they matched nothing and the whole list rendered raw."""
    rows = [chunk(t, f"S{i}", str(i)) for i, t in enumerate(TITLES)]
    out, cited = apply_citation_numbering(
        "Bioteknik (CBH) [982c61022f, 41318b481d, 9ce33099d8].", rows
    )
    assert out.endswith("[1][2][3].")
    assert len(cited) == 3


def test_numbering_is_stable_across_repeated_lines():
    """19 lines citing the same five sources must not produce 95 numbers."""
    rows = [chunk(t, f"S{i}", str(i)) for i, t in enumerate(TITLES)]
    body = "\n".join(["X [982c61022f, 41318b481d, 9ce33099d8]."] * 4)
    out, cited = apply_citation_numbering(body, rows)
    assert out.count("[1]") == 4
    assert len(cited) == 3


def test_duplicates_inside_one_bracket_collapse():
    rows = [chunk(TITLES[0], "S0", "0")]
    out, _ = apply_citation_numbering("X [982c61022f, 982c61022f].", rows)
    assert out.endswith("[1].")


def test_an_all_unknown_bracket_is_left_alone():
    """Never invent a number for something that matched nothing."""
    rows = [chunk("alfa 111", "S", "a")]
    body = "X [beta 222, gamma 333]."
    assert apply_citation_numbering(body, rows)[0] == body


def test_ordinary_bracketed_prose_is_untouched():
    rows = [chunk("alfa 111", "S", "a")]
    body = "Se reglerna [not, a, citation] och fortsätt."
    assert apply_citation_numbering(body, rows)[0] == body


def test_no_chunks_means_no_rewriting():
    body = "X [alfa 111]."
    assert apply_citation_numbering(body, [])[0] == body


# --- the same ambiguity rule, in every fallback ---------------------------
#
# The title fallback was fixed in #125; the section and fuzzy fallbacks kept
# requiring exactly one candidate. A one-character model typo
# ("tillgodoraknande" -> "tillgodoraknander") whose section matched exactly
# then fell through to raw text, because that document had two rows.


def test_a_mistyped_title_resolves_via_its_section():
    rows = [
        chunk("tillgodoraknande", "Tillgodoräknande > Relaterade sidor", "a"),
        chunk("tillgodoraknande", "Tillgodoräknande > Relaterade sidor", "b"),
    ]
    out, cited = apply_citation_numbering(
        "Se [tillgodoraknander · Tillgodoräknande > Relaterade sidor].", rows
    )
    assert out.endswith("[1].")
    assert len(cited) == 1


def test_a_section_shared_by_two_documents_still_resolves():
    """Best-ranked wins. Citing the top-scoring of two plausible documents
    beats showing the student an unresolved tag."""
    rows = [
        chunk("alfa", "Gemensam sektion", "a"),
        chunk("beta", "Gemensam sektion", "b"),
    ]
    out, _ = apply_citation_numbering("X [okänd titel · Gemensam sektion].", rows)
    assert out == "X [1]."


def test_fuzzy_title_match_survives_two_rows():
    rows = [
        chunk("tentamensregler", "Regler", "a"),
        chunk("tentamensregler", "Anmälan", "b"),
    ]
    out, _ = apply_citation_numbering("X [tentamensregler:].", rows)
    assert out == "X [1]."


def test_a_genuinely_unknown_tag_is_still_left_alone():
    """Loosening ambiguity must not become 'match anything'."""
    rows = [chunk("alfa", "Sektion A", "a")]
    body = "X [något helt annat · En sektion som inte finns]."
    assert apply_citation_numbering(body, rows)[0] == body


# --- the glossary is source material, not a document ----------------------
#
# Once the system prompt says the glossary may be used to answer, the model
# cites it: "…har programkoden TTFYM [Ordlista]." There is no Sources row for
# it, so the marker has to go.


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Koden är TTFYM [Ordlista].", "Koden är TTFYM."),
        ("Code is TTFYM [Glossary].", "Code is TTFYM."),
        ("Koden är TTFYM [ordlista].", "Koden är TTFYM."),
    ],
)
def test_the_glossary_marker_is_removed_without_leaving_a_gap(body, expected):
    rows = [chunk("alfa", "S", "a")]
    assert apply_citation_numbering(body, rows)[0] == expected


def test_removing_it_does_not_reflow_markdown():
    """Repairing the gap afterwards also collapsed list indentation."""
    rows = [chunk("alfa", "S", "a")]
    out, _ = apply_citation_numbering("Rad ett [Ordlista]\n\n*   Punkt [alfa · S]", rows)
    assert out == "Rad ett\n\n*   Punkt [1]"


def test_the_word_in_ordinary_prose_is_untouched():
    rows = [chunk("alfa", "S", "a")]
    out, _ = apply_citation_numbering("Ordet ordlista i text [alfa · S].", rows)
    assert out == "Ordet ordlista i text [1]."
