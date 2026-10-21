"""Snapshot on-disk state (Chroma index + SQLite databases) into a tarball.

Usage:
  uv run student-bot-backup                      # everything -> data/backups/
  uv run student-bot-backup --only chroma        # vector index only
  uv run student-bot-backup --out /tmp           # somewhere else
  uv run student-bot-backup --verify FILE.tar.gz # re-check an archive's checksums
  uv run student-bot-backup --restore FILE.tar.gz # put that archive's index back in data/chroma

On a host without uv (prod), run the same thing in the container. `./data` is
bind-mounted, so archives land on the host, and `--verify` takes the same
repo-relative path:
  docker compose run --rm -T web student-bot-backup
  docker compose run --rm -T web student-bot-backup --verify data/backups/FILE.tar.gz
  docker compose run --rm -T web student-bot-backup --restore data/backups/FILE.tar.gz

`--restore` is the counterpart of the `--only chroma` snapshot: it verifies the
archive, unpacks it next to `data/chroma`, and swaps the directories. Only
`chroma` is restored; the SQLite components of a full archive hold the
irreplaceable data and are left for a deliberate manual copy. The replaced
index is removed, not kept: `data/chroma` can always be rebuilt from the
corpus, so the cost of a wrong `--restore` is a reindex, not data. Stop the
services first and start them after — a running process keeps serving the
segment it loaded at startup either way. The weekly report prints the three
commands.

SQLite files (`chroma.sqlite3`, `logs.sqlite`, `web_cache.sqlite`) are copied
with SQLite's online backup API, so the running stack does not have to be
stopped and WAL contents are folded in. The Chroma HNSW segment files
(`data_level0.bin` & friends) are plain binaries that `scripts/reindex.py`
rewrites wholesale — stop the stack (`uv run student-bot-down`, or on prod
`docker compose stop web mattermost`) first if a reindex might run concurrently.

Every archive carries a MANIFEST.json recording sha256 per file plus the
chromadb/Python versions that wrote the index, which is what tells you whether
a persist directory will open cleanly elsewhere (see README, "Image notes").

PRIVACY: `logs.sqlite` holds `qa_log.question` / `qa_log.answer` — real student
text. Archives containing it stay internal; `--only chroma` is derived purely
from the public corpus and is the one to hand to a collaborator.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import click
from rich.console import Console

from student_bot.config import PROJECT_ROOT, get_config


COMPONENTS = ("chroma", "logs", "cache")
CACHE_DB_NAME = "web_cache.sqlite"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _copy_sqlite(src: Path, dst: Path) -> None:
    """Consistent hot copy via the online backup API (folds in any -wal)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def _package_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return "(not installed)"


def _row_count(db: Path, table: str) -> int | None:
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            return int(conn.execute(f"select count(*) from {table}").fetchone()[0])
    except sqlite3.Error:
        return None


def verify_hints(target: Path, root: Path = PROJECT_ROOT) -> list[str]:
    """The `--verify` command for `target`, in the uv and the container form.

    Prod has no uv, so a uv-only hint is a command that does not exist there.
    The container form needs a repo-relative path: inside the container the
    archive is /app/data/..., which means nothing on the host, while
    data/backups/... resolves the same on both sides of the bind mount. A
    target outside the repo is not reachable from the container at all, so it
    gets the uv form only.
    """
    try:
        rel = target.resolve().relative_to(root.resolve())
    except ValueError:
        return [f"uv run student-bot-backup --verify {target}"]
    return [
        f"uv run student-bot-backup --verify {rel}",
        f"docker compose run --rm -T web student-bot-backup --verify {rel}",
    ]


def _read_manifest(tar: tarfile.TarFile) -> tuple[str, dict] | None:
    """(prefix, manifest) for an archive we wrote, or None if it has no MANIFEST.json.

    `prefix` is the top-level folder every member sits under (`<name>/`): the
    archive is written with `arcname=name`, so a member's path inside the
    tarball is `prefix + rel` for each `rel` in the manifest.
    """
    manifest_name = next((n for n in tar.getnames() if n.endswith("MANIFEST.json")), None)
    if manifest_name is None:
        return None
    prefix = manifest_name[: -len("MANIFEST.json")]
    return prefix, json.load(tar.extractfile(manifest_name))


