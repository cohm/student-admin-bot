"""`student-bot-backup --restore` (scripts/backup.py).

The weekly maintenance job never rolls back on its own; it prints a restore
command. That command has to actually restore, and it has to refuse before it
touches the live index whenever the archive is not one it can trust. These
tests go through the real writer so the archive layout (`<name>/chroma/...`)
is the one `--restore` is built against.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from rich.console import Console

from scripts import backup
from scripts.backup import _restore, _sha256, main


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under `root` by content, except the SQLite file: the writer
    copies it through SQLite's online backup API, which yields an equal
    database rather than equal bytes (the header's change counter differs).
    `_sqlite_marker` covers that one."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "chroma.sqlite3"
    }


def _fake_index(chroma_dir: Path, marker: str) -> None:
    """A Chroma persist dir as backup.py sees it: sqlite + one segment folder."""
    chroma_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(chroma_dir / "chroma.sqlite3") as conn:
        conn.execute("create table if not exists t (v text)")
        conn.execute("delete from t")
        conn.execute("insert into t values (?)", (marker,))
    seg = chroma_dir / "0b3c0c7e-0000-4000-8000-000000000001"
    seg.mkdir(exist_ok=True)
    (seg / "data_level0.bin").write_bytes(marker.encode() * 64)
    (seg / "header.bin").write_bytes(b"hdr-" + marker.encode())


def _sqlite_marker(chroma_dir: Path) -> str:
    with sqlite3.connect(f"file:{chroma_dir / 'chroma.sqlite3'}?mode=ro", uri=True) as conn:
        return conn.execute("select v from t").fetchone()[0]


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the backup CLI's config at a throwaway data/ directory."""
    data = tmp_path / "data"
    data.mkdir()
    cfg = backup.get_config()
    paths = cfg.paths.model_copy(
        update={"chroma_dir": data / "chroma", "logs_db": data / "logs.sqlite"}
    )
    monkeypatch.setattr(backup, "get_config", lambda: cfg.model_copy(update={"paths": paths}))
    return data


def _write_archive(data: Path) -> Path:
    result = CliRunner().invoke(main, ["--only", "chroma", "--out", str(data / "backups")])
    assert result.exit_code == 0, result.output
    archives = list((data / "backups").glob("student-bot-backup-chroma-*.tar.gz"))
    assert len(archives) == 1
    return archives[0]


def test_round_trip_puts_the_snapshot_back(data_dir: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "good")
    archive = _write_archive(data_dir)
    good = _tree(chroma)

    # A bad reindex: content changed, and a stray file that must not survive.
    _fake_index(chroma, "bad")
    (chroma / "leftover.bin").write_bytes(b"junk")
    assert _sqlite_marker(chroma) == "bad"

    result = CliRunner().invoke(main, ["--restore", str(archive)])
    assert result.exit_code == 0, result.output
    assert "restored" in result.output

    assert _tree(chroma) == good
    assert _sqlite_marker(chroma) == "good"
    # No staging or pre-restore directory left beside the index.
    assert sorted(p.name for p in data_dir.iterdir()) == ["backups", "chroma"]
    # The archive itself is untouched, so the restore can be repeated.
    assert archive.exists()


def test_restore_works_when_there_is_no_index_yet(data_dir: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "good")
    archive = _write_archive(data_dir)
    good = _tree(chroma)

    shutil.rmtree(chroma)
    assert _restore(Console(), archive, chroma) == 0
    assert _tree(chroma) == good
    assert _sqlite_marker(chroma) == "good"


def test_checksum_mismatch_is_refused_before_the_index_is_touched(data_dir: Path, tmp_path: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    archive = _write_archive(data_dir)
    live = _tree(chroma)

    # Rewrite the archive with one member altered but the manifest unchanged.
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive, "r:gz") as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            fh = src.extractfile(member) if member.isfile() else None
            if member.name.endswith("data_level0.bin"):
                payload = b"not what the manifest says"
                member.size = len(payload)
                dst.addfile(member, io.BytesIO(payload))
            elif fh is not None:
                dst.addfile(member, fh)
            else:
                dst.addfile(member)

    console = Console(record=True)
    assert _restore(console, tampered, chroma) == 1
    assert "checksum mismatch" in console.export_text()
    assert _tree(chroma) == live
    assert sorted(p.name for p in data_dir.iterdir()) == ["backups", "chroma"]


