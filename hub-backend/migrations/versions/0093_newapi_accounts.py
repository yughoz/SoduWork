"""Nekowork: per-user New API gateway account mapping."""
from alembic import op
import sqlalchemy as sa

revision = "0093_newapi_accounts"
down_revision = "0092_task_text_storage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "nekowork_newapi_account",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("newapi_user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("token_id", sa.Integer(), nullable=True),
        sa.Column("token_name", sa.String(length=128), nullable=True),
        sa.Column("token_key_masked", sa.String(length=64), nullable=True),
        sa.Column("token_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_nekowork_newapi_account_user_id", "nekowork_newapi_account", ["user_id"], unique=True)
    op.create_index("uq_nekowork_newapi_account_username", "nekowork_newapi_account", ["username"], unique=True)


def downgrade():
    op.drop_index("uq_nekowork_newapi_account_username", table_name="nekowork_newapi_account")
    op.drop_index("ix_nekowork_newapi_account_user_id", table_name="nekowork_newapi_account")
    op.drop_table("nekowork_newapi_account")
