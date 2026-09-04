"""drop archive_changes raw-path indexes

The `path` column is unbounded Text. PostgreSQL caps a B-tree entry at about
a third of a page (~2.7 KB), so indexing the raw path makes the INSERT of a
change row for a sufficiently long archived path fail outright. archive_id
keeps its own index for per-archive reads; the repository-wide exact-path
lookup drives off that FK index and filters path.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-09-04
"""

from alembic import op

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("archive_changes") as batch:
        batch.drop_index("ix_archive_changes_archive_path")
        batch.drop_index("ix_archive_changes_path")


def downgrade() -> None:
    with op.batch_alter_table("archive_changes") as batch:
        batch.create_index("ix_archive_changes_archive_path", ["archive_id", "path"])
        batch.create_index("ix_archive_changes_path", ["path"])
