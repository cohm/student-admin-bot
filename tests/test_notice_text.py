"""The experimental-service notice, after the 2026-09 rewrite.

The old text told readers the server was slow and the model small, neither of
which survived the move to the gateway and gemma4-26b-a4b. What replaced the
slowness claim is a correctness caveat, which is the part that was always true.

The load-bearing bit is the middle sentence: it asserts the model runs on KTH
hardware, so it must appear only when that holds.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from student_bot.config import PROJECT_ROOT, get_config
from student_bot.web.app import _model_is_local, _notice_html, create_app


STATIC = PROJECT_ROOT / "src" / "student_bot" / "web" / "static"
I18N = (STATIC / "i18n.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def cfg():
    c = get_config().model_copy(deep=True)
    # Pin the ambient bits this module does not care about. get_config() is
    # lru_cached and other modules set WEB_AUTH_ENABLED before clearing it, so
    # inheriting whatever is cached makes these tests order-dependent — they
    # passed alone and failed in the suite.
    c.web.auth_enabled = False
    return c


def test_local_model_gets_the_hardware_sentence(cfg):
    provider = cfg.llm.providers[cfg.active_model().provider_key]
    provider.discloses_external = False
    assert _model_is_local(cfg)
    assert "notice.model.local" in _notice_html(cfg)


def test_cloud_model_does_not_claim_to_run_here(cfg):
    """The one sentence that would become a lie."""
    provider = cfg.llm.providers[cfg.active_model().provider_key]
    provider.discloses_external = True
    assert not _model_is_local(cfg)
    assert "notice.model.local" not in _notice_html(cfg)


def test_an_unresolvable_model_is_not_treated_as_local(cfg):
    """Fail toward silence: claiming local wrongly is worse than omitting it."""
    cfg.llm.active = "nosuchprovider/nosuchmodel"
    assert not _model_is_local(cfg)
    assert "notice.model.local" not in _notice_html(cfg)


def test_the_notice_always_carries_caveat_and_feedback(cfg):
    html = _notice_html(cfg)
    assert 'data-i18n="notice.body"' in html
    assert 'data-i18n="notice.feedback"' in html
    assert 'data-i18n="notice.title"' in html


def test_the_chat_page_ships_all_three_and_hides_one_in_js():
    """index.html is static, so the sentence ships and app.js removes it for a
    cloud model — the server-rendered pages simply never emit it."""
    assert 'id="notice-model-local"' in INDEX
    assert 'data-i18n="notice.feedback"' in INDEX
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "applyLocalModelSentence" in js


def test_the_same_signal_drives_the_notice_and_the_cloud_banner():
    """If these ever diverged, the page could claim local while warning about
    a cloud provider in the next breath."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    block = js[js.index("cloudProviderName = data.cloud_provider_name") :]
    head = block[: block.index("}")]
    assert "applyCloudProviderNotice" in head
    assert "applyLocalModelSentence" in head


@pytest.mark.parametrize("key", ["notice.body", "notice.model.local", "notice.feedback"])
def test_strings_exist_in_both_languages(key):
    assert I18N.count(f'"{key}"') == 2, key


@pytest.mark.parametrize(
    "claim",
    [
        "begränsade resurser",
        "språkmodellen är liten",
        "svar kan vara långsamma",
        "Första frågan kan ta extra lång tid",
        "limited resources",
        "the language model is small",
        "responses may be slow",
        "The first question can take noticeably longer",
    ],
)
def test_retired_claims_are_gone(claim):
    """All obsolete since the gateway move and gemma4-26b-a4b."""
    assert claim not in I18N


def test_the_feedback_sentence_names_what_feedback_actually_changes():
    assert "anpassa dess kunskapsbas" in I18N
    assert "shape its knowledge base" in I18N


def test_the_notice_renders_on_a_real_page(cfg):
    body = TestClient(create_app(cfg)).get("/about").text
    assert 'class="notice"' in body
    assert 'data-i18n="notice.feedback"' in body
