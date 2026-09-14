"""Octavia Load Balancer v2 (port 9876).

Every object starts PENDING_CREATE / OFFLINE and flips to ACTIVE / ONLINE once its stored
10-60s transition deadline passes -- the same stateless polling pattern used elsewhere.

The router carries relative paths so ``main.py`` can mount it at both ``/v2/lbaas`` and
``/v2.0/lbaas``, which is what different client generations ask for.
"""
from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, iso, now_utc, settle_transition, transition_deadline
from app.core.database import get_session
from app.core.pagination import collection_links, page_request, paginate
from app.core.middleware import AuthContext, OSPayload, body_object, fault, require
from app.models.loadbalancer import (
    HealthMonitor,
    L7Policy,
    L7Rule,
    Listener,
    LoadBalancer,
    Member,
    Pool,
)
from app.models.network import Network, Subnet
from app.services.networking import AddressPoolExhausted, create_port_record

SERVICE = "octavia"
router = APIRouter()
versions_router = APIRouter()
auth_dep = require(SERVICE)

ONLINE_STATUSES = {"ACTIVE": "ONLINE"}


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class LoadBalancerPayload(OSPayload):
    name: str = ""
    description: str = ""
    vip_subnet_id: str | None = None
    vip_network_id: str | None = None
    vip_address: str | None = None
    admin_state_up: bool = True
    provider: str = "amphora"
    flavor_id: str | None = None
    availability_zone: str | None = None
    tags: list[str] = Field(default_factory=list)


class ListenerPayload(OSPayload):
    loadbalancer_id: str
    protocol: str = "HTTP"
    protocol_port: int
    name: str = ""
    description: str = ""
    default_pool_id: str | None = None
    connection_limit: int = -1
    admin_state_up: bool = True
    tags: list[str] = Field(default_factory=list)


class PoolPayload(OSPayload):
    protocol: str = "HTTP"
    lb_algorithm: str = "ROUND_ROBIN"
    loadbalancer_id: str | None = None
    listener_id: str | None = None
    name: str = ""
    description: str = ""
    session_persistence: dict[str, Any] | None = None
    admin_state_up: bool = True
    tags: list[str] = Field(default_factory=list)


class MemberPayload(OSPayload):
    address: str
    protocol_port: int
    name: str = ""
    weight: int = 1
    subnet_id: str | None = None
    backup: bool = False
    monitor_address: str | None = None
    monitor_port: int | None = None
    admin_state_up: bool = True
    tags: list[str] = Field(default_factory=list)


class HealthMonitorPayload(OSPayload):
    pool_id: str
    type: str = "HTTP"
    delay: int = 5
    timeout: int = 3
    max_retries: int = 3
    max_retries_down: int = 3
    http_method: str | None = "GET"
    url_path: str | None = "/"
    expected_codes: str | None = "200"
    name: str = ""
    admin_state_up: bool = True


# --------------------------------------------------------------------------------------
# Transition handling
# --------------------------------------------------------------------------------------


def resolve(entity: Any) -> Any:
    """PENDING_CREATE -> ACTIVE (and OFFLINE -> ONLINE) once the deadline elapses."""
    target = settle_transition(entity, "provisioning_status")
    if target:
        if isinstance(entity, Member):
            entity.operating_status = "NO_MONITOR" if target == "ACTIVE" else "OFFLINE"
        else:
            entity.operating_status = ONLINE_STATUSES.get(target, "OFFLINE")
        entity.updated_at = now_utc()
    return entity


def _pending(target: str = "ACTIVE") -> dict[str, Any]:
    return {
        "provisioning_status": "PENDING_CREATE",
        "operating_status": "OFFLINE",
        "transition_until": transition_deadline(),
        "transition_target": target,
    }


# --------------------------------------------------------------------------------------
# Serialisers
# --------------------------------------------------------------------------------------


def lb_dict(lb: LoadBalancer, listener_ids: list[str], pool_ids: list[str]) -> dict[str, Any]:
    return {
        "id": lb.id,
        "name": lb.name,
        "description": lb.description,
        "project_id": lb.project_id,
        "tenant_id": lb.project_id,
        "provisioning_status": lb.provisioning_status,
        "operating_status": lb.operating_status,
        "admin_state_up": lb.admin_state_up,
        "provider": lb.provider,
        "flavor_id": lb.flavor_id,
        "vip_address": lb.vip_address,
        "vip_subnet_id": lb.vip_subnet_id,
        "vip_network_id": lb.vip_network_id,
        "vip_port_id": lb.vip_port_id,
        "vip_qos_policy_id": lb.vip_qos_policy_id,
        "availability_zone": lb.availability_zone,
        "listeners": [{"id": i} for i in listener_ids],
        "pools": [{"id": p} for p in pool_ids],
        "tags": list(lb.tags or []),
        "created_at": iso(lb.created_at),
        "updated_at": iso(lb.updated_at),
    }


