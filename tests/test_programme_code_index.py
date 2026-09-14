"""The programme code/name table as an indexed document.

`https://www.kth.se/student/kurser/kurser-inom-program` has every KTH programme
name and code in one place, and it is in the manifest — but it is a JavaScript
application, so scraping its HTML produced an eleven-line file with zero codes.
The data was only ever reachable through the routing alias table, which
retrieval cannot see, so "what is the programme code for X?" had no document to
cite and depended entirely on the prompt glossary.

This renders the same JSON store the router reads into markdown, so retrieval
can find it and an answer can cite a source.
"""

from __future__ import annotations

import re

import pytest

from student_bot.config import PROJECT_ROOT, get_config


@pytest.fixture(scope="module")
def doc() -> str:
    path = (
        PROJECT_ROOT / "docs" / "corpus" / "web_import" / "www.kth.se" / "programoversikt-koder.md"
    )
    if not path.is_file():
        pytest.skip("programme index not generated yet — run student-bot-fetch-url-corpus")
    return path.read_text(encoding="utf-8")


def test_it_carries_source_metadata(doc):
    """Without a source_url the Sources block cannot link anywhere."""
    assert "source_url: https://www.kth.se/student/kurser/kurser-inom-program" in doc
    assert "title: Programkoder vid KTH" in doc


def test_it_lists_a_realistic_number_of_programmes(doc):
    rows = [ln for ln in doc.splitlines() if re.match(r"^\| [A-Z]{5} \|", ln)]
    assert len(rows) > 250, f"only {len(rows)} programme rows"


@pytest.mark.parametrize("code", ["CTFYS", "TTFYM", "CFATE", "COPEN"])
def test_known_codes_are_present(doc, code):
    assert re.search(rf"^\| {code} \|", doc, re.M)


def test_both_languages_are_listed(doc):
    """The English name is what an English question matches on; the Swedish
    one is what the rest of the corpus uses."""
    row = next(ln for ln in doc.splitlines() if ln.startswith("| CTFYS |"))
    assert "civilingenjörsutbildning i teknisk fysik" in row.lower()
    assert "degree programme in engineering physics" in row.lower()


def test_the_code_is_not_repeated_as_its_own_name(doc):
    """The alias table maps the code to itself; that is noise in a name column."""
    row = next(ln for ln in doc.splitlines() if ln.startswith("| CTFYS |"))
    _, _, names = row.split("|", 2)
    assert "ctfys" not in names.lower()


def test_it_is_indexed(doc):
    """A document nothing retrieves is a document that does not exist."""
    from student_bot.ingest.embed import get_chroma_collection

    cfg = get_config()
    try:
        coll = get_chroma_collection(cfg)
        got = coll.get(where={"rel_source": "web_import/www.kth.se/programoversikt-koder.md"})
    except Exception as e:  # no local index in CI
        pytest.skip(f"no Chroma index available: {e}")
    if not got.get("ids"):
        pytest.skip("index predates the programme table — reindex to pick it up")
    assert len(got["ids"]) > 1
