"""System One topic classifier in ghost mode (bot/topic_shadow.py)."""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from student_bot.bot import topic_shadow
from student_bot.bot.topic_shadow import ShadowTopic, classify, submit_shadow_topic
from student_bot.bot.topics import load_topics
from student_bot.config import TopicShadowConfig, get_config
from student_bot.logging_db import LogDB


def answer(**fields) -> dict:
    """A System One response with one `topic` answer."""
    topic = {"choice": "examination_grading", **fields}
    return {"model": "nimble", "answers": {"topic": topic}}


ANSWER = answer(probabilities={"examination_grading": 0.9, "other": 0.1}, confidence=0.8)
TURN = dict(
    channel_type="D", channel_id="c", bot_post_id="b", root_id="r", lang="sv",
    retrieved_chunk_ids=[], rerank_top1=0.0, rerank_meanK=0.0, distinct_sources=0,
    gate_pass=True, gate_reason="pass", answer="Svar.", latency_ms=1,
)  # fmt: skip


@pytest.fixture
def cfg(tmp_path):
    c = get_config().model_copy(deep=True)
    c.paths.logs_db = tmp_path / "logs.sqlite"
    c.user_id_hash_salt = "salt"
    c.topic_shadow.enabled = True
    c.llm.providers["litellm"].base_url = "http://gateway:4000/v1"
    c.llm_api_keys["litellm"] = SecretStr("secret-key")
    return c


@pytest.fixture(autouse=True)
def fresh_slots_and_no_network(monkeypatch):
    monkeypatch.setattr(topic_shadow, "_slots", None)
    monkeypatch.setattr(topic_shadow.httpx, "post", lambda *a, **k: pytest.fail("network"))


@pytest.fixture
def fake_post(monkeypatch):
    """The HTTP call, replaced. `calls` records requests; `status`, `payload`,
    `text` and `raise_exc` shape the response."""
    state = SimpleNamespace(status=200, payload=ANSWER, text=None, raise_exc=None, calls=[])

    def post(url, headers, body, timeout):
        state.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        if state.raise_exc is not None:
            raise state.raise_exc
        if state.text is not None:
            return httpx.Response(state.status, text=state.text)
        return httpx.Response(state.status, json=state.payload)

    monkeypatch.setattr(topic_shadow, "_post", post)
    return state


@pytest.fixture
def db(cfg):
    return LogDB(cfg)


def turn(db: LogDB) -> int:
    qa_id = db.record_qa(user_id="u1", question="När är omtentan?", **TURN)
    assert qa_id is not None
    return qa_id


def shadow_row(cfg, qa_id):
    with sqlite3.connect(str(cfg.paths.logs_db)) as conn:
        return conn.execute(
            "SELECT shadow_topic, shadow_topic_confidence, shadow_topic_probs, shadow_topic_ms, "
            "shadow_topic_model, shadow_topic_error FROM qa_log WHERE id = ?",
            (qa_id,),
        ).fetchone()


# --- request and response ---


def test_the_request_follows_the_system_one_contract(cfg, fake_post):
    d = classify(cfg, "När är omtentan?", "sv")

    assert d == ShadowTopic(
        "examination_grading", 0.8, {"examination_grading": 0.9, "other": 0.1}, d.ms, "nimble"
    )
    (call,) = fake_post.calls
    assert call["url"] == "http://gateway:4000/bespoke/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["timeout"] == cfg.topic_shadow.timeout_seconds
    assert call["body"]["model"] == "nimble"
    assert call["body"]["state"] == {"message": "När är omtentan?"}
    q = call["body"]["questions"]["topic"]
    assert q["type"] == "choice"
    assert q["instructions"] == "Vilket ämne passar studentens fråga bäst?"
    assert set(q["criteria"]) == {t.id for t in load_topics(cfg)}
    assert q["criteria"]["thesis"].startswith("Examensarbete. ")

    classify(cfg, "When is the re-exam?", "en")
    q = fake_post.calls[1]["body"]["questions"]["topic"]
    assert q["instructions"] == "Which topic best matches the student's question?"
    assert q["criteria"]["thesis"].startswith("Thesis. ")


