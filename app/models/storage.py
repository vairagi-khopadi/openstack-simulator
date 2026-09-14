"""Cinder block storage models plus the Glance image catalog (metadata only)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import gen_id, now_utc
from app.core.database import Base

# Volume states that keep GB booked against the host storage pool.
VOLUME_STATES_HOLDING_DISK: tuple[str, ...] = (
    "creating",
    "available",
    "attaching",
    "in-use",
    "detaching",
    "reserved",
    "extending",
    "backing-up",
    "error",
)


class VolumeType(Base):
    __tablename__ = "volume_types"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_specs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Volume(Base):
    __tablename__ = "volumes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    size: Mapped[int] = mapped_column(Integer)  # GB
    status: Mapped[str] = mapped_column(String(32), default="creating", index=True)
    volume_type: Mapped[str] = mapped_column(String(64), default="__DEFAULT__")
    availability_zone: Mapped[str] = mapped_column(String(64), default="nova")
    bootable: Mapped[bool] = mapped_column(Boolean, default=False)
    multiattach: Mapped[bool] = mapped_column(Boolean, default=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    replication_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_volid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host: Mapped[str] = mapped_column(String(255), default="")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    attachments: Mapped[list["VolumeAttachment"]] = relationship(
        back_populates="volume", cascade="all, delete-orphan", lazy="selectin"
    )


class VolumeAttachment(Base):
    __tablename__ = "volume_attachments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    volume_id: Mapped[str] = mapped_column(
        ForeignKey("volumes.id", ondelete="CASCADE"), index=True
    )
    server_id: Mapped[str] = mapped_column(String(64), index=True)
    device: Mapped[str] = mapped_column(String(32), default="/dev/vdb")
    host_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attach_mode: Mapped[str] = mapped_column(String(16), default="rw")
    attach_status: Mapped[str] = mapped_column(String(32), default="attached")
    delete_on_termination: Mapped[bool] = mapped_column(Boolean, default=False)
    attached_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    volume: Mapped[Volume] = relationship(back_populates="attachments")


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    volume_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="creating")
    force: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Backup(Base):
    """A volume backup.

    Backups live in object storage on a real cloud, not on the compute node's disks, so
    unlike a volume a backup does not come out of the depletion pool -- only out of the
    project's ``backups`` and ``backup_gigabytes`` quota.
    """

    __tablename__ = "backups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    volume_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="creating")
    container: Mapped[str | None] = mapped_column(String(255), nullable=True)
    availability_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # An incremental backup depends on the full one before it, which is why Cinder
    # refuses to delete a backup that still has children.
    is_incremental: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Image(Base):
    """Glance image catalog entry. Uploaded bytes are discarded; only size is kept."""

    __tablename__ = "images"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    owner: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    visibility: Mapped[str] = mapped_column(String(32), default="public")
    protected: Mapped[bool] = mapped_column(Boolean, default=False)
    os_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    container_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    disk_format: Mapped[str | None] = mapped_column(String(32), nullable=True)
    min_disk: Mapped[int] = mapped_column(Integer, default=0)
    min_ram: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    virtual_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    os_hash_algo: Mapped[str | None] = mapped_column(String(32), nullable=True)
    os_hash_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
