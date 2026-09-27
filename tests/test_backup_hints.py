"""The `--verify` hint scripts/backup.py prints after writing an archive.

Prod has no uv, so the hint has to include the `docker compose run` form, and
that form only works with a repo-relative path: inside the container the
archive lives under /app/data, which does not exist on the host.
"""

from __future__ import annotations

from pathlib import Path

from scripts.backup import verify_hints


def test_archive_inside_the_repo_gets_both_forms_with_a_relative_path(tmp_path: Path):
    archive = tmp_path / "data" / "backups" / "student-bot-backup-chroma-20260927-021700.tar.gz"
    rel = "data/backups/student-bot-backup-chroma-20260927-021700.tar.gz"
    assert verify_hints(archive, root=tmp_path) == [
        f"uv run student-bot-backup --verify {rel}",
        f"docker compose run --rm -T web student-bot-backup --verify {rel}",
    ]


def test_archive_outside_the_repo_gets_only_the_uv_form(tmp_path: Path):
    root = tmp_path / "repo"
    archive = tmp_path / "elsewhere" / "x.tar.gz"
    assert verify_hints(archive, root=root) == [f"uv run student-bot-backup --verify {archive}"]
