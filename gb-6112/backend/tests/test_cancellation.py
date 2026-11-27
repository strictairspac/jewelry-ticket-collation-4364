"""Cancellation state-machine tests.

These tests exercise the durable queue semantics without needing MySQL or Redis:
the same conditional SQL and row-state guards run against SQLite. MySQL-specific
locking (FOR UPDATE / SKIP LOCKED) is parsed by SQLite as a no-op hint, while the
WHERE-guarded UPDATE keeps the "first outcome to land wins" guarantee intact.
"""
from time import time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from src.constants.job_state import JobState
from src.models.audit import audit
from src.models.base import metadata
from src.models.cases import cases
from src.models.runs import runs
from src.services import execution, runs as run_service


@pytest.fixture()
def db_engine(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    # execution.service_one/fail open their own sessions; the run service takes the
    # session as an argument, so only the execution module binds an engine by value.
    monkeypatch.setattr(execution, "engine", engine)
    yield engine
    engine.dispose()


def make_case(engine, code="CASE-1"):
    with engine.begin() as db:
        result = db.execute(
            cases.insert().values(
                code=code, title="冬展卷", description="试写资料", payload={"tickets": []}, created_by="seed"
            )
        )
        return result.inserted_primary_key[0]


def make_run(engine, case_id, status=JobState.QUEUED):
    with engine.begin() as db:
        result = db.execute(
            runs.insert().values(
                case_id=case_id, status=status, parameter=6, actor="operator", created_at=int(time())
            )
        )
        return result.inserted_primary_key[0]


def get_run(engine, run_id):
    with engine.connect() as db:
        return dict(db.execute(select(runs).where(runs.c.id == run_id)).mappings().one())


def audit_actions(engine, run_id):
    with engine.connect() as db:
        return [row[0] for row in db.execute(
            select(audit.c.action).where(audit.c.entity == "run", audit.c.entity_id == run_id)
        ).all()]


def test_cancel_queued_batch_lands_and_records_one_event(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        entry = run_service.cancel(db, run_id, "operator", "选错卷了")

    stored = get_run(db_engine, run_id)
    assert entry["status"] == JobState.CANCELLED
    assert stored["status"] == JobState.CANCELLED
    assert stored["cancel_reason"] == "选错卷了"
    assert stored["cancelled_by"] == "operator"
    assert stored["finished_at"] is not None
    assert stored["result"] is None
    assert audit_actions(db_engine, run_id) == ["cancel"]


def test_cancel_retry_is_idempotent_and_does_not_duplicate_audit(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        first = run_service.cancel(db, run_id, "operator")
    # Simulate a lost receipt: the operator retries the exact same request.
    with db_engine.begin() as db:
        second = run_service.cancel(db, run_id, "operator")

    assert second["status"] == JobState.CANCELLED
    assert second["cancelled_by"] == first["cancelled_by"]
    with db_engine.connect() as db:
        count = db.scalar(select(func.count()).select_from(audit).where(
            audit.c.entity == "run", audit.c.entity_id == run_id, audit.c.action == "cancel"))
    assert count == 1


def test_completed_batch_cannot_be_cancelled_and_keeps_handoff(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id, JobState.DONE)
    with db_engine.begin() as db:
        db.execute(runs.update().where(runs.c.id == run_id).values(result={"summary": "ok"}, digest="abc"))

    with db_engine.begin() as db:
        with pytest.raises(HTTPException) as exc:
            run_service.cancel(db, run_id, "operator")
    assert exc.value.status_code == 409
    assert get_run(db_engine, run_id)["status"] == JobState.DONE
    assert audit_actions(db_engine, run_id) == []


def test_failed_batch_cannot_be_cancelled(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id, JobState.FAILED)

    with db_engine.begin() as db:
        with pytest.raises(HTTPException) as exc:
            run_service.cancel(db, run_id, "operator")
    assert exc.value.status_code == 409
    assert get_run(db_engine, run_id)["status"] == JobState.FAILED


def test_cancel_unknown_run_is_404(db_engine):
    with db_engine.begin() as db:
        with pytest.raises(HTTPException) as exc:
            run_service.cancel(db, 9999, "operator")
    assert exc.value.status_code == 404


def test_worker_outcome_after_cancellation_is_discarded_without_handoff(db_engine):
    """Requirement: a late trial-write result must never produce a handoff document."""
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        run_service.cancel(db, run_id, "operator")

    # The worker finishes its in-flight trial write and calls finish() afterwards.
    with db_engine.begin() as db:
        execution.finish(db, run_id, {"status": JobState.DONE, "result": {"summary": "late"}, "digest": "d"})

    stored = get_run(db_engine, run_id)
    assert stored["status"] == JobState.CANCELLED
    assert stored["result"] is None
    assert stored["digest"] is None
    # No spurious completion event was recorded.
    assert audit_actions(db_engine, run_id) == ["cancel"]


def test_worker_failure_after_cancellation_keeps_cancelled_state(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)
    with db_engine.begin() as db:
        run_service.cancel(db, run_id, "operator")

    failure = execution.JobFailure(run_id, ValueError("bad payload"))
    execution.fail(failure)

    assert get_run(db_engine, run_id)["status"] == JobState.CANCELLED
    assert audit_actions(db_engine, run_id) == ["cancel"]


def test_finish_guard_still_records_done_for_live_queue_batch(db_engine):
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        execution.finish(db, run_id, {"status": JobState.DONE, "result": {"summary": "ok"}, "digest": "d"})

    stored = get_run(db_engine, run_id)
    assert stored["status"] == JobState.DONE
    assert stored["result"] == {"summary": "ok"}
    assert audit_actions(db_engine, run_id) == ["done"]


def test_cancel_then_finish_guard_makes_cancel_win(db_engine):
    """Deterministic "first to land wins": cancel commits first, so the worker's later
    guarded finish() matches no queued row and the batch stays cancelled without handoff."""
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        run_service.cancel(db, run_id, "operator")
    with db_engine.begin() as db:
        execution.finish(db, run_id, {"status": JobState.DONE, "result": {"summary": "ok"}, "digest": "d"})

    stored = get_run(db_engine, run_id)
    assert stored["status"] == JobState.CANCELLED
    assert stored["result"] is None
    assert audit_actions(db_engine, run_id) == ["cancel"]


def test_finish_then_cancel_guard_makes_finish_win(db_engine):
    """Mirror case: the worker finishes first, so the later cancellation is rejected with 409
    and the completed batch keeps its handoff evidence."""
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)

    with db_engine.begin() as db:
        execution.finish(db, run_id, {"status": JobState.DONE, "result": {"summary": "ok"}, "digest": "d"})
    with db_engine.begin() as db:
        with pytest.raises(HTTPException) as exc:
            run_service.cancel(db, run_id, "operator")
    assert exc.value.status_code == 409

    stored = get_run(db_engine, run_id)
    assert stored["status"] == JobState.DONE
    assert stored["result"] == {"summary": "ok"}
    assert audit_actions(db_engine, run_id) == ["done"]


def test_guarded_finish_updates_zero_rows_without_failing(db_engine):
    """The worker's conditional UPDATE must report success-but-no-op instead of overwriting
    a cancelled batch, and must not emit a completion audit event."""
    case_id = make_case(db_engine)
    run_id = make_run(db_engine, case_id)
    with db_engine.begin() as db:
        run_service.cancel(db, run_id, "operator")

    with db_engine.begin() as db:
        # finish() swallows the zero-row outcome internally; it must not raise.
        execution.finish(db, run_id, {"status": JobState.DONE, "result": {"summary": "late"}, "digest": "d"})

    assert get_run(db_engine, run_id)["status"] == JobState.CANCELLED
    assert audit_actions(db_engine, run_id) == ["cancel"]


def test_cancelling_one_batch_does_not_touch_other_batches_of_same_volume(db_engine, monkeypatch):
    case_id = make_case(db_engine)
    target = make_run(db_engine, case_id)
    sibling = make_run(db_engine, case_id)
    other_case = make_case(db_engine, code="CASE-2")
    other = make_run(db_engine, other_case)

    with db_engine.begin() as db:
        run_service.cancel(db, target, "operator")

    assert get_run(db_engine, target)["status"] == JobState.CANCELLED
    assert get_run(db_engine, sibling)["status"] == JobState.QUEUED
    assert get_run(db_engine, other)["status"] == JobState.QUEUED

    # The sibling batch is still picked up and completed by the worker loop.
    monkeypatch.setattr(execution.scenario, "run", lambda payload, parameter, db: {"summary": "ok"})
    with db_engine.begin() as db:
        assert execution.service_one() is True
    assert get_run(db_engine, sibling)["status"] == JobState.DONE
    assert get_run(db_engine, target)["status"] == JobState.CANCELLED


def test_service_one_skips_cancelled_batch_and_serves_next(db_engine, monkeypatch):
    case_id = make_case(db_engine)
    cancelled = make_run(db_engine, case_id, JobState.CANCELLED)
    queued = make_run(db_engine, case_id)

    monkeypatch.setattr(execution.scenario, "run", lambda payload, parameter, db: {"summary": "ok"})
    with db_engine.begin() as db:
        served = execution.service_one()
    assert served is True
    assert get_run(db_engine, cancelled)["status"] == JobState.CANCELLED
    assert get_run(db_engine, queued)["status"] == JobState.DONE


def test_service_one_returns_false_when_only_cancelled_batches_remain(db_engine):
    case_id = make_case(db_engine)
    make_run(db_engine, case_id, JobState.CANCELLED)
    with db_engine.begin() as db:
        assert execution.service_one() is False
