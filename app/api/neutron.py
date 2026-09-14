"""Neutron Networking v2.0 (port 9696): networks, subnets, ports, security groups,
floating IPs -- with simulated IPAM and conntrack accounting."""
from __future__ import annotations

import ipaddress
import random
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, iso_us, now_utc, settings
from app.core.database import get_session
from app.services import quotas as quota_service
from app.core.pagination import (
    collection_links,
    page_request,
    paginate,
    requested_fields,
    trim,
)
from app.core.middleware import AuthContext, OSPayload, body_object, fault, require
from app.models.quota import Quota
from app.models.network import (
    SubPort,
    SubnetPool,
    Trunk,
    FloatingIP,
    Network,
    Port,
    Router,
    SecurityGroup,
    SecurityGroupRule,
    Subnet,
)
from app.services.capacity import CapacityError, check_conntrack_capacity
from app.services.networking import (
    AddressPoolExhausted,
    allocation_pool,
    create_port_record,
    next_free_ip,
)

SERVICE = "neutron"
router = APIRouter()
auth_dep = require(SERVICE)


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class NetworkPayload(OSPayload):
    name: str = ""
    admin_state_up: bool = True
    shared: bool = False
    external: bool = Field(default=False, alias="router:external")
    mtu: int = 1450
    port_security_enabled: bool = True
    description: str = ""
    tenant_id: str | None = None
    project_id: str | None = None


class SubnetPayload(OSPayload):
    network_id: str
    cidr: str | None = None
    name: str = ""
    ip_version: int = 4
    gateway_ip: str | None = None
    enable_dhcp: bool = True
    dns_nameservers: list[str] = Field(default_factory=list)
    host_routes: list[dict[str, Any]] = Field(default_factory=list)
    allocation_pools: list[dict[str, str]] = Field(default_factory=list)
    description: str = ""
    subnetpool_id: str | None = None
    prefixlen: int | None = None


class PortPayload(OSPayload):
    network_id: str
    name: str = ""
    admin_state_up: bool = True
    device_id: str = ""
    device_owner: str = ""
    fixed_ips: list[dict[str, Any]] = Field(default_factory=list)
    security_groups: list[str] = Field(default_factory=list)
    description: str = ""


class SecurityGroupPayload(OSPayload):
    name: str
    description: str = ""
    stateful: bool = True


class SecurityGroupRulePayload(OSPayload):
    security_group_id: str
    direction: str = "ingress"
    ethertype: str = "IPv4"
    protocol: str | None = None
    port_range_min: int | None = None
    port_range_max: int | None = None
    remote_ip_prefix: str | None = None
    remote_group_id: str | None = None
    description: str = ""


class FloatingIPPayload(OSPayload):
    floating_network_id: str
    port_id: str | None = None
    fixed_ip_address: str | None = None
    floating_ip_address: str | None = None
    description: str = ""


# --------------------------------------------------------------------------------------
# Serialisers
# --------------------------------------------------------------------------------------


def network_dict(network: Network, subnet_ids: list[str]) -> dict[str, Any]:
    return {
        "id": network.id,
        "name": network.name,
        "status": network.status,
        "admin_state_up": network.admin_state_up,
        "shared": network.shared,
        "router:external": network.external,
        "is_default": False,
        "mtu": network.mtu,
        "port_security_enabled": network.port_security_enabled,
        "provider:network_type": network.provider_network_type,
        "provider:physical_network": network.provider_physical_network,
        "provider:segmentation_id": network.provider_segmentation_id,
        "subnets": subnet_ids,
        "project_id": network.project_id,
        "tenant_id": network.project_id,
        "availability_zones": ["nova"],
        "availability_zone_hints": list(network.availability_zone_hints or []),
        "description": network.description,
        "tags": list(network.tags or []),
        "revision_number": network.revision_number,
        "ipv4_address_scope": None,
        "ipv6_address_scope": None,
        "l2_adjacency": True,
        "qos_policy_id": None,
        "created_at": iso_us(network.created_at),
        "updated_at": iso_us(network.updated_at),
    }


def subnet_dict(subnet: Subnet) -> dict[str, Any]:
    return {
        "id": subnet.id,
        "name": subnet.name,
        "network_id": subnet.network_id,
        "project_id": subnet.project_id,
        "tenant_id": subnet.project_id,
        "cidr": subnet.cidr,
        "ip_version": subnet.ip_version,
        "gateway_ip": subnet.gateway_ip,
        "enable_dhcp": subnet.enable_dhcp,
        "allocation_pools": [
            {"start": subnet.allocation_start, "end": subnet.allocation_end}
        ],
        "dns_nameservers": list(subnet.dns_nameservers or []),
        "host_routes": list(subnet.host_routes or []),
        "ipv6_address_mode": None,
        "ipv6_ra_mode": None,
        "subnetpool_id": None,
        "service_types": [],
        "description": subnet.description,
        "tags": list(subnet.tags or []),
        "revision_number": subnet.revision_number,
        "created_at": iso_us(subnet.created_at),
        "updated_at": iso_us(subnet.updated_at),
    }


def port_dict(port: Port) -> dict[str, Any]:
    fixed_ips = (
        [{"subnet_id": port.subnet_id, "ip_address": port.ip_address}]
        if port.ip_address
        else []
    )
    return {
        "id": port.id,
        "name": port.name,
        "network_id": port.network_id,
        "project_id": port.project_id,
        "tenant_id": port.project_id,
        "mac_address": port.mac_address,
        "fixed_ips": fixed_ips,
        "status": port.status,
        "admin_state_up": port.admin_state_up,
        "device_id": port.device_id,
        "device_owner": port.device_owner,
        "security_groups": list(port.security_group_ids or []),
        "allowed_address_pairs": list(port.allowed_address_pairs or []),
        "extra_dhcp_opts": [],
        "binding:vnic_type": port.binding_vnic_type,
        "binding:host_id": port.binding_host_id,
        "binding:vif_type": "ovs",
        "binding:vif_details": {"connectivity": "l2", "port_filter": True},
        "binding:profile": {},
        "port_security_enabled": port.port_security_enabled,
        "qos_policy_id": None,
        "description": port.description,
        "tags": list(port.tags or []),
        "revision_number": port.revision_number,
        "created_at": iso_us(port.created_at),
        "updated_at": iso_us(port.updated_at),
    }


def rule_dict(rule: SecurityGroupRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "security_group_id": rule.security_group_id,
        "project_id": rule.project_id,
        "tenant_id": rule.project_id,
        "direction": rule.direction,
        "ethertype": rule.ethertype,
        "protocol": rule.protocol,
        "port_range_min": rule.port_range_min,
        "port_range_max": rule.port_range_max,
        "remote_ip_prefix": rule.remote_ip_prefix,
        "remote_group_id": rule.remote_group_id,
        "remote_address_group_id": None,
        "description": rule.description,
        "normalized_cidr": rule.remote_ip_prefix,
        "revision_number": rule.revision_number,
        "tags": [],
        "created_at": iso_us(rule.created_at),
        "updated_at": iso_us(rule.updated_at),
    }


