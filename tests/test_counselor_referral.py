"""When a refusal should, and should not, name the study counselor (#84).

Every refusal used to end with "or contact the study counselor", including for
questions about pasta recipes and football results. That is real work created
for a person who cannot help with any of it.
"""

from __future__ import annotations

import pytest

from student_bot.config import get_config
from student_bot.bot.prompts import question_is_offtopic, refusal_message


@pytest.fixture(scope="module")
def cfg():
    return get_config()


# Scores measured against eval/eval_set.yml on the current reranker: every
# in-domain refusal lands in -2.64..-0.88, while OOD refusals reach -6.11.
IN_DOMAIN_REFUSALS = [-2.64, -1.80, -1.76, -1.44, -1.12, -0.88]
CLEARLY_OFFTOPIC = [-6.11, -5.95, -5.30, -5.04, -5.03, -4.85, -4.42, -4.12, -3.27, -3.23]


@pytest.mark.parametrize("top1", IN_DOMAIN_REFUSALS)
def test_no_in_domain_refusal_loses_the_referral(cfg, top1):
    """The expensive mistake: a student with a real administrative question
    told only that the bot cannot help, with nowhere to go next."""
    assert not question_is_offtopic(cfg, top1)


@pytest.mark.parametrize("top1", CLEARLY_OFFTOPIC)
def test_clearly_offtopic_questions_drop_the_referral(cfg, top1):
    assert question_is_offtopic(cfg, top1)


def test_the_threshold_sits_below_every_in_domain_refusal(cfg):
    """Guards the margin, not just the outcome: if a reranker change pushes an
    in-domain refusal below the floor, this fails before a student does."""
    assert cfg.gate.offtopic_top1_max < min(IN_DOMAIN_REFUSALS)


def test_ambiguous_scores_keep_the_referral(cfg):
    """The two ranges overlap at the TOP — "What's 2+2?" scores -0.02 and
    "Recommend a good pizza place near KTH" +0.24, above every in-domain
    refusal. Those keep the referral, which is the safe direction."""
    for top1 in (-0.02, 0.24):
        assert not question_is_offtopic(cfg, top1)


@pytest.mark.parametrize(
    "lang,label_attr", [("sv", "counselor_label_sv"), ("en", "counselor_label_en")]
)
def test_in_scope_refusal_names_the_counselor(cfg, lang, label_attr):
    body = refusal_message(cfg, lang, offer_counselor=True)
    assert getattr(cfg.fallback, label_attr) in body


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_offtopic_refusal_names_nobody(cfg, lang):
    body = refusal_message(cfg, lang, offer_counselor=False)
    assert cfg.fallback.counselor_label_sv not in body
    assert cfg.fallback.counselor_label_en not in body
    assert cfg.fallback.counselor_link not in body


@pytest.mark.parametrize("lang", ["sv", "en"])
def test_both_refusals_still_say_what_the_bot_covers(cfg, lang):
    """Dropping the referral must not drop the scope — otherwise an off-topic
    asker learns nothing about what to ask instead."""
    for offer in (True, False):
        body = refusal_message(cfg, lang, offer_counselor=offer)
        assert ("tentamen" in body) or ("examinations" in body)


def test_the_default_keeps_the_referral(cfg):
    """A caller that has not thought about this gets the old, safe behaviour."""
    assert refusal_message(cfg, "sv") == refusal_message(cfg, "sv", offer_counselor=True)


def test_language_is_respected_in_both_variants(cfg):
    assert refusal_message(cfg, "en", offer_counselor=False).startswith("That question")
    assert refusal_message(cfg, "sv", offer_counselor=False).startswith("Den frågan")
