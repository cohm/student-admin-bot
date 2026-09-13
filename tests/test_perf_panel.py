"""The resource panel after #74.

Two things were wrong with it: it reported a Host column that nothing had
written since the app moved off the Mac mini, and it was on for everyone
whether or not they wanted it.
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from student_bot.config import PROJECT_ROOT, get_config
from student_bot.web.app import _active_model_label, create_app


WEB = PROJECT_ROOT / "src" / "student_bot" / "web"
STATIC = WEB / "static"


@pytest.fixture
def cfg():
    c = get_config().model_copy(deep=True)
    c.web.performance_panel_enabled = True
    return c


def test_system_load_reports_one_machine(cfg):
    """The host/container split is gone: there is only one machine serving
    this, and the other numbers were always blank."""
    body = TestClient(create_app(cfg)).get("/api/system-load").json()
    assert "server_load" in body
    assert "host_system_load" not in body
    assert "cpu_pct" in body["server_load"]


def test_system_load_says_nothing_when_the_deployment_disabled_it(cfg):
    cfg.web.performance_panel_enabled = False
    body = TestClient(create_app(cfg)).get("/api/system-load").json()
    assert body == {"performance_panel_enabled": False}


def test_the_host_metrics_side_channel_is_gone():
    """Collector, entry point, config file and reader all removed together —
    a leftover reference would be a command that no longer exists."""
    assert not (PROJECT_ROOT / "scripts" / "host_metrics_collector.py").exists()
    for path in (
        WEB / "app.py",
        PROJECT_ROOT / "src" / "student_bot" / "bot" / "mattermost_client.py",
        PROJECT_ROOT / "scripts" / "up.py",
        PROJECT_ROOT / "scripts" / "down.py",
        PROJECT_ROOT / "pyproject.toml",
    ):
        text = path.read_text(encoding="utf-8")
        assert "host_metrics" not in text, path
        assert "host-metrics" not in text, path


def test_the_model_label_names_the_model_but_not_the_endpoint(cfg):
    """The base URL is an internal tailnet address; it has no business in a
    page served to students."""
    label = _active_model_label(cfg)
    assert label
    base_url = cfg.active_model().base_url
    if base_url:
        assert base_url not in label
        assert "//" not in label


def test_the_panel_is_hidden_in_the_markup():
    """Default off (#74) — the deployment switch only decides whether the
    reader is *offered* it."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="perf-panel" class="perf-panel hidden"' in html
    assert 'id="perf-toggle-wrap" data-i18n-title' in html or "perf-toggle-wrap" in html


def test_the_panel_sits_outside_the_composer():
    """It used to live inside the composer row, where it took space on every
    visit. Below the form it costs nothing until it is switched on."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    form_end = html.index("</form>")
    assert html.index('id="perf-panel"') > form_end


def test_the_reader_preference_is_remembered_and_fails_soft():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "perfPanelShown" in js
    # Private browsing throws on localStorage; a resource panel must not take
    # the page down with it.
    read_fn = js[js.index("function readPerfPref") : js.index("function writePerfPref")]
    assert "catch" in read_fn


@pytest.mark.parametrize(
    "key",
    [
        "perf.server",
        "perf.model",
        "perf.toggle.label",
        "perf.tip.server",
        "perf.tip.model",
        "perf.stage.search",
        "perf.stage.rank",
        "perf.stage.total",
    ],
)
def test_new_strings_exist_in_both_languages(key):
    js = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert js.count(f'"{key}"') == 2, f"{key} must be in both sv and en"


@pytest.mark.parametrize(
    "key", ["perf.tokens", "perf.system", "perf.tip.tokens", "perf.tip.system"]
)
def test_retired_strings_are_gone(key):
    js = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert f'"{key}"' not in js
