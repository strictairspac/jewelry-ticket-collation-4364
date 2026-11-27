from time import time

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError, OperationalError

from src.constants.job_state import JobState
from src.domain.scenario import CONFIG
from src.models.cases import cases
from src.models.runs import runs
from . import audit, cases as case_service

# Lock wait timeouts (MySQL 1205) and deadlocks (MySQL 1213) are retryable: the batch is
# settled by the holder and the operator can simply confirm the cancellation again.
RETRYABLE_LOCK_ERRORS = {1205, 1213}


def query():
    return select(runs, cases.c.title.label("case_title")).join(cases, cases.c.id == runs.c.case_id)


def get(db, run_id):
    run = db.execute(query().where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return dict(run)


def list_entries(db):
    return [dict(row) for row in db.execute(query().order_by(runs.c.id.desc()).limit(100)).mappings()]


def enqueue(db, case_id, parameter, actor):
    if parameter > CONFIG["max_parameter"]:
        raise HTTPException(422, f"参数不能超过 {CONFIG['max_parameter']}")
    case_service.get(db, case_id)
    change = db.execute(insert(runs).values(
        case_id=case_id, status=JobState.QUEUED, parameter=parameter, actor=actor, created_at=int(time())
    ))
    run_id = change.inserted_primary_key[0]
    audit.record(db, "enqueue", actor, "run", run_id)
    return get(db, run_id)


def cancel(db, run_id, actor, reason=None):
    statement = select(runs.c.status).where(runs.c.id == run_id).with_for_update()
    try:
        status = db.scalar(statement)
    except (OperationalError, DBAPIError) as exc:
        if _is_lock_timeout(exc):
            raise HTTPException(409, "试写正在占用本批，请稍后重新确认撤单") from exc
        raise
    if status is None:
        raise HTTPException(404, "执行批次不存在")
    if status == JobState.CANCELLED:
        # Idempotent retry: the receipt was lost but the cancellation already landed.
        # Return the current batch without writing another audit event.
        return get(db, run_id)
    if status != JobState.QUEUED:
        raise HTTPException(409, "批次已结束，不能撤单；按当前结果照常交接")
    trimmed = (reason or "").strip()[:320] or None
    db.execute(update(runs).where(runs.c.id == run_id).values(
        status=JobState.CANCELLED, finished_at=int(time()),
        cancel_reason=trimmed, cancelled_by=actor,
    ))
    audit.record(db, "cancel", actor, "run", run_id)
    return get(db, run_id)


def _is_lock_timeout(exc):
    orig = getattr(exc, "orig", None)
    return orig is not None and getattr(orig, "args", None) and orig.args[0] in RETRYABLE_LOCK_ERRORS
