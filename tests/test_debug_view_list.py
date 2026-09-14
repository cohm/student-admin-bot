"""The debug view lists the whole session (#92).

It used to render exactly one turn — whichever 🔍 you last clicked — and
opening the tab on its own showed nothing at all. Comparing two answers meant
clicking 🔍, reading, going back, clicking the other, and there was no way to
tell a turn even had diagnostics without trying it.

These assert the wiring in the client, since there is no JS test runner here
and the failure modes are all "silently renders nothing".
"""

from __future__ import annotations

import re

import pytest

from student_bot.config import PROJECT_ROOT


STATIC = PROJECT_ROOT / "src" / "student_bot" / "web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
I18N = (STATIC / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_the_session_turns_are_tracked():
    assert "turns: []" in APP_JS
    assert "state.turns.push(" in APP_JS


def test_a_turn_records_the_question_that_produced_it():
    """qa_id only arrives in the meta event, by which point the question is out
    of scope where the turn is recorded — so it is carried on the bubble."""
    assert "botMsg.question = q;" in APP_JS
    assert "botMsg.question" in APP_JS


def test_turns_are_not_double_recorded():
    assert "state.turns.some(" in APP_JS


def test_opening_the_tab_renders_the_list():
    """The old behaviour: the tab switched view and showed whatever was left
    over from the last 🔍, or nothing."""
    tab_handler = APP_JS[APP_JS.index('document.querySelectorAll(".view-tab")') :]
    tab_handler = tab_handler[: tab_handler.index("\n});")]
    assert "renderDebugTurns()" in tab_handler


def test_the_magnifier_still_opens_its_own_turn():
    """The per-answer entry point must survive: it now opens the list *and*
    expands that turn, rather than replacing the list with one turn."""
    fn = APP_JS[APP_JS.index("function showDebugPanel(qaId)") :]
    fn = fn[: fn.index("\n}\n")]
    assert "renderDebugTurns()" in fn
    assert "target.open = true" in fn
    assert "loadDebugTurn(target)" in fn


def test_bodies_load_lazily():
    """A long session would otherwise fire one request per turn just to draw a
    list of headings."""
    assert 'addEventListener("toggle"' in APP_JS
    assert "if (details.open) loadDebugTurn(details)" in APP_JS
    assert 'details.dataset.loaded === "1"' in APP_JS


def test_a_transient_error_can_be_retried_but_a_404_cannot():
    """404 means the turn has no diagnostics — a fact, not a failure. Retrying
    it on every expand would be pointless; retrying a 502 is not."""
    fn = APP_JS[APP_JS.index("async function loadDebugTurn") :]
    fn = fn[: fn.index("\nfunction renderDebugTurns")]
    assert "resp.status === 404" in fn
    assert 'if (resp.status !== 404) details.dataset.loaded = ""' in fn


def test_reset_clears_the_list_and_the_cache():
    """Otherwise "Börja om" leaves the previous conversation listed."""
    assert "state.turns = [];" in APP_JS
    assert "state.debugCache.clear();" in APP_JS


def test_newest_turn_is_listed_first():
    """The turn you just asked about is the one you want, and it would
    otherwise be at the bottom of a growing list."""
    fn = APP_JS[APP_JS.index("function renderDebugTurns") :]
    assert ".reverse()" in fn[: fn.index("\n}\n")]


def test_question_text_is_escaped_into_the_summary():
    """Student-authored text rendered into innerHTML."""
    fn = APP_JS[APP_JS.index("function renderDebugTurns") :]
    fn = fn[: fn.index("\n}\n")]
    assert "escapeHtml(turn.question)" in fn


def test_the_qa_id_selector_is_escaped():
    """qa_id goes into a CSS attribute selector."""
    assert "CSS.escape(String(qaId))" in APP_JS


@pytest.mark.parametrize("key", ["debug.noturns", "debug.msg.loading"])
def test_new_strings_exist_in_both_languages(key):
    assert I18N.count(f'"{key}"') == 2, key


def test_the_intro_no_longer_tells_you_to_click_the_magnifier():
    """It described the old single-turn behaviour."""
    assert "Klicka på 🔍 vid ett svar" not in I18N
    assert "Click the 🔍 next to a reply" not in I18N


def test_the_list_uses_native_details_semantics():
    """Keyboard operation and screen-reader semantics come free; a div-based
    accordion would have to reimplement both."""
    assert '<details class="debug-turn"' in APP_JS
    assert '<summary class="debug-turn-head"' in APP_JS
    assert ".debug-turn-head::-webkit-details-marker" in CSS
    assert re.search(r"\.debug-turn-head\s*\{[^}]*list-style:\s*none", CSS)
