"""Phase 10 prep-stub residual: the funder-reply arq task placeholder.

The override route's stub was replaced by the real implementation in
this branch (see ``tests/test_override.py``). The
``process_funder_reply`` arq task remains a placeholder because 2D-main
implemented funder-reply ingestion as HTTP routes (webhook + operator-
paste) rather than an arq job; the arq task name + registration are
preserved so a future async LLM-extract pipeline can land without
re-reserving the WorkerSettings slot.
"""

from __future__ import annotations

import asyncio

import pytest

from aegis.workers import process_funder_reply


def test_process_funder_reply_task_still_raises_not_implemented() -> None:
    """The task remains a placeholder. Calling it must raise — a silent
    success here would be a regulator-defense gap once Phase 10 capture
    is in production, because callers would think they enqueued
    ingestion work that never actually ran."""
    with pytest.raises(NotImplementedError):
        asyncio.run(process_funder_reply({}, "{}"))


def test_process_funder_reply_is_still_registered_with_arq() -> None:
    """Lock the WorkerSettings.functions tuple shape. Keeping the slot
    reserved means a follow-up branch that promotes funder-reply
    ingestion to async (offloading LLM extraction off the request
    thread) doesn't have to touch WorkerSettings."""
    from aegis.workers import WorkerSettings, parse_document

    assert parse_document in WorkerSettings.functions
    assert process_funder_reply in WorkerSettings.functions