def listener_dict(listener: Listener) -> dict[str, Any]:
    return {
        "id": listener.id,
        "name": listener.name,
        "description": listener.description,
        "project_id": listener.project_id,
        "tenant_id": listener.project_id,
        "loadbalancers": [{"id": listener.loadbalancer_id}],
        "default_pool_id": listener.default_pool_id,
        "protocol": listener.protocol,
        "protocol_port": listener.protocol_port,
        "connection_limit": listener.connection_limit,
        "timeout_client_data": listener.timeout_client_data,
        "timeout_member_connect": listener.timeout_member_connect,
        "timeout_member_data": listener.timeout_member_data,
        "timeout_tcp_inspect": listener.timeout_tcp_inspect,
        "provisioning_status": listener.provisioning_status,
        "operating_status": listener.operating_status,
        "admin_state_up": listener.admin_state_up,
        "insert_headers": dict(listener.insert_headers or {}),
        "l7policies": [],
        "sni_container_refs": [],
        "default_tls_container_ref": None,
        "tags": list(listener.tags or []),
        "created_at": iso(listener.created_at),
        "updated_at": iso(listener.updated_at),
    }


def pool_dict(pool: Pool, member_ids: list[str]) -> dict[str, Any]:
    return {
        "id": pool.id,
        "name": pool.name,
        "description": pool.description,
        "project_id": pool.project_id,
        "tenant_id": pool.project_id,
        "loadbalancers": [{"id": pool.loadbalancer_id}],
        "listeners": [{"id": pool.listener_id}] if pool.listener_id else [],
        "members": [{"id": m} for m in member_ids],
        "healthmonitor_id": pool.healthmonitor_id,
        "protocol": pool.protocol,
        "lb_algorithm": pool.lb_algorithm,
        "session_persistence": pool.session_persistence,
        "provisioning_status": pool.provisioning_status,
        "operating_status": pool.operating_status,
        "admin_state_up": pool.admin_state_up,
        "tags": list(pool.tags or []),
        "created_at": iso(pool.created_at),
        "updated_at": iso(pool.updated_at),
    }


def member_dict(member: Member) -> dict[str, Any]:
    return {
        "id": member.id,
        "name": member.name,
        "project_id": member.project_id,
        "tenant_id": member.project_id,
        "address": member.address,
        "protocol_port": member.protocol_port,
        "weight": member.weight,
        "backup": member.backup,
        "subnet_id": member.subnet_id,
        "monitor_address": member.monitor_address,
        "monitor_port": member.monitor_port,
        "provisioning_status": member.provisioning_status,
        "operating_status": member.operating_status,
        "admin_state_up": member.admin_state_up,
        "tags": list(member.tags or []),
        "created_at": iso(member.created_at),
        "updated_at": iso(member.updated_at),
    }


def monitor_dict(monitor: HealthMonitor) -> dict[str, Any]:
    return {
        "id": monitor.id,
        "name": monitor.name,
        "project_id": monitor.project_id,
        "tenant_id": monitor.project_id,
        "pools": [{"id": monitor.pool_id}],
        "type": monitor.type,
        "delay": monitor.delay,
        "timeout": monitor.timeout,
        "max_retries": monitor.max_retries,
        "max_retries_down": monitor.max_retries_down,
        "http_method": monitor.http_method,
        "url_path": monitor.url_path,
        "expected_codes": monitor.expected_codes,
        "provisioning_status": monitor.provisioning_status,
        "operating_status": monitor.operating_status,
        "admin_state_up": monitor.admin_state_up,
        "tags": list(monitor.tags or []),
        "created_at": iso(monitor.created_at),
        "updated_at": iso(monitor.updated_at),
    }


async def _get_lb(session: AsyncSession, lb_id: str) -> LoadBalancer:
    lb = await session.get(LoadBalancer, lb_id)
    if lb is None or lb.deleted:
        raise fault(SERVICE, 404, f"Load Balancer {lb_id} not found.")
    return resolve(lb)


# --------------------------------------------------------------------------------------
# Version discovery (mounted at the service root)
# --------------------------------------------------------------------------------------


@versions_router.get("/", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": "v2.27",
                "status": "SUPPORTED",
                "updated": "2024-01-01T00:00:00Z",
                "links": [{"rel": "self", "href": "/v2"}],
            },
            {
                "id": "v2.0",
                "status": "CURRENT",
                "updated": "2024-01-01T00:00:00Z",
                "links": [{"rel": "self", "href": "/v2.0"}],
            },
        ]
    }


# --------------------------------------------------------------------------------------
# Load balancers
# --------------------------------------------------------------------------------------


async def _children(session: AsyncSession, lb_id: str) -> tuple[list[str], list[str]]:
    listeners = list(
        (
            await session.execute(
                select(Listener.id).where(
                    Listener.loadbalancer_id == lb_id, Listener.deleted.is_(False)
                )
            )
        ).scalars().all()
    )
    pools = list(
        (
            await session.execute(
                select(Pool.id).where(Pool.loadbalancer_id == lb_id, Pool.deleted.is_(False))
            )
        ).scalars().all()
    )
    return listeners, pools