def security_group_dict(group: SecurityGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "project_id": group.project_id,
        "tenant_id": group.project_id,
        "stateful": group.stateful,
        "shared": False,
        "security_group_rules": [rule_dict(r) for r in group.rules],
        "tags": list(group.tags or []),
        "revision_number": group.revision_number,
        "created_at": iso_us(group.created_at),
        "updated_at": iso_us(group.updated_at),
    }


def floating_ip_dict(fip: FloatingIP) -> dict[str, Any]:
    return {
        "id": fip.id,
        "floating_network_id": fip.floating_network_id,
        "floating_ip_address": fip.floating_ip_address,
        "fixed_ip_address": fip.fixed_ip_address,
        "port_id": fip.port_id,
        "router_id": fip.router_id,
        "status": fip.status,
        "project_id": fip.project_id,
        "tenant_id": fip.project_id,
        "description": fip.description,
        "dns_domain": fip.dns_domain,
        "dns_name": fip.dns_name,
        "port_details": None,
        "qos_policy_id": None,
        "port_forwardings": [],
        "tags": list(fip.tags or []),
        "revision_number": fip.revision_number,
        "created_at": iso_us(fip.created_at),
        "updated_at": iso_us(fip.updated_at),
    }


# --------------------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": "v2.0",
                "status": "CURRENT",
                "links": [
                    {
                        "rel": "self",
                        "href": f"http://{settings.advertise_host}:9696/v2.0/",
                    }
                ],
            }
        ]
    }


@router.get("/v2.0", include_in_schema=False)
@router.get("/v2.0/", include_in_schema=False)
async def version_v2() -> dict[str, Any]:
    return {"version": {"id": "v2.0", "status": "CURRENT"}}


@router.get("/v2.0/extensions")
async def extensions(auth: AuthContext = auth_dep) -> dict[str, Any]:
    names = [
        ("security-group", "security-group"),
        ("router", "router"),
        ("external-net", "external-net"),
        ("port-security", "port-security"),
        ("standard-attr-description", "standard-attr-description"),
        ("standard-attr-tag", "standard-attr-tag"),
        ("dhcp_agent_scheduler", "dhcp_agent_scheduler"),
        ("multi-provider", "multi-provider"),
        ("allowed-address-pairs", "allowed-address-pairs"),
        ("availability_zone", "availability_zone"),
        ("subnet_allocation", "subnet_allocation"),
    ]
    return {
        "extensions": [
            {
                "alias": alias,
                "name": name,
                "description": f"Simulated {name} extension",
                "updated": "2024-01-01T00:00:00Z",
                "links": [],
            }
            for alias, name in names
        ]
    }


@router.get("/v2.0/availability_zones")
async def availability_zones(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "availability_zones": [
            {"state": "available", "resource": "network", "name": "nova"},
            {"state": "available", "resource": "router", "name": "nova"},
        ]
    }


