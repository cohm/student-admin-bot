"""Per-user activity on /stats and /api/admin/recent across login methods.

A `data/web_users` account logs as `basic:<user>` via HTTP Basic and as
`kth:<user>` via KTH SSO. Before this was handled, the admin views only
looked for `basic:`, so every question asked over SSO went missing there.
The per-user table is admin-only.
"""

from __future__ import annotations

import base64
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from student_bot.config import get_config
from student_bot.logging_db import LogDB
from student_bot.web.app import _web_user_id, create_app
from student_bot.web.auth import hash_password, require_access


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "tok")
    c = get_config().model_copy(deep=True)
    c.paths.logs_db = tmp_path / "logs.sqlite"
    c.web.auth_enabled = True
    c.web.users_file = str(tmp_path / "web_users")
    pw = hash_password("pw")
    (tmp_path / "web_users").write_text(f"alice:{pw}:admin\nbob:{pw}\n")
    return c


def _sso_user(cfg, username: str) -> str:
    """ctx.user for a KTH SSO session, derived by the real require_access so
    this test follows the SSO id format if it ever changes again."""
    request = SimpleNamespace(session={"kth_user": {"username": username, "kthid": "u1000000"}})
    return require_access(request, cfg).user


def _log(db: LogDB, auth_user: str, question: str) -> None:
    """Write a qa_log row the way /api/chat does for this authenticated user."""
    db.record_qa(
        user_id=_web_user_id(SimpleNamespace(session_id="s", name=""), auth_user),
        channel_type="W",
        channel_id="s",
        bot_post_id=None,
        root_id=None,
        question=question,
        lang="sv",
        retrieved_chunk_ids=[],
        rerank_top1=0.9,
        rerank_meanK=0.5,
        distinct_sources=1,
        gate_pass=True,
        gate_reason="ok",
        answer="svar",
        latency_ms=1,
    )


@pytest.fixture
def app(cfg):
    """alice (admin) active before and after SSO, bob (not admin) only after."""
    db = LogDB(cfg)
    _log(db, "alice", "före SSO")  # HTTP Basic: ctx.user is the bare username
    _log(db, _sso_user(cfg, "alice"), "efter SSO")
    _log(db, _sso_user(cfg, "bob"), "bara SSO")
    return create_app(cfg)


def _client(app, user: str) -> TestClient:
    client = TestClient(app)
    client.get("/api/health?access=tok")
    client.headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()
    return client


@pytest.fixture
def admin(app):
    return _client(app, "alice")


@pytest.fixture
def student(app):
    return _client(app, "bob")


def test_admin_recent_finds_turns_from_both_login_methods(admin):
    alice = admin.get("/api/admin/recent?user=alice").json()["rows"]
    bob = admin.get("/api/admin/recent?user=bob").json()["rows"]

    assert [r["question"] for r in alice] == ["efter SSO", "före SSO"]
    assert [r["question"] for r in bob] == ["bara SSO"]


def test_stats_counts_both_login_methods_as_one_user(admin):
    html = admin.get("/stats").text

    def count(user):
        m = re.search(rf'<td class="text">{user}</td><td class="num">(\d+)</td>', html)
        return int(m.group(1)) if m else None

    assert count("alice") == 2
    assert count("bob") == 1


def test_user_table_is_admin_only(admin, student):
    """The table names who uses the bot and how much; other students must not
    see it."""
    assert 'class="stats-users"' in admin.get("/stats").text

    resp = student.get("/stats")
    assert resp.status_code == 200
    assert 'class="stats-users"' not in resp.text
    assert '<td class="text">alice</td>' not in resp.text
    assert "data-user=" not in resp.text
