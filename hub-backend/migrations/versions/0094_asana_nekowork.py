"""Nekowork: Asana caches + user identity mappings (PRD modul_asana)."""
from alembic import op
import sqlalchemy as sa

revision = "0094_asana_nekowork"
down_revision = "0093_newapi_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nekowork_asana_user_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("workspace_gid", sa.String(length=64), nullable=False),
        sa.Column("asana_gid", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("photo_url", sa.String(length=512), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_nw_asana_user_ws", "nekowork_asana_user_cache", ["workspace_gid"])
    op.create_index("ix_nw_asana_user_gid", "nekowork_asana_user_cache", ["asana_gid"])

    op.create_table(
        "nekowork_asana_project_cache",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_gid", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("keywords", sa.String(length=255), nullable=True),
        sa.Column("ai_notes", sa.String(length=1024), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_nw_asana_project_gid", "nekowork_asana_project_cache", ["project_gid"], unique=True)

    op.create_table(
        "nekowork_user_integration",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="asana"),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("provider_email", sa.String(length=255), nullable=True),
        sa.Column("provider_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "provider", name="uq_nw_user_integration"),
    )
    op.create_index("ix_nw_user_integration_user", "nekowork_user_integration", ["user_id"])


def downgrade():
    op.drop_index("ix_nw_user_integration_user", table_name="nekowork_user_integration")
    op.drop_table("nekowork_user_integration")
    op.drop_index("uq_nw_asana_project_gid", table_name="nekowork_asana_project_cache")
    op.drop_table("nekowork_asana_project_cache")
    op.drop_index("ix_nw_asana_user_gid", table_name="nekowork_asana_user_cache")
    op.drop_index("ix_nw_asana_user_ws", table_name="nekowork_asana_user_cache")
    op.drop_table("nekowork_asana_user_cache")