def _verify(console: Console, archive: Path) -> int:
    if not archive.exists():
        console.print(f"[red]no such archive: {archive}[/red]")
        return 1
    with tarfile.open(archive, "r:gz") as tar:
        found = _read_manifest(tar)
        if found is None:
            console.print("[red]archive has no MANIFEST.json — cannot verify[/red]")
            return 1
        prefix, manifest = found
        bad = 0
        for rel, meta in manifest["files"].items():
            member = tar.extractfile(prefix + rel)
            if member is None:
                console.print(f"[red]missing from archive:[/red] {rel}")
                bad += 1
                continue
            h = hashlib.sha256()
            for block in iter(lambda: member.read(1024 * 1024), b""):
                h.update(block)
            if h.hexdigest() != meta["sha256"]:
                console.print(f"[red]checksum mismatch:[/red] {rel}")
                bad += 1
    console.print(f"[bold]{archive.name}[/bold]  written {manifest['created_utc']}")
    console.print(f"components: {', '.join(manifest['components'])}")
    console.print(f"written by chromadb {manifest['chromadb']} on Python {manifest['python']}")
    if bad:
        console.print(f"[red]{bad} file(s) failed verification[/red]")
        return 1
    console.print(f"[green]all {len(manifest['files'])} files verified[/green]")
    return 0


def _restore(console: Console, archive: Path, chroma_dir: Path) -> int:
    """Replace `chroma_dir` with the `chroma/` tree inside `archive`.

    Order matters: verify, unpack to a sibling of `chroma_dir` (same
    filesystem, so the swap is two renames), and only then touch the live
    directory. Every failure before the swap leaves the live index exactly as
    it was and nothing behind on disk; a failed swap puts the old directory
    back. The replaced index is deleted rather than kept beside the new one —
    it is rebuildable from the corpus with `scripts.reindex`, so a wrong
    restore costs a reindex, not data. Only `chroma` is restored.

    Running services are not restarted here (nothing inside the container can
    do that); the caller stops them first and starts them after.
    """
    if _verify(console, archive) != 0:
        console.print("[red]refusing to restore an archive that does not verify[/red]")
        return 1

    # A symlinked data/chroma: operate on the directory it points at, so the
    # link survives and keeps pointing at the restored index.
    chroma_dir = chroma_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Both names match the `data/chroma-*/` gitignore rule, so a leftover from
    # an interrupted run cannot dirty the prod checkout.
    staging = chroma_dir.parent / f"chroma-restore-{stamp}"
    replaced = chroma_dir.parent / f"chroma-replaced-{stamp}"

    with tarfile.open(archive, "r:gz") as tar:
        prefix, manifest = _read_manifest(tar)  # verified above, so present
        components = list(manifest.get("components", []))
        if "chroma" not in components:
            console.print(
                f"[red]archive has no chroma component (components: "
                f"{', '.join(components) or 'none'})[/red]"
            )
            return 1
        members = [m for m in tar.getmembers() if m.name.startswith(prefix + "chroma/")]
        if not members:
            console.print(
                "[red]archive lists chroma in its manifest but holds no chroma/ files[/red]"
            )
            return 1
        wrote, running = str(manifest.get("chromadb", "")), _package_version("chromadb")
        if wrote.split(".")[0] != running.split(".")[0]:
            console.print(
                f"[yellow]archive written by chromadb {wrote}, this client is {running}: "
                "a different major version opens the index by migrating it in place "
                "(see README, Image notes) — make sure that is what you want[/yellow]"
            )
        try:
            staging.mkdir(parents=True)
            # `filter="data"` (3.11.4+) refuses absolute paths, `..` and links
            # that escape `staging`; none of ours do, but the archive came from
            # disk. Older 3.11 patch releases raise TypeError on the keyword;
            # catching it here keeps the "nothing left behind" promise there too.
            tar.extractall(path=staging, members=members, filter="data")
        except (OSError, tarfile.TarError, TypeError) as e:
            shutil.rmtree(staging, ignore_errors=True)
            console.print(f"[red]unpack failed — live index unchanged: {e}[/red]")
            return 1

    src = staging.joinpath(*(part for part in prefix.split("/") if part), "chroma")
    try:
        if chroma_dir.exists():
            chroma_dir.rename(replaced)
        src.rename(chroma_dir)
    except OSError as e:
        if replaced.exists() and not chroma_dir.exists():
            replaced.rename(chroma_dir)
        shutil.rmtree(staging, ignore_errors=True)
        console.print(f"[red]swap failed — live index unchanged: {e}[/red]")
        return 1

    # The swap is done: say so before any cleanup, so the caller can tell a
    # restored index with leftovers from a restore that never happened.
    files = sum(1 for m in members if m.isfile())
    console.print(f"[green]restored[/green] {chroma_dir} from {archive.name} ({files} files)")
    for leftover in (staging, replaced):
        shutil.rmtree(leftover, ignore_errors=True)
        if leftover.exists():
            console.print(f"[yellow]could not remove {leftover} — remove it by hand[/yellow]")
    skipped = [c for c in components if c != "chroma"]
    if skipped:
        console.print(
            f"not restored: {', '.join(skipped)} (logs.sqlite / web_cache.sqlite are left "
            "for a deliberate manual copy)"
        )
    # `restart`, not `up -d`: `up -d` is a no-op on services that were never
    # stopped, and a running process keeps serving the index it loaded at
    # startup — the swap would then be invisible.
    console.print("services load it only on (re)start: docker compose restart web mattermost")
    return 0


