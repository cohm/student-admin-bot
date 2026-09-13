"""The uv.lock downgrade guard (scripts/check_lock_downgrades.py, #90).

Reconstructs the #87 regression — one wrong digit in a version ceiling walked
the embedding stack back about a year, and nothing in CI noticed.
"""

from __future__ import annotations

import pytest

from scripts.check_lock_downgrades import (
    _version_key,
    allowances_from_pr_body,
    downgrades,
    locked_versions,
)


LOCK = """\
version = 1

[[package]]
name = "chromadb"
version = "0.6.3"

[[package]]
name = "transformers"
version = "5.7.0"
"""


def test_parses_names_and_versions():
    assert locked_versions(LOCK) == {"chromadb": ("0.6.3",), "transformers": ("5.7.0",)}


def test_the_87_regression_is_caught():
    """The exact four packages, and the exact versions, from the incident."""
    base = {
        "chromadb": ("0.6.3",),
        "tokenizers": ("0.22.2",),
        "transformers": ("5.7.0",),
        "huggingface-hub": ("1.13.0",),
    }
    head = {
        "chromadb": ("0.5.23",),
        "tokenizers": ("0.20.3",),
        "transformers": ("4.46.3",),
        "huggingface-hub": ("0.36.2",),
    }
    assert [n for n, _, _ in downgrades(base, head)] == [
        "chromadb",
        "huggingface-hub",
        "tokenizers",
        "transformers",
    ]


def test_upgrades_are_silent():
    assert downgrades({"chromadb": ("0.6.3",)}, {"chromadb": ("1.5.9",)}) == []


def test_unchanged_is_silent():
    assert downgrades({"a": ("1.0.0",)}, {"a": ("1.0.0",)}) == []


def test_added_and_removed_packages_are_not_downgrades():
    """A dependency appearing or disappearing is not this check's business."""
    assert downgrades({"a": ("1.0",)}, {"a": ("1.0",), "b": ("2.0",)}) == []
    assert downgrades({"a": ("1.0",), "b": ("2.0",)}, {"a": ("1.0",)}) == []


# --- packages with several entries ---------------------------------------
#
# uv.lock carries one entry per platform marker, so a name can appear more than
# once. Keeping only one of them hid a downgrade of the rest — and transformers,
# which has two entries in this very lock, was one of the four packages in #87.


def test_multiple_entries_per_package_are_all_parsed():
    lock = """\
version = 1

[[package]]
name = "transformers"
version = "5.15.1"

[[package]]
name = "transformers"
version = "5.8.1"
"""
    assert locked_versions(lock) == {"transformers": ("5.8.1", "5.15.1")}


def test_a_downgrade_of_only_the_lower_entry_is_caught():
    """The case the first version of this guard missed entirely."""
    found = downgrades(
        {"transformers": ("5.8.1", "5.15.1")}, {"transformers": ("4.46.3", "5.15.1")}
    )
    assert [n for n, _, _ in found] == ["transformers"]


def test_a_downgrade_of_only_the_higher_entry_is_caught():
    found = downgrades({"transformers": ("5.8.1", "5.15.1")}, {"transformers": ("5.8.1", "5.9.0")})
    assert [n for n, _, _ in found] == ["transformers"]


def test_both_entries_moving_forward_is_silent():
    assert (
        downgrades({"transformers": ("5.8.1", "5.15.1")}, {"transformers": ("5.9.0", "5.16.0")})
        == []
    )


def test_a_local_version_suffix_is_not_a_downgrade():
    """torch is locked as both 2.13.0 and 2.13.0+cpu in this repo."""
    assert (
        downgrades({"torch": ("2.13.0", "2.13.0+cpu")}, {"torch": ("2.14.0", "2.14.0+cpu")}) == []
    )


@pytest.mark.parametrize(
    "lower,higher",
    [
        ("0.6.3", "1.5.9"),
        ("1.9.0", "1.10.0"),  # numeric, not lexical: 10 > 9
        ("2.0.0rc1", "2.0.0"),  # a release beats its own pre-release
        ("1.2", "1.2.1"),
        ("0.36.2", "1.13.0"),
    ],
)
def test_version_ordering(lower, higher):
    assert _version_key(lower) < _version_key(higher)


def test_lexical_comparison_would_have_missed_this():
    """Guards the guard: '1.10.0' < '1.9.0' as strings, so a naive check would
    call a real upgrade a downgrade and cry wolf on ordinary PRs."""
    assert "1.10.0" < "1.9.0"
    assert downgrades({"a": ("1.9.0",)}, {"a": ("1.10.0",)}) == []


def test_an_unorderable_version_is_reported_not_ignored():
    """Where a downgrade could hide is exactly where silence is wrong."""
    found = downgrades({"weird": ("2024.1",)}, {"weird": ("1.0-alpha",)})
    assert [n for n, _, _ in found] == ["weird"]


def test_a_pr_can_authorise_a_specific_downgrade():
    body = "Pinning back off a broken release.\n\nallow-downgrade: chromadb\n"
    assert allowances_from_pr_body(body) == {"chromadb"}
    assert (
        downgrades({"chromadb": ("1.5.9",)}, {"chromadb": ("1.5.8",)}, allowed={"chromadb"}) == []
    )


def test_the_allowance_is_per_package_not_a_blanket():
    body = "allow-downgrade: chromadb"
    allowed = allowances_from_pr_body(body)
    found = downgrades(
        {"chromadb": ("1.5.9",), "torch": ("2.13.0",)},
        {"chromadb": ("1.5.8",), "torch": ("2.11.0",)},
        allowed=allowed,
    )
    assert [n for n, _, _ in found] == ["torch"]


def test_no_allowance_in_an_ordinary_description():
    assert allowances_from_pr_body("Ordinary PR, nothing special.") == frozenset()
    assert allowances_from_pr_body(None) == frozenset()


def test_the_real_lock_parses():
    """Catches a uv.lock format change breaking the guard silently."""
    from student_bot.config import PROJECT_ROOT

    versions = locked_versions((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert versions["chromadb"][0].startswith("1.")
    assert len(versions) > 50
    # The real lock must actually exercise the multi-entry path, or the tests
    # above are testing a situation this repo never produces.
    assert any(len(v) > 1 for v in versions.values())
