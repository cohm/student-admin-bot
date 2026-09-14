"""The talk deck hard-codes facts about the running system, and drifts.

`docs/slides/index.html` states gate thresholds, model names and eval numbers.
Nothing regenerates it, so it goes stale silently — by v0.2.0 it claimed 73 %
recall (actual 98 %), 100 % OOD refusal (93 %), a 77 % pass rate (87 %), a
five-document source-spread cap (eight) and a model two generations old.

These pin the values that live in config.yaml, where the comparison is exact
and cheap. Eval percentages are not pinned: they need the model stack to
recompute, and a test that re-runs retrieval does not belong in a 4-second
unit suite.
"""

from __future__ import annotations

import re

import pytest

from student_bot.config import PROJECT_ROOT, get_config


DECK = (PROJECT_ROOT / "docs" / "slides" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cfg():
    return get_config()


def test_the_deck_exists_where_the_web_app_serves_it():
    assert (PROJECT_ROOT / "docs" / "slides" / "index.html").is_file()


def test_gate_thresholds_match_config(cfg):
    assert f"top1 ≥ {cfg.gate.rerank_top1_min}" in DECK
    assert str(cfg.gate.rerank_meanK_min) in DECK


def test_source_spread_cap_matches_config(cfg):
    """Was ≤ 5 in the deck while config said 8 — and the first fix missed,
    because the deck uses a literal "≤" and the patch looked for "&le;"."""
    assert f"≤ {cfg.gate.max_distinct_sources_in_topk} distinct" in DECK


def test_the_offtopic_threshold_is_described(cfg):
    """Added in #84; the deck explained only two thresholds."""
    assert f"offtopic_top1_max = {cfg.gate.offtopic_top1_max}" in DECK


@pytest.mark.parametrize("attr", ["embedding", "reranker"])
def test_model_names_match_config(cfg, attr):
    assert getattr(cfg, attr).model in DECK


def test_no_stale_headline_numbers():
    """The specific wrong values shipped before v0.2.0. Cheap regression
    guard: if someone reverts the deck these come back."""
    for stale in ("73&thinsp;%", "77&thinsp;%", "100&thinsp;%"):
        assert stale not in DECK, stale


def test_the_debug_slide_describes_the_session_list():
    """#92 replaced per-answer 🔍 drilling with a session list; the slide
    described the old flow."""
    assert "every question from the session" in DECK
    assert "click the <strong>🔍</strong> next to any answer" not in DECK


def test_the_dynamic_web_allowlist_is_still_described_as_host_pinned():
    """The prompt-injection boundary. If this sentence goes, the deck is
    claiming a weaker guarantee than the code makes."""
    assert re.search(r"Host is fixed to\s*<code>www\.kth\.se</code>", DECK)