def test_berget_is_reached_under_its_own_path(cfg, fake_post):
    cfg.topic_shadow.provider, cfg.topic_shadow.path = "berget", "/v1/systemone"
    cfg.topic_shadow.model = "bev-latest"
    cfg.llm_api_keys["berget"] = SecretStr("berget-key")

    classify(cfg, "x", "sv")

    (call,) = fake_post.calls
    assert call["url"] == "https://api.berget.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer berget-key"
    assert call["body"]["model"] == "bev-latest"


@pytest.mark.parametrize(
    "field, value, reason",
    [
        ("status", 503, "http_503"),
        ("raise_exc", httpx.ReadTimeout("slow"), "timeout"),
        ("raise_exc", httpx.ConnectError("down"), "ConnectError"),
        ("raise_exc", RuntimeError("e.g. InvalidURL"), "RuntimeError"),
        ("text", "<html>502 Bad Gateway SECRET</html>", "bad_json"),
        ("payload", {"error": "bad key SECRET"}, "bad_response"),
        ("payload", [], "bad_response"),
        ("payload", {"answers": {"topic": ["thesis"]}}, "bad_response"),
        ("payload", answer(choice=["thesis"]), "bad_response"),
        ("payload", answer(choice="banana"), "unknown_label"),
        ("payload", answer(confidence="high SECRET"), "bad_numbers"),
        ("payload", answer(confidence="nan"), "bad_numbers"),
        ("payload", answer(confidence=7), "bad_numbers"),
        ("payload", answer(confidence=int("9" * 400)), "bad_numbers"),
        ("payload", answer(probabilities={"thesis": "inf"}), "bad_numbers"),
        ("payload", answer(probabilities={"thesis": None}), "bad_numbers"),
    ],
)
def test_failures_become_a_short_error_and_no_topic(cfg, fake_post, caplog, field, value, reason):
    setattr(fake_post, field, value)

    d = classify(cfg, "x", "sv")

    assert (d.topic, d.probs, d.confidence, d.error, d.model) == (
        None,
        None,
        None,
        reason,
        "nimble",
    )
    assert reason in caplog.text and "SECRET" not in caplog.text


@pytest.mark.parametrize(
    "payload, expected",
    [
        # only known topic ids are kept
        (
            answer(probabilities={"examination_grading": 0.9, "banana": 0.1, "other": 0.0}),
            {"probs": {"examination_grading": 0.9, "other": 0.0}},
        ),
        (answer(probabilities={"banana": 0.5}), {"probs": None}),
        (answer(), {"probs": None, "confidence": None}),
        (answer(confidence=1.0000001), {"error": None}),  # float rounding, not an error
        (
            {**ANSWER, "model": "bespokelabs/Bespoke-Nimble-9B-v2"},
            {"model": "bespokelabs/Bespoke-Nimble-9B-v2"},
        ),
        ({k: v for k, v in ANSWER.items() if k != "model"}, {"model": "nimble"}),
    ],
)
def test_the_answer_is_parsed_leniently(cfg, fake_post, payload, expected):
    fake_post.payload = payload

    d = classify(cfg, "x", "sv")

    assert d.topic == "examination_grading" and d.error is None
    assert {k: getattr(d, k) for k in expected} == expected


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda c: c.llm_api_keys.pop("litellm"), "no_api_key"),
        (lambda c: setattr(c.topic_shadow, "provider", "nope"), "no_endpoint"),
        (lambda c: setattr(c.topics, "file", "/nonexistent/topics.yaml"), "no_topics"),
    ],
)
def test_a_misconfiguration_means_no_call(cfg, fake_post, change, reason):
    change(cfg)

    assert classify(cfg, "x", "sv").error == reason
    assert fake_post.calls == []


@pytest.mark.parametrize(
    "kwargs", [{"timeout_seconds": 0}, {"max_pending": 0}, {"path": "bespoke/v1/systemone"}]
)
def test_config_rejects_bad_values(kwargs):
    with pytest.raises(ValidationError):
        TopicShadowConfig(**kwargs)


def test_the_env_var_switches_the_feature_on(monkeypatch):
    get_config.cache_clear()
    monkeypatch.setenv("TOPIC_SHADOW_ENABLED", "true")
    try:
        assert get_config().topic_shadow.enabled is True
    finally:
        get_config.cache_clear()


# --- storage ---