def test_missing_archive_is_refused(data_dir: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    live = _tree(chroma)
    result = CliRunner().invoke(main, ["--restore", str(data_dir / "backups" / "nope.tar.gz")])
    assert result.exit_code == 1
    assert "no such archive" in result.output
    assert _tree(chroma) == live


def test_archive_without_a_chroma_component_is_refused(data_dir: Path, tmp_path: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    live = _tree(chroma)

    # A logs-only archive, in the writer's layout, with a valid manifest.
    name = "student-bot-backup-logs-20260101-000000"
    staging = tmp_path / name
    staging.mkdir()
    (staging / "logs.sqlite").write_bytes(b"sqlite-ish")
    manifest = {
        "components": ["logs"],
        "created_utc": "2026-01-01T00:00:00+00:00",
        "python": "3.12",
        "chromadb": "1.5.9",
        "files": {"logs.sqlite": {"bytes": 10, "sha256": _sha256(staging / "logs.sqlite")}},
    }
    (staging / "MANIFEST.json").write_text(json.dumps(manifest))
    archive = tmp_path / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=name)

    console = Console(record=True)
    assert _restore(console, archive, chroma) == 1
    assert "no chroma component" in console.export_text()
    assert _tree(chroma) == live


def _leftovers(data: Path) -> list[str]:
    return sorted(p.name for p in data.iterdir() if p.name not in ("backups", "chroma"))


def test_unpack_failure_leaves_nothing_behind(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Disk full mid-extract is the realistic one: the VM has filled before,
    and a nearly full disk is a plausible reason to be restoring at all."""
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    archive = _write_archive(data_dir)
    live = _tree(chroma)

    def boom(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tarfile.TarFile, "extractall", boom)
    console = Console(record=True)
    assert _restore(console, archive, chroma) == 1
    assert "unpack failed" in console.export_text()
    assert _tree(chroma) == live
    assert _sqlite_marker(chroma) == "live"
    assert _leftovers(data_dir) == []


def test_swap_failure_rolls_back_and_cleans_up(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    archive = _write_archive(data_dir)
    live = _tree(chroma)

    # Freeze the stamp so the "replaced" target can be made to collide with a
    # non-empty directory, which makes the first rename fail with ENOTEMPTY.
    class FrozenDatetime(backup.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 2, 3, 4, 5, tzinfo=tz)

    monkeypatch.setattr(backup, "datetime", FrozenDatetime)
    blocker = data_dir / "chroma-replaced-20260102-030405"
    (blocker / "occupied").mkdir(parents=True)

    console = Console(record=True)
    assert _restore(console, archive, chroma) == 1
    assert "swap failed" in console.export_text()
    assert _tree(chroma) == live
    assert _sqlite_marker(chroma) == "live"
    # The staging directory is gone; only the pre-existing blocker remains.
    assert _leftovers(data_dir) == [blocker.name]


def test_symlinked_chroma_dir_is_restored_through_the_link(data_dir: Path, tmp_path: Path):
    target = tmp_path / "elsewhere" / "chroma"
    _fake_index(target, "good")
    link = data_dir / "chroma"
    link.symlink_to(target, target_is_directory=True)
    archive = _write_archive(data_dir)
    good = _tree(target)

    _fake_index(target, "bad")
    assert _restore(Console(), archive, link) == 0
    assert link.is_symlink() and link.resolve() == target.resolve()
    assert _tree(target) == good
    assert _sqlite_marker(link) == "good"
    assert sorted(p.name for p in target.parent.iterdir()) == ["chroma"]


def test_manifest_claims_chroma_but_archive_has_no_chroma_files(data_dir: Path, tmp_path: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "live")
    live = _tree(chroma)

    name = "student-bot-backup-chroma-20260101-000000"
    staging = tmp_path / name
    staging.mkdir()
    manifest = {
        "components": ["chroma"],
        "created_utc": "2026-01-01T00:00:00+00:00",
        "python": "3.12",
        "chromadb": "1.5.9",
        "files": {},
    }
    (staging / "MANIFEST.json").write_text(json.dumps(manifest))
    archive = tmp_path / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staging, arcname=name)

    console = Console(record=True)
    assert _restore(console, archive, chroma) == 1
    assert "holds no chroma/ files" in console.export_text()
    assert _tree(chroma) == live
    assert _leftovers(data_dir) == []


def test_full_archive_restores_only_chroma_and_says_so(data_dir: Path):
    chroma = data_dir / "chroma"
    _fake_index(chroma, "good")
    logs = data_dir / "logs.sqlite"
    with sqlite3.connect(logs) as conn:
        conn.execute("create table qa_log (id integer)")
    result = CliRunner().invoke(main, ["--out", str(data_dir / "backups")])
    assert result.exit_code == 0, result.output
    archive = next((data_dir / "backups").glob("student-bot-backup-*.tar.gz"))
    assert "+logs" in archive.name

    _fake_index(chroma, "bad")
    logs.write_bytes(b"the live log must not be touched")
    console = Console(record=True)
    assert _restore(console, archive, chroma) == 0
    assert _sqlite_marker(chroma) == "good"
    assert logs.read_bytes() == b"the live log must not be touched"
    assert "not restored: cache, logs" in console.export_text()
