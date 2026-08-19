"""The info dialog's archive list must reach the repository row.

The real sequence this guards (observed live): stats refresh writes
archive_count=1, a backup finishes two minutes later, the info click then shows
two archives in the dialog while the card still says one — because the info
routes fetched the authoritative list and threw it away.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.repository_info_sync import sync_archive_stats_from_info


class FakeDb:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeRepo:
    def __init__(self, borg_version, archive_count=1, last_backup=None):
        self.name = "repo"
        self.borg_version = borg_version
        self.archive_count = archive_count
        self.last_backup = last_backup


@pytest.mark.unit
def test_borg2_count_and_newest_time_are_written():
    """The timestamps are verbatim from the live case — offset-carrying ISO
    strings; the column stores naive UTC."""
    repo = FakeRepo(borg_version=2, archive_count=1)
    db = FakeDb()
    info = {
        "archives": [
            {"name": "k8s-borg", "start": "2026-08-19T20:03:15.388152+02:00"},
            {"name": "k8s-borg", "start": "2026-08-19T21:03:18.624537+02:00"},
        ]
    }

    sync_archive_stats_from_info(repo, info, db)

    assert repo.archive_count == 2
    assert repo.last_backup == datetime(2026, 8, 19, 19, 3, 18, 624537)
    assert db.commits == 1


@pytest.mark.unit
def test_borg1_is_never_touched():
    """Borg 1's repository-level info carries no archive list; the parsed shape
    yields [] even for a populated repository. Writing that back would wipe a
    real count to 0."""
    repo = FakeRepo(borg_version=1, archive_count=5)
    db = FakeDb()

    sync_archive_stats_from_info(repo, {"archives": []}, db)

    assert repo.archive_count == 5
    assert db.commits == 0


@pytest.mark.unit
def test_an_empty_borg2_repository_writes_zero_but_keeps_last_backup():
    repo = FakeRepo(borg_version=2, archive_count=3, last_backup=datetime(2026, 8, 1))
    db = FakeDb()

    sync_archive_stats_from_info(repo, {"archives": []}, db)

    assert repo.archive_count == 0
    assert repo.last_backup == datetime(2026, 8, 1)
    assert db.commits == 1


@pytest.mark.unit
@pytest.mark.parametrize("info", [{}, {"archives": None}, {"archives": "raw"}])
def test_a_response_without_a_list_is_ignored(info):
    repo = FakeRepo(borg_version=2, archive_count=4)
    db = FakeDb()

    sync_archive_stats_from_info(repo, info, db)

    assert repo.archive_count == 4
    assert db.commits == 0


@pytest.mark.unit
def test_unparsable_times_still_update_the_count():
    repo = FakeRepo(borg_version=2, archive_count=0, last_backup=None)
    db = FakeDb()
    info = {"archives": [{"name": "a", "time": "not-a-date"}, {"name": "b"}]}

    sync_archive_stats_from_info(repo, info, db)

    assert repo.archive_count == 2
    assert repo.last_backup is None


@pytest.mark.unit
def test_a_failing_commit_is_swallowed_and_rolled_back():
    """The info response has already been served — a stats write must never
    turn it into a 500."""

    class FailingDb(FakeDb):
        def commit(self):
            raise RuntimeError("database is locked")

    repo = FakeRepo(borg_version=2)
    db = FailingDb()

    sync_archive_stats_from_info(repo, {"archives": []}, db)

    assert db.rollbacks == 1
