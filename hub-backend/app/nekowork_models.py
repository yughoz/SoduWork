"""Nekowork extension: per-user New API (llm.nekotekno.xyz) gateway account mapping.

Token secrets never live here — the token itself is stored in New API; this table
only remembers the mapping, the masked key, and the ids needed to manage it.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
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
