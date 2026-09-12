"""Retention for nightly backups (scripts/backup.py).

Rotation keys on the timestamp embedded in the filename rather than on mtime:
the names are UTC and zero-padded, so lexical order is chronological, and a
restored or copied archive cannot be aged out just because the filesystem gave
it a fresh mtime.
"""

from __future__ import annotations

from scripts.backup import archives_to_prune


def _name(components: str, stamp: str) -> str:
    return f"student-bot-backup-{components}-{stamp}.tar.gz"


_CHROMA = [_name("chroma", f"2026091{d}-030000") for d in range(0, 10)]


def test_keeps_the_n_newest_and_deletes_the_rest():
    doomed = archives_to_prune(_CHROMA, keep=3)
    assert len(doomed) == 7
    for kept in _CHROMA[-3:]:
        assert kept not in doomed


def test_zero_disables_pruning():
    assert archives_to_prune(_CHROMA, keep=0) == []
    assert archives_to_prune(_CHROMA, keep=-1) == []


def test_keep_larger_than_the_set_deletes_nothing():
    assert archives_to_prune(_CHROMA, keep=99) == []


def test_component_sets_rotate_independently():
    """A nightly `--only chroma` run must not age out the full pre-deploy
    archives, which are a different thing kept for a different reason."""
    full = [_name("cache+chroma+logs", f"2026090{d}-120000") for d in range(1, 4)]
    doomed = archives_to_prune(_CHROMA + full, keep=2)
    # 8 chroma-only dropped, 1 full dropped — counted separately, not 9 of 13.
    assert sum(1 for d in doomed if "-chroma-" in d) == 8
    assert sum(1 for d in doomed if "cache+chroma+logs" in d) == 1


def test_ignores_files_that_are_not_our_archives():
    junk = ["notes.txt", "student-bot-backup-chroma-bad.tar.gz", "chroma-manual.tar.gz"]
    doomed = archives_to_prune(_CHROMA + junk, keep=1)
    for j in junk:
        assert j not in doomed


def test_ordering_is_lexical_which_is_chronological():
    # Same day, different times — the later one survives.
    pair = [_name("chroma", "20260912-030000"), _name("chroma", "20260912-235959")]
    assert archives_to_prune(pair, keep=1) == [_name("chroma", "20260912-030000")]
