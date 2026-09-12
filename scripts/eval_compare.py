"""Diff two `eval/run_eval.py --json-out` reports and say whether to worry.

    python -m scripts.eval_compare --before before.json --after after.json

Written for the weekly maintenance job (`scripts/maintain.sh`), which runs the
eval either side of a scrape+reindex. Comparing the two is what makes a drop
*attributable*: an eval run only after the fact cannot tell "KTH restructured a
page last night" apart from "something merged to main this week".

Prints a Markdown summary on stdout, ready to post to Mattermost as-is.

EXIT CODES (the caller branches on these; the text is for humans)
    0  ok        nothing worth waking up for
    2  warn      churn, a swapped failure, or a gate/OOD wobble — eyeball it
    3  critical  recall@5 fell: the index lost something it used to find
    1  the script itself failed (bad JSON, missing file)

WHY RECALL IS THE ONLY CRITICAL
    Gate pass-rate and OOD refuse-rate move with reranker scores, which are
    unbounded and drift a little with any corpus change; treating every wobble
    as critical would train us to ignore the alert. Recall@5 is different — it
    is a statement about whether the right document is still reachable at all,
    and it is exactly the signal that caught KTH moving the ITM programansvariga
    list to intra.kth.se on 2026-09-12 (35 chunks -> 5).

WHY SCORE DISTRIBUTIONS ARE NOT COMPARED AT ALL
    They jitter between runs on a loaded host. Two prod evals on 2026-09-12
    bracketing a reindex that changed nothing (`upserted=0 deleted=0`, same
    collection size) differed in exactly two numbers: the OOD minimums for top1
    (-6.108 -> -6.878) and meanK (-6.897 -> -7.011). Everything else — every
    in-domain distribution, the OOD median and max, every count — was identical,
    so it was one off-topic query landing on a different candidate set.

    The index was ruled out: a no-op reindex leaves the HNSW binaries
    byte-identical (only chroma.sqlite3 changes, from opening the collection for
    write), and locally four consecutive evals — including one either side of a
    no-op reindex — agree on every field. What is left is float
    non-determinism, which shows up first on off-topic queries because they sit
    in a flat low-similarity region where candidate ordering is fragile.

    It cannot change a verdict: those scores are ~-6 against a gate threshold of
    -0.5. Reporting it would be a weekly alert about noise, so the numbers ride
    along in the report's table for a human to eyeball and nothing branches on
    them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click


OK, WARN, CRITICAL = "ok", "warn", "critical"
_EXIT = {OK: 0, WARN: 2, CRITICAL: 3}
_RANK = {OK: 0, WARN: 1, CRITICAL: 2}
_LABEL = {OK: "✅ green", WARN: "⚠️ check this", CRITICAL: "🚨 recall dropped"}

# Chunk-count movement above this is reported even when the eval stays green.
# A normal week edits a handful of pages; 515 upserted chunks in one run (the
# programme-description import) is the kind of thing worth a sentence.
DEFAULT_MAX_CHURN = 150


def _pair(before: dict | None, after: dict, *keys: str):
    """Fetch the same nested key out of both reports. Missing -> None."""

    def dig(d: dict | None):
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    return dig(before), dig(after)


def _fraction(report: dict | None, group: str, num: str, den: str) -> str:
    if not report:
        return "–"
    block = report.get(group) or {}
    n, d = block.get(num), block.get(den)
    if n is None or d is None:
        return "–"
    return f"{n}/{d}"


def compare(before: dict | None, after: dict, *, max_churn: int = DEFAULT_MAX_CHURN) -> dict:
    """Pure comparison. Returns {severity, headline, findings[], rows[]}.

    `before` may be None (first run, or the baseline eval failed) — then there
    is nothing to diff and the result describes the current state only.
    """
    findings: list[str] = []
    severity = OK

    def flag(level: str, text: str) -> None:
        nonlocal severity
        findings.append(text)
        if _RANK[level] > _RANK[severity]:
            severity = level

    b_hits, a_hits = _pair(before, after, "recall_at_5", "hits")
    b_total, a_total = _pair(before, after, "recall_at_5", "total")

    # An eval set that grew this week makes raw hit counts incomparable; say so
    # rather than reporting a phantom regression.
    comparable = before is not None and b_total == a_total

    if comparable and b_hits is not None and a_hits is not None and a_hits < b_hits:
        flag(CRITICAL, f"recall@5 fell {b_hits}/{b_total} → {a_hits}/{a_total}")
    elif before is not None and not comparable:
        flag(WARN, f"eval set changed size ({b_total} → {a_total}); counts not comparable")

    # Which questions fail, not just how many. Equal counts can still hide a
    # swap — one question recovering while another breaks is exactly what a
    # moved page looks like, and a count-only check calls that "no change".
    # Only meaningful against a baseline: with none, today's known misses are
    # not "new", they are just the current state.
    b_fail = set((before or {}).get("recall_failures") or [])
    a_fail = set(after.get("recall_failures") or [])
    if before is not None:
        for q in sorted(a_fail - b_fail):
            flag(WARN, f"newly missing from top-5: “{q}”")
        for q in sorted(b_fail - a_fail):
            findings.append(f"recovered: “{q}”")

    b_pass, a_pass = _pair(before, after, "gate", "in_domain_pass")
    if comparable and b_pass is not None and a_pass is not None and a_pass < b_pass:
        flag(WARN, f"in-domain gate pass-rate fell {b_pass} → {a_pass}")

    b_ood, a_ood = _pair(before, after, "gate", "ood_refuse")
    if comparable and b_ood is not None and a_ood is not None and a_ood < b_ood:
        flag(WARN, f"OOD refuse-rate fell {b_ood} → {a_ood} (off-topic questions getting through)")

    b_size, a_size = before.get("collection_size") if before else None, after.get("collection_size")
    if b_size is not None and a_size is not None:
        delta = a_size - b_size
        if abs(delta) > max_churn:
            flag(
                WARN,
                f"large index churn: {b_size} → {a_size} chunks ({delta:+d}, "
                f"threshold ±{max_churn})",
            )

    rows = [
        (
            "recall@5 (in-domain)",
            _fraction(before, "recall_at_5", "hits", "total"),
            _fraction(after, "recall_at_5", "hits", "total"),
        ),
        (
            "gate pass (in-domain)",
            _fraction(before, "gate", "in_domain_pass", "in_domain_total"),
            _fraction(after, "gate", "in_domain_pass", "in_domain_total"),
        ),
        (
            "OOD refuse",
            _fraction(before, "gate", "ood_refuse", "ood_total"),
            _fraction(after, "gate", "ood_refuse", "ood_total"),
        ),
        (
            "chunks in index",
            str(b_size if b_size is not None else "–"),
            str(a_size if a_size is not None else "–"),
        ),
    ]

    return {
        "severity": severity,
        "headline": _LABEL[severity],
        "findings": findings,
        "rows": rows,
        "still_failing": sorted(a_fail & b_fail),
    }


def render(verdict: dict, *, title: str) -> str:
    """Markdown, sized for a Mattermost post."""
    out = [f"**{title} — {verdict['headline']}**", ""]
    out.append("| metric | before | after |")
    out.append("|---|---:|---:|")
    for name, b, a in verdict["rows"]:
        out.append(f"| {name} | {b} | {a} |")
    if verdict["findings"]:
        out.append("")
        out.extend(f"- {f}" for f in verdict["findings"])
    if verdict["still_failing"]:
        out.append("")
        out.append(
            f"_Unchanged known misses: {len(verdict['still_failing'])} (same as before the run)._"
        )
    return "\n".join(out)


def _load(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@click.command()
@click.option(
    "--before",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Baseline report. Omit (or point at a missing file) to describe --after alone.",
)
@click.option(
    "--after",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Report from the run being judged.",
)
@click.option(
    "--max-churn",
    type=int,
    default=DEFAULT_MAX_CHURN,
    show_default=True,
    help="Chunk-count movement that is worth mentioning even when green.",
)
@click.option("--title", default="Eval comparison", show_default=True)
def main(before: Path | None, after: Path, max_churn: int, title: str):
    try:
        after_report = _load(after)
    except json.JSONDecodeError as e:
        raise SystemExit(f"--after is not valid JSON: {e}") from e
    if after_report is None:
        raise SystemExit(f"--after not found: {after}")
    try:
        before_report = _load(before)
    except json.JSONDecodeError:
        # A corrupt baseline must not mask the result of the run we just did.
        before_report = None

    verdict = compare(before_report, after_report, max_churn=max_churn)
    print(render(verdict, title=title))
    sys.exit(_EXIT[verdict["severity"]])


if __name__ == "__main__":
    main()
