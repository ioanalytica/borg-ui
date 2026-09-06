import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base, Operation, Repository, SystemSettings
from app.services.operations.enqueue import enqueue, enqueue_chain
from app.services.operations.runner import (
    OperationContext,
    OperationRunner,
    Outcome,
)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(session_factory):
    s = session_factory()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def repo(db):
    r = Repository(name="r", path="/tmp/r", encryption="none", compression="lz4")
    db.add(r)
    db.add(SystemSettings())
    db.commit()
    return r


@pytest.fixture()
def registry():
    return {}


@pytest.fixture()
def runner(session_factory, registry, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    return OperationRunner(
        session_factory=session_factory, registry=registry, poll_interval=0.01
    )


async def _drain(runner, rounds=20):
    for _ in range(rounds):
        await runner.tick()
        if runner.running_tasks:
            await asyncio.gather(
                *list(runner.running_tasks.values()), return_exceptions=True
            )
        await asyncio.sleep(0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_order_priority_then_age(db, repo, runner, registry):
    order = []

    async def record(ctx: OperationContext):
        order.append(ctx.operation_id)
        return Outcome()

    registry["stats"] = record
    late_high = enqueue(db, "stats", repository_id=repo.id, priority=20)
    early_low = enqueue(db, "stats", repository_id=repo.id, priority=0)
    await _drain(runner)
    assert order == [early_low.id, late_high.id]
    db.expire_all()
    assert {o.status for o in db.query(Operation)} == {"completed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_gating_and_failure_skips_chain(db, repo, runner, registry):
    async def fail(ctx):
        raise RuntimeError("boom")

    async def ok(ctx):
        return Outcome()

    registry["stats"] = fail
    registry["archive_sync"] = ok
    chain = enqueue_chain(
        db, ["stats", "archive_sync"], repository_id=repo.id, trigger="manual"
    )
    await _drain(runner)
    db.expire_all()
    first, second = (db.get(Operation, c.id) for c in chain)
    assert first.status == "failed"
    assert first.error_message == "boom"
    assert second.status == "skipped"
    assert second.skip_reason == "dependency_failed"
    assert second.completed_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dependency_waits_for_success(db, repo, runner, registry):
    seen = []

    async def ok(ctx):
        seen.append(ctx.kind)
        return Outcome()

    registry["stats"] = ok
    registry["archive_sync"] = ok
    enqueue_chain(
        db, ["stats", "archive_sync"], repository_id=repo.id, trigger="manual"
    )
    dispatched = await runner.tick()
    assert dispatched == 1
    await asyncio.gather(*runner.running_tasks.values())
    await runner.tick()
    await asyncio.gather(*runner.running_tasks.values())
    assert seen == ["stats", "archive_sync"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skipped_dependency_satisfies_dependants(db, repo, runner, registry):
    """A skipped stage is not a failed one: `stats` behind a `history_index`
    that had nothing to do still runs (#917)."""

    async def skip(ctx):
        return Outcome(status="skipped", skip_reason="agent_diff_unsupported")

    async def ok(ctx):
        return Outcome(result={"unique_csize": 1})

    registry["history_index"] = skip
    registry["stats"] = ok
    chain = enqueue_chain(
        db, ["history_index", "stats"], repository_id=repo.id, trigger="reconcile"
    )
    await _drain(runner)
    db.expire_all()
    first, second = (db.get(Operation, c.id) for c in chain)
    assert first.status == "skipped"
    assert first.skip_reason == "agent_diff_unsupported"
    assert second.status == "completed"
    assert second.result == {"unique_csize": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_executor_is_skipped(db, repo, runner, registry):
    op = enqueue(db, "history_index", repository_id=repo.id)
    await runner.tick()
    db.expire_all()
    op = db.get(Operation, op.id)
    assert op.status == "skipped"
    assert op.skip_reason == "executor_unavailable"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_followups_created_on_success_only_for_registered_kinds(
    db, repo, runner, registry
):
    async def ok(ctx):
        return Outcome()

    registry["import_connect"] = ok
    registry["stats"] = ok
    registry["archive_sync"] = ok
    parent = enqueue(db, "import_connect", repository_id=repo.id, trigger="import")
    await _drain(runner)
    db.expire_all()
    rows = db.query(Operation).order_by(Operation.id).all()
    assert [r.kind for r in rows] == ["import_connect", "stats", "archive_sync"]
    assert rows[1].depends_on_id == parent.id
    assert rows[2].depends_on_id == rows[1].id
    assert {r.run_id for r in rows} == {parent.run_id}
    assert rows[1].trigger == "followup" and rows[1].priority == 10
    assert {r.status for r in rows} == {"completed"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_followups_skip_history_kinds_on_community(
    db, repo, runner, registry, monkeypatch
):
    """A successful backup on a Community install enqueues archive_sync and
    stats but no history_index, even though its executor is registered."""

    async def ok(ctx):
        return Outcome()

    registry["backup"] = ok
    registry["archive_sync"] = ok
    registry["history_index"] = ok
    registry["stats"] = ok

    monkeypatch.setattr(
        "app.services.operations.runner.history_enabled", lambda db: False
    )
    enqueue(db, "backup", repository_id=repo.id, trigger="manual")
    await _drain(runner)
    db.expire_all()
    rows = db.query(Operation).order_by(Operation.id).all()
    assert [r.kind for r in rows] == ["backup", "archive_sync", "stats"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_followups_include_history_kinds_on_pro(
    db, repo, runner, registry, monkeypatch
):
    async def ok(ctx):
        return Outcome()

    registry["backup"] = ok
    registry["archive_sync"] = ok
    registry["history_index"] = ok
    registry["stats"] = ok

    monkeypatch.setattr(
        "app.services.operations.runner.history_enabled", lambda db: True
    )
    enqueue(db, "backup", repository_id=repo.id, trigger="manual")
    await _drain(runner)
    db.expire_all()
    rows = db.query(Operation).order_by(Operation.id).all()
    assert [r.kind for r in rows] == [
        "backup",
        "archive_sync",
        "history_index",
        "stats",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_followups_on_failure(db, repo, runner, registry):
    async def fail(ctx):
        return Outcome(status="failed", error_message="nope")

    registry["import_connect"] = fail
    enqueue(db, "import_connect", repository_id=repo.id, trigger="import")
    await _drain(runner)
    db.expire_all()
    assert db.query(Operation).count() == 1
    assert db.query(Operation).first().error_message == "nope"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_lane_blocks_second_exclusive_until_first_finishes(
    db, repo, runner, registry
):
    gate = asyncio.Event()

    async def wait(ctx):
        await gate.wait()
        return Outcome()

    registry["history_index"] = wait
    enqueue(db, "history_index", repository_id=repo.id)
    enqueue(db, "history_index", repository_id=repo.id)
    assert await runner.tick() == 1
    assert await runner.tick() == 0
    gate.set()
    await asyncio.gather(*runner.running_tasks.values())
    assert await runner.tick() == 1
    await asyncio.gather(*runner.running_tasks.values())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_queued_and_running(db, repo, runner, registry):
    started = asyncio.Event()

    async def slow(ctx):
        started.set()
        while not ctx.cancelled():
            await asyncio.sleep(0.01)
        return Outcome(status="skipped", skip_reason="cancelled_by_user")

    registry["stats"] = slow
    queued = enqueue(db, "stats", repository_id=repo.id, priority=5)
    running = enqueue(db, "stats", repository_id=repo.id, priority=0)
    await runner.tick()
    await started.wait()
    assert await runner.request_cancel(queued.id) is True
    assert await runner.request_cancel(running.id) is True
    await asyncio.gather(*runner.running_tasks.values(), return_exceptions=True)
    db.expire_all()
    assert db.get(Operation, queued.id).status == "cancelled"
    assert db.get(Operation, running.id).status == "cancelled"
    assert await runner.request_cancel(queued.id) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_refused_for_running_rows_this_process_does_not_own(
    db, repo, runner
):
    """Rows left running by another worker, or kept by recovery because their
    process is still alive, have no task here to observe the flag."""
    orphan = enqueue(db, "stats", repository_id=repo.id)
    orphan.status = "running"
    db.commit()
    assert await runner.request_cancel(orphan.id) is False
    assert orphan.id not in runner.cancel_requested
    db.expire_all()
    assert db.get(Operation, orphan.id).status == "running"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_progress_and_log(db, repo, runner, registry, tmp_path):
    async def work(ctx):
        ctx.log("hello")
        await ctx.progress(current=1, total=2, message="half")
        await ctx.progress(current=2, total=2, message="done")
        return Outcome(result={"n": 2})

    registry["stats"] = work
    op = enqueue(db, "stats", repository_id=repo.id)
    await _drain(runner)
    db.expire_all()
    op = db.get(Operation, op.id)
    assert op.result == {"n": 2}
    assert op.progress_current == 2 and op.progress_total == 2
    assert op.progress_message == "done"
    assert op.log_file_path and op.log_file_path.endswith(f"operation_{op.id}.log")
    assert open(op.log_file_path).read() == "hello\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_log_uses_current_settings_after_config_reload(
    db, repo, runner, registry, tmp_path, monkeypatch
):
    import importlib

    import app.config as config

    # Restore the original settings object after exercising the real reload.
    monkeypatch.setattr(config, "settings", config.settings)
    importlib.reload(config)
    current_dir = tmp_path / "reloaded"
    monkeypatch.setattr(config.settings, "data_dir", str(current_dir))

    async def work(ctx):
        ctx.log("after reload")
        return Outcome()

    registry["stats"] = work
    op = enqueue(db, "stats", repository_id=repo.id)
    await _drain(runner)
    db.expire_all()
    op = db.get(Operation, op.id)
    expected_path = current_dir / "logs" / f"operation_{op.id}.log"
    assert op.status == "completed"
    assert op.log_file_path == str(expected_path)
    assert expected_path.read_text() == "after reload\n"
    assert not (tmp_path / "logs" / f"operation_{op.id}.log").exists()


@pytest.mark.unit
def test_recover_on_startup(db, repo, runner, monkeypatch):
    idx = enqueue(db, "stats", repository_id=repo.id)
    idx.status = "running"
    idx.started_at = datetime(2026, 9, 1)
    idx.progress_current = 3
    dead = enqueue(db, "check", repository_id=repo.id)
    dead.status = "running"
    dead.process_pid = 4242
    dead.process_start_time = 1.0
    alive = enqueue(db, "compact", repository_id=repo.id)
    alive.status = "running"
    alive.process_pid = 4343
    alive.process_start_time = 2.0
    queued = enqueue(db, "stats", repository_id=repo.id)
    db.commit()
    monkeypatch.setattr(
        "app.services.operations.runner.is_process_alive",
        lambda pid, start: pid == 4343,
    )
    counts = runner.recover_on_startup(db)
    assert counts == {"requeued": 1, "failed": 1, "kept": 1}
    db.expire_all()
    assert db.get(Operation, idx.id).status == "queued"
    assert db.get(Operation, idx.id).started_at is None
    assert db.get(Operation, idx.id).progress_current is None
    assert db.get(Operation, dead.id).status == "failed"
    assert db.get(Operation, dead.id).error_message == "interrupted by restart"
    assert db.get(Operation, alive.id).status == "running"
    assert db.get(Operation, queued.id).status == "queued"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_start_loop_dispatches_and_stops(db, repo, runner, registry):
    done = asyncio.Event()

    async def ok(ctx):
        done.set()
        return Outcome()

    registry["stats"] = ok
    enqueue(db, "stats", repository_id=repo.id)
    task = asyncio.create_task(runner.start())
    await asyncio.wait_for(done.wait(), timeout=2)
    runner.stop()
    runner.wake()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("root_status", ["failed", "cancelled"])
@pytest.mark.parametrize("length", [3, 4])
async def test_dependency_failure_propagates_through_skipped_children(
    db, repo, runner, registry, root_status, length
):
    executed = []

    async def record(ctx):
        executed.append(ctx.kind)
        return Outcome()

    kinds = ["archive_sync", "history_merge", "history_index", "stats"][:length]
    registry.update({kind: record for kind in kinds})
    chain = enqueue_chain(db, kinds, repository_id=repo.id, trigger="reconcile")
    chain[0].status = root_status
    db.commit()
    await _drain(runner)
    db.expire_all()
    assert executed == []
    for op in chain[1:]:
        persisted = db.get(Operation, op.id)
        assert persisted.status == "skipped"
        assert persisted.skip_reason == "dependency_failed"
        assert persisted.completed_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["agent_diff_unsupported", "plan_locked"])
async def test_intentional_skip_in_chain_keeps_stats_runnable(
    db, repo, runner, registry, reason
):
    async def ok(ctx):
        return Outcome()

    async def optional_stage(ctx):
        return Outcome(status="skipped", skip_reason=reason)

    registry.update(archive_sync=ok, history_index=optional_stage, stats=ok)
    chain = enqueue_chain(
        db,
        ["archive_sync", "history_index", "stats"],
        repository_id=repo.id,
        trigger="reconcile",
    )
    await _drain(runner)
    db.expire_all()
    rows = [db.get(Operation, op.id) for op in chain]
    assert [op.status for op in rows] == ["completed", "skipped", "completed"]
    assert rows[1].skip_reason == reason
    assert rows[2].skip_reason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repository_busy_defers_instead_of_failing(db, repo, runner, registry):
    """Seen live: the follow-up listing is dispatched 45 ms before the plan
    creates its prune job and admission refuses it with 409. The operation
    goes back to the queue with a deferral count and runs on a later tick;
    its dependants are untouched meanwhile."""
    from fastapi import HTTPException

    attempts = []

    async def busy_then_done(ctx):
        attempts.append(ctx.operation_id)
        if len(attempts) == 1:
            raise HTTPException(
                status_code=409,
                detail={"key": "backend.errors.jobs.repositoryOperationActive"},
            )
        return Outcome(result={"listed": 1})

    registry["archive_sync"] = busy_then_done
    registry["stats"] = lambda ctx: _done()
    first = enqueue(db, "archive_sync", repository_id=repo.id)
    child = enqueue(db, "stats", repository_id=repo.id, depends_on_id=first.id)
    await runner.tick()
    await asyncio.gather(*list(runner.running_tasks.values()), return_exceptions=True)
    db.expire_all()
    first = db.get(Operation, first.id)
    assert first.status == "queued" and first.started_at is None
    assert first.params["deferrals"] == 1 and first.error_message is None
    assert db.get(Operation, child.id).status == "queued"
    # a deferral does not wake the runner: no tight retry loop
    assert not runner._event().is_set()

    await _drain(runner)
    db.expire_all()
    assert db.get(Operation, first.id).status == "completed"
    assert db.get(Operation, first.id).result == {"listed": 1}
    assert db.get(Operation, child.id).status == "completed"
    assert len(attempts) == 2


async def _done():
    return Outcome()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repository_busy_fails_after_the_deferral_cap(db, repo, runner, registry):
    from fastapi import HTTPException

    from app.services.operations import runner as runner_module

    async def always_busy(ctx):
        raise HTTPException(
            status_code=409,
            detail={"key": "backend.errors.jobs.repositoryOperationActive"},
        )

    registry["archive_sync"] = always_busy
    op = enqueue(db, "archive_sync", repository_id=repo.id)
    await _drain(runner, rounds=runner_module.MAX_DEFERRALS + 5)
    db.expire_all()
    op = db.get(Operation, op.id)
    assert op.status == "failed"
    assert op.params["deferrals"] == runner_module.MAX_DEFERRALS
    assert "still busy" in op.error_message

    # any other exception, and a 409 without the admission key, still fail
    # at once
    async def other(ctx):
        raise HTTPException(status_code=409, detail="something else")

    registry["archive_sync"] = other
    other_op = enqueue(db, "archive_sync", repository_id=repo.id)
    await _drain(runner)
    db.expire_all()
    assert db.get(Operation, other_op.id).status == "failed"
    assert (db.get(Operation, other_op.id).params or {}).get("deferrals") is None
