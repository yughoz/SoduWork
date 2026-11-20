"""Nekowork extension: per-user New API (llm.nekotekno.xyz) gateway account mapping.

Token secrets never live here — the token itself is stored in New API; this table
only remembers the mapping, the masked key, and the ids needed to manage it.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class NewApiAccount(Base):
    __tablename__ = "nekowork_newapi_account"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)  # Hub app_user.id
    newapi_user_id: Mapped[int] = mapped_column(Integer)                        # id user di New API
    username: Mapped[str] = mapped_column(String(64), unique=True)              # nk-{user_id}
    token_id: Mapped[int | None] = mapped_column(Integer, default=None)         # id token aktif
    token_name: Mapped[str | None] = mapped_column(String(128), default=None)   # nekowork-{email}
    token_key_masked: Mapped[str | None] = mapped_column(String(64), default=None)
    token_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AsanaUserCache(Base):
    """Mirror of workspace members from Asana (via Maton); refreshed by admin sync."""
    __tablename__ = "nekowork_asana_user_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_gid: Mapped[str] = mapped_column(String(64), index=True)
    asana_gid: Mapped[str] = mapped_column(String(64), index=True)
    email: Mapped[str] = mapped_column(String(255), default="")   # lowercase for matching
    name: Mapped[str] = mapped_column(String(255), default="")
    photo_url: Mapped[str | None] = mapped_column(String(512), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AsanaProjectCache(Base):
    """Mirror of workspace projects; ai_enabled/keywords/ai_notes are Nekowork-owned gates."""
    __tablename__ = "nekowork_asana_project_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_gid: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    keywords: Mapped[str | None] = mapped_column(String(255), default=None)
    ai_notes: Mapped[str | None] = mapped_column(String(1024), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserIntegration(Base):
    """Hub user ↔ external identity mapping (provider='asana' in v1; unique per provider)."""
    __tablename__ = "nekowork_user_integration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)   # Hub app_user.id
    provider: Mapped[str] = mapped_column(String(32), default="asana")
    external_id: Mapped[str] = mapped_column(String(64))           # asana user gid
    provider_email: Mapped[str | None] = mapped_column(String(255), default=None)
    provider_name: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(16), default="manual")  # auto | manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_nw_user_integration"),)
