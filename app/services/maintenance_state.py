from datetime import datetime
from typing import Optional

import structlog

from app.core.borg_errors import is_borg_warning_exit_code
from app.services.borg2_compact_stats import PRECISION_EXACT, PRECISION_ROUNDED

logger = structlog.get_logger()


def apply_compact_stats(
    job, repository, stats: Optional[dict], *, refresh_size: bool = True
) -> None:
    """Persist `borg compact --stats` output on the job and refresh the
    repository size from it, labelled `compact_stats`: pack file bytes,
    a different quantity from the index sum or the store walk (see
    storage_usage). The value is as exact as Borg printed it
    (`stats["size_precision"]`). On an operation the facade files them
    under `result["stats"]`; a pre-phase-5 legacy row has nowhere to keep
    them and drops the attribute with the instance. `refresh_size=False`
    keeps a size a later measurement already wrote; a rounded figure
    (`size_precision` other than exact) never replaces a measured size."""
    if not stats:
        return
    job.stats = stats
    if not refresh_size:
        return
    from app.services.storage_usage import SOURCE_COMPACT_STATS

    # A compact that ran without BORG_UNITS=raw printed rounded sizes (every
    # compact path sets it; a rounded figure means the Borg build ignored
    # it). Such a figure still beats no size and an older compact's figure,
    # but not a measurement (index sum, store walk, cache stats; a size
    # without a source label predates the label and counts as measured).
    if stats.get("size_precision", PRECISION_ROUNDED) != PRECISION_EXACT and (
        getattr(repository, "total_size", None)
        and getattr(repository, "total_size_source", None) != SOURCE_COMPACT_STATS
    ):
        logger.info(
            "Compact statistics are rounded, measured repository size kept",
            repository_id=getattr(repository, "id", None),
            size_precision=stats.get("size_precision"),
        )
        return
    size = stats.get("repository_size")
    # 0 is a measurement here (an emptied repository), unlike the size
    # fallbacks, where 0 means "could not measure".
    if isinstance(size, int) and size >= 0:
        from app.api.repositories import format_bytes

        repository.total_size = format_bytes(size)
        repository.total_size_source = SOURCE_COMPACT_STATS


def apply_compact_completion(
    job, repository, returncode: int, *, now=None, stats: Optional[dict] = None
) -> None:
    """Apply the shared terminal compact state to a job and repository."""
    completed_at = now or datetime.utcnow()
    job.completed_at = completed_at

    if returncode == 0:
        job.status = "completed"
        job.progress = 100
        job.progress_message = "Compact completed successfully"
        repository.last_compact = completed_at
        apply_compact_stats(job, repository, stats)
        return

    if is_borg_warning_exit_code(returncode):
        job.status = "completed_with_warnings"
        job.progress = 100
        job.progress_message = (
            f"Compact completed with warnings (exit code {returncode})"
        )
        job.error_message = job.progress_message
        repository.last_compact = completed_at
        apply_compact_stats(job, repository, stats)
        return

    job.status = "failed"
    job.error_message = f"Compact failed with exit code {returncode}"
