"""CloudKitty Rating v1 (port 8889): usage-cost evaluation computed on request."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, iso, now_utc, settings
from app.core.database import get_session
from app.core.middleware import AuthContext, fault, require
from app.models.compute import Server
from app.models.rating import MAP_TYPES, HashMapEntry
from app.services import rating

SERVICE = "cloudkitty"
router = APIRouter()
auth_dep = require(SERVICE)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise fault(SERVICE, 400, f"Invalid timestamp: {value}")


@router.get("/")
async def index() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": "v1",
                "status": "CURRENT",
                "links": [{"rel": "self", "href": "/v1"}],
            }
        ]
    }


@router.get("/v1")
async def version() -> dict[str, Any]:
    return {
        "version": "1.0",
        "resources": ["report", "rating", "storage", "info"],
    }


@router.get("/v1/report/summary")
async def report_summary(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """(accumulated_seconds / 3600) * unit_cost, aggregated live from SQLite."""
    params = request.query_params
    tenant_id = params.get("tenant_id") or params.get("project_id")
    if params.get("all_tenants", "false").lower() not in ("true", "1"):
        tenant_id = tenant_id or auth.project_id
    groupby = [g for g in params.get("groupby", "").split(",") if g] or [
        "res_type",
        "tenant_id",
    ]
    rows = await rating.summary(
        session,
        project_id=tenant_id,
        groupby=groupby,
        begin=_parse_time(params.get("begin")),
        end=_parse_time(params.get("end")),
    )
    return {"summary": rows}


@router.get("/v1/report/total")
async def report_total(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_id = request.query_params.get("tenant_id") or auth.project_id
    total = await rating.total_cost(session, tenant_id)
    return {
        "begin": None,
        "end": iso(now_utc()),
        "tenant_id": tenant_id,
        "rate": total,
        "total": total,
    }


@router.get("/v1/report/tenants")
async def report_tenants(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> list[str]:
    rows = (
        await session.execute(select(Server.project_id).distinct())
    ).scalars().all()
    return [row for row in rows if row]


@router.get("/v1/rating/modules")
async def rating_modules(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "modules": [
            {
                "module_id": "hashmap",
                "description": "Simulated hashmap rating module",
                "enabled": True,
                "hot_config": False,
                "priority": 1,
            },
            {
                "module_id": "noop",
                "description": "No-op rating module",
                "enabled": False,
                "hot_config": False,
                "priority": 0,
            },
        ]
    }


@router.get("/v1/rating/modules/{module_id}")
async def rating_module(module_id: str, auth: AuthContext = auth_dep) -> dict[str, Any]:
    if module_id not in ("hashmap", "noop"):
        raise fault(SERVICE, 404, f"Module {module_id} not found.")
    return {
        "module_id": module_id,
        "enabled": module_id == "hashmap",
        "hot_config": False,
        "priority": 1 if module_id == "hashmap" else 0,
    }


@router.post("/v1/rating/quote")
async def rating_quote(
    body: dict[str, Any] | None = None, auth: AuthContext = auth_dep
) -> float:
    """Price a hypothetical instance without creating anything."""
    resources = (body or {}).get("resources", [])
    total = 0.0
    for resource in resources:
        desc = resource.get("desc", {})
        total += rating.instance_hourly_cost(
            int(desc.get("vcpus", 1)), int(desc.get("memory", 512))
        ) * float(resource.get("volume", 1))
    return round(total, 6)


@router.get("/v1/info/config")
async def info_config(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "collect": {"period": 3600, "wait_periods": 0, "scope_key": "project_id"},
        "rates": {
            "vcpu_hour": settings.rate_vcpu_hour,
            "ram_gb_hour": settings.rate_ram_gb_hour,
            "idle_multiplier": settings.rate_idle_multiplier,
            "volume_gb_hour": settings.rate_volume_gb_hour,
            "floating_ip_hour": settings.rate_floating_ip_hour,
            "loadbalancer_hour": settings.rate_loadbalancer_hour,
            "object_gb_hour": settings.rate_object_gb_hour,
        },
    }


@router.get("/v1/info/service")
async def info_service(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "services": [
            {"service_id": res, "unit": unit}
            for res, unit in (
                (rating.RES_INSTANCE, "hour"),
                (rating.RES_INSTANCE_IDLE, "hour"),
                (rating.RES_VOLUME, "GiB-hour"),
                (rating.RES_FLOATING_IP, "hour"),
                (rating.RES_LOADBALANCER, "hour"),
                (rating.RES_OBJECT, "GiB-hour"),
            )
        ]
    }


@router.get("/v1/storage/dataframes")
async def dataframes(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """One synthetic dataframe covering the current period."""
    rows = await rating.collect(session, auth.project_id)
    total_servers = (
        await session.execute(
            select(func.count(Server.id)).where(Server.deleted.is_(False))
        )
    ).scalar_one()
    return {
        "total": len(rows),
        "dataframes": [
            {
                "begin": None,
                "end": iso(now_utc()),
                "tenant_id": row["tenant_id"],
                "resources": [
                    {
                        "service": row["res_type"],
                        "volume": row["qty"],
                        "rating": row["rate"],
                        "desc": {"instances": total_servers},
                    }
                ],
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------------------------
# Hashmap rating configuration
# --------------------------------------------------------------------------------------

_BASE = "/v1/rating/module_config/hashmap"


def _entry_dict(entry: HashMapEntry) -> dict[str, Any]:
    """Each kind reports only the fields CloudKitty gives it."""
    if entry.kind == "service":
        return {"service_id": entry.id, "name": entry.name}
    if entry.kind == "field":
        return {"field_id": entry.id, "name": entry.name, "service_id": entry.parent_id}
    if entry.kind == "group":
        return {"group_id": entry.id, "name": entry.name}
    body: dict[str, Any] = {
        "mapping_id" if entry.kind == "mapping" else "threshold_id": entry.id,
        "value": entry.value,
        "cost": str(entry.cost),
        "type": entry.map_type,
        "tenant_id": entry.project_id,
        "group_id": entry.group_id,
    }
    # A rule hangs off exactly one of a service or a field. A field rule always carries
    # the value it matches on; a service rule has nothing to match and so carries none.
    if entry.value is None:
        body["service_id"], body["field_id"] = entry.parent_id, None
    else:
        body["service_id"], body["field_id"] = None, entry.parent_id
    if entry.kind == "threshold":
        body["level"] = str(entry.level) if entry.level is not None else None
    return body


async def _get_entry(session: AsyncSession, entry_id: str, kind: str) -> HashMapEntry:
    entry = await session.get(HashMapEntry, entry_id)
    if entry is None or entry.kind != kind:
        raise fault(SERVICE, 404, f"No such {kind}: {entry_id}")
    return entry


async def _listing(
    session: AsyncSession, kind: str, parent_id: str | None = None
) -> list[HashMapEntry]:
    stmt = select(HashMapEntry).where(HashMapEntry.kind == kind)
    if parent_id is not None:
        stmt = stmt.where(HashMapEntry.parent_id == parent_id)
    return list((await session.execute(stmt.order_by(HashMapEntry.created_at))).scalars())


@router.get(f"{_BASE}/services")
async def list_hashmap_services(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {"services": [_entry_dict(e) for e in await _listing(session, "service")]}


@router.post(f"{_BASE}/services", status_code=201)
async def create_hashmap_service(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    name = (body or {}).get("name")
    if not name:
        raise fault(SERVICE, 400, "A hashmap service requires a name.")
    clash = (
        await session.execute(
            select(HashMapEntry).where(
                HashMapEntry.kind == "service", HashMapEntry.name == name
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise fault(SERVICE, 409, f"Service {name} already exists.")
    entry = HashMapEntry(id=gen_id(), kind="service", name=name)
    session.add(entry)
    await session.commit()
    return _entry_dict(entry)


@router.get(f"{_BASE}/services/{{service_id}}")
async def get_hashmap_service(
    service_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return _entry_dict(await _get_entry(session, service_id, "service"))


@router.delete(f"{_BASE}/services/{{service_id}}", status_code=204)
async def delete_hashmap_service(
    service_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    entry = await _get_entry(session, service_id, "service")
    # Everything hanging off it goes too, or the rules would price nothing.
    for child in (
        await session.execute(
            select(HashMapEntry).where(HashMapEntry.parent_id == entry.id)
        )
    ).scalars().all():
        for grandchild in (
            await session.execute(
                select(HashMapEntry).where(HashMapEntry.parent_id == child.id)
            )
        ).scalars().all():
            await session.delete(grandchild)
        await session.delete(child)
    await session.delete(entry)
    await session.commit()
    return Response(status_code=204)


@router.get(f"{_BASE}/fields")
async def list_hashmap_fields(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    parent = request.query_params.get("service_id")
    return {"fields": [_entry_dict(e) for e in await _listing(session, "field", parent)]}


@router.post(f"{_BASE}/fields", status_code=201)
async def create_hashmap_field(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = body or {}
    if not payload.get("name"):
        raise fault(SERVICE, 400, "A hashmap field requires a name.")
    await _get_entry(session, payload.get("service_id", ""), "service")
    entry = HashMapEntry(
        id=gen_id(),
        kind="field",
        name=payload["name"],
        parent_id=payload["service_id"],
    )
    session.add(entry)
    await session.commit()
    return _entry_dict(entry)


@router.delete(f"{_BASE}/fields/{{field_id}}", status_code=204)
async def delete_hashmap_field(
    field_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    entry = await _get_entry(session, field_id, "field")
    for child in (
        await session.execute(
            select(HashMapEntry).where(HashMapEntry.parent_id == entry.id)
        )
    ).scalars().all():
        await session.delete(child)
    await session.delete(entry)
    await session.commit()
    return Response(status_code=204)


@router.get(f"{_BASE}/groups")
async def list_hashmap_groups(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {"groups": [_entry_dict(e) for e in await _listing(session, "group")]}


@router.post(f"{_BASE}/groups", status_code=201)
async def create_hashmap_group(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    name = (body or {}).get("name")
    if not name:
        raise fault(SERVICE, 400, "A hashmap group requires a name.")
    entry = HashMapEntry(id=gen_id(), kind="group", name=name)
    session.add(entry)
    await session.commit()
    return _entry_dict(entry)


@router.delete(f"{_BASE}/groups/{{group_id}}", status_code=204)
async def delete_hashmap_group(
    group_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    entry = await _get_entry(session, group_id, "group")
    await session.delete(entry)
    await session.commit()
    return Response(status_code=204)


async def _create_rule(
    session: AsyncSession, body: dict[str, Any], kind: str
) -> HashMapEntry:
    payload = body or {}
    service_id, field_id = payload.get("service_id"), payload.get("field_id")
    if bool(service_id) == bool(field_id):
        raise fault(
            SERVICE,
            400,
            f"A {kind} attaches to exactly one of service_id or field_id.",
        )
    parent_id = field_id or service_id
    await _get_entry(session, parent_id, "field" if field_id else "service")

    map_type = payload.get("type", "flat")
    if map_type not in MAP_TYPES:
        raise fault(
            SERVICE, 400, f"Invalid type {map_type!r}: must be one of {list(MAP_TYPES)}."
        )
    # A field rule matches a value; a service rule applies to everything it covers.
    if field_id and payload.get("value") is None:
        raise fault(SERVICE, 400, f"A field {kind} requires a value to match.")

    try:
        cost = float(payload.get("cost", 0))
    except (TypeError, ValueError):
        raise fault(SERVICE, 400, f"Cost {payload.get('cost')!r} is not a number.")

    entry = HashMapEntry(
        id=gen_id(),
        kind=kind,
        parent_id=parent_id,
        value=payload.get("value"),
        cost=cost,
        map_type=map_type,
        level=float(payload["level"]) if payload.get("level") is not None else None,
        project_id=payload.get("tenant_id"),
        group_id=payload.get("group_id"),
    )
    session.add(entry)
    await session.commit()
    return entry


@router.get(f"{_BASE}/mappings")
async def list_hashmap_mappings(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    parent = request.query_params.get("field_id") or request.query_params.get("service_id")
    return {
        "mappings": [_entry_dict(e) for e in await _listing(session, "mapping", parent)]
    }


@router.post(f"{_BASE}/mappings", status_code=201)
async def create_hashmap_mapping(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return _entry_dict(await _create_rule(session, body, "mapping"))


@router.get(f"{_BASE}/mappings/{{mapping_id}}")
async def get_hashmap_mapping(
    mapping_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return _entry_dict(await _get_entry(session, mapping_id, "mapping"))


@router.put(f"{_BASE}/mappings/{{mapping_id}}")
async def update_hashmap_mapping(
    mapping_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entry = await _get_entry(session, mapping_id, "mapping")
    payload = body or {}
    if "cost" in payload:
        try:
            entry.cost = float(payload["cost"])
        except (TypeError, ValueError):
            raise fault(SERVICE, 400, f"Cost {payload['cost']!r} is not a number.")
    if "value" in payload:
        entry.value = payload["value"]
    if "type" in payload:
        if payload["type"] not in MAP_TYPES:
            raise fault(SERVICE, 400, f"Invalid type {payload['type']!r}.")
        entry.map_type = payload["type"]
    await session.commit()
    return _entry_dict(entry)


@router.delete(f"{_BASE}/mappings/{{mapping_id}}", status_code=204)
async def delete_hashmap_mapping(
    mapping_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await session.delete(await _get_entry(session, mapping_id, "mapping"))
    await session.commit()
    return Response(status_code=204)


@router.get(f"{_BASE}/thresholds")
async def list_hashmap_thresholds(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    parent = request.query_params.get("field_id") or request.query_params.get("service_id")
    return {
        "thresholds": [
            _entry_dict(e) for e in await _listing(session, "threshold", parent)
        ]
    }


@router.post(f"{_BASE}/thresholds", status_code=201)
async def create_hashmap_threshold(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if (body or {}).get("level") is None:
        raise fault(SERVICE, 400, "A threshold requires a level.")
    return _entry_dict(await _create_rule(session, body, "threshold"))


@router.delete(f"{_BASE}/thresholds/{{threshold_id}}", status_code=204)
async def delete_hashmap_threshold(
    threshold_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await session.delete(await _get_entry(session, threshold_id, "threshold"))
    await session.commit()
    return Response(status_code=204)
