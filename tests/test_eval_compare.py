"""Verdicts from scripts/eval_compare.py — the weekly job's decision rule.

The exit code decides whether Christian gets woken up, so the interesting
cases are the boundaries: what counts as critical, what is merely a warning,
and what must stay silent.
"""

from __future__ import annotations

import pytest

from scripts.eval_compare import CRITICAL, OK, WARN, compare, render


def report(
    *,
    hits: int = 44,
    total: int = 45,
    in_pass: int = 39,
    ood_refuse: int = 14,
    ood_total: int = 15,
    size: int | None = 2361,
    failures: list[str] | None = None,
) -> dict:
    return {
        "schema": 1,
        "collection_size": size,
        "recall_at_5": {"hits": hits, "total": total},
        "gate": {
            "in_domain_pass": in_pass,
            "in_domain_total": total,
            "ood_refuse": ood_refuse,
            "ood_total": ood_total,
        },
        "recall_failures": failures if failures is not None else ["known miss"],
    }


def test_identical_runs_are_silent():
    v = compare(report(), report())
    assert v["severity"] == OK
    assert v["findings"] == []


def test_recall_drop_is_critical():
    v = compare(report(hits=44), report(hits=41, failures=["known miss", "a", "b", "c"]))
    assert v["severity"] == CRITICAL
    assert any("recall@5 fell 44/45 → 41/45" in f for f in v["findings"])


def test_recall_improvement_is_not_a_finding():
    v = compare(report(hits=43, failures=["a", "b"]), report(hits=45, failures=[]))
    assert v["severity"] == OK
    # Recovered questions are still worth saying, just not worth alerting on.
    assert any("recovered" in f for f in v["findings"])


def test_swapped_failure_at_equal_recall_still_warns():
    """Same score, different question failing. A count-only check would miss
    this, and it is exactly what a page moving looks like."""
    v = compare(report(hits=44, failures=["old miss"]), report(hits=44, failures=["new miss"]))
    assert v["severity"] == WARN
    assert any("newly missing from top-5: “new miss”" in f for f in v["findings"])


def test_gate_pass_drop_warns_but_is_not_critical():
    v = compare(report(in_pass=39), report(in_pass=35))
    assert v["severity"] == WARN
    assert any("gate pass-rate fell 39 → 35" in f for f in v["findings"])


def test_ood_refuse_drop_warns():
    v = compare(report(ood_refuse=14), report(ood_refuse=11))
    assert v["severity"] == WARN
    assert any("OOD refuse-rate fell" in f for f in v["findings"])


@pytest.mark.parametrize("delta,expected", [(150, OK), (151, WARN), (-151, WARN)])
def test_churn_threshold_is_inclusive_at_the_limit(delta, expected):
    v = compare(report(size=2361), report(size=2361 + delta), max_churn=150)
    assert v["severity"] == expected


def test_churn_is_reported_even_when_the_eval_is_green():
    v = compare(report(size=2361), report(size=2876))
    assert v["severity"] == WARN
    assert any("large index churn" in f for f in v["findings"])


def test_missing_baseline_describes_the_run_without_alerting():
    """First run ever, or the baseline eval crashed. There is nothing to
    compare, and inventing a regression would be worse than staying quiet."""
    v = compare(None, report())
    assert v["severity"] == OK
    assert v["rows"][0][1] == "–"


def test_a_resized_eval_set_warns_instead_of_reporting_a_phantom_drop():
    """44/45 → 44/48 is three new questions, not three regressions."""
    v = compare(report(hits=44, total=45), report(hits=44, total=48))
    assert v["severity"] == WARN
    assert any("not comparable" in f for f in v["findings"])
    assert not any("recall@5 fell" in f for f in v["findings"])


def test_a_critical_finding_is_not_downgraded_by_a_later_warning():
    v = compare(
        report(hits=44, in_pass=39, failures=["known miss"]),
        report(hits=42, in_pass=35, failures=["known miss", "x", "y"]),
    )
    assert v["severity"] == CRITICAL


def test_missing_collection_size_does_not_crash_or_alert():
    v = compare(report(size=None), report(size=None))
    assert v["severity"] == OK
    assert v["rows"][-1] == ("chunks in index", "–", "–")


def test_render_produces_a_markdown_table_and_survives_an_empty_baseline():
    text = render(compare(None, report()), title="Weekly maintenance")
    assert text.startswith("**Weekly maintenance — ")
    assert "| recall@5 (in-domain) | – | 44/45 |" in text
