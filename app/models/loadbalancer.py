"""Octavia LBaaS v2 models with PENDING_CREATE -> ACTIVE polling transitions."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import gen_id, now_utc
from app.core.database import Base


class LoadBalancer(Base):
    __tablename__ = "load_balancers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    provider: Mapped[str] = mapped_column(String(64), default="amphora")
    flavor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    vip_address: Mapped[str] = mapped_column(String(64), default="")
    vip_subnet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vip_network_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vip_port_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vip_qos_policy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    availability_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Listener(Base):
    __tablename__ = "listeners"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    loadbalancer_id: Mapped[str] = mapped_column(
        ForeignKey("load_balancers.id", ondelete="CASCADE"), index=True
    )
    default_pool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    protocol: Mapped[str] = mapped_column(String(32), default="HTTP")
    protocol_port: Mapped[int] = mapped_column(Integer)
    connection_limit: Mapped[int] = mapped_column(Integer, default=-1)
    timeout_client_data: Mapped[int] = mapped_column(Integer, default=50000)
    timeout_member_connect: Mapped[int] = mapped_column(Integer, default=5000)
    timeout_member_data: Mapped[int] = mapped_column(Integer, default=50000)
    timeout_tcp_inspect: Mapped[int] = mapped_column(Integer, default=0)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    insert_headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Pool(Base):
    __tablename__ = "pools"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    loadbalancer_id: Mapped[str] = mapped_column(
        ForeignKey("load_balancers.id", ondelete="CASCADE"), index=True
    )
    listener_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    healthmonitor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    protocol: Mapped[str] = mapped_column(String(32), default="HTTP")
    lb_algorithm: Mapped[str] = mapped_column(String(32), default="ROUND_ROBIN")
    session_persistence: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class L7Policy(Base):
    """A rule set that diverts matching traffic away from the listener's default pool.

    The policy says *what to do* (redirect, reject, send to another pool); the rules
    hanging off it say *when*. All of a policy's rules must match for it to fire -- they
    are ANDed, which is why an impossible pair simply never matches rather than erroring.
    """

    __tablename__ = "l7policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    listener_id: Mapped[str] = mapped_column(
        ForeignKey("listeners.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32), default="REJECT")
    redirect_pool_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redirect_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    redirect_prefix: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    redirect_http_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Lower runs first; Octavia renumbers the rest when one is inserted or removed.
    position: Mapped[int] = mapped_column(Integer, default=1)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class L7Rule(Base):
    """One condition of an L7 policy."""

    __tablename__ = "l7rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    l7policy_id: Mapped[str] = mapped_column(
        ForeignKey("l7policies.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), default="PATH")
    compare_type: Mapped[str] = mapped_column(String(32), default="STARTS_WITH")
    key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    invert: Mapped[bool] = mapped_column(Boolean, default=False)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Member(Base):
    __tablename__ = "pool_members"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id", ondelete="CASCADE"), index=True)

    address: Mapped[str] = mapped_column(String(64))
    protocol_port: Mapped[int] = mapped_column(Integer)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    backup: Mapped[bool] = mapped_column(Boolean, default=False)
    subnet_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    monitor_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    monitor_port: Mapped[int | None] = mapped_column(Integer, nullable=True)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="NO_MONITOR")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class HealthMonitor(Base):
    __tablename__ = "health_monitors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), default="")
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id", ondelete="CASCADE"), index=True)

    type: Mapped[str] = mapped_column(String(32), default="HTTP")
    delay: Mapped[int] = mapped_column(Integer, default=5)
    timeout: Mapped[int] = mapped_column(Integer, default=3)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    max_retries_down: Mapped[int] = mapped_column(Integer, default=3)
    http_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    url_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_codes: Mapped[str | None] = mapped_column(String(64), nullable=True)

    provisioning_status: Mapped[str] = mapped_column(String(32), default="PENDING_CREATE")
    operating_status: Mapped[str] = mapped_column(String(32), default="OFFLINE")
    admin_state_up: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    transition_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    transition_target: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
