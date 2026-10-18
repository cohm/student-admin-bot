"""renderMarkdown must not let a link URL break out of its href (#147).

Since #145 an admin can open another user's turn from /stats, and that turn's
answer is drawn with the same renderMarkdown as a chat bubble. A model-emitted
link with a `"` in the URL used to close the href attribute and add one of its
own, e.g. `onmouseover`, which then ran in the admin's browser.

There is no JS test runner here, so this runs the real functions from app.js
in node. GitHub's runners ship node; the test skips where it is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from student_bot.config import PROJECT_ROOT


APP_JS = (PROJECT_ROOT / "src" / "student_bot" / "web" / "static" / "app.js").read_text(
    encoding="utf-8"
)


def _function_source(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    end = APP_JS.index("\n}\n", start) + 2
    return APP_JS[start:end]


def _render(markdown: str) -> str:
    script = (
        _function_source("escapeAttr")
        + _function_source("renderMarkdown")
        + "\nconst input = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        + "\nprocess.stdout.write(JSON.stringify(renderMarkdown(input)));"
    )
    out = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(markdown),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@needs_node
def test_a_double_quote_in_a_link_cannot_add_an_attribute():
    html = _render('[länk](https://kth.se/"onmouseover="alert(1))')
    assert '"onmouseover' not in html
    assert "&quot;onmouseover=&quot;" in html


@needs_node
def test_a_single_quote_in_a_link_is_escaped():
    html = _render("[länk](https://kth.se/'onmouseover='alert(1))")
    assert "'onmouseover" not in html


@needs_node
def test_ordinary_links_and_formatting_still_render():
    html = _render("Se **schemat** på [KTH](https://www.kth.se/student?a=1&b=2).")
    assert (
        '<a href="https://www.kth.se/student?a=1&amp;b=2" target="_blank" rel="noopener">KTH</a>'
        in html
    )
    assert "<strong>schemat</strong>" in html
    assert _render("Det är _snart_ dags.") == "<p>Det är <em>snart</em> dags.</p>"


@needs_node
def test_quotes_in_plain_text_still_read_as_quotes():
    """Escaped as entities, which the browser shows as the characters."""
    html = _render('Kursen heter "Flervariabelanalys" och det är "rätt".')
    assert html == "<p>Kursen heter &quot;Flervariabelanalys&quot; och det är &quot;rätt&quot;.</p>"


def test_starting_over_also_drops_the_turn_opened_from_stats():
    """Otherwise the other user's turn stays listed after "Börja om"."""
    reset = APP_JS[APP_JS.index('fetch("api/reset"') :]
    reset = reset[: reset.index("\n});")]
    assert "state.turns = [];" in reset
    assert "state.foreignTurn = null;" in reset
