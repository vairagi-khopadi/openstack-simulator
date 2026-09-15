"""Swift object store models -- metadata only, uploaded bytes are never persisted."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import gen_id, now_utc
from app.core.database import Base


class SwiftAccount(Base):
    __tablename__ = "swift_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    project_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Container(Base):
    __tablename__ = "swift_containers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    read_acl: Mapped[str] = mapped_column(String(1024), default="")
    write_acl: Mapped[str] = mapped_column(String(1024), default="")
    storage_policy: Mapped[str] = mapped_column(String(64), default="Policy-0")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    objects: Mapped[list["ObjectMetadata"]] = relationship(
        back_populates="container", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_container_project_name"),
    )


class ObjectMetadata(Base):
    """Size/etag/content-type of a discarded upload -- no bytes hit the local disk."""

    __tablename__ = "swift_objects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    container_id: Mapped[str] = mapped_column(
        ForeignKey("swift_containers.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(1024), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    content_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    etag: Mapped[str] = mapped_column(String(64), default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    # X-Delete-At / X-Delete-After. Swift's reaper removes the object later; a read
    # after this moment already behaves as though it were gone.
    delete_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_modified: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    container: Mapped[Container] = relationship(back_populates="objects")

    __table_args__ = (
        UniqueConstraint("container_id", "name", name="uq_object_container_name"),
    )
