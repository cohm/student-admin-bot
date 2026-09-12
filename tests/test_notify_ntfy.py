"""ntfy wiring in scripts/notify.py.

ntfy is the channel that actually reaches a phone at 04:00, so the parts worth
pinning are the ones whose failure is silent: a severity that maps to the wrong
priority still delivers — just not through a silenced phone — and a topic that
lands in the wrong place returns a cheerful 200 from somebody else's topic.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from scripts.notify import SEVERITIES, ntfy_request, severity_at_least
from student_bot.config import NotifyConfig


class _Cfg:
    """Just enough Config for ntfy_request()."""

    def __init__(self, *, server="https://ntfy.sh", topic="s3cret-topic", token=None):
        self.notify = NotifyConfig(ntfy_server=server)
        self.ntfy_topic = SecretStr(topic) if topic else None
        self.ntfy_token = SecretStr(token) if token else None


def test_topic_is_appended_to_the_server():
    url, _, _ = ntfy_request(_Cfg(), "hi", "ok")
    assert url == "https://ntfy.sh/s3cret-topic"


def test_a_trailing_slash_on_the_server_does_not_double_up():
    url, _, _ = ntfy_request(_Cfg(server="https://push.example.org/"), "hi", "ok")
    assert url == "https://push.example.org/s3cret-topic"


def test_self_hosted_server_is_honoured():
    url, _, _ = ntfy_request(_Cfg(server="https://ntfy.internal.kth.se"), "hi", "ok")
    assert url.startswith("https://ntfy.internal.kth.se/")


def test_missing_topic_names_the_env_var():
    with pytest.raises(SystemExit) as e:
        ntfy_request(_Cfg(topic=None), "hi", "ok")
    assert "NTFY_TOPIC" in str(e.value)


def test_body_is_utf8_so_swedish_questions_survive():
    _, _, body = ntfy_request(_Cfg(), "Vem är programansvarig för Teknisk fysik?", "critical")
    assert body.decode("utf-8").endswith("Teknisk fysik?")


def test_markdown_is_requested_so_the_report_table_renders():
    _, headers, _ = ntfy_request(_Cfg(), "| a | b |", "ok")
    assert headers["Markdown"] == "yes"


def test_critical_uses_max_priority():
    """Priority 5 is what rings through a silenced phone. This is the entire
    reason ntfy is here rather than only a Mattermost DM."""
    _, headers, _ = ntfy_request(_Cfg(), "recall fell", "critical")
    assert headers["Priority"] == "5"


def test_green_is_quieter_than_default():
    """A weekly all-green push that buzzes is one people learn to swipe away,
    and then the real alert goes with it."""
    _, headers, _ = ntfy_request(_Cfg(), "all good", "ok")
    assert int(headers["Priority"]) < 3


def test_priority_increases_with_severity():
    priorities = [int(ntfy_request(_Cfg(), "m", s)[1]["Priority"]) for s in SEVERITIES]
    assert priorities == sorted(priorities)
    assert len(set(priorities)) == len(SEVERITIES)


def test_each_severity_has_its_own_title():
    titles = {ntfy_request(_Cfg(), "m", s)[1]["Title"] for s in SEVERITIES}
    assert len(titles) == len(SEVERITIES)


def test_an_unknown_severity_is_treated_as_critical():
    """Defensive: a typo must not silently downgrade an alert."""
    _, headers, _ = ntfy_request(_Cfg(), "m", "cirtical")
    assert headers["Priority"] == "5"


def test_token_becomes_a_bearer_header_only_when_set():
    _, plain, _ = ntfy_request(_Cfg(), "m", "ok")
    assert "Authorization" not in plain
    _, authed, _ = ntfy_request(_Cfg(token="tk_abc"), "m", "ok")
    assert authed["Authorization"] == "Bearer tk_abc"


def test_the_topic_is_redacted_in_a_config_repr():
    """The topic is a publish credential on a public server, and configs get
    logged."""
    cfg = _Cfg()
    assert "s3cret-topic" not in repr(cfg.ntfy_topic)


@pytest.mark.parametrize(
    "severity,floor,expected",
    [
        ("ok", "ok", True),
        ("warn", "ok", True),
        ("critical", "ok", True),
        ("ok", "warn", False),
        ("warn", "warn", True),
        ("critical", "warn", True),
        ("ok", "critical", False),
        ("warn", "critical", False),
        ("critical", "critical", True),
    ],
)
def test_severity_floor(severity, floor, expected):
    assert severity_at_least(severity, floor) is expected


def test_a_misconfigured_floor_lets_everything_through():
    """Fail open: a typo in min_severity must not silence the weekly job."""
    assert severity_at_least("ok", "warnn") is True
