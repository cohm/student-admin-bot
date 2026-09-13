"""Fail when a PR lowers a locked dependency version.

    python -m scripts.check_lock_downgrades --base <base uv.lock> [--head uv.lock]

WHY THIS EXISTS
    PR #87 shipped four silent downgrades. `pyproject.toml` on that branch
    briefly declared `chromadb>=0.5,<0.6` — one wrong digit in a ceiling — and
    `uv.lock` was regenerated against it:

        chromadb         0.6.3  -> 0.5.23
        tokenizers       0.22.2 -> 0.20.3
        transformers     5.7.0  -> 4.46.3
        huggingface-hub  1.13.0 -> 0.36.2

    The ceiling was corrected afterwards, but the lock was not regenerated, and
    `uv lock` does NOT repair this on its own: 0.5.23 still satisfies the fixed
    range, and uv keeps existing entries as preferences. The bad versions
    survive until something explicitly discards them. Nothing in CI noticed,
    and the embedding stack ran about a year behind for months.

    A version floor (chromadb>=1.5) makes that specific resolution
    unrepresentable — that is the stronger guard, and #89 added it. This is the
    general one: it catches the next ceiling typo, in a package nobody thought
    to floor.

DELIBERATE INTENTIONALITY ESCAPE HATCH
    Downgrades are sometimes correct — pinning back off a broken release, for
    instance. Put `allow-downgrade: <name>` in the PR description, or add the
    package to ALWAYS_ALLOWED below for a standing exception.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import click


# Packages whose version may move backwards without explanation. Empty on
# purpose: every entry here is a guard switched off forever, so prefer the
# per-PR escape hatch unless a package genuinely churns.
ALWAYS_ALLOWED: frozenset[str] = frozenset()

_ALLOW_RE = re.compile(r"allow-downgrade:\s*([A-Za-z0-9._-]+)", re.I)


def locked_versions(lock_text: str) -> dict[str, tuple[str, ...]]:
    """`{package name: (version, ...)}` from a uv.lock, ascending.

    A tuple rather than a single string because uv.lock legitimately carries
    SEVERAL entries per package, one per platform marker — this lock has five
    (transformers 5.8.1 and 5.15.1, torch 2.13.0 and 2.13.0+cpu, numpy, scipy,
    lingua). Keeping only one silently hid a downgrade of the others, and
    `transformers` was one of the four packages in the #87 incident, so that
    bug would have missed a quarter of the regression this exists to catch.
    """
    data = tomllib.loads(lock_text)
    grouped: dict[str, list[str]] = {}
    for pkg in data.get("package", []):
        if "name" in pkg and "version" in pkg:
            grouped.setdefault(pkg["name"], []).append(pkg["version"])
    return {name: tuple(sorted(vs, key=_version_key)) for name, vs in grouped.items()}


def _version_key(version: str) -> tuple:
    """Sortable key for a version string.

    Deliberately simple: numeric segments compared numerically, anything else
    lexically, and a release sorts above any pre-release of the same numbers so
    1.2.0 > 1.2.0rc1. This does not implement PEP 440 — it only has to be right
    enough to tell "went backwards" from "went forwards", and to never crash on
    an odd version, since crashing would block every PR.
    """
    parts: list[tuple[int, object]] = []
    for chunk in re.split(r"[.\-_+]", version):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        elif chunk:
            # A trailing 'rc1'/'b2' sorts below a bare release.
            parts.append((-1, chunk))
    return tuple(parts)


def downgrades(base, head, *, allowed=frozenset()) -> list[tuple]:
    """`[(name, base_versions, head_versions)]` for packages that moved back.

    Compares both the lowest and the highest locked version, because a package
    with several platform entries can regress in either: a whole-tree
    re-resolution drags them all down, while a single bad marker moves only one.

    Packages added or removed between the two locks are ignored — a dependency
    legitimately disappearing is not this check's business. Adding a *lower*
    platform variant can therefore read as a downgrade; that is the intended
    bias, and the per-PR `allow-downgrade:` note exists for it. A false positive
    here costs one line in a PR description; a false negative cost four months.
    """
    found = []
    for name, base_versions in base.items():
        if name in allowed or name in ALWAYS_ALLOWED:
            continue
        head_versions = head.get(name)
        if head_versions is None or head_versions == base_versions:
            continue
        try:
            regressed = _version_key(head_versions[0]) < _version_key(
                base_versions[0]
            ) or _version_key(head_versions[-1]) < _version_key(base_versions[-1])
        except TypeError:
            # A version we cannot order is exactly where a downgrade could
            # hide, so report rather than silently pass.
            regressed = True
        if regressed:
            found.append((name, base_versions, head_versions))
    return sorted(found)


def allowances_from_pr_body(body: str | None) -> frozenset[str]:
    return frozenset(m.group(1).lower() for m in _ALLOW_RE.finditer(body or ""))


@click.command()
@click.option(
    "--base",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False),
    help="uv.lock from the base branch.",
)
@click.option(
    "--head",
    default=Path("uv.lock"),
    type=click.Path(path_type=Path, dir_okay=False),
    show_default=True,
    help="uv.lock from this branch.",
)
@click.option(
    "--pr-body", default=None, help="PR description, scanned for 'allow-downgrade: <name>'."
)
def main(base: Path, head: Path, pr_body: str | None):
    if not base.exists():
        click.echo(f"[lock-guard] no base lock at {base}; nothing to compare", err=True)
        return
    allowed = allowances_from_pr_body(pr_body if pr_body is not None else os.environ.get("PR_BODY"))
    found = downgrades(
        locked_versions(base.read_text(encoding="utf-8")),
        locked_versions(head.read_text(encoding="utf-8")),
        allowed=allowed,
    )
    if not found:
        click.echo("[lock-guard] no dependency downgrades")
        return
    click.echo("[lock-guard] uv.lock moves these dependencies BACKWARDS:\n", err=True)
    width = max(len(name) for name, _, _ in found)
    for name, before, after in found:
        click.echo(f"  {name:<{width}}  {', '.join(before)}  ->  {', '.join(after)}", err=True)
    click.echo(
        "\nIf that is deliberate, say so in the PR description:\n"
        f"    allow-downgrade: {found[0][0]}\n"
        "Otherwise regenerate the lock — note that `uv lock` alone will NOT undo\n"
        "this, since the older version still satisfies the range and uv keeps it\n"
        "as a preference. Use `uv lock --upgrade-package <name>`.",
        err=True,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
