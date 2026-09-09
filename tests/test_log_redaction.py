"""Tests for log-time secret redaction (#18).

`_mask_secret` is a small pure helper, exercised directly.
`_RedactAccessTokenAccessLog` is imported from the module and run against
uvicorn's real `AccessFormatter` — the filter has to survive that formatter,
not just produce a token-free string, so the formatter is part of the
contract under test.
"""

from __future__ import annotations

import logging

import pytest
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter

from student_bot.web.app import _mask_secret, _RedactAccessTokenAccessLog


def _access_record(full_path: str, status: int = 200) -> logging.LogRecord:
    """Build a record shaped exactly like uvicorn's access log emits.

    Mirrors `uvicorn.protocols.http.httptools_impl`: a `%`-style message with
    a five-element args tuple. The shape is the whole point — a filter that
    breaks it takes the log line down with it.
    """
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", full_path, "1.1", status),
        exc_info=None,
    )


@pytest.fixture
def access_filter() -> _RedactAccessTokenAccessLog:
    return _RedactAccessTokenAccessLog()


class TestMaskSecret:
    def test_long_secret_keeps_only_prefix(self):
        # Default keep=6; long secret → first 6 + ellipsis.
        assert _mask_secret("abcdef1234567890XYZ") == "abcdef…"

    def test_short_secret_does_not_partial_leak(self):
        # Anything ≤ keep is replaced wholesale — partial value would leak
        # too much when the secret itself is short.
        assert _mask_secret("abc123") == "<short>"
        assert _mask_secret("abc") == "<short>"

    def test_empty_secret_surfaces_unset(self):
        assert _mask_secret("") == "<unset>"
        assert _mask_secret(None) == "<unset>"  # type: ignore[arg-type]

    def test_custom_keep_length(self):
        assert _mask_secret("abcdef1234567890", keep=3) == "abc…"


class TestAccessTokenRedaction:
    """The token must not survive into the formatted access-log line."""

    def test_redacts_token_in_request_line(self, access_filter):
        record = _access_record("/?access=secrettoken123")
        access_filter.filter(record)
        assert record.args[2] == "/?access=<redacted>"

    def test_redacts_token_when_followed_by_other_params(self, access_filter):
        record = _access_record("/?access=secrettoken123&debug=1")
        access_filter.filter(record)
        # Token stops at `&`; the rest of the query string is preserved.
        assert record.args[2] == "/?access=<redacted>&debug=1"

    def test_redacts_token_on_a_subpath(self, access_filter):
        record = _access_record("/betabot/?access=ABCdef123_XYZ")
        access_filter.filter(record)
        assert "ABCdef123_XYZ" not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_does_not_touch_unrelated_requests(self, access_filter):
        record = _access_record("/api/health")
        access_filter.filter(record)
        assert record.args == ("127.0.0.1:54321", "GET", "/api/health", "1.1", 200)

    def test_does_not_touch_path_containing_the_word_access(self, access_filter):
        # The regex requires `access=` literally, so a bare path segment
        # named "access" is left alone.
        record = _access_record("/auth/kth/access")
        access_filter.filter(record)
        assert record.args[2] == "/auth/kth/access"


class TestSurvivesUvicornFormatter:
    """Regression: the filter used to set `record.args = ()`, which made
    `AccessFormatter.formatMessage` raise `ValueError: not enough values to
    unpack (expected 5, got 0)`. The access-log line was then dropped for
    exactly the requests that claim a session — the ones most worth logging.
    """

    @pytest.fixture
    def formatter(self) -> AccessFormatter:
        # Build from uvicorn's own configured format string so the test
        # tracks production rather than a hand-copied fmt.
        return AccessFormatter(fmt=LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False)

    def test_redacted_record_still_formats(self, access_filter, formatter):
        record = _access_record("/?access=secrettoken123")
        access_filter.filter(record)
        formatted = formatter.format(record)
        assert "secrettoken123" not in formatted
        assert "<redacted>" in formatted
        # The rest of the line survives intact.
        assert "127.0.0.1:54321" in formatted
        assert "GET" in formatted

    def test_untouched_record_still_formats(self, access_filter, formatter):
        record = _access_record("/api/health")
        access_filter.filter(record)
        assert "/api/health" in formatter.format(record)

    def test_args_keep_the_five_element_shape(self, access_filter):
        record = _access_record("/?access=secrettoken123")
        access_filter.filter(record)
        assert isinstance(record.args, tuple)
        assert len(record.args) == 5

    def test_sso_callback_url_is_unaffected(self, access_filter, formatter):
        # SSO adds a second family of auth URLs to the access log. Its code
        # and state params are single-use and already spent by log time, so
        # they are deliberately not redacted — but they must not break the
        # formatter either.
        record = _access_record("/auth/kth/callback?code=abc&state=xyz", status=303)
        access_filter.filter(record)
        assert "/auth/kth/callback" in formatter.format(record)


class TestNonUvicornRecords:
    """Records that aren't uvicorn-shaped fall back to collapsing msg+args."""

    def test_preformatted_message_is_redacted(self, access_filter):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='127.0.0.1 - "GET /?access=secretXYZ HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        access_filter.filter(record)
        assert "secretXYZ" not in record.getMessage()
        assert "<redacted>" in record.getMessage()

    def test_unexpected_arity_is_redacted_via_fallback(self, access_filter):
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s" %s',
            args=("127.0.0.1", "GET /?access=secretXYZ HTTP/1.1", 200),
            exc_info=None,
        )
        access_filter.filter(record)
        assert "secretXYZ" not in record.getMessage()
        # Collapsed, so a later format pass can't reintroduce the token.
        assert record.args == ()
