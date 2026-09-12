"""The machine-readable eval report (eval/run_eval.py --json-out).

This is the contract scripts/eval_compare.py reads, so the shape matters more
than the numbers: a renamed key would turn the weekly job's verdict into a
silent "no change".
"""

from __future__ import annotations

import json

from eval.run_eval import _distribution, build_report
from scripts.eval_compare import OK, compare


class _Gate:
    rerank_top1_min = -0.5
    rerank_meanK_min = -1.0


class _Cfg:
    gate = _Gate()


def _report(**overrides):
    kwargs = dict(
        collection_size=2361,
        in_total=45,
        ood_total=15,
        recall_hits=44,
        in_pass=39,
        ood_refuse=14,
        in_top1=[-2.643, 4.805, 11.153],
        in_meanK=[-1.0, 0.5, 2.0],
        ood_top1=[-9.0, -7.0],
        ood_meanK=[-9.5, -8.0],
        recall_failures=["How long do I have to finish a master thesis?"],
        refusals=["a refused question"],
    )
    kwargs.update(overrides)
    return build_report(_Cfg(), **kwargs)


def test_distribution_is_min_median_max():
    assert _distribution([3.0, 1.0, 2.0]) == {"min": 1.0, "median": 2.0, "max": 3.0}


def test_distribution_of_nothing_is_none_not_zero():
    """An empty set must serialise as null. Zero would read as a real score
    sitting right on the gate threshold."""
    assert _distribution([]) is None


def test_report_round_trips_through_json():
    assert json.loads(json.dumps(_report()))["collection_size"] == 2361


def test_report_carries_the_keys_eval_compare_reads():
    r = _report()
    assert r["recall_at_5"] == {"hits": 44, "total": 45}
    assert r["gate"]["in_domain_pass"] == 39
    assert r["gate"]["ood_refuse"] == 14
    assert r["gate"]["ood_total"] == 15
    assert r["collection_size"] == 2361
    assert isinstance(r["recall_failures"], list)


def test_the_failing_questions_travel_with_the_counts():
    """ "Which question broke" is the whole diagnosis when a page moves; a bare
    count would mean re-running the eval by hand to find out."""
    assert _report()["recall_failures"] == ["How long do I have to finish a master thesis?"]


def test_thresholds_are_recorded_so_a_gate_retune_is_visible_later():
    assert _report()["thresholds"] == {"rerank_top1_min": -0.5, "rerank_meanK_min": -1.0}


def test_a_report_compared_against_itself_is_green():
    """The end-to-end contract: what run_eval writes, eval_compare reads."""
    r = json.loads(json.dumps(_report()))
    verdict = compare(r, r)
    assert verdict["severity"] == OK
    assert verdict["findings"] == []


def test_comparing_real_shaped_reports_detects_a_recall_drop():
    before = json.loads(json.dumps(_report()))
    after = json.loads(
        json.dumps(_report(recall_hits=40, recall_failures=["a", "b", "c", "d", "e"]))
    )
    assert compare(before, after)["severity"] == "critical"
