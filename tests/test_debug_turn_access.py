"""/api/debug/{qa_id}: one turn's question, answer and diagnostics.

The /stats inspector deep-links an admin to another user's turn. That needs
the question and answer, and most turns have no diagnostics at all (they are
only stored with "Show how the bot thinks" on), so a missing qa_debug row
must still return the text. Everyone else may only read their own turns.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from student_bot.config import get_config
from student_bot.logging_db import LogDB
from student_bot.web.app import _web_user_id, create_app
from student_bot.web.auth import hash_password


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


def _log(db: LogDB, user: str, question: str, answer: str, *, debug: bool) -> int:
    """Write a turn the way /api/chat does for an HTTP Basic user."""
    uid = _web_user_id(SimpleNamespace(session_id="s", name=""), user)
    qa_id = db.record_qa(
        user_id=uid,
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
        answer=answer,
        latency_ms=1,
    )
    if debug:
        db.record_qa_debug(qa_id=qa_id, user_id=uid, payload={"routing": {"lang": "sv"}})
    return qa_id


@pytest.fixture
def turns(cfg):
    db = LogDB(cfg)
    return {
        "bob_plain": _log(
            db, "bob", "Hur ansöker jag?", "Du ansöker via **antagning.se**.", debug=False
        ),
        "bob_debug": _log(db, "bob", "Vem är PA?", "PA för CTFYS är ...", debug=True),
        "alice": _log(db, "alice", "Admins egen fråga", "svar", debug=False),
    }


def _client(cfg, user: str) -> TestClient:
    client = TestClient(create_app(cfg))
    client.get("/api/health?access=tok")
    client.headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:pw".encode()).decode()
    return client


def test_an_admin_reads_another_users_question_and_answer_without_diagnostics(cfg, turns):
    resp = _client(cfg, "alice").get(f"/api/debug/{turns['bob_plain']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"] == "Hur ansöker jag?"
    assert body["answer"] == "Du ansöker via **antagning.se**."
    assert body["payload"] is None


def test_an_admin_gets_the_diagnostics_too_when_they_exist(cfg, turns):
    body = _client(cfg, "alice").get(f"/api/debug/{turns['bob_debug']}").json()
    assert body["payload"] == {"routing": {"lang": "sv"}}
    assert body["question"] == "Vem är PA?"


def test_a_non_admin_cannot_read_someone_elses_turn(cfg, turns):
    assert _client(cfg, "bob").get(f"/api/debug/{turns['alice']}").status_code == 403


def test_a_user_reads_their_own_turn_without_diagnostics(cfg, turns):
    """Was a 404 before; the chat page still shows it as "no diagnostics"."""
    body = _client(cfg, "bob").get(f"/api/debug/{turns['bob_plain']}").json()
    assert body["payload"] is None
    assert body["answer"] == "Du ansöker via **antagning.se**."


def test_a_turn_that_does_not_exist_is_404(cfg, turns):
    assert _client(cfg, "alice").get("/api/debug/999999").status_code == 404
