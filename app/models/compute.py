"""Nova compute models: the bare-metal host, flavors, keypairs and servers."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import gen_id, now_utc
from app.core.database import Base

# Server states that keep vCPU + RAM pinned on the host.
STATES_HOLDING_COMPUTE: tuple[str, ...] = (
    "BUILD",
    "ACTIVE",
    "SHUTOFF",
    "PAUSED",
    "SUSPENDED",
    "REBOOT",
    "HARD_REBOOT",
    "RESIZE",
    "VERIFY_RESIZE",
    "RESCUE",
    "ERROR",
    "SHELVED",
)
# Shelve-offloading hands vCPU/RAM back to the free pool but keeps the root disk.
STATES_HOLDING_DISK: tuple[str, ...] = STATES_HOLDING_COMPUTE + ("SHELVED_OFFLOADED",)


class Hypervisor(Base):
    """A single simulated bare-metal node; capacity is deducted against these totals."""

    __tablename__ = "hypervisors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hypervisor_type: Mapped[str] = mapped_column(String(64), default="QEMU")
    hypervisor_version: Mapped[int] = mapped_column(Integer, default=8002000)
    host_ip: Mapped[str] = mapped_column(String(64), default="127.0.0.1")
    state: Mapped[str] = mapped_column(String(16), default="up")
    status: Mapped[str] = mapped_column(String(16), default="enabled")

    sockets: Mapped[int] = mapped_column(Integer, default=2)
    cores: Mapped[int] = mapped_column(Integer, default=32)
    threads: Mapped[int] = mapped_column(Integer, default=64)
    vcpus: Mapped[int] = mapped_column(Integer, default=64)
    memory_mb: Mapped[int] = mapped_column(Integer, default=262144)
    local_gb: Mapped[int] = mapped_column(Integer, default=4096)

    cpu_allocation_ratio: Mapped[float] = mapped_column(Float, default=3.0)
    ram_allocation_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    disk_allocation_ratio: Mapped[float] = mapped_column(Float, default=1.0)

    reserved_memory_mb: Mapped[int] = mapped_column(Integer, default=512)
    reserved_disk_gb: Mapped[int] = mapped_column(Integer, default=0)
    conntrack_max: Mapped[int] = mapped_column(Integer, default=65536)

    cpu_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Flavor(Base):
    __tablename__ = "flavors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    vcpus: Mapped[int] = mapped_column(Integer)
    ram: Mapped[int] = mapped_column(Integer)  # MB
    disk: Mapped[int] = mapped_column(Integer)  # GB
    ephemeral: Mapped[int] = mapped_column(Integer, default=0)
    swap: Mapped[int] = mapped_column(Integer, default=0)
    rxtx_factor: Mapped[float] = mapped_column(Float, default=1.0)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    extra_specs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Keypair(Base):
    __tablename__ = "keypairs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    public_key: Mapped[str] = mapped_column(String(2048), default="")
    fingerprint: Mapped[str] = mapped_column(String(128), default="")
    type: Mapped[str] = mapped_column(String(16), default="ssh")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ServerGroup(Base):
    """An affinity or anti-affinity group.

    On a real cloud the policy is advice to the scheduler: ``anti-affinity`` keeps members
    on different hosts. There is exactly one host here, so the interesting half is the
    *refusal* -- a second anti-affinity member has nowhere else to go, which is the error
    a multi-node deployment would only produce once it ran out of hosts.
    """

    __tablename__ = "server_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy: Mapped[str] = mapped_column(String(32), default="anti-affinity")
    rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Server(Base):
    """A simulated instance. No QEMU is launched -- only the allocation is booked."""

    __tablename__ = "servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)

    flavor_id: Mapped[str] = mapped_column(ForeignKey("flavors.id"))
    image_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    key_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set from the group_id scheduler hint at boot; membership is the instance's, so a
    # deleted instance leaves the group automatically.
    server_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    host: Mapped[str] = mapped_column(String(255), index=True)
    availability_zone: Mapped[str] = mapped_column(String(64), default="nova")

    status: Mapped[str] = mapped_column(String(32), default="BUILD", index=True)
    task_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vm_state: Mapped[str] = mapped_column(String(32), default="building")
    power_state: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    fault: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Booked capacity, snapshotted at boot so flavor edits never corrupt accounting.
    allocated_vcpus: Mapped[int] = mapped_column(Integer, default=0)
    allocated_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    allocated_disk_gb: Mapped[int] = mapped_column(Integer, default=0)
    overhead_ram_mb: Mapped[int] = mapped_column(Integer, default=0)

    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    user_data: Mapped[str | None] = mapped_column(String(65535), nullable=True)
    config_drive: Mapped[bool] = mapped_column(Boolean, default=False)
    security_group_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Stateless polling: the instance reads BUILD until now() passes this deadline.
    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    launched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Billing accumulators, advanced lazily whenever the row is touched.
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    idle_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    accounted_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    admin_pass: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
