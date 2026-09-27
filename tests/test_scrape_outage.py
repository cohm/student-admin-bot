"""The URL scrape during a kth.se outage (scripts/fetch_url_corpus.py).

The source map used to be rebuilt from scratch on every run, so a run where
kth.se was down wrote a near-empty map over a full one, and every web-imported
citation lost its `host: title` label until a later run succeeded. Now a failed
page keeps its entry, and a run that fetches nothing at all fails without
writing anything, so `maintain.sh` reports it and skips the reindex.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import scripts.fetch_url_corpus as fuc
from student_bot.bot import web_retrieval as wr
from student_bot.config import get_config

_UP = "https://www.kth.se/student/up"
_DOWN = "https://www.kth.se/student/down"


def _entry(url: str, title: str) -> dict:
    return {"source_url": url, "canonical_url": url, "fetched_at": 1, "title": title}


# --- merge_source_map -----------------------------------------------------


def test_a_page_that_was_not_refetched_keeps_its_entry(tmp_path: Path):
    (tmp_path / "web_import").mkdir()
    (tmp_path / "web_import" / "old.md").write_text("x", encoding="utf-8")
    previous = {"web_import/old.md": _entry(_DOWN, "Old")}
    merged, kept = fuc.merge_source_map({}, previous, tmp_path)
    assert merged == previous
    assert kept == 1


def test_a_fresh_entry_wins_over_the_previous_one(tmp_path: Path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    merged, kept = fuc.merge_source_map(
        {"a.md": _entry(_UP, "New")}, {"a.md": _entry(_UP, "Old")}, tmp_path
    )
    assert merged["a.md"]["title"] == "New"
    assert kept == 0


def test_an_entry_whose_file_is_gone_is_dropped(tmp_path: Path):
    merged, kept = fuc.merge_source_map({}, {"gone.md": _entry(_DOWN, "Gone")}, tmp_path)
    assert merged == {}
    assert kept == 0


def test_an_unreadable_previous_map_counts_as_empty(tmp_path: Path):
    p = tmp_path / "map.json"
    assert fuc._read_source_map(p) == {}
    p.write_text("{not json", encoding="utf-8")
    assert fuc._read_source_map(p) == {}
    p.write_text("[1, 2]", encoding="utf-8")
    assert fuc._read_source_map(p) == {}


# --- the whole run, with kth.se stubbed -----------------------------------


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch):
    """A config pointing every path the scrape touches into tmp_path, with a
    previous run's map and page already on disk."""
    cfg = get_config().model_copy(deep=True)
    docs = tmp_path / "docs"
    cfg.paths.docs_dir = docs
    cfg.url_ingest.output_dir = str(docs / "web_import")
    cfg.url_ingest.manifest_file = str(tmp_path / "manifest.yaml")
    cfg.url_ingest.source_map_file = str(tmp_path / "url_source_map.json")
    cfg.url_ingest.filtered_links_report_file = str(tmp_path / "filtered.json")
    cfg.study_plan_cache.enabled = False
    Path(cfg.url_ingest.manifest_file).write_text(
        yaml.safe_dump({"entries": [{"url": _UP}, {"url": _DOWN}]}), encoding="utf-8"
    )

    down_rel = fuc._rel_source_for_url(_DOWN, Path("web_import"))
    (docs / down_rel).parent.mkdir(parents=True)
    (docs / down_rel).write_text("# previously scraped\n", encoding="utf-8")
    previous = {down_rel: _entry(_DOWN, "Down page")}
    Path(cfg.url_ingest.source_map_file).write_text(json.dumps(previous), encoding="utf-8")

    monkeypatch.setattr(fuc, "get_config", lambda: cfg)
    # The programme code index reads the alias table, which would hit kth.se.
    monkeypatch.setattr(wr, "_get_program_aliases", lambda _cfg: {})
    return cfg, down_rel, previous


def _fetch_only(*reachable: str):
    def fetch(url, _cfg):
        if url not in reachable:
            raise TimeoutError("The read operation timed out")
        html = b"<html><head><title>Up page</title></head><body><main><p>Hello.</p></main></body></html>"
        return url, html, "text/html"

    return fetch


def test_an_outage_fails_the_run_and_writes_nothing(corpus, monkeypatch):
    cfg, _, previous = corpus
    monkeypatch.setattr(fuc, "_fetch", _fetch_only())
    result = CliRunner().invoke(fuc.main, [])
    assert result.exit_code == 1
    assert "no page could be fetched" in result.output
    assert json.loads(Path(cfg.url_ingest.source_map_file).read_text()) == previous
    assert not Path(cfg.url_ingest.filtered_links_report_file).exists()


def test_a_partial_outage_keeps_the_pages_it_could_not_reach(corpus, monkeypatch):
    cfg, down_rel, previous = corpus
    monkeypatch.setattr(fuc, "_fetch", _fetch_only(_UP))
    result = CliRunner().invoke(fuc.main, [])
    assert result.exit_code == 0, result.output
    got = json.loads(Path(cfg.url_ingest.source_map_file).read_text())
    assert got[down_rel] == previous[down_rel]
    up_rel = fuc._rel_source_for_url(_UP, Path("web_import"))
    assert got[up_rel]["source_url"] == _UP
    assert "Kept 1 source-map entries" in result.output
