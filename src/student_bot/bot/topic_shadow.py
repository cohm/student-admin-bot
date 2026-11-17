"""Ghost mode for a System One topic classifier.

Sends the student's question to a System One endpoint (TypeSafe's Jev
`/v1/systemone` contract: Nimble behind the LiteLLM gateway, or Berget) and
stores the answer in the `shadow_topic*` columns of qa_log, next to the
topic the chat LLM picked. Nothing reads the result at runtime; the point
is to log how the classifier would have decided so the two can be compared.

Runs AFTER the answer is sent, on a daemon thread, so it adds no
user-visible latency and never delays the Mattermost worker. Skipped for
opted-out users (their turn has no qa_id).
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from student_bot.bot.topics import Topic, load_topics
from student_bot.config import Config
from student_bot.logging_db import LogDB

log = logging.getLogger("student_bot.topic_shadow")

# The question key inside the System One request/answer envelope.
QUESTION_KEY = "topic"


@dataclass(frozen=True)
class ShadowTopic:
    """One classifier call. `topic` is None whenever `error` is set."""

    topic: str | None
    confidence: float | None
    probs: dict[str, float] | None
    ms: int
    # The model the response names; None when no call was made.
    model: str | None
    error: str | None = None


def endpoint_url(cfg: Config) -> str | None:
    """Scheme and host of the configured provider plus `topic_shadow.path`.

    The provider's `base_url` ends in the chat API prefix (`/v1`), which the
    System One route does not live under, so only its origin is kept.
    """
    provider = cfg.llm.providers.get(cfg.topic_shadow.provider)
    if provider is None or not provider.base_url:
        return None
    parts = urlsplit(provider.base_url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}{cfg.topic_shadow.path}"


def build_request(cfg: Config, topics: list[Topic], question: str, lang: str) -> dict[str, Any]:
    if lang == "en":
        instructions = "Which topic best matches the student's question?"
    else:
        instructions = "Vilket ämne passar studentens fråga bäst?"
    criteria = {t.id: f"{t.label(lang)}. {t.hint}" for t in topics}
    return {
        "model": cfg.topic_shadow.model,
        "state": {"message": question},
        "questions": {
            QUESTION_KEY: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }


def _post(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float
) -> httpx.Response:
    return httpx.post(url, headers=headers, json=body, timeout=timeout)


def _parse(
    payload: Any, valid_ids: set[str]
) -> tuple[str, float | None, dict[str, float] | None, str | None]:
    """Pull (choice, confidence, probabilities, served model) out of a System
    One answer. Probabilities are kept only for known topic ids.

    Raises ValueError with a short reason; the reason is what gets stored
    in `shadow_topic_error`, so it must never contain response text.
    """
    try:
        answer = payload["answers"][QUESTION_KEY]
        choice = answer["choice"]
    except (KeyError, TypeError, IndexError):
        raise ValueError("bad_response") from None
    if not isinstance(answer, dict) or not isinstance(choice, str):
        raise ValueError("bad_response")
    if choice not in valid_ids:
        raise ValueError("unknown_label")
    probs_raw = answer.get("probabilities")
    confidence = answer.get("confidence")
    try:
        probs = None
        if isinstance(probs_raw, dict):
            probs = {k: float(probs_raw[k]) for k in sorted(valid_ids) if k in probs_raw}
        conf = float(confidence) if confidence is not None else None
    except (TypeError, ValueError, OverflowError):
        # float() quotes the offending value in its message; keep it out.
        raise ValueError("bad_numbers") from None
    values = list(probs.values()) if probs else []
    if conf is not None:
        values.append(conf)
    # Probabilities; a hair outside [0, 1] is float rounding, not an error.
    if not all(math.isfinite(v) and -1e-6 <= v <= 1.0 + 1e-6 for v in values):
        raise ValueError("bad_numbers")
    probs = probs or None
    served = payload.get("model")
    model = served if isinstance(served, str) and 0 < len(served) <= 100 else None
    return choice, conf, probs, model


def classify(cfg: Config, question: str, lang: str) -> ShadowTopic:
    """Call the classifier once. Never raises; failures come back as `error`."""
    shadow = cfg.topic_shadow
    t0 = time.monotonic()

    def fail(reason: str) -> ShadowTopic:
        ms = int((time.monotonic() - t0) * 1000)
        log.warning("topic shadow: %s (%s, %d ms)", reason, shadow.model, ms)
        return ShadowTopic(None, None, None, ms, shadow.model, reason)

    url = endpoint_url(cfg)
    if url is None:
        return fail("no_endpoint")
    secret = cfg.llm_api_keys.get(shadow.provider)
    if secret is None:
        return fail("no_api_key")
    try:
        topics = load_topics(cfg)
    except Exception:
        topics = []
    if not topics:
        return fail("no_topics")
    headers = {
        "Authorization": f"Bearer {secret.get_secret_value()}",
        "Content-Type": "application/json",
    }
    body = build_request(cfg, topics, question, lang)
    try:
        resp = _post(url, headers, body, shadow.timeout_seconds)
    except httpx.TimeoutException:
        return fail("timeout")
    except Exception as e:
        # Transport errors, and config mistakes such as an invalid URL.
        return fail(type(e).__name__)
    if resp.status_code != 200:
        # Only the status: a body could echo the request or the key.
        return fail(f"http_{resp.status_code}")
    try:
        payload = resp.json()
    except ValueError:
        return fail("bad_json")
    try:
        topic, confidence, probs, served = _parse(payload, {t.id for t in topics})
    except ValueError as e:
        return fail(str(e))
    ms = int((time.monotonic() - t0) * 1000)
    # The response names the model that served the call; the alias in config
    # can be re-pointed on the gateway without this code noticing.
    return ShadowTopic(topic, confidence, probs, ms, served or shadow.model)


def record(db: LogDB, qa_id: int, decision: ShadowTopic) -> None:
    db.update_shadow_topic(
        qa_id,
        topic=decision.topic,
        confidence=decision.confidence,
        probs=json.dumps(decision.probs) if decision.probs is not None else None,
        ms=decision.ms,
        model=decision.model,
        error=decision.error,
    )


# Bounds the number of in-flight calls: a slow or unreachable gateway must
# not pile up one thread per question. Sized from config on first use.
_slots: threading.Semaphore | None = None
_slots_lock = threading.Lock()


def _acquire_slot(cfg: Config) -> threading.Semaphore | None:
    """The semaphore to release when done, or None when every slot is busy."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.Semaphore(max(1, cfg.topic_shadow.max_pending))
        slots = _slots
    return slots if slots.acquire(blocking=False) else None


