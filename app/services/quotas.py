"""Per-project quota limits, usage and enforcement.

Three services own quotas for their own resources and there is no central quota service,
so the *policy* lives here once and each API module wires its own endpoints and checks to
it. What a project may use is the stored override if there is one, and the service default
otherwise.

Quota is checked **before** capacity, which is the order a real cloud checks them in and
the order that makes the error legible: an admin who set `--instances 2` wants to be told
they hit their own limit, not that a 256 GB node is full. Capacity still binds after --
a generous quota does not conjure RAM.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, now_utc, settings
from app.models.compute import Server
from app.models.network import (
    FloatingIP,
    Network,
    Port,
    Router,
    SecurityGroup,
    SecurityGroupRule,
    Subnet,
)
from app.models.quota import UNLIMITED, Quota
from app.models.storage import Snapshot, Volume

# --------------------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------------------

# What a project gets with nothing stored for it. These are upstream's own defaults, not
# this node's capacity: a quota that moved when you edited OPENSTACK_SIMULATOR_HOST_RAM_MB
# would not be a quota. Capacity still applies on top, so the effective ceiling is
# whichever is lower.
NOVA_DEFAULTS: dict[str, int] = {
    "instances": 10,
    "cores": 20,
    "ram": 51200,
    "key_pairs": 100,
    "metadata_items": 128,
    "server_groups": 10,
    "server_group_members": 10,
}

CINDER_DEFAULTS: dict[str, int] = {
    "volumes": 10,
    "snapshots": 10,
    "gigabytes": 1000,
    "backups": 10,
    "backup_gigabytes": 1000,
    "per_volume_gigabytes": UNLIMITED,
    "groups": 10,
}

NEUTRON_DEFAULTS: dict[str, int] = {
    "network": 100,
    "subnet": 100,
    "port": 500,
    "router": 10,
    "floatingip": 50,
    "security_group": 10,
    "security_group_rule": 100,
    "rbac_policy": 10,
    "subnetpool": UNLIMITED,
}

DEFAULTS: dict[str, dict[str, int]] = {
    "nova": NOVA_DEFAULTS,
    "cinder": CINDER_DEFAULTS,
    "neutron": NEUTRON_DEFAULTS,
}


class QuotaError(Exception):
    """A project's own limit refuses this request, whatever the node has free."""

    def __init__(self, service: str, resource: str, requested: int, used: int, limit: int):
        self.service = service
        self.resource = resource
        self.requested = requested
        self.used = used
        self.limit = limit
        super().__init__(
            f"Quota exceeded for {resource}: requested {requested}, "
            f"but already used {used} of {limit} {resource}"
        )


# --------------------------------------------------------------------------------------
# Reading and writing limits
# --------------------------------------------------------------------------------------


async def overrides(session: AsyncSession, service: str, project_id: str) -> dict[str, int]:
    """Only what has actually been set for this project."""
    rows = (
        await session.execute(
            select(Quota).where(Quota.service == service, Quota.project_id == project_id)
        )
    ).scalars().all()
    return {row.resource: row.hard_limit for row in rows}


async def limits(session: AsyncSession, service: str, project_id: str) -> dict[str, int]:
    """The effective limits: defaults with this project's overrides applied."""
    return DEFAULTS[service] | await overrides(session, service, project_id)


async def limit_for(
    session: AsyncSession, service: str, project_id: str, resource: str
) -> int:
    """One effective limit, or ``UNLIMITED`` for a resource with no default at all."""
    stored = (
        await session.execute(
            select(Quota.hard_limit).where(
                Quota.service == service,
                Quota.project_id == project_id,
                Quota.resource == resource,
            )
        )
    ).scalar_one_or_none()
    if stored is not None:
        return stored
    return DEFAULTS[service].get(resource, UNLIMITED)


class InvalidLimit(Exception):
    """A limit for a resource this service owns is not an integer."""

    def __init__(self, resource: str, value: Any) -> None:
        super().__init__(resource)
        self.resource = resource
        self.value = value
        self.message = f"Quota limit {value!r} for {resource} is not an integer."


def parse_limits(service: str, payload: dict[str, Any]) -> dict[str, int]:
    """Pull the quota values out of a request body.

    Only keys this service actually owns are read. The clients pad the body with
    ``tenant_id``, ``project_id``, ``id`` and ``force``, and rejecting those as
    "not an integer" is how ``openstack quota set`` ends up failing on a valid request.
    """
    known = DEFAULTS.get(service, {})
    values: dict[str, int] = {}
    for resource, value in payload.items():
        if resource not in known:
            continue
        try:
            values[resource] = int(value)
        except (TypeError, ValueError):
            raise InvalidLimit(resource, value)
    return values


