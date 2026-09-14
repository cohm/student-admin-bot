"""Run questions through the FULL pipeline and check the generated answers.

    uv run student-bot-sweep [--only role] [--show]

`eval/run_eval.py` scores retrieval and the gate, and deliberately never calls
the LLM. That leaves the last mile untested — and every production bug in
September 2026 lived there: citation tags rendered as raw text, a refusal that
referred an off-topic question to a study counselor, a programme code that
reached the prompt and was ignored.

Each case names the change it guards and an assertion about the answer, so this
reports pass/fail instead of leaving a human to read twenty answers. Slower and
less deterministic than run_eval — it needs a working LLM (`student-bot-doctor
--generate`) and takes a few minutes. Run it before a release, and add a case
whenever a real answer comes out wrong.

THIS IS NOT A PASS/FAIL GATE
    Unlike `run_eval`, the same case can pass and fail across runs, for two
    honest reasons. The study-plan cases fetch kth.se live, so a network hiccup
    shows up as `web_unreachable_no_cache`. And the model is not deterministic
    at temperature 0.2: the off-topic refusal is driven by a NEGATIVE
    instruction ("do not refer them to a counselor"), which it follows most of
    the time but not always — roughly one run in eight mentions the counselor
    anyway. Treat a single red row as a prompt to look, and re-run before
    concluding anything. A case that fails repeatedly is real.

WRITING A CHECK THAT CANNOT LIE
    The first version of this flagged 10 of 13 cases as broken. Almost all were
    the checker's fault: it read `[Christian Ohm](https://…)` — an ordinary
    markdown link — as an unnumbered citation, and the content hash inside a
    legitimate `/docs/…-cfb26875ad.md` URL as a leak. A check built on the same
    loose assumption as the thing it checks will confirm whatever you expected.
    `unnumbered_citations()` and `visible_content_hashes()` below are written
    against those two false positives.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from typing import Callable

import click
from rich.console import Console
from rich.table import Table

from student_bot.bot.pipeline import answer
from student_bot.config import get_config


console = Console()


def unnumbered_citations(text: str) -> list[str]:
    """Bracketed spans that should have become `[N]` and did not.

    Excludes markdown links — `[label](url)` — which are not citations at all.
    """
    out = []
    for m in re.finditer(r"\[([^\[\]]{6,})\]", text):
        if re.fullmatch(r"\d+", m.group(1).strip()):
            continue
        if text[m.end() : m.end() + 1] == "(":
            continue
        out.append(m.group(1)[:70])
    return out


def visible_content_hashes(text: str) -> list[str]:
    """Scraper hashes shown to the reader.

    A hash inside a `…-<hash>.md` URL is the real filename and belongs there;
    only a hash in prose or a link label is a defect.
    """
    return [h for h in re.findall(r"\b[0-9a-f]{10}\b", text) if f"-{h}.md" not in text]


@dataclass
class Case:
    id: str
    group: str
    question: str
    guards: str
    check: Callable[[object], str | None]  # None = pass, str = why it failed


def _names(*needles: str) -> Callable[[object], str | None]:
    def check(r):
        body = answer_body(r)
        return None if any(n in body for n in needles) else f"does not mention {needles[0]}"

    return check


def answer_body(result) -> str:
    """The generated answer, without the badge, Sources block or literacy tip.

    Checking `rendered` is a trap: the rotating literacy footer includes "boten
    är ett komplement, inte en ersättning för studievägledaren", so an
    off-topic refusal that correctly names no counselor was reported as leaking
    one — about one run in six, depending on which tip came up.
    """
    return result.numbered_body or result.rendered


def _absent(needle: str, why: str) -> Callable[[object], str | None]:
    def check(r):
        return why if needle.lower() in answer_body(r).lower() else None

    return check


CASES: list[Case] = [
    # --- role questions (#129): must reach the programansvariga pages --------
    Case(
        "role-code",
        "role",
        "Vem är ansvarig för CTFYS?",
        "#129 role questions skip the study-plan fetch",
        _names("Ohm"),
    ),
    Case("role-pa", "role", "Vem är PA för CTFYS?", "#129 + the PA abbreviation", _names("Ohm")),
    Case(
        "role-copen",
        "role",
        "Vem är ansvarig för COPEN?",
        "#129, a different programme",
        _names("Sellberg"),
    ),
    Case(
        "role-name",
        "role",
        "Vem är PA för teknisk fysik?",
        "the phrasing that always worked — must not regress",
        _names("Ohm"),
    ),
    Case(
        "role-stale",
        "role",
        "Vem är ansvarig för CTFYS?",
        "#129: no predecessor from a stale syllabus",
        _absent("tidigare", "names a predecessor"),
    ),
    # --- the study plan must still be fetched when it IS the answer ---------
    Case(
        "studyplan-ask",
        "studyplan",
        "Vilka kurser är obligatoriska i CTFYS årskurs 2?",
        "#129 must not over-narrow: still asks for an admission year",
        lambda r: (
            None
            if ("antagningsomgång" in answer_body(r) or "HT20" in answer_body(r))
            else "no longer routes to the study plan"
        ),
    ),
    Case(
        "studyplan-year",
        "studyplan",
        "Vilka kurser ingår i CTFYS årskurs 2 HT2024?",
        "study plan with a year supplied",
        lambda r: None if r.answered else "refused",
    ),
    # --- citations (#125, #126) ---------------------------------------------
    Case(
        "cite-list",
        "citations",
        "Hur många civilingenjörsprogram erbjuder KTH? Vilka skolor är ansvariga för dem?",
        "#125 several tags in one bracket",
        lambda r: None,
    ),
    Case(
        "cite-single",
        "citations",
        "Vad gäller för omtentamen?",
        "#125/#126 ordinary citation rendering",
        lambda r: None,
    ),
    # --- the prompt glossary (#85, #132) ------------------------------------
    Case(
        "code-master",
        "glossary",
        "Vad har masterprogrammet i teknisk fysik för programkod?",
        "#85 name -> code, answered from the glossary",
        _names("TTFYM"),
    ),
    Case(
        "code-civ",
        "glossary",
        "Vilken är programkoden för civilingenjör teknisk fysik?",
        "#85 name -> code",
        _names("CTFYS"),
    ),
    # --- refusals (#84, #127) -----------------------------------------------
    Case(
        "offtopic-sv",
        "refusal",
        "Jag ska laga lasagne, har du något bra recept?",
        "#127 off-topic drops the counselor referral",
        _absent("studievägled", "refers an off-topic question to a counselor"),
    ),
    Case(
        "offtopic-en",
        "refusal",
        "How do I cook pasta carbonara?",
        "#127 English off-topic",
        _absent("counsel", "refers an off-topic question to a counselor"),
    ),
    Case(
        "inscope-miss",
        "refusal",
        "Hur lång tid har jag på mig att slutföra ett examensarbete?",
        "#84 an in-scope miss KEEPS the referral",
        lambda r: (
            None
            if (r.answered or "studievägled" in answer_body(r).lower())
            else "in-scope miss lost its referral"
        ),
    ),
]


@click.command()
@click.option(
    "--only", default=None, help="Run one group: role, studyplan, citations, glossary, refusal."
)
@click.option("--show", is_flag=True, help="Print each answer body.")
def main(only: str | None, show: bool):
    cfg = get_config()
    cases = [c for c in CASES if not only or c.group == only]
    if not cases:
        raise SystemExit(f"no cases in group {only!r}")

    table = Table(title=f"Answer sweep ({len(cases)} cases)")
    table.add_column("case")
    table.add_column("", justify="center")
    table.add_column("gate")
    table.add_column("s", justify="right")
    table.add_column("detail")

    failures = 0
    for case in cases:
        t0 = time.monotonic()
        try:
            result = answer(case.question, cfg=cfg, channel="cli")
        except Exception as e:
            table.add_row(case.id, "[red]ERR[/red]", "-", "-", f"{type(e).__name__}: {e}")
            failures += 1
            continue
        secs = f"{time.monotonic() - t0:.0f}"

        problems = [p for p in [case.check(result)] if p]
        # Two checks every answer must pass, regardless of what it guards.
        raw = unnumbered_citations(answer_body(result))
        if raw:
            problems.append(f"unnumbered citation: {raw[0]}")
        hashes = visible_content_hashes(answer_body(result))
        if hashes:
            problems.append(f"content hash shown: {hashes[0]}")

        ok = not problems
        failures += 0 if ok else 1
        table.add_row(
            case.id,
            "[green]ok[/green]" if ok else "[red]NO[/red]",
            result.gate.reason,
            secs,
            "; ".join(problems) if problems else case.guards,
        )
        if show:
            console.print(f"[dim]--- {case.id}: {case.question}[/dim]")
            console.print(answer_body(result).strip()[:500])

    console.print(table)
    if failures:
        console.print(f"[red]{failures} of {len(cases)} failed.[/red]")
        sys.exit(1)
    console.print(f"[green]All {len(cases)} answers look right.[/green]")


if __name__ == "__main__":
    main()