@router.get("/v2.0/quotas")
async def list_quotas(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Every project that has had a quota set on it. Untouched projects are not listed."""
    rows = (await session.execute(select(Quota).where(Quota.service == SERVICE))).scalars()
    projects = sorted({row.project_id for row in rows})
    return {
        "quotas": [
            {**await quota_service.limits(session, SERVICE, project), "project_id": project,
             "tenant_id": project}
            for project in projects
        ]
    }


@router.get("/v2.0/quotas/{project_id}")
async def show_quota(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"quota": await quota_service.limits(session, SERVICE, project_id)}


@router.get("/v2.0/quotas/{project_id}/default")
async def show_default_quota(
    project_id: str, auth: AuthContext = auth_dep
) -> dict[str, Any]:
    return {"quota": dict(quota_service.NEUTRON_DEFAULTS)}


@router.get("/v2.0/quotas/{project_id}/details")
async def show_quota_details(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Limits with usage, as ``openstack quota show --network`` reads them."""
    return {"quota": await quota_service.detail(session, SERVICE, project_id)}


@router.put("/v2.0/quotas/{project_id}")
async def update_quota(
    project_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Set this project's network limits, enforced on the next create."""
    try:
        values = quota_service.parse_limits(SERVICE, body.get("quota") or {})
    except quota_service.InvalidLimit as exc:
        raise fault(SERVICE, 400, exc.message)
    effective = await quota_service.set_limits(session, SERVICE, project_id, values)
    await session.commit()
    return {"quota": effective}


@router.delete("/v2.0/quotas/{project_id}", status_code=204)
async def delete_quota(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await quota_service.clear_limits(session, SERVICE, project_id)
    await session.commit()
    return Response(status_code=204)


async def _enforce(session: AsyncSession, project_id: str, resource: str) -> None:
    """Neutron reports going over as a 409 OverQuota naming the resource."""
    try:
        await quota_service.enforce(session, SERVICE, project_id, resource)
    except quota_service.QuotaError as exc:
        raise fault(
            SERVICE,
            409,
            f"Quota exceeded for resources: ['{exc.resource}'].",
            type="OverQuota",
        )


# --------------------------------------------------------------------------------------
# Tenancy
# --------------------------------------------------------------------------------------


def _requested_project(request: Request) -> str | None:
    return request.query_params.get("project_id") or request.query_params.get("tenant_id")


def scope_to_project(stmt: Any, model: Any, auth: AuthContext, request: Request) -> Any:
    """Limit a listing to what the caller may see.

    Mirrors Neutron's default policy: a tenant sees its own resources, an admin sees
    everything, and either can narrow with ?project_id=.
    """
    wanted = _requested_project(request)
    if wanted:
        return stmt.where(model.project_id == wanted)
    if auth.is_admin:
        return stmt
    return stmt.where(model.project_id == auth.project_id)


def visible_to(resource: Any, auth: AuthContext) -> bool:
    """Networks that are shared or external are visible to every project."""
    if auth.is_admin or resource.project_id == auth.project_id:
        return True
    return bool(getattr(resource, "shared", False) or getattr(resource, "external", False))


def ensure_visible(resource: Any, auth: AuthContext, kind: str, resource_id: str) -> None:
    if not visible_to(resource, auth):
        # Neutron hides other tenants' resources behind a 404 rather than a 403.
        raise fault(SERVICE, 404, f"{kind} {resource_id} could not be found.",
                    type=f"{kind.replace(' ', '')}NotFound")


# --------------------------------------------------------------------------------------
# Networks
# --------------------------------------------------------------------------------------


async def _subnet_ids(session: AsyncSession, network_id: str) -> list[str]:
    return list(
        (
            await session.execute(select(Subnet.id).where(Subnet.network_id == network_id))
        )
        .scalars()
        .all()
    )


@router.get("/v2.0/networks")
async def list_networks(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Network)
    if not (_requested_project(request) or auth.is_admin):
        stmt = stmt.where(
            (Network.project_id == auth.project_id)
            | Network.shared.is_(True)
            | Network.external.is_(True)
        )
    elif _requested_project(request):
        stmt = stmt.where(Network.project_id == _requested_project(request))
    params = request.query_params
    if "name" in params:
        stmt = stmt.where(Network.name == params["name"])
    if "id" in params:
        stmt = stmt.where(Network.id == params["id"])
    if "router:external" in params:
        stmt = stmt.where(Network.external.is_(params["router:external"].lower() == "true"))
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, Network, page, sort_column=Network.created_at, descending=False
    )
    networks = list((await session.execute(stmt)).scalars().all())
    return {
        "networks": trim(
            [network_dict(n, await _subnet_ids(session, n.id)) for n in networks],
            requested_fields(request.query_params),
        ),
        **collection_links(request, "networks", networks, page),
    }


@router.post("/v2.0/networks", status_code=201)
async def create_network(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = NetworkPayload(**body_object(SERVICE, body, "network"))
    await _enforce(session, payload.project_id or payload.tenant_id or auth.project_id,
                   "network")
    network = Network(
        id=gen_id(),
        name=payload.name or f"net-{gen_id()[:8]}",
        project_id=payload.project_id or payload.tenant_id or auth.project_id,
        admin_state_up=payload.admin_state_up,
        shared=payload.shared,
        external=payload.external,
        mtu=payload.mtu,
        port_security_enabled=payload.port_security_enabled,
        description=payload.description,
        provider_segmentation_id=random.randint(1, 4095),
    )
    session.add(network)
    await session.commit()
    return {"network": network_dict(network, [])}


@router.get("/v2.0/networks/{network_id}")
async def get_network(
    network_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    network = await session.get(Network, network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {network_id} could not be found.",
                    type="NetworkNotFound")
    ensure_visible(network, auth, "Network", network_id)
    return {"network": network_dict(network, await _subnet_ids(session, network.id))}


@router.put("/v2.0/networks/{network_id}")
async def update_network(
    network_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    network = await session.get(Network, network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {network_id} could not be found.",
                    type="NetworkNotFound")
    for key, value in body_object(SERVICE, body, "network").items():
        if key in ("name", "admin_state_up", "shared", "description", "mtu", "tags"):
            setattr(network, key, value)
    network.revision_number += 1
    network.updated_at = now_utc()
    await session.commit()
    return {"network": network_dict(network, await _subnet_ids(session, network.id))}


@router.delete("/v2.0/networks/{network_id}", status_code=204)
async def delete_network(
    network_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    network = await session.get(Network, network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {network_id} could not be found.",
                    type="NetworkNotFound")
    ensure_visible(network, auth, "Network", network_id)
    in_use = (
        await session.execute(
            select(Port).where(Port.network_id == network_id, Port.device_id != "")
        )
    ).scalars().first()
    if in_use is not None:
        raise fault(
            SERVICE,
            409,
            f"Unable to complete operation on network {network_id}. "
            "There are one or more ports still in use.",
            type="NetworkInUse",
        )
    await session.delete(network)  # subnets and unbound ports cascade
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Subnets
# --------------------------------------------------------------------------------------


@router.get("/v2.0/subnets")
async def list_subnets(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Subnet)
    if not auth.is_admin:
        shared_networks = select(Network.id).where(
            Network.shared.is_(True) | Network.external.is_(True)
        )
        stmt = stmt.where(
            (Subnet.project_id == auth.project_id) | Subnet.network_id.in_(shared_networks)
        )
    if "network_id" in request.query_params:
        stmt = stmt.where(Subnet.network_id == request.query_params["network_id"])
    if "name" in request.query_params:
        stmt = stmt.where(Subnet.name == request.query_params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, Subnet, page, sort_column=Subnet.created_at, descending=False
    )
    subnets = list((await session.execute(stmt)).scalars().all())
    return {
        "subnets": trim(
            [subnet_dict(s) for s in subnets], requested_fields(request.query_params)
        ),
        **collection_links(request, "subnets", subnets, page),
    }


@router.post("/v2.0/subnets", status_code=201)
async def create_subnet(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = SubnetPayload(**body_object(SERVICE, body, "subnet"))
    await _enforce(session, auth.project_id, "subnet")
    network = await session.get(Network, payload.network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {payload.network_id} could not be found.",
                    type="NetworkNotFound")
    cidr = payload.cidr
    if payload.subnetpool_id:
        # Ask the pool for space instead of naming it: the caller wants "a /26", not a
        # particular /26, and the pool guarantees it will not collide with another.
        pool = await _get_pool(session, payload.subnetpool_id)
        prefixlen = payload.prefixlen or pool.default_prefixlen
        if not (pool.min_prefixlen <= prefixlen <= pool.max_prefixlen):
            raise fault(
                SERVICE,
                400,
                f"Prefix length /{prefixlen} is outside the pool's range "
                f"/{pool.min_prefixlen}-/{pool.max_prefixlen}.",
                type="HTTPBadRequest",
            )
        taken = list(
            (
                await session.execute(
                    select(Subnet.cidr).where(Subnet.subnetpool_id == pool.id)
                )
            ).scalars().all()
        )
        cidr = _allocate_from_pool(pool, taken, prefixlen)
    if not cidr:
        raise fault(SERVICE, 400, "A cidr must be supplied.", type="BadRequest")
    payload.cidr = cidr
    try:
        gateway, start, end = allocation_pool(payload.cidr)
    except ValueError as exc:
        raise fault(SERVICE, 400, f"Invalid CIDR {payload.cidr}: {exc}", type="BadRequest")
    if payload.allocation_pools:
        start = payload.allocation_pools[0].get("start", start)
        end = payload.allocation_pools[0].get("end", end)

    subnet = Subnet(
        id=gen_id(),
        name=payload.name or f"subnet-{gen_id()[:8]}",
        network_id=network.id,
        project_id=auth.project_id,
        cidr=payload.cidr,
        subnetpool_id=payload.subnetpool_id,
        ip_version=payload.ip_version,
        gateway_ip=payload.gateway_ip or gateway,
        enable_dhcp=payload.enable_dhcp,
        allocation_start=start,
        allocation_end=end,
        dns_nameservers=payload.dns_nameservers,
        host_routes=payload.host_routes,
        description=payload.description,
    )
    session.add(subnet)
    await session.commit()
    return {"subnet": subnet_dict(subnet)}


@router.get("/v2.0/subnets/{subnet_id}")
async def get_subnet(
    subnet_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        raise fault(SERVICE, 404, f"Subnet {subnet_id} could not be found.",
                    type="SubnetNotFound")
    return {"subnet": subnet_dict(subnet)}


@router.put("/v2.0/subnets/{subnet_id}")
async def update_subnet(
    subnet_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        raise fault(SERVICE, 404, f"Subnet {subnet_id} could not be found.",
                    type="SubnetNotFound")
    for key, value in body_object(SERVICE, body, "subnet").items():
        if key in ("name", "gateway_ip", "enable_dhcp", "dns_nameservers", "description", "tags"):
            setattr(subnet, key, value)
    subnet.revision_number += 1
    subnet.updated_at = now_utc()
    await session.commit()
    return {"subnet": subnet_dict(subnet)}


@router.delete("/v2.0/subnets/{subnet_id}", status_code=204)
async def delete_subnet(
    subnet_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        raise fault(SERVICE, 404, f"Subnet {subnet_id} could not be found.",
                    type="SubnetNotFound")
    await session.delete(subnet)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------------------


@router.get("/v2.0/ports")
async def list_ports(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(select(Port), Port, auth, request)
    params = request.query_params
    if "device_id" in params:
        stmt = stmt.where(Port.device_id == params["device_id"])
    if "network_id" in params:
        stmt = stmt.where(Port.network_id == params["network_id"])
    if "mac_address" in params:
        stmt = stmt.where(Port.mac_address == params["mac_address"])
    if "device_owner" in params:
        stmt = stmt.where(Port.device_owner == params["device_owner"])
    # `port list --fixed-ip subnet=<id>` arrives as fixed_ips=subnet_id=<id>, and may
    # repeat for ip-address=<addr>. Anything unrecognised is ignored rather than
    # silently matching nothing.
    for spec in params.getlist("fixed_ips"):
        key, _, value = spec.partition("=")
        if key == "subnet_id" and value:
            stmt = stmt.where(Port.subnet_id == value)
        elif key == "ip_address" and value:
            stmt = stmt.where(Port.ip_address == value)
    if "name" in params:
        stmt = stmt.where(Port.name == params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, Port, page, sort_column=Port.created_at, descending=False
    )
    ports = list((await session.execute(stmt)).scalars().all())
    return {
        "ports": trim(
            [port_dict(p) for p in ports], requested_fields(request.query_params)
        ),
        **collection_links(request, "ports", ports, page),
    }


@router.post("/v2.0/ports", status_code=201)
async def create_port(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = PortPayload(**body_object(SERVICE, body, "port"))
    await _enforce(session, auth.project_id, "port")
    network = await session.get(Network, payload.network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {payload.network_id} could not be found.",
                    type="NetworkNotFound")
    fixed_ip = None
    if payload.fixed_ips:
        fixed_ip = payload.fixed_ips[0].get("ip_address")
    try:
        port = await create_port_record(
            session,
            network,
            auth.project_id,
            device_id=payload.device_id,
            device_owner=payload.device_owner,
            name=payload.name,
            security_group_ids=payload.security_groups,
            fixed_ip=fixed_ip,
        )
    except AddressPoolExhausted as exc:
        raise fault(SERVICE, 409, str(exc), type="IpAddressGenerationFailure")
    port.description = payload.description
    port.admin_state_up = payload.admin_state_up
    await session.commit()
    return {"port": port_dict(port)}


@router.get("/v2.0/ports/{port_id}")
async def get_port(
    port_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    port = await session.get(Port, port_id)
    if port is None:
        raise fault(SERVICE, 404, f"Port {port_id} could not be found.", type="PortNotFound")
    ensure_visible(port, auth, "Port", port_id)
    return {"port": port_dict(port)}


@router.put("/v2.0/ports/{port_id}")
async def update_port(
    port_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    port = await session.get(Port, port_id)
    if port is None:
        raise fault(SERVICE, 404, f"Port {port_id} could not be found.", type="PortNotFound")
    payload = body_object(SERVICE, body, "port")
    for key, value in payload.items():
        if key == "security_groups":
            port.security_group_ids = value
        elif key in ("name", "admin_state_up", "device_id", "device_owner", "description", "tags"):
            setattr(port, key, value)
    port.revision_number += 1
    port.updated_at = now_utc()
    await session.commit()
    return {"port": port_dict(port)}


@router.delete("/v2.0/ports/{port_id}", status_code=204)
async def delete_port(
    port_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    port = await session.get(Port, port_id)
    if port is None:
        raise fault(SERVICE, 404, f"Port {port_id} could not be found.", type="PortNotFound")
    await session.delete(port)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Security groups -- each rule burns a conntrack slot
# --------------------------------------------------------------------------------------


DEFAULT_EGRESS = (("IPv4", "egress"), ("IPv6", "egress"))


@router.get("/v2.0/security-groups")
async def list_security_groups(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(select(SecurityGroup), SecurityGroup, auth, request)
    if "name" in request.query_params:
        stmt = stmt.where(SecurityGroup.name == request.query_params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, SecurityGroup, page,
        sort_column=SecurityGroup.created_at, descending=False,
    )
    groups = list((await session.execute(stmt)).scalars().all())
    return {
        "security_groups": trim(
            [security_group_dict(g) for g in groups],
            requested_fields(request.query_params),
        ),
        **collection_links(request, "security_groups", groups, page),
    }


@router.post("/v2.0/security-groups", status_code=201)
async def create_security_group(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = SecurityGroupPayload(**body_object(SERVICE, body, "security_group"))
    await _enforce(session, auth.project_id, "security_group")
    try:
        await check_conntrack_capacity(session, len(DEFAULT_EGRESS))
    except CapacityError as exc:
        raise fault(SERVICE, 409, str(exc), type="SecurityGroupLimitExceeded")

    group = SecurityGroup(
        id=gen_id(),
        name=payload.name,
        description=payload.description or payload.name,
        project_id=auth.project_id,
        stateful=payload.stateful,
    )
    session.add(group)
    await session.flush()
    for ethertype, direction in DEFAULT_EGRESS:
        session.add(
            SecurityGroupRule(
                id=gen_id(),
                security_group_id=group.id,
                project_id=auth.project_id,
                direction=direction,
                ethertype=ethertype,
            )
        )
    await session.commit()
    await session.refresh(group)
    return {"security_group": security_group_dict(group)}


@router.get("/v2.0/security-groups/{group_id}")
async def get_security_group(
    group_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    group = await session.get(SecurityGroup, group_id)
    if group is None:
        raise fault(SERVICE, 404, f"Security group {group_id} does not exist.",
                    type="SecurityGroupNotFound")
    ensure_visible(group, auth, "Security group", group_id)
    return {"security_group": security_group_dict(group)}


@router.put("/v2.0/security-groups/{group_id}")
async def update_security_group(
    group_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    group = await session.get(SecurityGroup, group_id)
    if group is None:
        raise fault(SERVICE, 404, f"Security group {group_id} does not exist.",
                    type="SecurityGroupNotFound")
    for key, value in body_object(SERVICE, body, "security_group").items():
        if key in ("name", "description", "tags"):
            setattr(group, key, value)
    group.revision_number += 1
    group.updated_at = now_utc()
    await session.commit()
    return {"security_group": security_group_dict(group)}


@router.delete("/v2.0/security-groups/{group_id}", status_code=204)
async def delete_security_group(
    group_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    group = await session.get(SecurityGroup, group_id)
    if group is None:
        raise fault(SERVICE, 404, f"Security group {group_id} does not exist.",
                    type="SecurityGroupNotFound")
    ensure_visible(group, auth, "Security group", group_id)
    await session.delete(group)  # rules cascade, releasing their conntrack slots
    await session.commit()
    return Response(status_code=204)


@router.get("/v2.0/security-group-rules")
async def list_security_group_rules(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(select(SecurityGroupRule), SecurityGroupRule, auth, request)
    if "security_group_id" in request.query_params:
        stmt = stmt.where(
            SecurityGroupRule.security_group_id
            == request.query_params["security_group_id"]
        )
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, SecurityGroupRule, page,
        sort_column=SecurityGroupRule.created_at, descending=False,
    )
    rules = list((await session.execute(stmt)).scalars().all())
    return {
        "security_group_rules": [rule_dict(r) for r in rules],
        **collection_links(request, "security_group_rules", rules, page),
    }


@router.post("/v2.0/security-group-rules", status_code=201)
async def create_security_group_rule(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = SecurityGroupRulePayload(**body_object(SERVICE, body, "security_group_rule"))
    await _enforce(session, auth.project_id, "security_group_rule")
    group = await session.get(SecurityGroup, payload.security_group_id)
    if group is None:
        raise fault(
            SERVICE,
            404,
            f"Security group {payload.security_group_id} does not exist.",
            type="SecurityGroupNotFound",
        )
    try:
        await check_conntrack_capacity(session, 1)
    except CapacityError as exc:
        raise fault(SERVICE, 409, str(exc), type="SecurityGroupRuleLimitExceeded")

    rule = SecurityGroupRule(
        id=gen_id(),
        security_group_id=group.id,
        project_id=auth.project_id,
        direction=payload.direction,
        ethertype=payload.ethertype,
        protocol=payload.protocol,
        port_range_min=payload.port_range_min,
        port_range_max=payload.port_range_max,
        remote_ip_prefix=payload.remote_ip_prefix,
        remote_group_id=payload.remote_group_id,
        description=payload.description,
    )
    session.add(rule)
    await session.commit()
    return {"security_group_rule": rule_dict(rule)}


@router.get("/v2.0/security-group-rules/{rule_id}")
async def get_security_group_rule(
    rule_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rule = await session.get(SecurityGroupRule, rule_id)
    if rule is None:
        raise fault(SERVICE, 404, f"Security group rule {rule_id} does not exist.",
                    type="SecurityGroupRuleNotFound")
    return {"security_group_rule": rule_dict(rule)}


@router.delete("/v2.0/security-group-rules/{rule_id}", status_code=204)
async def delete_security_group_rule(
    rule_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    rule = await session.get(SecurityGroupRule, rule_id)
    if rule is None:
        raise fault(SERVICE, 404, f"Security group rule {rule_id} does not exist.",
                    type="SecurityGroupRuleNotFound")
    await session.delete(rule)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Floating IPs
# --------------------------------------------------------------------------------------


@router.get("/v2.0/floatingips")
async def list_floating_ips(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(
        select(FloatingIP).where(FloatingIP.released.is_(False)), FloatingIP, auth, request
    )
    params = request.query_params
    if "port_id" in params:
        stmt = stmt.where(FloatingIP.port_id == params["port_id"])
    if "floating_ip_address" in params:
        stmt = stmt.where(
            FloatingIP.floating_ip_address == params["floating_ip_address"]
        )
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, FloatingIP, page,
        sort_column=FloatingIP.created_at, descending=False,
    )
    fips = list((await session.execute(stmt)).scalars().all())
    return {
        "floatingips": trim(
            [floating_ip_dict(f) for f in fips], requested_fields(request.query_params)
        ),
        **collection_links(request, "floatingips", fips, page),
    }


@router.post("/v2.0/floatingips", status_code=201)
async def create_floating_ip(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = FloatingIPPayload(**body_object(SERVICE, body, "floatingip"))
    await _enforce(session, auth.project_id, "floatingip")
    network = await session.get(Network, payload.floating_network_id)
    if network is None:
        raise fault(
            SERVICE,
            404,
            f"External network {payload.floating_network_id} could not be found.",
            type="NetworkNotFound",
        )
    subnet = (
        await session.execute(
            select(Subnet).where(Subnet.network_id == network.id).order_by(Subnet.created_at)
        )
    ).scalars().first()
    if subnet is None:
        raise fault(
            SERVICE,
            400,
            f"Network {network.id} does not contain any IPv4 subnet.",
            type="ExternalIpAddressExhausted",
        )
    try:
        address = payload.floating_ip_address or await next_free_ip(session, subnet)
    except AddressPoolExhausted as exc:
        raise fault(SERVICE, 409, str(exc), type="IpAddressGenerationFailure")

    fixed_ip = payload.fixed_ip_address
    status = "DOWN"
    if payload.port_id:
        port = await session.get(Port, payload.port_id)
        if port is None:
            raise fault(SERVICE, 404, f"Port {payload.port_id} could not be found.",
                        type="PortNotFound")
        fixed_ip = fixed_ip or port.ip_address
        status = "ACTIVE"

    fip = FloatingIP(
        id=gen_id(),
        project_id=auth.project_id,
        floating_network_id=network.id,
        floating_ip_address=address,
        fixed_ip_address=fixed_ip,
        port_id=payload.port_id,
        status=status,
        description=payload.description,
    )
    session.add(fip)
    await session.commit()
    return {"floatingip": floating_ip_dict(fip)}


@router.get("/v2.0/floatingips/{fip_id}")
async def get_floating_ip(
    fip_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    fip = await session.get(FloatingIP, fip_id)
    if fip is None or fip.released:
        raise fault(SERVICE, 404, f"Floating IP {fip_id} could not be found.",
                    type="FloatingIPNotFound")
    ensure_visible(fip, auth, "Floating IP", fip_id)
    return {"floatingip": floating_ip_dict(fip)}


@router.put("/v2.0/floatingips/{fip_id}")
async def update_floating_ip(
    fip_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Associate (port_id set) or disassociate (port_id null) the floating IP."""
    fip = await session.get(FloatingIP, fip_id)
    if fip is None or fip.released:
        raise fault(SERVICE, 404, f"Floating IP {fip_id} could not be found.",
                    type="FloatingIPNotFound")
    payload = body_object(SERVICE, body, "floatingip")
    if "port_id" in payload:
        port_id = payload["port_id"]
        if port_id:
            port = await session.get(Port, port_id)
            if port is None:
                raise fault(SERVICE, 404, f"Port {port_id} could not be found.",
                            type="PortNotFound")
            fip.port_id = port.id
            fip.fixed_ip_address = payload.get("fixed_ip_address") or port.ip_address
            fip.status = "ACTIVE"
        else:
            fip.port_id = None
            fip.fixed_ip_address = None
            fip.status = "DOWN"
    if "description" in payload:
        fip.description = payload["description"]
    fip.revision_number += 1
    fip.updated_at = now_utc()
    await session.commit()
    return {"floatingip": floating_ip_dict(fip)}


@router.delete("/v2.0/floatingips/{fip_id}", status_code=204)
async def delete_floating_ip(
    fip_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    fip = await session.get(FloatingIP, fip_id)
    if fip is None or fip.released:
        raise fault(SERVICE, 404, f"Floating IP {fip_id} could not be found.",
                    type="FloatingIPNotFound")
    # Soft release: the address returns to the pool but the row survives for rating.
    fip.released = True
    fip.port_id = None
    fip.fixed_ip_address = None
    fip.status = "DOWN"
    fip.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)

# --------------------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------------------

ROUTER_INTERFACE_OWNER = "network:router_interface"
ROUTER_GATEWAY_OWNER = "network:router_gateway"


class RouterPayload(OSPayload):
    name: str = ""
    admin_state_up: bool = True
    description: str = ""
    distributed: bool = False
    ha: bool = False
    external_gateway_info: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


def router_dict(router: Router, interfaces: list[Port]) -> dict[str, Any]:
    gateway: dict[str, Any] | None = None
    if router.external_network_id:
        # configureRouter() greps `router show` for "network_id" to decide whether the
        # gateway is already set, so this key must be absent until one is attached.
        gateway = {
            "network_id": router.external_network_id,
            "enable_snat": router.enable_snat,
            "external_fixed_ips": (
                [{"ip_address": router.external_fixed_ip}] if router.external_fixed_ip else []
            ),
        }
    return {
        "id": router.id,
        "name": router.name,
        "status": router.status,
        "admin_state_up": router.admin_state_up,
        "project_id": router.project_id,
        "tenant_id": router.project_id,
        "description": router.description,
        "external_gateway_info": gateway,
        "distributed": router.distributed,
        "ha": router.ha,
        "routes": list(router.routes or []),
        "availability_zones": ["nova"],
        "availability_zone_hints": list(router.availability_zone_hints or []),
        "flavor_id": None,
        "interfaces_info": [
            {
                "port_id": port.id,
                "ip_address": port.ip_address,
                "subnet_id": port.subnet_id,
            }
            for port in interfaces
        ],
        "tags": list(router.tags or []),
        "revision_number": router.revision_number,
        "created_at": iso_us(router.created_at),
        "updated_at": iso_us(router.updated_at),
    }


async def _router_ports(session: AsyncSession, router_id: str) -> list[Port]:
    return list(
        (
            await session.execute(
                select(Port).where(
                    Port.device_id == router_id,
                    Port.device_owner == ROUTER_INTERFACE_OWNER,
                )
            )
        ).scalars().all()
    )


async def _get_router(session: AsyncSession, router_id: str, auth: AuthContext) -> Router:
    router = await session.get(Router, router_id)
    if router is None:
        raise fault(SERVICE, 404, f"Router {router_id} could not be found.",
                    type="RouterNotFound")
    ensure_visible(router, auth, "Router", router_id)
    return router


async def _set_gateway(
    session: AsyncSession, router: Router, info: dict[str, Any] | None
) -> None:
    """Attach or clear the external gateway, mirroring `router set/unset`."""
    if not info:
        router.external_network_id = None
        router.external_fixed_ip = None
        return
    network_id = info.get("network_id")
    if not network_id:
        return
    network = await session.get(Network, network_id)
    if network is None:
        raise fault(SERVICE, 404, f"Network {network_id} could not be found.",
                    type="NetworkNotFound")
    if not network.external:
        raise fault(
            SERVICE,
            400,
            f"Network {network_id} is not an external network.",
            type="BadRequest",
        )
    router.external_network_id = network.id
    router.enable_snat = bool(info.get("enable_snat", True))
    subnet = (
        await session.execute(
            select(Subnet).where(Subnet.network_id == network.id).order_by(Subnet.created_at)
        )
    ).scalars().first()
    if subnet is not None and router.external_fixed_ip is None:
        try:
            router.external_fixed_ip = await next_free_ip(session, subnet)
        except AddressPoolExhausted as exc:
            raise fault(SERVICE, 409, str(exc), type="IpAddressGenerationFailure")


@router.get("/v2.0/routers")
async def list_routers(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(select(Router), Router, auth, request)
    if "name" in request.query_params:
        stmt = stmt.where(Router.name == request.query_params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, Router, page, sort_column=Router.created_at, descending=False
    )
    routers = list((await session.execute(stmt)).scalars().all())
    return {
        "routers": trim(
            [router_dict(r, await _router_ports(session, r.id)) for r in routers],
            requested_fields(request.query_params),
        ),
        **collection_links(request, "routers", routers, page),
    }


@router.post("/v2.0/routers", status_code=201)
async def create_router(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = RouterPayload(**body_object(SERVICE, body, "router"))
    await _enforce(session, auth.project_id, "router")
    record = Router(
        id=gen_id(),
        name=payload.name or f"router-{gen_id()[:8]}",
        project_id=auth.project_id,
        admin_state_up=payload.admin_state_up,
        description=payload.description,
        distributed=payload.distributed,
        ha=payload.ha,
        tags=payload.tags,
    )
    session.add(record)
    await _set_gateway(session, record, payload.external_gateway_info)
    await session.commit()
    return {"router": router_dict(record, [])}


@router.get("/v2.0/routers/{router_id}")
async def get_router(
    router_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await _get_router(session, router_id, auth)
    return {"router": router_dict(record, await _router_ports(session, record.id))}


@router.put("/v2.0/routers/{router_id}")
async def update_router(
    router_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await _get_router(session, router_id, auth)
    payload = body_object(SERVICE, body, "router")
    for key, value in payload.items():
        if key in ("name", "admin_state_up", "description", "routes", "tags"):
            setattr(record, key, value)
    if "external_gateway_info" in payload:
        await _set_gateway(session, record, payload["external_gateway_info"])
    record.revision_number += 1
    record.updated_at = now_utc()
    await session.commit()
    return {"router": router_dict(record, await _router_ports(session, record.id))}


@router.delete("/v2.0/routers/{router_id}", status_code=204)
async def delete_router(
    router_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    record = await _get_router(session, router_id, auth)
    interfaces = await _router_ports(session, record.id)
    if interfaces:
        raise fault(
            SERVICE,
            409,
            f"Router {router_id} still has ports; remove its interfaces first.",
            type="RouterInUse",
        )
    await session.delete(record)
    await session.commit()
    return Response(status_code=204)


@router.put("/v2.0/routers/{router_id}/add_router_interface")
async def add_router_interface(
    router_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Attach a subnet by creating a router-owned port holding its gateway address."""
    record = await _get_router(session, router_id, auth)
    subnet_id = body.get("subnet_id")
    port_id = body.get("port_id")
    if port_id and not subnet_id:
        port = await session.get(Port, port_id)
        if port is None:
            raise fault(SERVICE, 404, f"Port {port_id} could not be found.",
                        type="PortNotFound")
        subnet_id = port.subnet_id
    if not subnet_id:
        raise fault(SERVICE, 400, "Either subnet_id or port_id must be specified.",
                    type="BadRequest")

    subnet = await session.get(Subnet, subnet_id)
    if subnet is None:
        raise fault(SERVICE, 404, f"Subnet {subnet_id} could not be found.",
                    type="SubnetNotFound")

    existing = [p for p in await _router_ports(session, record.id) if p.subnet_id == subnet.id]
    if existing:
        # Neutron rejects a redundant add with a self-overlap 400 rather than a 409.
        raise fault(
            SERVICE,
            400,
            f"Cidr {subnet.cidr} of subnet {subnet.id} overlaps with cidr "
            f"{subnet.cidr} of subnet {subnet.id}",
            type="BadRequest",
        )

    network = await session.get(Network, subnet.network_id)
    port = await create_port_record(
        session,
        network,
        record.project_id,
        device_id=record.id,
        device_owner=ROUTER_INTERFACE_OWNER,
        name=f"router-if-{subnet.name}",
        fixed_ip=subnet.gateway_ip,
    )
    await session.flush()
    record.updated_at = now_utc()
    await session.commit()
    return {
        "id": record.id,
        "tenant_id": record.project_id,
        "project_id": record.project_id,
        "port_id": port.id,
        "subnet_id": subnet.id,
        "subnet_ids": [subnet.id],
        "network_id": subnet.network_id,
    }


@router.put("/v2.0/routers/{router_id}/remove_router_interface")
async def remove_router_interface(
    router_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    record = await _get_router(session, router_id, auth)
    subnet_id = body.get("subnet_id")
    port_id = body.get("port_id")
    ports = await _router_ports(session, record.id)
    match = None
    for port in ports:
        if (subnet_id and port.subnet_id == subnet_id) or (port_id and port.id == port_id):
            match = port
            break
    if match is None:
        raise fault(
            SERVICE,
            404,
            f"Router {router_id} has no interface on subnet {subnet_id or port_id}.",
            type="RouterInterfaceNotFound",
        )
    result = {
        "id": record.id,
        "tenant_id": record.project_id,
        "project_id": record.project_id,
        "port_id": match.id,
        "subnet_id": match.subnet_id,
        "subnet_ids": [match.subnet_id],
    }
    await session.delete(match)
    record.updated_at = now_utc()
    await session.commit()
    return result


# --------------------------------------------------------------------------------------
# Subnet pools
# --------------------------------------------------------------------------------------


class SubnetPoolPayload(OSPayload):
    name: str
    prefixes: list[str] = Field(default_factory=list)
    default_prefixlen: int | None = None
    min_prefixlen: int | None = None
    max_prefixlen: int | None = None
    shared: bool = False
    is_default: bool = False
    description: str = ""
    address_scope_id: str | None = None
    default_quota: int | None = None


def subnet_pool_dict(pool: SubnetPool) -> dict[str, Any]:
    return {
        "id": pool.id,
        "name": pool.name,
        "project_id": pool.project_id,
        "tenant_id": pool.project_id,
        "prefixes": list(pool.prefixes or []),
        "default_prefixlen": str(pool.default_prefixlen),
        "min_prefixlen": str(pool.min_prefixlen),
        "max_prefixlen": str(pool.max_prefixlen),
        "ip_version": pool.ip_version,
        "shared": pool.shared,
        "is_default": pool.is_default,
        "description": pool.description,
        "address_scope_id": pool.address_scope_id,
        "default_quota": pool.default_quota,
        "created_at": iso_us(pool.created_at),
        "updated_at": iso_us(pool.updated_at),
        "revision_number": 0,
    }


async def _get_pool(session: AsyncSession, pool_id: str) -> SubnetPool:
    pool = await session.get(SubnetPool, pool_id)
    if pool is None:
        raise fault(SERVICE, 404, f"Subnet pool {pool_id} could not be found.",
                    type="SubnetPoolNotFound")
    return pool


def _allocate_from_pool(pool: SubnetPool, taken: list[str], prefixlen: int) -> str:
    """Carve the next free prefix of this length out of the pool.

    Walks each of the pool's prefixes in order and returns the first candidate that
    overlaps nothing already allocated -- which is the guarantee a pool exists to make.
    """
    used = [ipaddress.ip_network(cidr) for cidr in taken]
    for raw in pool.prefixes or []:
        parent = ipaddress.ip_network(raw)
        if prefixlen < parent.prefixlen:
            continue  # a bigger block than the pool itself holds
        for candidate in parent.subnets(new_prefix=prefixlen):
            if not any(candidate.overlaps(existing) for existing in used):
                return str(candidate)
    raise fault(
        SERVICE,
        409,
        f"Subnet pool {pool.id} has no free prefix of length /{prefixlen} left.",
        type="SubnetPoolQuotaExceeded",
    )


@router.get("/v2.0/subnetpools")
async def list_subnet_pools(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(SubnetPool)
    if not auth.is_admin:
        stmt = stmt.where(
            (SubnetPool.project_id == auth.project_id) | SubnetPool.shared.is_(True)
        )
    if "name" in request.query_params:
        stmt = stmt.where(SubnetPool.name == request.query_params["name"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, SubnetPool, page, sort_column=SubnetPool.created_at,
        descending=False,
    )
    pools = list((await session.execute(stmt)).scalars().all())
    return {
        "subnetpools": [subnet_pool_dict(p) for p in pools],
        **collection_links(request, "subnetpools", pools, page),
    }


@router.post("/v2.0/subnetpools", status_code=201)
async def create_subnet_pool(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = SubnetPoolPayload(**body_object(SERVICE, body, "subnetpool"))
    if not payload.prefixes:
        raise fault(SERVICE, 400, "A subnet pool requires at least one prefix.",
                    type="HTTPBadRequest")
    try:
        networks = [ipaddress.ip_network(prefix) for prefix in payload.prefixes]
    except ValueError as exc:
        raise fault(SERVICE, 400, f"Invalid prefix: {exc}", type="HTTPBadRequest")

    await _enforce(session, auth.project_id, "subnetpool")

    version = networks[0].version
    pool = SubnetPool(
        id=gen_id(),
        name=payload.name,
        project_id=auth.project_id,
        prefixes=[str(n) for n in networks],
        default_prefixlen=payload.default_prefixlen or (24 if version == 4 else 64),
        min_prefixlen=payload.min_prefixlen or (8 if version == 4 else 64),
        max_prefixlen=payload.max_prefixlen or (32 if version == 4 else 128),
        ip_version=version,
        shared=payload.shared,
        is_default=payload.is_default,
        description=payload.description,
        address_scope_id=payload.address_scope_id,
        default_quota=payload.default_quota,
    )
    session.add(pool)
    await session.commit()
    return {"subnetpool": subnet_pool_dict(pool)}


@router.get("/v2.0/subnetpools/{pool_id}")
async def get_subnet_pool(
    pool_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"subnetpool": subnet_pool_dict(await _get_pool(session, pool_id))}


@router.put("/v2.0/subnetpools/{pool_id}")
async def update_subnet_pool(
    pool_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    pool = await _get_pool(session, pool_id)
    payload = body_object(SERVICE, body, "subnetpool")
    for field in ("name", "description", "shared", "is_default", "default_quota"):
        if field in payload:
            setattr(pool, field, payload[field])
    if "prefixes" in payload:
        # Neutron only ever grows a pool: shrinking would strand allocations inside it.
        existing = set(pool.prefixes or [])
        pool.prefixes = sorted(existing | set(payload["prefixes"]))
    pool.updated_at = now_utc()
    await session.commit()
    return {"subnetpool": subnet_pool_dict(pool)}


@router.delete("/v2.0/subnetpools/{pool_id}", status_code=204)
async def delete_subnet_pool(
    pool_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    pool = await _get_pool(session, pool_id)
    allocated = (
        await session.execute(
            select(Subnet.id).where(Subnet.subnetpool_id == pool.id)
        )
    ).scalars().first()
    if allocated:
        raise fault(
            SERVICE,
            409,
            f"Subnet pool {pool_id} is in use by one or more subnets.",
            type="SubnetPoolInUse",
        )
    await session.delete(pool)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Trunks
# --------------------------------------------------------------------------------------


class TrunkPayload(OSPayload):
    port_id: str
    name: str = ""
    description: str = ""
    admin_state_up: bool = True
    sub_ports: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def subport_dict(subport: SubPort) -> dict[str, Any]:
    return {
        "port_id": subport.port_id,
        "segmentation_type": subport.segmentation_type,
        "segmentation_id": subport.segmentation_id,
    }


def trunk_dict(trunk: Trunk, subports: list[SubPort]) -> dict[str, Any]:
    return {
        "id": trunk.id,
        "name": trunk.name,
        "description": trunk.description,
        "project_id": trunk.project_id,
        "tenant_id": trunk.project_id,
        "port_id": trunk.port_id,
        "status": trunk.status,
        "admin_state_up": trunk.admin_state_up,
        "sub_ports": [subport_dict(s) for s in subports],
        "tags": list(trunk.tags or []),
        "created_at": iso_us(trunk.created_at),
        "updated_at": iso_us(trunk.updated_at),
        "revision_number": 0,
    }


async def _get_trunk(session: AsyncSession, trunk_id: str) -> Trunk:
    trunk = await session.get(Trunk, trunk_id)
    if trunk is None or trunk.deleted:
        raise fault(SERVICE, 404, f"Trunk {trunk_id} could not be found.",
                    type="TrunkNotFound")
    return trunk


async def _trunk_subports(session: AsyncSession, trunk_id: str) -> list[SubPort]:
    return list(
        (
            await session.execute(
                select(SubPort)
                .where(SubPort.trunk_id == trunk_id)
                .order_by(SubPort.segmentation_id)
            )
        ).scalars().all()
    )


async def _port_is_in_use(session: AsyncSession, port_id: str) -> str | None:
    """Whether a port is already a trunk parent or a subport somewhere.

    A port carries one role at a time: being both a parent and a subport, or a subport of
    two trunks, would make its traffic ambiguous.
    """
    parent = (
        await session.execute(
            select(Trunk.id).where(Trunk.port_id == port_id, Trunk.deleted.is_(False))
        )
    ).scalars().first()
    if parent:
        return f"port {port_id} is already the parent of trunk {parent}"
    child = (
        await session.execute(select(SubPort.trunk_id).where(SubPort.port_id == port_id))
    ).scalars().first()
    if child:
        return f"port {port_id} is already a subport of trunk {child}"
    return None


async def _add_subports(
    session: AsyncSession, trunk: Trunk, entries: list[dict[str, Any]]
) -> None:
    existing = {s.segmentation_id for s in await _trunk_subports(session, trunk.id)}
    for entry in entries:
        port_id = entry.get("port_id")
        if not port_id:
            raise fault(SERVICE, 400, "A subport requires a port_id.", type="HTTPBadRequest")
        port = await session.get(Port, port_id)
        if port is None:
            raise fault(SERVICE, 404, f"Port {port_id} could not be found.",
                        type="PortNotFound")
        if port_id == trunk.port_id:
            raise fault(
                SERVICE, 409,
                f"Port {port_id} is the trunk's own parent port.",
                type="TrunkPortInUse",
            )
        conflict = await _port_is_in_use(session, port_id)
        if conflict:
            raise fault(SERVICE, 409, f"Cannot add subport: {conflict}.",
                        type="TrunkPortInUse")
        try:
            segmentation_id = int(entry.get("segmentation_id"))
        except (TypeError, ValueError):
            raise fault(SERVICE, 400, "A subport requires an integer segmentation_id.",
                        type="HTTPBadRequest")
        if not 1 <= segmentation_id <= 4094:
            raise fault(
                SERVICE, 400,
                f"segmentation_id {segmentation_id} is outside the VLAN range 1-4094.",
                type="HTTPBadRequest",
            )
        if segmentation_id in existing:
            raise fault(
                SERVICE, 409,
                f"Segmentation id {segmentation_id} is already used on this trunk.",
                type="DuplicateSubPort",
            )
        existing.add(segmentation_id)
        session.add(
            SubPort(
                id=gen_id(),
                trunk_id=trunk.id,
                port_id=port_id,
                segmentation_type=entry.get("segmentation_type", "vlan"),
                segmentation_id=segmentation_id,
            )
        )


@router.get("/v2.0/trunks")
async def list_trunks(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = scope_to_project(
        select(Trunk).where(Trunk.deleted.is_(False)), Trunk, auth, request
    )
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, stmt, Trunk, page, sort_column=Trunk.created_at, descending=False
    )
    trunks = list((await session.execute(stmt)).scalars().all())
    return {
        "trunks": [
            trunk_dict(t, await _trunk_subports(session, t.id)) for t in trunks
        ],
        **collection_links(request, "trunks", trunks, page),
    }


@router.post("/v2.0/trunks", status_code=201)
async def create_trunk(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = TrunkPayload(**body_object(SERVICE, body, "trunk"))
    port = await session.get(Port, payload.port_id)
    if port is None:
        raise fault(SERVICE, 404, f"Port {payload.port_id} could not be found.",
                    type="PortNotFound")
    conflict = await _port_is_in_use(session, payload.port_id)
    if conflict:
        raise fault(SERVICE, 409, f"Cannot create trunk: {conflict}.",
                    type="TrunkPortInUse")
    await _enforce(session, auth.project_id, "trunk")

    trunk = Trunk(
        id=gen_id(),
        name=payload.name or f"trunk-{gen_id()[:8]}",
        description=payload.description,
        project_id=auth.project_id,
        port_id=payload.port_id,
        admin_state_up=payload.admin_state_up,
        tags=payload.tags,
    )
    session.add(trunk)
    await session.flush()
    await _add_subports(session, trunk, payload.sub_ports)
    await session.commit()
    return {"trunk": trunk_dict(trunk, await _trunk_subports(session, trunk.id))}


@router.get("/v2.0/trunks/{trunk_id}")
async def get_trunk(
    trunk_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    trunk = await _get_trunk(session, trunk_id)
    return {"trunk": trunk_dict(trunk, await _trunk_subports(session, trunk.id))}


@router.put("/v2.0/trunks/{trunk_id}")
async def update_trunk(
    trunk_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    trunk = await _get_trunk(session, trunk_id)
    payload = body_object(SERVICE, body, "trunk")
    for field in ("name", "description", "admin_state_up", "tags"):
        if field in payload:
            setattr(trunk, field, payload[field])
    trunk.updated_at = now_utc()
    await session.commit()
    return {"trunk": trunk_dict(trunk, await _trunk_subports(session, trunk.id))}


@router.put("/v2.0/trunks/{trunk_id}/add_subports")
async def add_subports(
    trunk_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    trunk = await _get_trunk(session, trunk_id)
    await _add_subports(session, trunk, (body or {}).get("sub_ports") or [])
    trunk.updated_at = now_utc()
    await session.commit()
    return trunk_dict(trunk, await _trunk_subports(session, trunk.id))


@router.put("/v2.0/trunks/{trunk_id}/remove_subports")
async def remove_subports(
    trunk_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    trunk = await _get_trunk(session, trunk_id)
    wanted = {entry.get("port_id") for entry in (body or {}).get("sub_ports") or []}
    for subport in await _trunk_subports(session, trunk.id):
        if subport.port_id in wanted:
            await session.delete(subport)
    trunk.updated_at = now_utc()
    await session.commit()
    return trunk_dict(trunk, await _trunk_subports(session, trunk.id))


@router.delete("/v2.0/trunks/{trunk_id}", status_code=204)
async def delete_trunk(
    trunk_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    trunk = await _get_trunk(session, trunk_id)
    for subport in await _trunk_subports(session, trunk.id):
        await session.delete(subport)
    trunk.deleted = True
    await session.commit()
    return Response(status_code=204)
