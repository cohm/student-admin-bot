"""Both topic classifiers get the joined question after a clarification.

A reply such as "HT2024" used to be classified on its own, by the chat model
and by the System One shadow classifier, and landed as "other". The pipeline
joins the reply with the question it answers; that joined text is what the
model answered and what both classifiers now get.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import student_bot.bot.mattermost_client as mm
import student_bot.bot.topics as topics
import student_bot.web.app as web
from student_bot.bot.gate import GateDecision
from student_bot.bot.mattermost_client import StudentBot, _Job
from student_bot.bot.memory import ConversationMemory
from student_bot.bot.pipeline import AnswerResult
from student_bot.bot.retrieval import RetrievalResult
from student_bot.config import get_config
from student_bot.logging_db import LogDB

JOINED = "Vilka spärrkurser finns i årskurs 2?\n\nHT2024"
CASES = [("HT2024", JOINED, JOINED), ("Hej", None, "Hej")]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "tok")
    c = get_config().model_copy(deep=True)
    c.paths.logs_db = tmp_path / "logs.sqlite"
    c.user_id_hash_salt = "salt"
    c.topics.enabled = True
    c.mattermost.use_attachments = False
    return c


@pytest.fixture
def seen(monkeypatch):
    """What each classifier was asked to classify."""
    calls = SimpleNamespace(gemma=[], shadow=[])
    monkeypatch.setattr(
        topics, "classify", lambda _c, text, _l: calls.gemma.append(text) or ("other", 1.0)
    )
    monkeypatch.setattr(web, "classify", topics.classify)

    def shadow(_cfg, _db, qa_id, text, lang, *, refused):
        calls.shadow.append((qa_id, text, lang, refused))

    monkeypatch.setattr(web, "submit_shadow_topic", shadow)
    monkeypatch.setattr(mm, "submit_shadow_topic", shadow)
    return calls


def fake_answer(merged, **flags):
    def answer(q, **_kw):
        return AnswerResult(
            question=q,
            lang="sv",
            answered=True,
            answer="Svar",
            rendered="Svar",
            gate=GateDecision(True, "ok", 0.0, 0.0, 0),
            retrieval=RetrievalResult(query=q),
            latency_ms=0,
            merged_question=merged,
            **flags,
        )

    return answer


def web_chat(cfg, question):
    client = TestClient(web.create_app(cfg))
    client.get("/api/health?access=tok")
    return client.post("/api/chat", json={"question": question, "name": "Test"})


def mm_chat(cfg, monkeypatch, question):
    bot = StudentBot.__new__(StudentBot)
    bot.cfg, bot.db, bot.memory = cfg, LogDB(cfg), ConversationMemory(cfg)
    for name in ("_post", "_react", "_unreact"):
        monkeypatch.setattr(bot, name, lambda *_a, **_kw: "post1")
    monkeypatch.setattr(bot, "_handle_logging_command", lambda *_a: False)
    monkeypatch.setattr(bot, "_handle_jargon_command", lambda *_a: False)
    bot._handle_job(_Job("u1", "c1", "O", "r1", "p1", question))


@pytest.mark.parametrize("reply, merged, expected", CASES)
def test_the_web_app_gives_both_classifiers_the_joined_question(
    cfg, seen, monkeypatch, reply, merged, expected
):
    monkeypatch.setattr(web, "answer", fake_answer(merged))

    r = web_chat(cfg, reply)

    assert r.status_code == 200 and "meta" in r.text
    assert seen.gemma == [expected]
    ((qa_id, text, lang, refused),) = seen.shadow
    assert isinstance(qa_id, int) and (text, lang, refused) == (expected, "sv", None)


@pytest.mark.parametrize("reply, merged, expected", CASES)
def test_the_mattermost_bot_gives_both_classifiers_the_joined_question(
    cfg, seen, monkeypatch, reply, merged, expected
):
    monkeypatch.setattr(mm, "answer", fake_answer(merged))

    mm_chat(cfg, monkeypatch, reply)

    assert seen.gemma == [expected]
    ((qa_id, text, lang, refused),) = seen.shadow
    assert isinstance(qa_id, int) and (text, lang, refused) == (expected, "sv", None)


@pytest.mark.parametrize("flag", ["too_long", "rate_limited"])
def test_a_refused_turn_passes_its_reason_to_the_shadow_call(cfg, seen, monkeypatch, flag):
    monkeypatch.setattr(web, "answer", fake_answer(None, **{flag: True}))

    web_chat(cfg, "x" * 50)

    assert seen.shadow[0][3] == flag