def refusal(result: Any) -> str | None:
    """Why `pipeline.answer()` refused a turn before answering, or None.

    Takes an `AnswerResult`; typed loosely to keep this module free of a
    pipeline import.
    """
    if result.too_long:
        return "too_long"
    if result.rate_limited:
        return "rate_limited"
    return None


def submit_shadow_topic(
    cfg: Config,
    db: LogDB,
    qa_id: int | None,
    question: str,
    lang: str,
    *,
    refused: str | None = None,
) -> threading.Thread | None:
    """Classify `question` in the background and store the result on `qa_id`.

    Returns the started thread (tests join it), or None when nothing was
    started: the feature is off, the turn was not logged (opt-out), the
    the pipeline refused the turn (`refused` is then its reason, see
    `refusal`), or too many calls are already pending. The last two store
    that reason / `busy` as the error, so the row can usually be told apart
    from one logged while the feature was off.
    """
    if qa_id is None or not cfg.topic_shadow.enabled:
        return None

    def skip(reason: str) -> None:
        # A skipped turn gets a reason, so it is not mistaken for one logged
        # while the feature was off. This write runs on the caller's thread,
        # so it must not raise into the request.
        try:
            record(db, qa_id, ShadowTopic(None, None, None, 0, None, reason))
        except Exception:
            log.warning("topic shadow: could not record %s for qa %d", reason, qa_id)

    if refused:
        # The pipeline did not answer this turn; don't spend the classifier
        # on it. `question` may be a joined text longer than the input cap,
        # so the pipeline's verdict is used, not the length.
        skip(refused)
        return None
    slots = _acquire_slot(cfg)
    if slots is None:
        log.warning("topic shadow: every slot busy, skipping qa %d", qa_id)
        skip("busy")
        return None

    def run() -> None:
        try:
            record(db, qa_id, classify(cfg, question, lang))
        except Exception:
            log.exception("topic shadow: failed for qa %d", qa_id)
        finally:
            slots.release()

    thread = threading.Thread(target=run, name=f"topic-shadow-{qa_id}", daemon=True)
    try:
        thread.start()
    except Exception as e:
        # Thread exhaustion must not reach the caller or keep the slot.
        slots.release()
        log.warning("topic shadow: could not start thread for qa %d: %s", qa_id, type(e).__name__)
        skip("no_thread")
        return None
    return thread


__all__ = [
    "ShadowTopic",
    "classify",
    "endpoint_url",
    "build_request",
    "record",
    "refusal",
    "submit_shadow_topic",
]
