"""Agent repository operations surface borg lock failures as HTTP 423.

An external process (e.g. a scheduled backup on another host) can hold the
repository lock. The agent's borg command then fails. Detection is both
exit-code based (granular 70-75, only emitted with BORG_EXIT_CODES=modern)
and stderr based (Borg 1.x legacy default: any error is rc 2, so the lock is
only recognisable from borg's "Failed to create/acquire the lock" message
which the agent streams into the job logs). Either signal must yield a
"repository locked" 423 so the UI shows the break-lock flow instead of
rendering an empty repository.
"""

import pytest
from fastapi import HTTPException

from app.core.security import get_password_hash
from app.database.models import AgentJob, AgentJobLog, AgentMachine
from app.services.repository_executor import (
    wait_for_agent_repository_operation_job,
)


def _create_agent(test_db):
    agent = AgentMachine(
        name="Lock Agent",
        agent_id="agt_lock",
        token_hash=get_password_hash("borgui_agent_secret"),
        token_prefix="borgui_agent_secret"[:20],
        status="online",
        capabilities=["repository.list_archives"],
    )
    test_db.add(agent)
    test_db.commit()
    test_db.refresh(agent)
    return agent


def _create_failed_job(test_db, agent, *, result, stderr=None):
    job = AgentJob(
        agent_machine_id=agent.id,
        job_type="repository_operation",
        status="failed",
        payload={"job_kind": "repository.list_archives"},
        error_message="repository.list_archives exited with code 2",
        result=result,
    )
    test_db.add(job)
    test_db.commit()
    test_db.refresh(job)
    if stderr is not None:
        test_db.add(
            AgentJobLog(
                agent_job_id=job.id,
                sequence=2,
                stream="stderr",
                message=stderr,
                created_at=job.created_at,
            )
        )
        test_db.commit()
    return job


_BORG_LOCK_STDERR = (
    "Failed to create/acquire the lock "
    "ssh://u2@borg01:23/./styxnet/k8s-borg-styxnet (timeout)."
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lock_exit_code_surfaces_as_423(test_db):
    agent = _create_agent(test_db)
    # 73 = "Failed to create/acquire the lock (timeout)"
    job = _create_failed_job(test_db, agent, result={"return_code": 73})

    with pytest.raises(HTTPException) as exc:
        await wait_for_agent_repository_operation_job(test_db, job.id, repository_id=42)

    assert exc.value.status_code == 423
    detail = exc.value.detail
    assert detail["error"] == "repository_locked"
    assert detail["message"] == "backend.errors.repo.repositoryLocked"
    assert detail["can_break_lock"] is True
    assert detail["repository_id"] == 42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_exit_code_lock_detected_from_stderr(test_db):
    # Real-world Borg 1.x default: a lock failure exits rc 2 (legacy codes),
    # so only the streamed output identifies it as a lock. Read ops keep stderr
    # separate.
    agent = _create_agent(test_db)
    job = _create_failed_job(
        test_db, agent, result={"return_code": 2}, stderr=_BORG_LOCK_STDERR
    )

    with pytest.raises(HTTPException) as exc:
        await wait_for_agent_repository_operation_job(test_db, job.id, repository_id=42)

    assert exc.value.status_code == 423
    assert exc.value.detail["error"] == "repository_locked"
    # "key" lets error-toast callers (e.g. file download) render the message.
    assert exc.value.detail["key"] == "backend.errors.repo.repositoryLocked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_lock_failure_still_502(test_db):
    agent = _create_agent(test_db)
    # 2 = generic error, with unrelated stderr — must NOT be treated as a lock.
    job = _create_failed_job(
        test_db,
        agent,
        result={"return_code": 2},
        stderr="Repository.DoesNotExist: no repository at this location",
    )

    with pytest.raises(HTTPException) as exc:
        await wait_for_agent_repository_operation_job(test_db, job.id, repository_id=42)

    assert exc.value.status_code == 502
    assert exc.value.detail["key"] == "backend.errors.agents.repositoryOperationFailed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_without_return_code_still_502(test_db):
    agent = _create_agent(test_db)
    job = _create_failed_job(test_db, agent, result={})

    with pytest.raises(HTTPException) as exc:
        await wait_for_agent_repository_operation_job(test_db, job.id)

    assert exc.value.status_code == 502
