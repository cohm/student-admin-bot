"""Ghost-mode section of student-bot-stats (LogDB.shadow_topic_stats)."""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from scripts import stats
from student_bot.config import get_config
from student_bot.logging_db import LogDB

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
    return c


@pytest.fixture
def db(cfg):
    return LogDB(cfg)


def row(db: LogDB, cfg, **cols) -> int:
    """One logged turn with the given qa_log columns set."""
    qa_id = db.record_qa(user_id="u1", question="Fråga?", **TURN)
    assert qa_id is not None
    if cols:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with sqlite3.connect(cfg.paths.logs_db) as conn:
            conn.execute(f"UPDATE qa_log SET {sets} WHERE id = ?", (*cols.values(), qa_id))
    return qa_id


def shadow(topic, conf, ms=300, **extra) -> dict:
    return dict(shadow_topic=topic, shadow_topic_confidence=conf, shadow_topic_ms=ms, **extra)


def test_none_without_shadow_rows(db, cfg):
    row(db, cfg, topic="other", topic_confidence=1.0)
    assert db.shadow_topic_stats() is None


def test_counts_agreement_bands_and_latency_split(db, cfg):
    # Before ghost mode: two answered turns.
    row(db, cfg, topic="other", topic_confidence=1.0, ttft_ms=2000, llm_ms=7000)
    row(db, cfg, topic="other", topic_confidence=1.0, ttft_ms=3000, llm_ms=9000)
    first = row(
        db, cfg, topic="curriculum_courses", topic_confidence=1.0, ttft_ms=1000, llm_ms=5000,
        **shadow("curriculum_courses", 0.95, ms=2800),
    )  # fmt: skip
    row(db, cfg, topic="curriculum_courses", topic_confidence=1.0, **shadow("other", 0.63))
    row(db, cfg, topic="registration_admission", topic_confidence=1.0, **shadow("other", 0.53))
    row(
        db,
        cfg,
        topic="examination_grading",
        topic_confidence=1.0,
        **shadow("examination_grading", 0.75),
    )
    # The chat model's call failed: classified, but not comparable.
    row(db, cfg, topic="other", topic_confidence=0.0, **shadow("curriculum_courses", 0.9))
    row(
        db,
        cfg,
        topic="other",
        topic_confidence=1.0,
        shadow_topic_error="timeout",
        shadow_topic_ms=10000,
    )
    row(db, cfg, topic="other", topic_confidence=1.0, shadow_topic_error="busy", shadow_topic_ms=0)
    # Ghost mode switched off again, or the call never finished.
    row(db, cfg, topic="other", topic_confidence=1.0)

    s = db.shadow_topic_stats()
    assert s["first_id"] == first
    assert (s["attempted"], s["classified"], s["unwritten"]) == (7, 5, 1)
    assert s["errors"] == {"busy": 1, "timeout": 1}
    assert (s["agree"], s["comparable"]) == (2, 4)
    # Failed calls carry no result, so their ms stay out of the call times.
    assert (s["ms_median"], s["ms_max"]) == (300, 2800)
    bands = {b["band"]: (b["agree"], b["n"]) for b in s["bands"]}
    assert bands == {"≥ 0.9": (1, 1), "0.7–0.9": (1, 1), "0.5–0.7": (0, 2), "< 0.5": (0, 0)}
    assert s["disagreements"] == [
        {"topic": "curriculum_courses", "shadow_topic": "other", "n": 1},
        {"topic": "registration_admission", "shadow_topic": "other", "n": 1},
    ]
    ttft = s["latency"]["ttft_ms"]
    assert (ttft["before_median"], ttft["before_n"]) == (2500, 2)
    assert (ttft["after_median"], ttft["after_n"]) == (1000, 1)


def test_since_filter_applies(db, cfg):
    row(db, cfg, topic="other", topic_confidence=1.0, ts=100, **shadow("other", 0.9))
    assert db.shadow_topic_stats(since_ts=0)["attempted"] == 1
    assert db.shadow_topic_stats(since_ts=200) is None


def test_cli_prints_section(db, cfg, monkeypatch):
    monkeypatch.setattr(stats, "get_config", lambda: cfg)
    row(db, cfg, topic="other", topic_confidence=1.0, ttft_ms=2000, llm_ms=7000)
    out = CliRunner().invoke(stats.main, [], catch_exceptions=False).output
    assert "ghost mode: no shadow-classified rows" in out

    row(db, cfg, topic="curriculum_courses", topic_confidence=1.0, **shadow("other", 0.6))
    out = CliRunner().invoke(stats.main, [], catch_exceptions=False).output
    assert "Ghost mode, System One topic" in out
    assert "0/1 (0%)" in out
    assert "widen --since" not in out
