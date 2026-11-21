"""Nekowork: Titen per-user keys and shared projects."""
import sqlalchemy as sa
from alembic import op

revision = "0096_titen"
down_revision = "0095_mcp_tokens"


def _cols():
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "nekowork_titen_key",
        *_cols(),
        sa.Column("user_id", sa.String(36), index=True),
        sa.Column("key_id", sa.String(64), unique=True),
        sa.Column("key_hash", sa.String(64), unique=True, index=True),
        sa.Column("label", sa.String(120)),
        sa.Column("status", sa.String(16)),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "nekowork_titen_shared_project",
        *_cols(),
        sa.Column("project_gid", sa.String(64), unique=True),
        sa.Column("name", sa.String(120)),
        sa.Column("reference", sa.String(120)),
        sa.Column("created_by", sa.String(36)),
    )
    op.create_table(
        "nekowork_titen_shared_member",
        *_cols(),
        sa.Column("project_row_id", sa.Integer(), index=True),
        sa.Column("user_id", sa.String(36), index=True),
        sa.UniqueConstraint("project_row_id", "user_id", name="uq_nw_titen_shared_member"),
    )


def downgrade() -> None:
    op.drop_table("nekowork_titen_shared_member")
    op.drop_table("nekowork_titen_shared_project")
    op.drop_table("nekowork_titen_key")