async def set_limits(
    session: AsyncSession, service: str, project_id: str, values: dict[str, int]
) -> dict[str, int]:
    """Store overrides, replacing any already there. Returns the new effective limits."""
    known = DEFAULTS[service]
    for resource, value in values.items():
        if resource not in known:
            continue  # not a quota this service owns; ignored rather than 400
        row = (
            await session.execute(
                select(Quota).where(
                    Quota.service == service,
                    Quota.project_id == project_id,
                    Quota.resource == resource,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                Quota(
                    id=gen_id(),
                    project_id=project_id,
                    service=service,
                    resource=resource,
                    hard_limit=int(value),
                )
            )
        else:
            row.hard_limit = int(value)
            row.updated_at = now_utc()
    await session.flush()
    return await limits(session, service, project_id)


async def clear_limits(session: AsyncSession, service: str, project_id: str) -> None:
    """Drop every override, putting the project back on the defaults."""
    for row in (
        await session.execute(
            select(Quota).where(Quota.service == service, Quota.project_id == project_id)
        )
    ).scalars().all():
        await session.delete(row)
    await session.flush()


# --------------------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Counter:
    """How to count one resource's current usage for a project."""

    model: Any
    column: Any | None = None  # summed when set, rows counted when not
    extra: Callable[[Any], Sequence[Any]] = lambda model: ()


def _live(model: Any) -> Sequence[Any]:
    """Soft-deleted rows do not occupy quota."""
    return (model.deleted.is_(False),) if hasattr(model, "deleted") else ()


_COUNTERS: dict[str, dict[str, Counter]] = {
    "nova": {
        "instances": Counter(Server),
        "cores": Counter(Server, Server.allocated_vcpus),
        "ram": Counter(Server, Server.allocated_ram_mb),
    },
    "cinder": {
        "volumes": Counter(Volume),
        "gigabytes": Counter(Volume, Volume.size),
        "snapshots": Counter(Snapshot),
    },
    "neutron": {
        "network": Counter(Network),
        "subnet": Counter(Subnet),
        "port": Counter(Port),
        "router": Counter(Router),
        "floatingip": Counter(FloatingIP, extra=lambda m: (m.released.is_(False),)),
        "security_group": Counter(SecurityGroup),
        "security_group_rule": Counter(SecurityGroupRule),
    },
}


async def in_use(
    session: AsyncSession, service: str, project_id: str, resource: str
) -> int:
    """Current usage of one resource by one project, or 0 for one we do not track."""
    counter = _COUNTERS.get(service, {}).get(resource)
    if counter is None:
        return 0
    model = counter.model
    aggregate = func.sum(counter.column) if counter.column is not None else func.count()
    stmt = select(aggregate).select_from(model).where(model.project_id == project_id)
    for clause in (*_live(model), *counter.extra(model)):
        stmt = stmt.where(clause)
    return int((await session.scalar(stmt)) or 0)


async def usage(session: AsyncSession, service: str, project_id: str) -> dict[str, int]:
    """Usage for every resource of this service that is tracked."""
    return {
        resource: await in_use(session, service, project_id, resource)
        for resource in _COUNTERS.get(service, {})
    }


async def detail(
    session: AsyncSession, service: str, project_id: str
) -> dict[str, dict[str, int]]:
    """The ``{limit, in_use, reserved}`` shape the detail endpoints return.

    ``reserved`` is always 0: it counts resources held by an in-flight request in a real
    cloud, and nothing here holds a reservation across requests.
    """
    effective = await limits(session, service, project_id)
    used = await usage(session, service, project_id)
    return {
        resource: {"limit": value, "in_use": used.get(resource, 0), "reserved": 0}
        for resource, value in effective.items()
    }


# --------------------------------------------------------------------------------------
# Enforcement
# --------------------------------------------------------------------------------------


async def enforce(
    session: AsyncSession,
    service: str,
    project_id: str,
    resource: str,
    requested: int = 1,
) -> None:
    """Raise :class:`QuotaError` if this request would put the project over its limit."""
    if not settings.enforce_quotas:
        return
    limit = await limit_for(session, service, project_id, resource)
    if limit == UNLIMITED:
        return
    used = await in_use(session, service, project_id, resource)
    if used + requested > limit:
        raise QuotaError(service, resource, requested, used, limit)


async def enforce_all(
    session: AsyncSession,
    service: str,
    project_id: str,
    wanted: dict[str, int],
) -> None:
    """Check several resources, failing on the first that is over.

    Order matters for the message a user sees, so the caller's dict order is honoured:
    a boot checks instances before cores before ram, which is how Nova reports it.
    """
    for resource, requested in wanted.items():
        await enforce(session, service, project_id, resource, requested)
