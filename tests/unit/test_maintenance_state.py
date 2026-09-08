from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.maintenance_state import (
    apply_compact_completion,
    apply_compact_stats,
)


@pytest.mark.unit
def test_apply_compact_completion_marks_success_and_updates_repository():
    now = datetime(2026, 4, 16, 10, 0, 0)
    job = SimpleNamespace(
        status="running",
        progress=10,
        progress_message=None,
        error_message=None,
        completed_at=None,
    )
    repo = SimpleNamespace(last_compact=None)

    apply_compact_completion(job, repo, 0, now=now)

    assert job.status == "completed"
    assert job.progress == 100
    assert job.progress_message == "Compact completed successfully"
    assert job.error_message is None
    assert job.completed_at == now
    assert repo.last_compact == now


@pytest.mark.unit
def test_apply_compact_completion_marks_warnings_and_updates_repository():
    now = datetime(2026, 4, 16, 10, 0, 0)
    job = SimpleNamespace(
        status="running",
        progress=10,
        progress_message=None,
        error_message=None,
        completed_at=None,
    )
    repo = SimpleNamespace(last_compact=None)

    apply_compact_completion(job, repo, 100, now=now)

    assert job.status == "completed_with_warnings"
    assert job.progress == 100
    assert "warnings" in job.progress_message
    assert job.error_message == job.progress_message
    assert job.completed_at == now
    assert repo.last_compact == now


@pytest.mark.unit
def test_apply_compact_completion_marks_failure_without_repository_update():
    now = datetime(2026, 4, 16, 10, 0, 0)
    previous_compact = datetime(2026, 4, 15, 10, 0, 0)
    job = SimpleNamespace(
        status="running",
        progress=10,
        progress_message=None,
        error_message=None,
        completed_at=None,
    )
    repo = SimpleNamespace(last_compact=previous_compact)

    apply_compact_completion(job, repo, 2, now=now)

    assert job.status == "failed"
    assert job.error_message == "Compact failed with exit code 2"
    assert job.completed_at == now
    assert repo.last_compact == previous_compact


@pytest.mark.unit
def test_apply_compact_completion_persists_stats_and_refreshes_size():
    """`borg compact --stats` output is the only Borg 2 repository size
    (#931): it lands on the job and refreshes total_size."""
    job = SimpleNamespace(
        status="running",
        progress=0,
        progress_message=None,
        error_message=None,
        completed_at=None,
        stats=None,
    )
    repo = SimpleNamespace(
        last_compact=None, total_size=None, total_size_source="storage_used"
    )
    stats = {"repository_size": 502_000, "object_count": 6, "size_precision": "exact"}

    apply_compact_completion(job, repo, 0, stats=stats)

    assert job.stats == stats
    assert repo.total_size == "490.23 KB"
    # value and provenance move together: the label names pack file bytes,
    # not whatever source the previous measurement had
    assert repo.total_size_source == "compact_stats"

    # An emptied repository measures 0 and must replace the stale size.
    repo.total_size_source = "borg2_index"
    apply_compact_completion(
        job, repo, 0, stats={"repository_size": 0, "size_precision": "exact"}
    )
    assert repo.total_size == "0.00 B"
    assert repo.total_size_source == "compact_stats"

    # the warning completion path writes them too
    repo = SimpleNamespace(
        last_compact=None, total_size=None, total_size_source="borg2_index"
    )
    apply_compact_completion(job, repo, 100, stats=stats)
    assert job.status == "completed_with_warnings"
    assert repo.total_size == "490.23 KB"
    assert repo.total_size_source == "compact_stats"


@pytest.mark.unit
def test_apply_compact_completion_keeps_size_without_stats_or_on_failure():
    job = SimpleNamespace(
        status="running",
        progress=0,
        progress_message=None,
        error_message=None,
        completed_at=None,
        stats="untouched",
    )
    repo = SimpleNamespace(
        last_compact=None, total_size="keep", total_size_source="storage_used"
    )

    apply_compact_completion(job, repo, 0, stats=None)
    assert job.stats == "untouched" and repo.total_size == "keep"
    assert repo.total_size_source == "storage_used"

    apply_compact_completion(job, repo, 2, stats={"repository_size": 1})
    assert job.stats == "untouched" and repo.total_size == "keep"
    assert repo.total_size_source == "storage_used"


@pytest.mark.unit
def test_apply_compact_stats_can_keep_a_newer_size():
    """Statistics that arrive after a later measurement wrote the size
    still land on the job; the size is left alone."""
    job = SimpleNamespace(stats=None)
    repo = SimpleNamespace(total_size="7.00 GB", total_size_source="borg2_index")

    apply_compact_stats(
        job, repo, {"repository_size": 5, "size_precision": "exact"}, refresh_size=False
    )

    assert job.stats == {"repository_size": 5, "size_precision": "exact"}
    assert repo.total_size == "7.00 GB"
    assert repo.total_size_source == "borg2_index"


@pytest.mark.unit
def test_apply_compact_stats_keeps_the_size_for_a_rounded_figure():
    """A compact that ran without BORG_UNITS=raw printed rounded sizes: the
    statistics land on the job, the size is not replaced by them."""
    job = SimpleNamespace(stats=None)
    repo = SimpleNamespace(total_size="1.43 MB", total_size_source="borg2_index")

    apply_compact_stats(
        job,
        repo,
        {"repository_size": 1_000_000, "size_precision": "rounded_to_printed_unit"},
    )

    assert job.stats["repository_size"] == 1_000_000
    assert repo.total_size == "1.43 MB"
    assert repo.total_size_source == "borg2_index"

    # a size without a source label predates the label: measured, kept
    repo = SimpleNamespace(total_size="5.00 GB", total_size_source=None)
    apply_compact_stats(
        job,
        repo,
        {"repository_size": 1_000_000, "size_precision": "rounded_to_printed_unit"},
    )
    assert repo.total_size == "5.00 GB"

    # an unlabelled figure is not trusted either
    apply_compact_stats(job, repo, {"repository_size": 1_000_000})
    assert repo.total_size == "5.00 GB"

    # ... but it beats no size, and an older compact's figure
    for total_size, source in ((None, None), ("old", "compact_stats")):
        repo = SimpleNamespace(total_size=total_size, total_size_source=source)
        apply_compact_stats(
            job,
            repo,
            {"repository_size": 1_000_000, "size_precision": "rounded_to_printed_unit"},
        )
        assert repo.total_size == "976.56 KB"
        assert repo.total_size_source == "compact_stats"
