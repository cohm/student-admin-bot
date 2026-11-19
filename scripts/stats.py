"""student-bot-stats: per-topic question counts, latency, and 👍/👎 ratios,
plus the System One ghost mode against the chat model's topic when it has run.

Reads SQLite logs only — does not call the LLM.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.table import Table

from student_bot.bot.topics import load_topics
from student_bot.config import get_config
from student_bot.logging_db import LogDB


def _parse_since(since: str | None) -> int:
    if not since:
        return 0
    if since.endswith(("d", "h")):
        unit = since[-1]
        n = int(since[:-1])
        secs = n * (86400 if unit == "d" else 3600)
        return int(time.time() - secs)
    # ISO date.
    return int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp())


@click.command()
@click.option("--since", default=None, help="Limit to last 7d / 24h / ISO date.")
def main(since: str | None):
    cfg = get_config()
    db = LogDB(cfg)
    console = Console()

    since_ts = _parse_since(since)
    overall = db.overall_counts(since_ts)
    by_topic = db.stats_by_topic(since_ts)

    topics = {t.id: t for t in load_topics(cfg)}

    range_label = f"since {since}" if since else "all time"
    head = Table(title=f"Overall ({range_label})", show_header=False)
    head.add_column("metric", style="bold")
    head.add_column("value", justify="right")
    head.add_row("logged questions", str(overall["logged"]))
    head.add_row("answered (gate pass)", str(overall["answered"]))
    head.add_row("avg latency (ms)", str(overall["avg_latency_ms"]))
    head.add_row("anonymous-only counter", str(overall["anon"]))
    console.print(head)

    if not by_topic:
        console.print("[dim]no logged questions yet[/dim]")
        return

    body = Table(title=f"By topic ({range_label})")
    body.add_column("topic")
    body.add_column("label")
    body.add_column("n", justify="right")
    body.add_column("answered", justify="right")
    body.add_column("avg ms", justify="right")
    body.add_column("👍", justify="right", style="green")
    body.add_column("👎", justify="right", style="red")
    body.add_column("👍 ratio", justify="right")

    for row in by_topic:
        tid = row["topic"]
        label = topics.get(tid).sv if tid in topics else tid
        ratio = ""
        denom = row["thumbs_up"] + row["thumbs_down"]
        if denom:
            ratio = f"{(row['thumbs_up'] / denom) * 100:.0f}%"
        body.add_row(
            tid,
            label,
            str(row["n"]),
            str(row["answered"]),
            str(row["avg_latency_ms"]),
            str(row["thumbs_up"]),
            str(row["thumbs_down"]),
            ratio,
        )
    console.print(body)
    _print_shadow(console, db.shadow_topic_stats(since_ts), range_label)


def _ms(v: int | None) -> str:
    return "-" if v is None else str(v)


def _print_shadow(console: Console, s: dict | None, range_label: str) -> None:
    """The System One ghost mode (`topic_shadow:`), when any row has a result."""
    if s is None:
        console.print("[dim]ghost mode: no shadow-classified rows in range[/dim]")
        return
    on_since = datetime.fromtimestamp(s["first_ts"]).strftime("%Y-%m-%d %H:%M")

    head = Table(title=f"Ghost mode, System One topic ({range_label})", show_header=False)
    head.add_column("metric", style="bold")
    head.add_column("value", justify="right")
    head.add_row("first shadow row", f"#{s['first_id']} at {on_since}")
    head.add_row("classified", f"{s['classified']} of {s['attempted']}")
    for reason, n in s["errors"].items():
        head.add_row(f"  error / skip: {reason}", str(n))
    if s["unwritten"]:
        head.add_row("no result written", str(s["unwritten"]))
    head.add_row(
        "call ms median / p95 / max",
        f"{_ms(s['ms_median'])} / {_ms(s['ms_p95'])} / {_ms(s['ms_max'])}",
    )
    if s["comparable"]:
        pct = s["agree"] / s["comparable"] * 100
        agree = f"{s['agree']}/{s['comparable']} ({pct:.0f}%)"
    else:
        agree = "-"
    head.add_row("agrees with chat model", agree)
    console.print(head)

    bands = Table(title="Agreement by System One confidence")
    bands.add_column("confidence")
    bands.add_column("n", justify="right")
    bands.add_column("agree", justify="right")
    for b in s["bands"]:
        share = f"{b['agree'] / b['n'] * 100:.0f}%" if b["n"] else "-"
        bands.add_row(b["band"], str(b["n"]), f"{b['agree']} ({share})" if b["n"] else "-")
    console.print(bands)

    if s["disagreements"]:
        dis = Table(title="Disagreements (chat model → System One)")
        dis.add_column("chat model")
        dis.add_column("System One")
        dis.add_column("n", justify="right")
        for d in s["disagreements"][:10]:
            dis.add_row(d["topic"], d["shadow_topic"], str(d["n"]))
        console.print(dis)

    lat = Table(title="Chat-model latency before / since the first shadow row (medians)")
    lat.add_column("metric")
    lat.add_column("before", justify="right")
    lat.add_column("since", justify="right")
    for col, v in s["latency"].items():
        lat.add_row(
            col,
            f"{_ms(v['before_median'])} (n={v['before_n']})",
            f"{_ms(v['after_median'])} (n={v['after_n']})",
        )
    console.print(lat)
    if not s["latency"]["ttft_ms"]["before_n"]:
        console.print("[dim]no rows before the first shadow row in range; widen --since[/dim]")
    console.print(
        "[dim]Latency mixes question types and load; only a long, busy window says "
        "anything about GPU contention.[/dim]"
    )


if __name__ == "__main__":
    main()
