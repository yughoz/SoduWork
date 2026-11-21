"""Nekowork: MCP bearer tokens (PRD modul_mcp §2)."""
from alembic import op
import sqlalchemy as sa

revision = "0095_mcp_tokens"
down_revision = "0094_asana_nekowork"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nekowork_mcp_token",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("uq_nw_mcp_token_name", "nekowork_mcp_token", ["name"], unique=True)
    op.create_index("uq_nw_mcp_token_hash", "nekowork_mcp_token", ["token_hash"], unique=True)


def downgrade():
    op.drop_index("uq_nw_mcp_token_hash", table_name="nekowork_mcp_token")
    op.drop_index("uq_nw_mcp_token_name", table_name="nekowork_mcp_token")
    op.drop_table("nekowork_mcp_token")
