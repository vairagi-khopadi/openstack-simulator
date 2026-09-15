"""On-the-fly rating engine backing CloudKitty.

There is no collector daemon: costs are derived at request time from lazily-accrued
instance counters plus pure SQL age aggregations over the other resources.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Float, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import iso, now_utc, settings
from app.models.compute import Server
from app.models.rating import HashMapEntry
from app.models.loadbalancer import LoadBalancer
from app.models.network import FloatingIP
from app.models.objectstore import ObjectMetadata
from app.models.storage import Volume

# Instance states that bill at the full rate; everything else that still holds
# resources bills at the idle multiplier.
BILLABLE_ACTIVE: tuple[str, ...] = ("ACTIVE", "BUILD", "REBOOT", "HARD_REBOOT", "RESCUE")
BILLABLE_IDLE: tuple[str, ...] = (
    "SHUTOFF",
    "PAUSED",
    "SUSPENDED",
    "SHELVED",
    "SHELVED_OFFLOADED",
    "VERIFY_RESIZE",
    "ERROR",
)

RES_INSTANCE = "instance"
RES_INSTANCE_IDLE = "instance.idle"
RES_VOLUME = "volume.size"
RES_FLOATING_IP = "network.floating.ip"
RES_OBJECT = "object.size"
RES_LOADBALANCER = "network.loadbalancer"


def _hours_since(column: ColumnElement[Any]) -> ColumnElement[Any]:
    """Age of a row in hours, computed by SQLite itself."""
    return (func.julianday("now") - func.julianday(column)) * 24.0


async def accrue(session: AsyncSession, commit: bool = True) -> None:
    """Advance every live instance's active/idle second counters up to now.

    Called on each rating request (and whenever Nova mutates a server), which is what
    lets the simulator bill accurately without a background daemon.
    """
    now = now_utc()
    elapsed = func.max(
        (func.julianday(now) - func.julianday(Server.accounted_at)) * 86400.0, 0.0
    )
    await session.execute(
        update(Server)
        .where(Server.deleted.is_(False), Server.status.in_(BILLABLE_ACTIVE))
        .values(active_seconds=Server.active_seconds + elapsed, accounted_at=now)
    )
    await session.execute(
        update(Server)
        .where(Server.deleted.is_(False), Server.status.in_(BILLABLE_IDLE))
        .values(idle_seconds=Server.idle_seconds + elapsed, accounted_at=now)
    )
    if commit:
        await session.commit()


def instance_hourly_cost(vcpus: int, ram_mb: int) -> float:
    return vcpus * settings.rate_vcpu_hour + (ram_mb / 1024.0) * settings.rate_ram_gb_hour


async def _instance_rows(
    session: AsyncSession, project_id: str | None
) -> list[dict[str, Any]]:
    """(accumulated_seconds / 3600) * unit_cost, aggregated per project in SQL."""
    hourly = (
        Server.allocated_vcpus * settings.rate_vcpu_hour
        + (cast(Server.allocated_ram_mb, Float) / 1024.0) * settings.rate_ram_gb_hour
    )
    stmt = select(
        Server.project_id,
        func.coalesce(func.sum(Server.active_seconds / 3600.0), 0.0),
        func.coalesce(func.sum((Server.active_seconds / 3600.0) * hourly), 0.0),
        func.coalesce(func.sum(Server.idle_seconds / 3600.0), 0.0),
        func.coalesce(
            func.sum(
                (Server.idle_seconds / 3600.0) * hourly * settings.rate_idle_multiplier
            ),
            0.0,
        ),
        func.count(Server.id),
    ).group_by(Server.project_id)
    if project_id:
        stmt = stmt.where(Server.project_id == project_id)

    rows: list[dict[str, Any]] = []
    for pid, active_h, active_cost, idle_h, idle_cost, count in (
        await session.execute(stmt)
    ).all():
        if active_h:
            rows.append(
                {
                    "res_type": RES_INSTANCE,
                    "tenant_id": pid,
                    "qty": round(float(active_h), 6),
                    "rate": round(float(active_cost), 6),
                    "count": int(count),
                }
            )
        if idle_h:
            rows.append(
                {
                    "res_type": RES_INSTANCE_IDLE,
                    "tenant_id": pid,
                    "qty": round(float(idle_h), 6),
                    "rate": round(float(idle_cost), 6),
                    "count": int(count),
                }
            )
    return rows


async def _aged_rows(
    session: AsyncSession,
    project_id: str | None,
    res_type: str,
    model: Any,
    quantity: ColumnElement[Any] | float,
    unit_cost: float,
    where: list[Any],
) -> list[dict[str, Any]]:
    """Generic "quantity x age-in-hours x unit cost" aggregation for non-instance types."""
    hours = _hours_since(model.created_at)
    qty_expr = quantity if not isinstance(quantity, (int, float)) else float(quantity)
    stmt = select(
        model.project_id,
        func.coalesce(func.sum(qty_expr * hours), 0.0),
        func.coalesce(func.sum(qty_expr * hours * unit_cost), 0.0),
        func.count(model.id),
    ).group_by(model.project_id)
    for clause in where:
        stmt = stmt.where(clause)
    if project_id:
        stmt = stmt.where(model.project_id == project_id)

    rows: list[dict[str, Any]] = []
    for pid, qty, rate, count in (await session.execute(stmt)).all():
        if not qty:
            continue
        rows.append(
            {
                "res_type": res_type,
                "tenant_id": pid,
                "qty": round(float(qty), 6),
                "rate": round(float(rate), 6),
                "count": int(count),
            }
        )
    return rows


# CloudKitty's hashmap service names, mapped onto this module's line-item types. A rule
# configured against "compute" has to find the rows it is meant to reprice.
_HASHMAP_SERVICES: dict[str, tuple[str, ...]] = {
    "compute": (RES_INSTANCE, RES_INSTANCE_IDLE),
    "instance": (RES_INSTANCE, RES_INSTANCE_IDLE),
    "volume": (RES_VOLUME,),
    "volume.size": (RES_VOLUME,),
    "network.floating": (RES_FLOATING_IP,),
    "loadbalancer": (RES_LOADBALANCER,),
    "object": (RES_OBJECT,),
    "image": (),
}


async def _apply_hashmap(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Reprice line items according to the configured hashmap rules.

    Rules attached to a *service* apply to every line item of the types that service
    covers. ``flat`` adds its cost per unit of quantity; ``rate`` multiplies what the
    built-in rates already produced -- which is how an operator applies a discount or a
    surcharge without restating every price.

    Nothing configured means nothing changes, so a cloud that never touches the hashmap
    API bills exactly as it did before.
    """
    services = (
        await session.execute(
            select(HashMapEntry).where(HashMapEntry.kind == "service")
        )
    ).scalars().all()
    if not services:
        return rows

    by_id = {service.id: service for service in services}
    mappings = (
        await session.execute(
            select(HashMapEntry).where(
                HashMapEntry.kind == "mapping",
                HashMapEntry.parent_id.in_(list(by_id) or [""]),
            )
        )
    ).scalars().all()
    if not mappings:
        return rows

    for row in rows:
        for mapping in mappings:
            service = by_id.get(mapping.parent_id or "")
            if service is None:
                continue
            if row["res_type"] not in _HASHMAP_SERVICES.get(service.name or "", ()):
                continue
            if mapping.project_id and mapping.project_id != row["tenant_id"]:
                continue
            if mapping.map_type == "rate":
                row["rate"] = round(row["rate"] * mapping.cost, 6)
            else:
                row["rate"] = round(row["rate"] + row["qty"] * mapping.cost, 6)
    return rows