_ARCHIVE_RE = re.compile(r"^student-bot-backup-(?P<components>[a-z+]+)-\d{8}-\d{6}\.tar\.gz$")


def archives_to_prune(existing: list[str], keep: int) -> list[str]:
    """Return the archive names to delete so that `keep` newest remain.

    Rotation is per component-set, so a nightly `--only chroma` run never
    counts against, or deletes, the full archives taken before a deploy — they
    are different things with different retention needs.

    The timestamp is embedded in the name and is UTC and zero-padded, so
    lexical order is chronological; no stat() call is needed, which also means
    a restored or copied file cannot be aged out by its mtime.
    """
    if keep <= 0:
        return []
    by_components: dict[str, list[str]] = {}
    for name in existing:
        m = _ARCHIVE_RE.match(name)
        if m:
            by_components.setdefault(m.group("components"), []).append(name)
    doomed: list[str] = []
    for names in by_components.values():
        names.sort(reverse=True)  # newest first
        doomed.extend(names[keep:])
    return sorted(doomed)


@click.command()
@click.option(
    "--only",
    multiple=True,
    type=click.Choice(COMPONENTS),
    help="Back up only these components (repeatable). Default: all of them.",
)
@click.option(
    "--out",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Destination directory (default: <data dir>/backups).",
)
@click.option("--no-archive", is_flag=True, help="Leave the staging directory; skip the tarball.")
@click.option(
    "--keep",
    type=int,
    default=0,
    show_default=True,
    help=(
        "After writing, delete older archives so only this many remain "
        "(counted per component-set). 0 disables pruning. Use --keep 3 for the "
        "nightly job; leave it off for one-off or pre-deploy snapshots."
    ),
)
@click.option(
    "--verify",
    "verify_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Verify an existing archive against its MANIFEST.json and exit.",
)
@click.option(
    "--restore",
    "restore_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Replace data/chroma with the index in this archive (verified first) and exit. "
        "Stop the services before, start them after."
    ),
)
def main(
    only: tuple[str, ...],
    out: Path | None,
    no_archive: bool,
    keep: int,
    verify_path: Path | None,
    restore_path: Path | None,
):
    console = Console()
    if verify_path is not None:
        sys.exit(_verify(console, verify_path))

    cfg = get_config()
    chroma_dir = cfg.absolute(cfg.paths.chroma_dir)
    if restore_path is not None:
        sys.exit(_restore(console, restore_path, chroma_dir))
    logs_db = cfg.absolute(cfg.paths.logs_db)
    cache_db = logs_db.parent / CACHE_DB_NAME
    wanted = set(only) if only else set(COMPONENTS)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"student-bot-backup-{'+'.join(sorted(wanted))}-{stamp}"
    dest_root = out if out is not None else logs_db.parent / "backups"
    staging = dest_root / name
    if staging.exists():
        console.print(f"[red]staging directory already exists: {staging}[/red]")
        sys.exit(1)
    staging.mkdir(parents=True)

    files: dict[str, dict] = {}
    collections: list[str] = []

    def record(rel: str, path: Path) -> None:
        files[rel] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}

    if "chroma" in wanted:
        sqlite_src = chroma_dir / "chroma.sqlite3"
        if not sqlite_src.exists():
            console.print(f"[red]no Chroma index at {chroma_dir} — run scripts.reindex first[/red]")
            shutil.rmtree(staging)
            sys.exit(1)
        _copy_sqlite(sqlite_src, staging / "chroma" / "chroma.sqlite3")
        record("chroma/chroma.sqlite3", staging / "chroma" / "chroma.sqlite3")
        # HNSW segment directories, one per collection, named by collection UUID.
        for seg in sorted(p for p in chroma_dir.iterdir() if p.is_dir()):
            collections.append(seg.name)
            shutil.copytree(seg, staging / "chroma" / seg.name)
            for f in sorted((staging / "chroma" / seg.name).rglob("*")):
                if f.is_file():
                    record(f"chroma/{seg.name}/{f.relative_to(staging / 'chroma' / seg.name)}", f)

    if "logs" in wanted and logs_db.exists():
        _copy_sqlite(logs_db, staging / logs_db.name)
        record(logs_db.name, staging / logs_db.name)

    if "cache" in wanted and cache_db.exists():
        _copy_sqlite(cache_db, staging / cache_db.name)
        record(cache_db.name, staging / cache_db.name)

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "python": platform.python_version(),
        "chromadb": _package_version("chromadb"),
        "components": sorted(wanted),
        "chroma_collections": collections,
        "qa_log_rows": _row_count(logs_db, "qa_log") if "logs" in wanted else None,
        "files": files,
    }
    (staging / "MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_mb = sum(f["bytes"] for f in files.values()) / 1e6
    if no_archive:
        console.print(f"[green]wrote[/green] {staging}  ({len(files)} files, {total_mb:.1f} MB)")
        target = staging
    else:
        archive = dest_root / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=name)
        shutil.rmtree(staging)
        size_mb = archive.stat().st_size / 1e6
        console.print(f"[green]wrote[/green] {archive}  ({len(files)} files, {size_mb:.1f} MB)")
        target = archive

    console.print(f"written by chromadb {manifest['chromadb']} on Python {manifest['python']}")
    if "logs" in wanted:
        console.print(
            "[yellow]contains logs.sqlite: qa_log holds student questions and answers — "
            "keep this archive internal (use --only chroma to share)[/yellow]"
        )
    console.print("verify with:")
    for hint in verify_hints(target):
        console.print(f"  {hint}", markup=False, highlight=False, soft_wrap=True)

    # Prune only after the new archive is safely on disk, so a failure above
    # never costs us the old ones too.
    if keep > 0 and not no_archive:
        names = [p.name for p in dest_root.glob("student-bot-backup-*.tar.gz")]
        doomed = archives_to_prune(names, keep)
        for name in doomed:
            (dest_root / name).unlink()
        if doomed:
            console.print(f"pruned {len(doomed)} archive(s), keeping the {keep} newest")