def test_the_verdict_or_the_failure_is_stored_on_the_turn(cfg, db):
    ok, failed = turn(db), turn(db)

    topic_shadow.record(db, ok, ShadowTopic("thesis", 0.7, {"thesis": 0.95}, 412, "nimble"))
    topic_shadow.record(db, failed, ShadowTopic(None, None, None, 10000, "nimble", "timeout"))

    topic, conf, probs, ms, model, error = shadow_row(cfg, ok)
    assert (topic, conf, ms, model, error) == ("thesis", 0.7, 412, "nimble", None)
    assert json.loads(probs) == {"thesis": 0.95}
    assert shadow_row(cfg, failed) == (None, None, None, 10000, "nimble", "timeout")


def test_the_columns_come_back_after_being_dropped(cfg, db):
    with sqlite3.connect(str(cfg.paths.logs_db)) as conn:
        for col in ("shadow_topic", "shadow_topic_error"):
            conn.execute(f"ALTER TABLE qa_log DROP COLUMN {col}")

    db = LogDB(cfg)

    assert shadow_row(cfg, turn(db)) == (None, None, None, None, None, None)


# --- background submission ---


def test_submit_classifies_in_the_background_and_stores_the_row(cfg, db, fake_post):
    # A clarification reply joined with its question can exceed the input
    # cap; only the pipeline's own verdict (`refused`) skips the call.
    qa_id = turn(db)
    question = "När är omtentan? " * 100

    thread = submit_shadow_topic(cfg, db, qa_id, question, "sv")

    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert shadow_row(cfg, qa_id)[:2] == ("examination_grading", 0.8)
    assert fake_post.calls[0]["body"]["state"] == {"message": question}


def test_submit_skips_unlogged_refused_and_disabled_turns(cfg, db, fake_post):
    qa_id = turn(db)

    assert submit_shadow_topic(cfg, db, None, "x", "sv") is None
    assert submit_shadow_topic(cfg, db, qa_id, "x", "sv", refused="too_long") is None
    assert shadow_row(cfg, qa_id) == (None, None, None, 0, None, "too_long")
    cfg.topic_shadow.enabled = False
    assert submit_shadow_topic(cfg, db, qa_id, "x", "sv") is None
    assert fake_post.calls == []


def test_a_skip_record_that_cannot_be_written_does_not_raise(cfg, fake_post, caplog):
    broken = SimpleNamespace(update_shadow_topic=lambda *a, **k: 1 / 0)

    assert submit_shadow_topic(cfg, broken, 1, "x", "sv", refused="too_long") is None
    assert "could not record too_long" in caplog.text


def test_too_many_pending_calls_are_skipped_not_queued(cfg, db, monkeypatch, caplog):
    cfg.topic_shadow.max_pending = 1
    release = threading.Event()
    monkeypatch.setattr(
        topic_shadow, "_post", lambda *a: release.wait(5) and httpx.Response(200, json=ANSWER)
    )
    first, second = turn(db), turn(db)

    t1 = submit_shadow_topic(cfg, db, first, "x", "sv")
    t2 = submit_shadow_topic(cfg, db, second, "y", "sv")
    release.set()
    t1.join(timeout=5)

    assert t2 is None and f"skipping qa {second}" in caplog.text
    assert shadow_row(cfg, first)[0] == "examination_grading"
    assert shadow_row(cfg, second) == (None, None, None, 0, None, "busy")
    # The slot is free again once the first call finished.
    t3 = submit_shadow_topic(cfg, db, second, "y", "sv")
    assert t3 is not None
    t3.join(timeout=5)


def test_a_store_failure_or_a_thread_that_cannot_start_frees_the_slot(
    cfg, db, fake_post, monkeypatch, caplog
):
    cfg.topic_shadow.max_pending = 1
    qa_id = turn(db)
    broken = SimpleNamespace(update_shadow_topic=lambda *a, **k: 1 / 0)

    submit_shadow_topic(cfg, broken, qa_id, "x", "sv").join(timeout=5)
    assert "failed for qa" in caplog.text

    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda self: 1 / 0)
    assert submit_shadow_topic(cfg, db, qa_id, "x", "sv") is None
    assert "could not start thread" in caplog.text
    assert shadow_row(cfg, qa_id)[5] == "no_thread"

    monkeypatch.setattr(threading.Thread, "start", real_start)
    t = submit_shadow_topic(cfg, db, qa_id, "x", "sv")
    assert t is not None
    t.join(timeout=5)
    assert shadow_row(cfg, qa_id)[0] == "examination_grading"
