"""Every Alembic revision comes with a test.

A test module claims a revision by declaring ``REVISION = "<id>"``. A new
revision should be tested from the releases that do not have it yet, with
the `upgraded_from` fixture (tests/migrations/conftest.py) and rows of its
own; test_backup_plan_restore_check_after_upgrade.py is an example.
"""

import re
import shutil
from pathlib import Path

from alembic.script import ScriptDirectory

from tests.migrations.upgrade_paths import REPO_ROOT

ALEMBIC_DIR = REPO_ROOT / "app" / "database" / "alembic"
TESTS_DIR = REPO_ROOT / "tests"
DECLARATION = re.compile(
    r"""^REVISION\s*(?::\s*str)?\s*=\s*["']([0-9a-f]+)["']""", re.MULTILINE
)

# Revisions that shipped before a test was required. The list only shrinks:
# a revision that gets a test must be removed from it.
WITHOUT_TEST = frozenset(
    {
        "a1b2c3d4e5f6",  # add licensing license_key
        "a1c9f4d27b60",  # add ssh_connections known_host_key
        "a3d1e7b4c9f2",  # add availability schedule modes
        "a4c8e2f6b1d9",  # add repository_storage sftp ssh key
        "b1e2f3a4c5d6",  # add operations and archives
        "b2c3d4e5f6a7",  # add licensing trial_features_used
        "b7e1a3c95d84",  # add ssh_connections host key TOFU flag
        "b8c9d0e1f2a3",  # add operation backup details
        "b9d2c5e7f1a4",  # add agent_machines timezone
        "c2d3e4f5a6b7",  # add history index excludes
        "c3d5e7f9a1b2",  # merge: repository size source + SSH host keys
        "c5d6e7f8a9b0",  # add prune comparison lost size
        "c7e4f8a1d2b3",  # add availability schedule skip history
        "c8e1f4a7b2d9",  # restore plan links lost to the cascade
        "d3e4f5a6b7c8",  # add archive history attempts
        "d5e6f7a8b9c0",  # merge: operations index + prune comparisons
        "d6e7f8a9b0c1",  # add repository size samples
        "e1a2b3c4d5f6",  # add operations runner lease
        "e5f6a7b8c9d0",  # add operation wipe and rclone details
        "e7f8a9b0c1d2",  # add prune comparison verdicts
        "f2b3c4d5e6a7",  # re-encrypt transferred passphrases
        "f6c46c665fd3",  # baseline schema
        "f7a8b9c0d1e2",  # add operation restore details
    }
)


def revisions(alembic_dir: Path) -> set[str]:
    return {s.revision for s in ScriptDirectory(str(alembic_dir)).walk_revisions()}


TEST_FUNCTION = re.compile(r"^\s*(?:async\s+)?def test_", re.MULTILINE)


def declared(tests_dir: Path) -> set[str]:
    """Revisions claimed by a test module that has at least one test."""
    found = set()
    for path in tests_dir.rglob("test_*.py"):
        source = path.read_text()
        if TEST_FUNCTION.search(source):
            found.update(DECLARATION.findall(source))
    return found


def untested(alembic_dir: Path, tests_dir: Path) -> set[str]:
    return revisions(alembic_dir) - declared(tests_dir) - WITHOUT_TEST


def test_every_revision_has_a_test():
    missing = untested(ALEMBIC_DIR, TESTS_DIR)
    assert not missing, (
        f"revision(s) without a test: {', '.join(sorted(missing))}. Add a test "
        'module declaring REVISION = "<id>" that upgrades a seeded database with '
        "the upgraded_from fixture (tests/migrations/conftest.py)."
    )


def test_the_list_of_untested_revisions_only_shrinks():
    tested = WITHOUT_TEST & declared(TESTS_DIR)
    assert not tested, (
        f"remove from WITHOUT_TEST, they have tests now: {sorted(tested)}"
    )
    unknown = WITHOUT_TEST - revisions(ALEMBIC_DIR)
    assert not unknown, f"not revisions: {sorted(unknown)}"


def test_a_new_revision_without_a_test_is_flagged(tmp_path):
    alembic_dir = tmp_path / "alembic"
    shutil.copytree(
        ALEMBIC_DIR, alembic_dir, ignore=shutil.ignore_patterns("__pycache__")
    )
    head = ScriptDirectory(str(ALEMBIC_DIR)).get_current_head()
    (alembic_dir / "versions" / "f00dfeed0002_untested.py").write_text(
        f'revision = "f00dfeed0002"\ndown_revision = "{head}"\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
    )

    assert untested(alembic_dir, TESTS_DIR) == {"f00dfeed0002"}


def test_a_declaration_without_a_test_does_not_count(tmp_path):
    (tmp_path / "test_empty.py").write_text('REVISION = "f00dfeed0003"\n')
    (tmp_path / "test_real.py").write_text(
        'REVISION = "f00dfeed0004"\n\n\ndef test_it():\n    pass\n'
    )

    assert declared(tmp_path) == {"f00dfeed0004"}
