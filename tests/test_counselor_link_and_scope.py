"""The counselor referral renders as a link, and off-topic questions skip it.

Reported from prod: a lasagne-recipe question was told to contact the study
counselor, with the URL as bare parenthesised text rather than a link.
"""

from __future__ import annotations

import pytest

from student_bot.config import get_config
from student_bot.bot.prompts import (
    SCOPE_EN,
    SCOPE_SV,
    counselor_ref,
    meta_fallback_system_prompt,
    refusal_message,
)


@pytest.fixture(scope="module")
def cfg():
    return get_config()


# --- the link ------------------------------------------------------------


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_the_referral_is_a_markdown_link(cfg, lang):
    """Every channel renders markdown, so " (https://…)" was never going to
    become a link anywhere."""
    ref = counselor_ref(cfg, lang)
    assert ref.startswith("[") and "](" in ref and ref.endswith(")")
    assert cfg.fallback.counselor_link in ref


def test_a_bare_url_no_longer_appears_in_the_refusal(cfg):
    body = refusal_message(cfg, "sv")
    # A space before the paren is the old " (https://…)" form. Testing for
    # "(url)" alone would fail on the correct output too, since a markdown
    # link contains it as a substring.
    assert f" ({cfg.fallback.counselor_link})" not in body
    assert f"]({cfg.fallback.counselor_link})" in body


def test_no_link_configured_degrades_to_the_plain_label(cfg):
    c = cfg.model_copy(deep=True)
    c.fallback.counselor_link = ""
    assert counselor_ref(c, "sv") == c.fallback.counselor_label_sv
    assert "[" not in counselor_ref(c, "sv")


# --- off-topic must skip the referral on the path it actually takes -------


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_meta_fallback_offers_the_counselor_when_in_scope(cfg, lang):
    sp = meta_fallback_system_prompt(cfg, lang, offer_counselor=True)
    assert cfg.fallback.counselor_link in sp


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_meta_fallback_forbids_the_referral_when_off_topic(cfg, lang):
    """The gap in #84: that change only touched `refusal_message`, which is the
    fallback-of-the-fallback. An off-topic question goes through the model with
    this prompt, which still said "refer them to the study counselor"."""
    sp = meta_fallback_system_prompt(cfg, lang, offer_counselor=False)
    assert ("Hänvisa INTE" in sp) or ("Do NOT refer" in sp)


def test_the_default_still_offers_the_counselor(cfg):
    assert meta_fallback_system_prompt(cfg, "sv") == meta_fallback_system_prompt(
        cfg, "sv", offer_counselor=True
    )


# --- scope ---------------------------------------------------------------


@pytest.mark.parametrize("term", ["behörighet", "utbildningsplaner"])
def test_swedish_scope_lists_the_added_areas(term):
    assert term in SCOPE_SV


@pytest.mark.parametrize("term", ["eligibility", "syllabuses"])
def test_english_scope_lists_the_added_areas(term):
    assert term in SCOPE_EN


@pytest.mark.parametrize("scope", [SCOPE_SV, SCOPE_EN])
def test_no_area_is_listed_twice(scope):
    areas = [a.strip().lower() for a in scope.replace(" samt ", "; ").split(";")]
    words = [w for a in areas for w in a.split() if len(w) > 5]
    assert len(words) == len(set(words)), sorted(w for w in words if words.count(w) > 1)