async def collect(
    session: AsyncSession, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Every rated line item across compute, storage, network and object store."""
    await accrue(session)
    rows = await _instance_rows(session, project_id)
    rows += await _aged_rows(
        session,
        project_id,
        RES_VOLUME,
        Volume,
        Volume.size,
        settings.rate_volume_gb_hour,
        [Volume.deleted.is_(False)],
    )
    rows += await _aged_rows(
        session,
        project_id,
        RES_FLOATING_IP,
        FloatingIP,
        1.0,
        settings.rate_floating_ip_hour,
        [FloatingIP.released.is_(False)],
    )
    rows += await _aged_rows(
        session,
        project_id,
        RES_LOADBALANCER,
        LoadBalancer,
        1.0,
        settings.rate_loadbalancer_hour,
        [LoadBalancer.deleted.is_(False)],
    )
    rows += await _aged_rows(
        session,
        project_id,
        RES_OBJECT,
        ObjectMetadata,
        cast(ObjectMetadata.bytes, Float) / (1024.0 * 1024.0 * 1024.0),
        settings.rate_object_gb_hour,
        [],
    )
    return await _apply_hashmap(session, rows)


async def summary(
    session: AsyncSession,
    project_id: str | None = None,
    groupby: list[str] | None = None,
    begin: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """CloudKitty ``/v1/report/summary`` rows, grouped as the client asked."""
    groupby = groupby or []
    rows = await collect(session, project_id)
    begin_s = iso(begin) if begin else None
    end_s = iso(end or now_utc())

    by_type = "res_type" in groupby or "type" in groupby
    by_project = "project_id" in groupby or "tenant_id" in groupby

    buckets: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for row in rows:
        key = (
            row["res_type"] if by_type else None,
            row["tenant_id"] if by_project else None,
        )
        bucket = buckets.setdefault(
            key,
            {
                "begin": begin_s,
                "end": end_s,
                "qty": 0.0,
                "rate": 0.0,
                "res_type": row["res_type"] if by_type else "ALL",
                "tenant_id": row["tenant_id"] if by_project else "ALL",
            },
        )
        bucket["qty"] = round(bucket["qty"] + row["qty"], 6)
        bucket["rate"] = round(bucket["rate"] + row["rate"], 6)
    return list(buckets.values())


async def total_cost(session: AsyncSession, project_id: str | None = None) -> float:
    rows = await collect(session, project_id)
    return round(sum(row["rate"] for row in rows), 6)
