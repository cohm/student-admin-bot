"""Fixtures shared by the test files."""

from __future__ import annotations

import threading
import types

import pytest


@pytest.fixture
def chat_turn_inline(monkeypatch):
    """Run the turn behind /api/chat before the response starts.

    The endpoint runs the turn in a worker thread that feeds an
    `asyncio.Queue`. Under TestClient nothing wakes the event loop once it
    waits on that queue, so a turn that takes more than a moment never
    returns (the running server's loop is woken by uvicorn's own timer). For
    tests that send a real pipeline turn through the endpoint."""
    from student_bot.web import app as web_app

    class _Inline:
        def __init__(self, target, **_kw):
            self._target = target

        def start(self):
            self._target()

    inline = types.SimpleNamespace(**{**vars(threading), "Thread": _Inline})
    monkeypatch.setattr(web_app, "threading", inline)
