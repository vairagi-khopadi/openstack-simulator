"""Neutron networking models: networks, subnets, ports, security groups, floating IPs."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import gen_id, now_utc
from app.core.database import Base


class Network(Base):
    __tablename__ = "networks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    external: Mapped[bool] = mapped_column(Boolean, default=False)
    mtu: Mapped[int] = mapped_column(Integer, default=1450)
    port_security_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    provider_network_type: Mapped[str] = mapped_column(String(32), default="vxlan")
    provider_physical_network: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_segmentation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    availability_zone_hints: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(1024), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    subnets: Mapped[list["Subnet"]] = relationship(
        back_populates="network", cascade="all, delete-orphan", lazy="selectin"
    )


class Subnet(Base):
    __tablename__ = "subnets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    network_id: Mapped[str] = mapped_column(
        ForeignKey("networks.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    cidr: Mapped[str] = mapped_column(String(64))
    # Set when the cidr was carved out of a pool rather than given explicitly; it is
    # what stops the pool being deleted out from under the allocation.
    subnetpool_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip_version: Mapped[int] = mapped_column(Integer, default=4)
    gateway_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enable_dhcp: Mapped[bool] = mapped_column(Boolean, default=True)
    allocation_start: Mapped[str] = mapped_column(String(64))
    allocation_end: Mapped[str] = mapped_column(String(64))
    # Monotonic cursor into the allocation pool -- simulated IPAM without a table scan.
    next_ip_offset: Mapped[int] = mapped_column(Integer, default=0)
    dns_nameservers: Mapped[list[str]] = mapped_column(JSON, default=list)
    host_routes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(1024), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    network: Mapped[Network] = relationship(back_populates="subnets")


class Port(Base):
    __tablename__ = "ports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="")
    network_id: Mapped[str] = mapped_column(
        ForeignKey("networks.id", ondelete="CASCADE"), index=True
    )
    subnet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    mac_address: Mapped[str] = mapped_column(String(32))
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    device_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    device_owner: Mapped[str] = mapped_column(String(64), default="")
    binding_vnic_type: Mapped[str] = mapped_column(String(32), default="normal")
    binding_host_id: Mapped[str] = mapped_column(String(255), default="")
    port_security_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    security_group_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_address_pairs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(String(1024), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class SubnetPool(Base):
    """A pool of address space subnets are carved out of.

    The point of a pool is that a tenant can ask for "a /26" without knowing or caring
    which one, and that two allocations never overlap.
    """

    __tablename__ = "subnet_pools"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    prefixes: Mapped[list[str]] = mapped_column(JSON, default=list)
    default_prefixlen: Mapped[int] = mapped_column(Integer, default=24)
    min_prefixlen: Mapped[int] = mapped_column(Integer, default=8)
    max_prefixlen: Mapped[int] = mapped_column(Integer, default=32)
    ip_version: Mapped[int] = mapped_column(Integer, default=4)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(String(1024), default="")
    address_scope_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Trunk(Base):
    """A parent port carrying several networks, one per VLAN tag.

    An instance gets one vNIC and reaches every subport through it. The rules worth
    modelling are the exclusivity ones: a port can be the parent of only one trunk, a
    subport of only one trunk, and never both at once.
    """

    __tablename__ = "trunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    port_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class SubPort(Base):
    """One network carried on a trunk, identified by its segmentation id."""

    __tablename__ = "subports"
    __table_args__ = (
        UniqueConstraint("trunk_id", "segmentation_id", name="uq_trunk_segmentation"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    trunk_id: Mapped[str] = mapped_column(
        ForeignKey("trunks.id", ondelete="CASCADE"), index=True
    )
    port_id: Mapped[str] = mapped_column(String(64), index=True)
    segmentation_type: Mapped[str] = mapped_column(String(32), default="vlan")
    segmentation_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class SecurityGroup(Base):
    __tablename__ = "security_groups"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    stateful: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    rules: Mapped[list["SecurityGroupRule"]] = relationship(
        back_populates="security_group", cascade="all, delete-orphan", lazy="selectin"
    )


class SecurityGroupRule(Base):
    """Each rule burns exactly one conntrack slot on the host (default cap 65536)."""

    __tablename__ = "security_group_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    security_group_id: Mapped[str] = mapped_column(
        ForeignKey("security_groups.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16), default="ingress")
    ethertype: Mapped[str] = mapped_column(String(16), default="IPv4")
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    port_range_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    port_range_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_ip_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    remote_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    revision_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    security_group: Mapped[SecurityGroup] = relationship(back_populates="rules")


class FloatingIP(Base):
    __tablename__ = "floating_ips"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    floating_network_id: Mapped[str] = mapped_column(String(64), index=True)
    floating_ip_address: Mapped[str] = mapped_column(String(64), unique=True)
    fixed_ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    port_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    router_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="DOWN")
    description: Mapped[str] = mapped_column(String(1024), default="")
    dns_domain: Mapped[str] = mapped_column(String(255), default="")
    dns_name: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    released: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Router(Base):
    """A tenant router.

    Interfaces are not stored here: attaching a subnet creates a Port owned by the
    router (``device_owner='network:router_interface'``), exactly as Neutron does, so
    ``port list --router`` finds them without a second source of truth.
    """

    __tablename__ = "routers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(1024), default="")

    # External gateway: null until `router set --external-gateway` is called.
    external_network_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    external_fixed_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enable_snat: Mapped[bool] = mapped_column(Boolean, default=True)

    distributed: Mapped[bool] = mapped_column(Boolean, default=False)
    ha: Mapped[bool] = mapped_column(Boolean, default=False)
    routes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    availability_zone_hints: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision_number: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
