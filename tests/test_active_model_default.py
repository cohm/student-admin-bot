"""The committed default model must be the one actually running.

Prod ran gemma4-26b-a4b from 2026-09-10 via an LLM_ACTIVE override while
config.yaml still said gemma4-31b-mtp. Nothing broke, because prod's .env kept
overriding it — but the repo, the talk deck and the running system disagreed,
and losing that one .env line would have silently swapped the model instead of
failing.
"""

from __future__ import annotations

import re

from student_bot.config import PROJECT_ROOT, get_config


DECK = (PROJECT_ROOT / "docs" / "slides" / "index.html").read_text(encoding="utf-8")


def test_the_default_resolves():
    """A typo here is only caught at first generation — by a student."""
    cfg = get_config()
    resolved = cfg.active_model()
    assert resolved.model_id
    assert resolved.provider_key in cfg.llm.providers


def test_the_deck_names_the_active_model():
    """`CLAUDE.md` flags the deck as drift-prone, and the model name is one of
    the facts it hard-codes."""
    model_id = get_config().active_model().model_id
    assert model_id in DECK, f"deck does not mention {model_id}"


def test_the_deck_does_not_still_advertise_a_different_gemma():
    """Catches the half-update: new name added, old one left in place."""
    model_id = get_config().active_model().model_id
    others = {m for m in re.findall(r"gemma4-[0-9a-z-]+", DECK) if m != model_id}
    assert not others, f"deck also names {sorted(others)}"


def test_the_active_model_is_local():
    """The notice claims the model runs on KTH hardware whenever this is
    False. A default that disclosed externally would make it lie."""
    assert not get_config().active_model().discloses_external