@router.get("/loadbalancers")
async def list_loadbalancers(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(LoadBalancer).where(LoadBalancer.deleted.is_(False))
    if "name" in request.query_params:
        stmt = stmt.where(LoadBalancer.name == request.query_params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, LoadBalancer, page,
        sort_column=LoadBalancer.created_at, descending=False,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        resolve(row)
    await session.commit()
    result = []
    for row in rows:
        listeners, pools = await _children(session, row.id)
        result.append(lb_dict(row, listeners, pools))
    return {
        "loadbalancers": result,
        **collection_links(request, "loadbalancers", rows, page),
    }


@router.post("/loadbalancers", status_code=201)
async def create_loadbalancer(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = LoadBalancerPayload(**body_object(SERVICE, body, "loadbalancer"))
    vip_address = payload.vip_address
    vip_port_id: str | None = None
    network_id = payload.vip_network_id

    if payload.vip_subnet_id:
        subnet = await session.get(Subnet, payload.vip_subnet_id)
        if subnet is None:
            raise fault(SERVICE, 404, f"Subnet {payload.vip_subnet_id} not found.")
        network_id = subnet.network_id
    if network_id and not vip_address:
        network = await session.get(Network, network_id)
        if network is not None:
            try:
                port = await create_port_record(
                    session,
                    network,
                    auth.project_id,
                    device_owner="Octavia",
                    name=f"octavia-lb-vip-{payload.name or 'lb'}",
                )
            except AddressPoolExhausted as exc:
                raise fault(SERVICE, 409, f"Cannot allocate a VIP address: {exc}")
            await session.flush()
            vip_address = port.ip_address
            vip_port_id = port.id

    lb = LoadBalancer(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        project_id=auth.project_id,
        admin_state_up=payload.admin_state_up,
        provider=payload.provider,
        flavor_id=payload.flavor_id,
        vip_address=vip_address or "",
        vip_subnet_id=payload.vip_subnet_id,
        vip_network_id=network_id,
        vip_port_id=vip_port_id,
        availability_zone=payload.availability_zone,
        tags=payload.tags,
        **_pending(),
    )
    session.add(lb)
    await session.commit()
    return {"loadbalancer": lb_dict(lb, [], [])}


@router.get("/loadbalancers/{lb_id}")
async def get_loadbalancer(
    lb_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    lb = await _get_lb(session, lb_id)
    await session.commit()
    listeners, pools = await _children(session, lb.id)
    return {"loadbalancer": lb_dict(lb, listeners, pools)}


@router.put("/loadbalancers/{lb_id}")
async def update_loadbalancer(
    lb_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    lb = await _get_lb(session, lb_id)
    for key, value in body_object(SERVICE, body, "loadbalancer").items():
        if key in ("name", "description", "admin_state_up", "tags"):
            setattr(lb, key, value)
    lb.updated_at = now_utc()
    await session.commit()
    listeners, pools = await _children(session, lb.id)
    return {"loadbalancer": lb_dict(lb, listeners, pools)}


@router.delete("/loadbalancers/{lb_id}", status_code=204)
async def delete_loadbalancer(
    lb_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    lb = await _get_lb(session, lb_id)
    cascade = request.query_params.get("cascade", "false").lower() == "true"
    listeners, pools = await _children(session, lb.id)
    if (listeners or pools) and not cascade:
        raise fault(
            SERVICE,
            409,
            f"Load Balancer {lb_id} has children and cascade is false.",
        )
    for listener_id in listeners:
        listener = await session.get(Listener, listener_id)
        if listener:
            listener.deleted = True
    for pool_id in pools:
        pool = await session.get(Pool, pool_id)
        if pool:
            pool.deleted = True
    lb.deleted = True
    lb.provisioning_status = "DELETED"
    lb.operating_status = "OFFLINE"
    lb.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


@router.get("/loadbalancers/{lb_id}/status")
async def loadbalancer_status(
    lb_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The nested status tree used by ``openstack loadbalancer status show``."""
    lb = await _get_lb(session, lb_id)
    listener_rows = (
        await session.execute(
            select(Listener).where(
                Listener.loadbalancer_id == lb.id, Listener.deleted.is_(False)
            )
        )
    ).scalars().all()
    pool_rows = (
        await session.execute(
            select(Pool).where(Pool.loadbalancer_id == lb.id, Pool.deleted.is_(False))
        )
    ).scalars().all()
    for row in (*listener_rows, *pool_rows):
        resolve(row)

    pools_by_listener: dict[str | None, list[Pool]] = {}
    for pool in pool_rows:
        pools_by_listener.setdefault(pool.listener_id, []).append(pool)

    async def _pool_status(pool: Pool) -> dict[str, Any]:
        members = (
            await session.execute(
                select(Member).where(Member.pool_id == pool.id, Member.deleted.is_(False))
            )
        ).scalars().all()
        for member in members:
            resolve(member)
        monitor = (
            await session.execute(
                select(HealthMonitor).where(
                    HealthMonitor.pool_id == pool.id, HealthMonitor.deleted.is_(False)
                )
            )
        ).scalars().first()
        if monitor:
            resolve(monitor)
        return {
            "id": pool.id,
            "name": pool.name,
            "provisioning_status": pool.provisioning_status,
            "operating_status": pool.operating_status,
            "health_monitor": (
                {
                    "id": monitor.id,
                    "type": monitor.type,
                    "provisioning_status": monitor.provisioning_status,
                    "operating_status": monitor.operating_status,
                }
                if monitor
                else None
            ),
            "members": [
                {
                    "id": m.id,
                    "address": m.address,
                    "protocol_port": m.protocol_port,
                    "provisioning_status": m.provisioning_status,
                    "operating_status": m.operating_status,
                }
                for m in members
            ],
        }

    listeners_status = []
    for listener in listener_rows:
        listeners_status.append(
            {
                "id": listener.id,
                "name": listener.name,
                "provisioning_status": listener.provisioning_status,
                "operating_status": listener.operating_status,
                "pools": [
                    await _pool_status(p) for p in pools_by_listener.get(listener.id, [])
                ],
            }
        )
    orphan_pools = [await _pool_status(p) for p in pools_by_listener.get(None, [])]
    await session.commit()
    return {
        "statuses": {
            "loadbalancer": {
                "id": lb.id,
                "name": lb.name,
                "operating_status": lb.operating_status,
                "provisioning_status": lb.provisioning_status,
                "listeners": listeners_status,
                "pools": orphan_pools,
            }
        }
    }


# --------------------------------------------------------------------------------------
# Listeners
# --------------------------------------------------------------------------------------


@router.get("/listeners")
async def list_listeners(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session,
        select(Listener).where(Listener.deleted.is_(False)),
        Listener, page, sort_column=Listener.created_at, descending=False,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        resolve(row)
    await session.commit()
    return {
        "listeners": [listener_dict(r) for r in rows],
        **collection_links(request, "listeners", rows, page),
    }


@router.post("/listeners", status_code=201)
async def create_listener(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = ListenerPayload(**body_object(SERVICE, body, "listener"))
    await _get_lb(session, payload.loadbalancer_id)
    listener = Listener(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        project_id=auth.project_id,
        loadbalancer_id=payload.loadbalancer_id,
        default_pool_id=payload.default_pool_id,
        protocol=payload.protocol,
        protocol_port=payload.protocol_port,
        connection_limit=payload.connection_limit,
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
        **_pending(),
    )
    session.add(listener)
    await session.commit()
    return {"listener": listener_dict(listener)}


@router.get("/listeners/{listener_id}")
async def get_listener(
    listener_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    listener = await session.get(Listener, listener_id)
    if listener is None or listener.deleted:
        raise fault(SERVICE, 404, f"Listener {listener_id} not found.")
    resolve(listener)
    await session.commit()
    return {"listener": listener_dict(listener)}


@router.put("/listeners/{listener_id}")
async def update_listener(
    listener_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    listener = await session.get(Listener, listener_id)
    if listener is None or listener.deleted:
        raise fault(SERVICE, 404, f"Listener {listener_id} not found.")
    for key, value in body_object(SERVICE, body, "listener").items():
        if key in ("name", "description", "admin_state_up", "connection_limit",
                   "default_pool_id", "tags"):
            setattr(listener, key, value)
    listener.updated_at = now_utc()
    await session.commit()
    return {"listener": listener_dict(listener)}


@router.delete("/listeners/{listener_id}", status_code=204)
async def delete_listener(
    listener_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    listener = await session.get(Listener, listener_id)
    if listener is None or listener.deleted:
        raise fault(SERVICE, 404, f"Listener {listener_id} not found.")
    listener.deleted = True
    listener.provisioning_status = "DELETED"
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Pools
# --------------------------------------------------------------------------------------


@router.get("/pools")
async def list_pools(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session,
        select(Pool).where(Pool.deleted.is_(False)),
        Pool, page, sort_column=Pool.created_at, descending=False,
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        resolve(row)
    await session.commit()
    result = []
    for pool in rows:
        members = list(
            (
                await session.execute(
                    select(Member.id).where(
                        Member.pool_id == pool.id, Member.deleted.is_(False)
                    )
                )
            ).scalars().all()
        )
        result.append(pool_dict(pool, members))
    return {"pools": result, **collection_links(request, "pools", rows, page)}


@router.post("/pools", status_code=201)
async def create_pool(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = PoolPayload(**body_object(SERVICE, body, "pool"))
    lb_id = payload.loadbalancer_id
    if not lb_id and payload.listener_id:
        listener = await session.get(Listener, payload.listener_id)
        if listener is None:
            raise fault(SERVICE, 404, f"Listener {payload.listener_id} not found.")
        lb_id = listener.loadbalancer_id
    if not lb_id:
        raise fault(SERVICE, 400, "A pool requires a loadbalancer_id or listener_id.")
    await _get_lb(session, lb_id)

    pool = Pool(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        project_id=auth.project_id,
        loadbalancer_id=lb_id,
        listener_id=payload.listener_id,
        protocol=payload.protocol,
        lb_algorithm=payload.lb_algorithm,
        session_persistence=payload.session_persistence,
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
        **_pending(),
    )
    session.add(pool)
    if payload.listener_id:
        listener = await session.get(Listener, payload.listener_id)
        if listener is not None:
            listener.default_pool_id = pool.id
    await session.commit()
    return {"pool": pool_dict(pool, [])}


@router.get("/pools/{pool_id}")
async def get_pool(
    pool_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    pool = await session.get(Pool, pool_id)
    if pool is None or pool.deleted:
        raise fault(SERVICE, 404, f"Pool {pool_id} not found.")
    resolve(pool)
    await session.commit()
    members = list(
        (
            await session.execute(
                select(Member.id).where(Member.pool_id == pool.id, Member.deleted.is_(False))
            )
        ).scalars().all()
    )
    return {"pool": pool_dict(pool, members)}


@router.put("/pools/{pool_id}")
async def update_pool(
    pool_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    pool = await session.get(Pool, pool_id)
    if pool is None or pool.deleted:
        raise fault(SERVICE, 404, f"Pool {pool_id} not found.")
    for key, value in body_object(SERVICE, body, "pool").items():
        if key in ("name", "description", "admin_state_up", "lb_algorithm",
                   "session_persistence", "tags"):
            setattr(pool, key, value)
    pool.updated_at = now_utc()
    await session.commit()
    return {"pool": pool_dict(pool, [])}


@router.delete("/pools/{pool_id}", status_code=204)
async def delete_pool(
    pool_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    pool = await session.get(Pool, pool_id)
    if pool is None or pool.deleted:
        raise fault(SERVICE, 404, f"Pool {pool_id} not found.")
    pool.deleted = True
    pool.provisioning_status = "DELETED"
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------------------


@router.get("/pools/{pool_id}/members")
async def list_members(
    pool_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Member).where(Member.pool_id == pool_id, Member.deleted.is_(False))
        )
    ).scalars().all()
    for row in rows:
        resolve(row)
    await session.commit()
    return {"members": [member_dict(r) for r in rows]}


@router.post("/pools/{pool_id}/members", status_code=201)
async def create_member(
    pool_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    pool = await session.get(Pool, pool_id)
    if pool is None or pool.deleted:
        raise fault(SERVICE, 404, f"Pool {pool_id} not found.")
    payload = MemberPayload(**body_object(SERVICE, body, "member"))
    member = Member(
        id=gen_id(),
        name=payload.name,
        project_id=auth.project_id,
        pool_id=pool.id,
        address=payload.address,
        protocol_port=payload.protocol_port,
        weight=payload.weight,
        backup=payload.backup,
        subnet_id=payload.subnet_id,
        monitor_address=payload.monitor_address,
        monitor_port=payload.monitor_port,
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
        **_pending(),
    )
    session.add(member)
    await session.commit()
    return {"member": member_dict(member)}


@router.get("/pools/{pool_id}/members/{member_id}")
async def get_member(
    pool_id: str,
    member_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    member = await session.get(Member, member_id)
    if member is None or member.deleted or member.pool_id != pool_id:
        raise fault(SERVICE, 404, f"Member {member_id} not found.")
    resolve(member)
    await session.commit()
    return {"member": member_dict(member)}


@router.put("/pools/{pool_id}/members/{member_id}")
async def update_member(
    pool_id: str,
    member_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    member = await session.get(Member, member_id)
    if member is None or member.deleted or member.pool_id != pool_id:
        raise fault(SERVICE, 404, f"Member {member_id} not found.")
    for key, value in body_object(SERVICE, body, "member").items():
        if key in ("name", "weight", "admin_state_up", "backup", "monitor_address",
                   "monitor_port", "tags"):
            setattr(member, key, value)
    member.updated_at = now_utc()
    await session.commit()
    return {"member": member_dict(member)}


@router.delete("/pools/{pool_id}/members/{member_id}", status_code=204)
async def delete_member(
    pool_id: str,
    member_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    member = await session.get(Member, member_id)
    if member is None or member.deleted or member.pool_id != pool_id:
        raise fault(SERVICE, 404, f"Member {member_id} not found.")
    member.deleted = True
    member.provisioning_status = "DELETED"
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Health monitors
# --------------------------------------------------------------------------------------


@router.get("/healthmonitors")
async def list_health_monitors(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(HealthMonitor).where(HealthMonitor.deleted.is_(False))
        )
    ).scalars().all()
    for row in rows:
        resolve(row)
    await session.commit()
    return {"healthmonitors": [monitor_dict(r) for r in rows]}


@router.post("/healthmonitors", status_code=201)
async def create_health_monitor(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = HealthMonitorPayload(**body_object(SERVICE, body, "healthmonitor"))
    pool = await session.get(Pool, payload.pool_id)
    if pool is None or pool.deleted:
        raise fault(SERVICE, 404, f"Pool {payload.pool_id} not found.")
    monitor = HealthMonitor(
        id=gen_id(),
        name=payload.name,
        project_id=auth.project_id,
        pool_id=pool.id,
        type=payload.type,
        delay=payload.delay,
        timeout=payload.timeout,
        max_retries=payload.max_retries,
        max_retries_down=payload.max_retries_down,
        http_method=payload.http_method,
        url_path=payload.url_path,
        expected_codes=payload.expected_codes,
        admin_state_up=payload.admin_state_up,
        **_pending(),
    )
    session.add(monitor)
    pool.healthmonitor_id = monitor.id
    await session.commit()
    return {"healthmonitor": monitor_dict(monitor)}


@router.get("/healthmonitors/{monitor_id}")
async def get_health_monitor(
    monitor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    monitor = await session.get(HealthMonitor, monitor_id)
    if monitor is None or monitor.deleted:
        raise fault(SERVICE, 404, f"Health Monitor {monitor_id} not found.")
    resolve(monitor)
    await session.commit()
    return {"healthmonitor": monitor_dict(monitor)}


@router.put("/healthmonitors/{monitor_id}")
async def update_health_monitor(
    monitor_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Retune a monitor in place -- the usual reason is a backend that needs longer."""
    monitor = await session.get(HealthMonitor, monitor_id)
    if monitor is None or monitor.deleted:
        raise fault(SERVICE, 404, f"Health Monitor {monitor_id} not found.")
    resolve(monitor)
    payload = body.get("healthmonitor") or {}
    for field in ("name", "delay", "timeout", "max_retries", "max_retries_down",
                  "http_method", "url_path", "expected_codes", "admin_state_up", "tags"):
        if field in payload:
            setattr(monitor, field, payload[field])
    # Octavia refuses a timeout longer than the interval between probes: the next check
    # would start before the previous one gave up.
    if monitor.timeout > monitor.delay:
        raise fault(
            SERVICE,
            400,
            f"Invalid input: timeout ({monitor.timeout}) must not be larger than "
            f"delay ({monitor.delay}).",
        )
    monitor.updated_at = now_utc()
    await session.commit()
    return {"healthmonitor": monitor_dict(monitor)}


@router.delete("/healthmonitors/{monitor_id}", status_code=204)
async def delete_health_monitor(
    monitor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    monitor = await session.get(HealthMonitor, monitor_id)
    if monitor is None or monitor.deleted:
        raise fault(SERVICE, 404, f"Health Monitor {monitor_id} not found.")
    monitor.deleted = True
    monitor.provisioning_status = "DELETED"
    pool = await session.get(Pool, monitor.pool_id)
    if pool is not None:
        pool.healthmonitor_id = None
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------------------


@router.get("/providers")
async def list_providers(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "providers": [
            {"name": "amphora", "description": "Simulated amphora driver"},
            {"name": "octavia", "description": "Deprecated alias for amphora"},
        ]
    }


@router.get("/flavors")
async def list_flavors(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"flavors": []}


# --------------------------------------------------------------------------------------
# L7 policies and rules
# --------------------------------------------------------------------------------------

L7_ACTIONS = ("REJECT", "REDIRECT_TO_POOL", "REDIRECT_TO_URL", "REDIRECT_PREFIX")
L7_RULE_TYPES = (
    "HOST_NAME", "PATH", "FILE_TYPE", "HEADER", "COOKIE", "SSL_CONN_HAS_CERT",
    "SSL_VERIFY_RESULT", "SSL_DN_FIELD",
)
L7_COMPARE_TYPES = ("REGEX", "STARTS_WITH", "ENDS_WITH", "CONTAINS", "EQUAL_TO")
# These carry a key as well as a value -- "the Cookie named X equals Y".
L7_KEYED_TYPES = ("HEADER", "COOKIE", "SSL_DN_FIELD")


class L7PolicyPayload(OSPayload):
    listener_id: str
    name: str = ""
    description: str = ""
    action: str = "REJECT"
    redirect_pool_id: str | None = None
    redirect_url: str | None = None
    redirect_prefix: str | None = None
    redirect_http_code: int | None = None
    position: int | None = None
    admin_state_up: bool = True
    tags: list[str] = Field(default_factory=list)


class L7RulePayload(OSPayload):
    type: str
    compare_type: str
    value: str
    key: str | None = None
    invert: bool = False
    admin_state_up: bool = True
    tags: list[str] = Field(default_factory=list)


def l7policy_dict(policy: L7Policy, rules: list[str]) -> dict[str, Any]:
    return {
        "id": policy.id,
        "name": policy.name,
        "description": policy.description,
        "project_id": policy.project_id,
        "listener_id": policy.listener_id,
        "action": policy.action,
        "redirect_pool_id": policy.redirect_pool_id,
        "redirect_url": policy.redirect_url,
        "redirect_prefix": policy.redirect_prefix,
        "redirect_http_code": policy.redirect_http_code,
        "position": policy.position,
        "provisioning_status": policy.provisioning_status,
        "operating_status": policy.operating_status,
        "admin_state_up": policy.admin_state_up,
        "rules": [{"id": rule_id} for rule_id in rules],
        "tags": list(policy.tags or []),
        "created_at": iso(policy.created_at),
        "updated_at": iso(policy.updated_at),
    }


def l7rule_dict(rule: L7Rule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "project_id": rule.project_id,
        "type": rule.type,
        "compare_type": rule.compare_type,
        "key": rule.key,
        "value": rule.value,
        "invert": rule.invert,
        "provisioning_status": rule.provisioning_status,
        "operating_status": rule.operating_status,
        "admin_state_up": rule.admin_state_up,
        "tags": list(rule.tags or []),
        "created_at": iso(rule.created_at),
        "updated_at": iso(rule.updated_at),
    }


async def _get_listener(session: AsyncSession, listener_id: str) -> Listener:
    listener = await session.get(Listener, listener_id)
    if listener is None or listener.deleted:
        raise fault(SERVICE, 404, f"Listener {listener_id} not found.")
    return resolve(listener)


async def _get_policy(session: AsyncSession, policy_id: str) -> L7Policy:
    policy = await session.get(L7Policy, policy_id)
    if policy is None or policy.deleted:
        raise fault(SERVICE, 404, f"L7Policy {policy_id} not found.")
    return resolve(policy)


async def _get_rule(session: AsyncSession, policy_id: str, rule_id: str) -> L7Rule:
    rule = await session.get(L7Rule, rule_id)
    if rule is None or rule.deleted or rule.l7policy_id != policy_id:
        raise fault(SERVICE, 404, f"L7Rule {rule_id} not found.")
    return resolve(rule)


async def _policy_rules(session: AsyncSession, policy_id: str) -> list[str]:
    return list(
        (
            await session.execute(
                select(L7Rule.id).where(
                    L7Rule.l7policy_id == policy_id, L7Rule.deleted.is_(False)
                )
            )
        ).scalars().all()
    )


async def _renumber(session: AsyncSession, listener_id: str) -> None:
    """Close the gaps in a listener's policy positions, as Octavia does on every change."""
    policies = (
        await session.execute(
            select(L7Policy)
            .where(L7Policy.listener_id == listener_id, L7Policy.deleted.is_(False))
            .order_by(L7Policy.position, L7Policy.created_at)
        )
    ).scalars().all()
    for index, policy in enumerate(policies, start=1):
        policy.position = index


def _validate_policy_action(payload: L7PolicyPayload) -> None:
    """Each action needs its own companion field, and Octavia refuses without it."""
    if payload.action not in L7_ACTIONS:
        raise fault(
            SERVICE, 400,
            f"Invalid input for action: {payload.action!r} must be one of {list(L7_ACTIONS)}.",
        )
    required = {
        "REDIRECT_TO_POOL": ("redirect_pool_id", payload.redirect_pool_id),
        "REDIRECT_TO_URL": ("redirect_url", payload.redirect_url),
        "REDIRECT_PREFIX": ("redirect_prefix", payload.redirect_prefix),
    }.get(payload.action)
    if required and not required[1]:
        raise fault(
            SERVICE, 400,
            f"Invalid input: action {payload.action} requires {required[0]}.",
        )


@router.get("/l7policies")
async def list_l7policies(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(L7Policy).where(L7Policy.deleted.is_(False))
    if "listener_id" in request.query_params:
        stmt = stmt.where(L7Policy.listener_id == request.query_params["listener_id"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, L7Policy, page, sort_column=L7Policy.created_at, descending=False
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        resolve(row)
    await session.commit()
    return {
        "l7policies": [
            l7policy_dict(p, await _policy_rules(session, p.id)) for p in rows
        ],
        **collection_links(request, "l7policies", rows, page),
    }


@router.post("/l7policies", status_code=201)
async def create_l7policy(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = L7PolicyPayload(**(body.get("l7policy") or {}))
    listener = await _get_listener(session, payload.listener_id)
    _validate_policy_action(payload)
    if payload.action == "REDIRECT_TO_POOL":
        pool = await session.get(Pool, payload.redirect_pool_id)
        if pool is None or pool.deleted:
            raise fault(SERVICE, 404, f"Pool {payload.redirect_pool_id} not found.")

    existing = len(
        (
            await session.execute(
                select(L7Policy.id).where(
                    L7Policy.listener_id == listener.id, L7Policy.deleted.is_(False)
                )
            )
        ).scalars().all()
    )
    policy = L7Policy(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        project_id=auth.project_id,
        listener_id=listener.id,
        action=payload.action,
        redirect_pool_id=payload.redirect_pool_id,
        redirect_url=payload.redirect_url,
        redirect_prefix=payload.redirect_prefix,
        redirect_http_code=payload.redirect_http_code
        or (302 if payload.action.startswith("REDIRECT") else None),
        position=min(payload.position or existing + 1, existing + 1),
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
        **_pending(),
    )
    session.add(policy)
    await session.flush()
    await _renumber(session, listener.id)
    await session.commit()
    return {"l7policy": l7policy_dict(policy, [])}


@router.get("/l7policies/{policy_id}")
async def get_l7policy(
    policy_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    policy = await _get_policy(session, policy_id)
    rules = await _policy_rules(session, policy.id)
    await session.commit()
    return {"l7policy": l7policy_dict(policy, rules)}


@router.put("/l7policies/{policy_id}")
async def update_l7policy(
    policy_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    policy = await _get_policy(session, policy_id)
    payload = body.get("l7policy") or {}
    for field in ("name", "description", "redirect_url", "redirect_prefix",
                  "redirect_pool_id", "redirect_http_code", "admin_state_up", "tags"):
        if field in payload:
            setattr(policy, field, payload[field])
    if "action" in payload:
        if payload["action"] not in L7_ACTIONS:
            raise fault(SERVICE, 400, f"Invalid input for action: {payload['action']!r}.")
        policy.action = payload["action"]
    if "position" in payload:
        policy.position = payload["position"]
        await _renumber(session, policy.listener_id)
    policy.updated_at = now_utc()
    await session.commit()
    rules = await _policy_rules(session, policy.id)
    return {"l7policy": l7policy_dict(policy, rules)}


@router.delete("/l7policies/{policy_id}", status_code=204)
async def delete_l7policy(
    policy_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    policy = await _get_policy(session, policy_id)
    policy.deleted = True
    for rule_id in await _policy_rules(session, policy.id):
        rule = await session.get(L7Rule, rule_id)
        if rule is not None:
            rule.deleted = True
    await session.flush()
    await _renumber(session, policy.listener_id)
    await session.commit()
    return Response(status_code=204)


@router.get("/l7policies/{policy_id}/rules")
async def list_l7rules(
    policy_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_policy(session, policy_id)
    rows = (
        await session.execute(
            select(L7Rule)
            .where(L7Rule.l7policy_id == policy_id, L7Rule.deleted.is_(False))
            .order_by(L7Rule.created_at)
        )
    ).scalars().all()
    for row in rows:
        resolve(row)
    await session.commit()
    return {"rules": [l7rule_dict(r) for r in rows]}


@router.post("/l7policies/{policy_id}/rules", status_code=201)
async def create_l7rule(
    policy_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    policy = await _get_policy(session, policy_id)
    payload = L7RulePayload(**(body.get("rule") or {}))
    if payload.type not in L7_RULE_TYPES:
        raise fault(
            SERVICE, 400,
            f"Invalid input for type: {payload.type!r} must be one of {list(L7_RULE_TYPES)}.",
        )
    if payload.compare_type not in L7_COMPARE_TYPES:
        raise fault(
            SERVICE, 400,
            f"Invalid input for compare_type: {payload.compare_type!r} must be one of "
            f"{list(L7_COMPARE_TYPES)}.",
        )
    # "the header named X contains Y" is meaningless without the name.
    if payload.type in L7_KEYED_TYPES and not payload.key:
        raise fault(
            SERVICE, 400, f"Invalid input: a {payload.type} rule requires a key."
        )

    rule = L7Rule(
        id=gen_id(),
        project_id=auth.project_id,
        l7policy_id=policy.id,
        type=payload.type,
        compare_type=payload.compare_type,
        key=payload.key,
        value=payload.value,
        invert=payload.invert,
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
        **_pending(),
    )
    session.add(rule)
    await session.commit()
    return {"rule": l7rule_dict(rule)}


@router.get("/l7policies/{policy_id}/rules/{rule_id}")
async def get_l7rule(
    policy_id: str,
    rule_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rule = await _get_rule(session, policy_id, rule_id)
    await session.commit()
    return {"rule": l7rule_dict(rule)}


@router.put("/l7policies/{policy_id}/rules/{rule_id}")
async def update_l7rule(
    policy_id: str,
    rule_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rule = await _get_rule(session, policy_id, rule_id)
    payload = body.get("rule") or {}
    for field in ("value", "key", "invert", "admin_state_up", "tags"):
        if field in payload:
            setattr(rule, field, payload[field])
    if "compare_type" in payload:
        if payload["compare_type"] not in L7_COMPARE_TYPES:
            raise fault(SERVICE, 400, "Invalid input for compare_type.")
        rule.compare_type = payload["compare_type"]
    rule.updated_at = now_utc()
    await session.commit()
    return {"rule": l7rule_dict(rule)}


@router.delete("/l7policies/{policy_id}/rules/{rule_id}", status_code=204)
async def delete_l7rule(
    policy_id: str,
    rule_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    rule = await _get_rule(session, policy_id, rule_id)
    rule.deleted = True
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------


def _stats_for(resource_id: str, members: int = 0) -> dict[str, int]:
    """Plausible counters derived from the id, not measurements.

    Nothing proxies traffic here, so these cannot be real. They are deterministic per
    resource so a dashboard polling them sees stable, non-jittering numbers rather than
    figures that jump on every read.
    """
    seed = int(hashlib.sha256(resource_id.encode()).hexdigest()[:12], 16)
    scale = max(members, 1)
    return {
        "active_connections": (seed % 97) * scale,
        "bytes_in": (seed % 1_000_003) * 1024 * scale,
        "bytes_out": (seed % 1_000_033) * 2048 * scale,
        "request_errors": seed % 7,
        "total_connections": (seed % 50_021) * scale,
    }


@router.get("/loadbalancers/{lb_id}/stats")
async def loadbalancer_stats(
    lb_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    lb = await _get_lb(session, lb_id)
    listeners, _ = await _children(session, lb.id)
    return {"stats": _stats_for(lb.id, len(listeners))}


@router.get("/listeners/{listener_id}/stats")
async def listener_stats(
    listener_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    listener = await _get_listener(session, listener_id)
    return {"stats": _stats_for(listener.id)}
