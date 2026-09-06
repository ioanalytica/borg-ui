"""Tests for revision 8f2a4c6e1d3b (repository size source, borg last_modified)."""

from alembic import command
import pytest
from sqlalchemy import inspect

from app.database.db_upgrade import _alembic_config, _engine

REVISION = "8f2a4c6e1d3b"
PREVIOUS = "7de0064b0d99"


def _migrate(url, target, *, down=False):
    engine = _engine(url)
    config = _alembic_config(url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, target)
        connection.commit()
    engine.dispose()


def _columns(url):
    engine = _engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("repositories")}
    finally:
        engine.dispose()


@pytest.mark.unit
def test_upgrade_adds_and_downgrade_drops_the_columns(tmp_path):
    url = f"sqlite:///{tmp_path / 'borg.db'}"
    _migrate(url, PREVIOUS)
    assert not {"total_size_source", "borg_last_modified"} & _columns(url)

    _migrate(url, REVISION)
    assert {"total_size_source", "borg_last_modified"} <= _columns(url)

    _migrate(url, PREVIOUS, down=True)
    assert not {"total_size_source", "borg_last_modified"} & _columns(url)


@pytest.mark.unit
def test_merge_revision_joins_the_ssh_host_key_branch():
    """8f2a4c6e1d3b keeps its original parent (installs applied it there);
    c3d5e7f9a1b2 joins it with the SSH host-key branch so the graph has one
    head and an install at 8f2a4c6e1d3b still receives the host-key columns."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert script.get_revision(REVISION).down_revision == "7de0064b0d99"
    merge = script.get_revision("c3d5e7f9a1b2")
    assert set(merge.down_revision) == {REVISION, "b7e1a3c95d84"}
