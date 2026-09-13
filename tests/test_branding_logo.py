"""The optional second header logo (issue #71).

The bot is a KTH service, so it must not ship wearing one section's badge —
but a deployment should still be able to add its own. Default off, opt-in via
`web.branding_logo`.
"""

from __future__ import annotations

import pytest

from student_bot.config import get_config
from student_bot.web.app import _branding_logo_html


@pytest.fixture
def cfg():
    c = get_config().model_copy(deep=True)
    c.web.branding_logo = ""
    c.web.branding_logo_alt = ""
    return c


def test_no_logo_by_default(cfg):
    assert _branding_logo_html(cfg, "/static") == ""


def test_the_f_logo_is_gone_from_every_page_source():
    """The whole point of the issue. It must not linger in the static chat
    page, which does not go through the shared header template."""
    from student_bot.config import PROJECT_ROOT

    web = PROJECT_ROOT / "src" / "student_bot" / "web"
    for path in (web / "app.py", web / "static" / "index.html"):
        assert "FrakturF" not in path.read_text(encoding="utf-8"), path


def test_the_asset_is_kept_so_it_can_be_switched_back_on():
    """Removing the default is not the same as deleting the capability."""
    from student_bot.config import PROJECT_ROOT

    static = PROJECT_ROOT / "src" / "student_bot" / "web" / "static"
    assert (static / "FrakturF2020.svg").is_file()


def test_a_filename_resolves_against_the_static_prefix(cfg):
    cfg.web.branding_logo = "FrakturF2020.svg"
    cfg.web.branding_logo_alt = "Fysiksektionen"
    html = _branding_logo_html(cfg, "/static")
    assert 'src="/static/FrakturF2020.svg"' in html
    assert 'alt="Fysiksektionen"' in html
    assert "logo-branding" in html


def test_a_base_path_deployment_gets_the_prefix(cfg):
    """Served under /betabot, the logo must not 404 at the site root."""
    cfg.web.branding_logo = "logo.svg"
    assert 'src="/betabot/static/logo.svg"' in _branding_logo_html(cfg, "/betabot/static")


def test_an_absolute_url_is_left_alone(cfg):
    cfg.web.branding_logo = "https://example.org/logo.svg"
    assert 'src="https://example.org/logo.svg"' in _branding_logo_html(cfg, "/static")


def test_a_leading_slash_does_not_double_up(cfg):
    cfg.web.branding_logo = "/logo.svg"
    assert 'src="/static/logo.svg"' in _branding_logo_html(cfg, "/static")


def test_whitespace_only_counts_as_unset(cfg):
    cfg.web.branding_logo = "   "
    assert _branding_logo_html(cfg, "/static") == ""


def test_quotes_are_escaped(cfg):
    """config.yaml is not user input, but this markup goes into every page —
    an unescaped quote would break all of them at once."""
    cfg.web.branding_logo = 'x.svg" onerror="alert(1)'
    cfg.web.branding_logo_alt = 'a "quoted" name'
    html = _branding_logo_html(cfg, "/static")
    assert 'onerror="alert(1)"' not in html
    assert "&quot;" in html


def test_health_reports_it_so_the_static_chat_page_can_inject_it(cfg):
    """index.html is a static file and cannot interpolate config."""
    from fastapi.testclient import TestClient

    from student_bot.web.app import create_app

    cfg.web.branding_logo = "FrakturF2020.svg"
    cfg.web.branding_logo_alt = "Fysiksektionen"
    client = TestClient(create_app(cfg))
    body = client.get("/api/health").json()
    assert "logo-branding" in body["branding_logo_html"]


def test_health_reports_empty_when_unset(cfg):
    from fastapi.testclient import TestClient

    from student_bot.web.app import create_app

    client = TestClient(create_app(cfg))
    assert client.get("/api/health").json()["branding_logo_html"] == ""
