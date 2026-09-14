"""Check what this machine can actually exercise, before trusting a change.

    uv run student-bot-doctor

Written after three fixes to the prompt and citation paths were made without
being able to run either — and after the reverse mistake, assuming the dynamic
web fetch was unavailable locally when it worked fine. Both are the same error:
guessing at the environment instead of asking it.

Checks, cheapest first, and never fails the process — the point is a report:

  corpus + index      can retrieval run at all
  reranker            the cross-encoder loads and scores
  dynamic web         can kth.se be fetched and parsed
  LLM gateway         is it reachable, and will it actually generate

Secrets are never printed. The LLM check reports whether a key is present, not
what it is.
"""

from __future__ import annotations

import time

import click
from rich.console import Console
from rich.table import Table

from student_bot.config import get_config


console = Console()


def _row(name: str, ok: bool | None, detail: str) -> tuple[str, str, str]:
    mark = {True: "[green]ok[/green]", False: "[red]NO[/red]", None: "[yellow]--[/yellow]"}[ok]
    return name, mark, detail


def check_index(cfg) -> tuple[str, str, str]:
    from student_bot.ingest.embed import get_chroma_collection

    try:
        n = get_chroma_collection(cfg).count()
    except Exception as e:
        return _row("corpus index", False, f"{type(e).__name__}: {e}")
    if not n:
        return _row("corpus index", False, "empty — run `python -m scripts.reindex`")
    return _row("corpus index", True, f"{n} chunks")


def check_retrieval(cfg) -> tuple[str, str, str]:
    from student_bot.bot.retrieval import retrieve

    try:
        t0 = time.monotonic()
        res = retrieve(cfg, "anmälan till tentamen", query_language="sv")
        ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return _row("retrieval + rerank", False, f"{type(e).__name__}: {e}")
    if not res.reranked:
        return _row("retrieval + rerank", False, "no chunks came back")
    return _row("retrieval + rerank", True, f"top1={res.reranked[0].rerank_score:.2f} in {ms} ms")


def check_dynamic_web(cfg) -> tuple[str, str, str]:
    """The path that answers programme questions. Needs outbound network."""
    from student_bot.bot.web_retrieval import maybe_fetch_dynamic_web

    if not cfg.dynamic_web.enabled:
        return _row("dynamic web fetch", None, "disabled in config.yaml")
    try:
        t0 = time.monotonic()
        res = maybe_fetch_dynamic_web(cfg, "Vem är ansvarig för CTFYS?", "sv")
        ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return _row("dynamic web fetch", False, f"{type(e).__name__}: {e}")
    chunks = list(getattr(res, "chunks", []) or []) if res else []
    if not chunks:
        return _row("dynamic web fetch", False, "no chunks — offline, or the allowlist rejected it")
    return _row("dynamic web fetch", True, f"{len(chunks)} chunks in {ms} ms")


def check_llm(cfg, *, generate: bool) -> tuple[str, str, str]:
    """Reachability, and optionally a real generation.

    A 401 proves only that something is listening and the auth path works — not
    that the model can serve. Only `--generate` settles that.
    """
    import httpx

    try:
        resolved = cfg.active_model()
    except RuntimeError as e:
        return _row("LLM", False, f"config: {e}")

    has_key = resolved.api_key is not None
    base = resolved.base_url.rstrip("/")
    try:
        code = httpx.get(f"{base}/models", timeout=8).status_code
    except Exception as e:
        return _row("LLM gateway", False, f"unreachable: {type(e).__name__}")
    if not has_key:
        return _row(
            "LLM gateway",
            False,
            f"listening (HTTP {code}) but no {resolved.provider_key} key — set it in .env; "
            "generation cannot be tested",
        )
    if not generate:
        return _row("LLM gateway", None, f"listening (HTTP {code}), key present — use --generate")

    from student_bot.bot.llm import chat  # noqa: PLC0415  (optional path)

    try:
        t0 = time.monotonic()
        out = "".join(chat(cfg, [{"role": "user", "content": "Svara med ordet OK."}]))
        ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:
        return _row("LLM generation", False, f"{type(e).__name__}: {e}")
    if not out.strip():
        return _row("LLM generation", False, "empty response")
    return _row("LLM generation", True, f"{resolved.model_id} answered in {ms} ms")


@click.command()
@click.option("--generate", is_flag=True, help="Actually call the model, not just probe the port.")
def main(generate: bool):
    cfg = get_config()
    table = Table(title="Dev environment")
    table.add_column("check")
    table.add_column("", justify="center")
    table.add_column("detail")

    rows = [check_index(cfg), check_retrieval(cfg), check_dynamic_web(cfg)]
    rows.append(check_llm(cfg, generate=generate))
    for r in rows:
        table.add_row(*r)
    console.print(table)

    blind = [r[0] for r in rows if "NO" in r[1]]
    if blind:
        console.print(
            f"[yellow]Cannot exercise locally: {', '.join(blind)}. "
            "Changes to those paths are being made blind.[/yellow]"
        )
    else:
        console.print("[green]Every path can be exercised locally.[/green]")


if __name__ == "__main__":
    main()
