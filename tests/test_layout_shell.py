"""Layout invariants for the chat shell.

CSS has no test suite, so these assert the load-bearing declarations rather
than appearance — the ones whose removal reintroduces a bug we measured:
a page that scrolls *and* a transcript that scrolls, with the composer pushed
below the fold.
"""

from __future__ import annotations

import re

import pytest

from student_bot.config import PROJECT_ROOT


STATIC = PROJECT_ROOT / "src" / "student_bot" / "web" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    """Every declaration for an exact selector, concatenated.

    A selector can legitimately appear more than once — `body.viewing-debug
    #debug-panel` sets `display` in one place and the scroll behaviour in
    another — so returning only the first match would test the wrong block.
    """
    bodies = re.findall(rf"(?:^|\}}|\*/)\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS, re.M)
    assert bodies, f"no rule for {selector}"
    return " ".join(bodies)


def test_the_chat_page_does_not_scroll():
    """Scoped to `.app-shell`, not bare `body`. Unscoped it also locked the
    server-rendered About / Ordlista / Statistik pages, which have no internal
    scroll region — so their content was simply clipped."""
    shell = _rule("body.app-shell")
    assert "height: 100dvh" in shell
    assert "overflow: hidden" in shell


def test_every_other_page_scrolls_normally():
    body = _rule("body")
    assert "min-height: 100dvh" in body
    assert "overflow: hidden" not in body


def test_only_the_chat_page_carries_the_shell_class():
    assert 'class="app-shell"' in INDEX


def test_the_transcript_is_the_scrolling_region():
    messages = _rule("#messages")
    assert "overflow-y: auto" in messages
    assert "flex: 1" in messages


def test_the_transcript_has_no_fixed_cap():
    """`max-height: 60dvh` was half the old problem: it created a second
    scrollbar while the page kept its own."""
    assert "max-height: 60dvh" not in CSS


@pytest.mark.parametrize("selector", ["body.app-shell main", "#chat", "#messages"])
def test_every_flex_ancestor_can_shrink(selector):
    """Without `min-height: 0` a flex child refuses to shrink below its content
    and the overflow escapes the shell — the failure is silent and looks like
    the CSS simply did nothing."""
    assert "min-height: 0" in _rule(selector), selector


def test_the_tagline_sits_outside_the_brand_cluster():
    """It has to be its own grid item to occupy a header row of its own, which
    is what the mobile layout needs."""
    brand_end = INDEX.index("</div>", INDEX.index('class="brand"'))
    tagline = INDEX.index('data-i18n="header.tagline"')
    assert tagline > brand_end


def test_there_is_a_narrow_viewport_breakpoint():
    """Before this there were none at all, so phones got the desktop grid."""
    assert "@media (max-width: 767px)" in CSS


def test_mobile_releases_the_viewport_lock():
    """A phone keyboard resizes the viewport under the page; pinning a composer
    against that is unreliable, so mobile scrolls normally with a sticky
    composer instead."""
    mobile = CSS[CSS.index("@media (max-width: 767px)") :]
    assert "position: sticky" in mobile
    assert "overflow: visible" in mobile


def test_debug_view_keeps_its_horizontal_scroll():
    """The retrieval table has a 1000px floor. With the page locked, the scroll
    must live on body — `main` IS the oversized element, so overflow-x there
    does nothing and the right-hand columns are silently clipped."""
    assert "body.app-shell.viewing-debug { overflow-x: auto; }" in CSS


def test_debug_panel_scrolls_vertically():
    assert "overflow-y: auto" in _rule("body.viewing-debug #debug-panel")
