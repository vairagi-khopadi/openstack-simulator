"""Nova Compute v2.1 (port 8774): servers, flavors, keypairs, hypervisors,
diagnostics and console output -- all booked against the bare-metal envelope."""
from __future__ import annotations

import base64
import hashlib
import random
import string
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    API_VERSIONS,
    gen_id,
    iso,
    now_utc,
    service_url,
    settings,
    settle_transition,
    transition_deadline,
)
from app.core.database import get_session
from app.core.microversion import at_least
from app.core.pagination import collection_links, page_request, paginate
from app.core.middleware import AuthContext, OSPayload, fault, require
from app.models.compute import Flavor, Keypair, Server
from app.models.network import Network, Port, SecurityGroup
from app.models.storage import Image, Volume, VolumeAttachment
from app.services import telemetry
from app.services.capacity import (
    CapacityError,
    check_instance_capacity,
    get_host,
    get_usage,
)
from app.services.rating import BILLABLE_ACTIVE, BILLABLE_IDLE
from app.services.networking import (
    AddressPoolExhausted,
    create_port_record,
    pick_network,
)

SERVICE = "nova"
router = APIRouter()
auth_dep = require(SERVICE)

_, MIN_VERSION, MAX_VERSION = API_VERSIONS["nova"]
DEVICE_LETTERS = string.ascii_lowercase[1:]  # vdb, vdc, ...


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class ServerPayload(OSPayload):
    name: str
    flavorRef: str | None = None
    imageRef: str | None = None
    networks: Any = "auto"
    key_name: str | None = None
    security_groups: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    user_data: str | None = None
    availability_zone: str = "nova"
    config_drive: bool = False
    block_device_mapping_v2: list[dict[str, Any]] = Field(default_factory=list)
    min_count: int = 1
    max_count: int = 1
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class FlavorPayload(OSPayload):
    name: str
    vcpus: int
    ram: int
    disk: int
    id: str | None = None
    swap: int | str = 0
    rxtx_factor: float = 1.0
    is_public: bool = Field(default=True, alias="os-flavor-access:is_public")
    description: str | None = None
    ephemeral: int = Field(default=0, alias="OS-FLV-EXT-DATA:ephemeral")


class KeypairPayload(OSPayload):
    name: str
    public_key: str | None = None
    type: str = "ssh"
    user_id: str | None = None


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _links(kind: str, resource_id: str) -> list[dict[str, str]]:
    return [
        {"rel": "self", "href": service_url(SERVICE, f"/v2.1/{kind}/{resource_id}")},
        {"rel": "bookmark", "href": service_url(SERVICE, f"/{kind}/{resource_id}")},
    ]


def _accrue(server: Server) -> None:
    """Fold elapsed wall time into the billing counters before a state change."""
    now = now_utc()
    delta = max((now - server.accounted_at).total_seconds(), 0.0)
    if server.status in BILLABLE_ACTIVE:
        server.active_seconds += delta
    elif server.status in BILLABLE_IDLE:
        server.idle_seconds += delta
    server.accounted_at = now


def _set_state(server: Server, status: str, task_state: str | None = None) -> None:
    _accrue(server)
    vm_state, power_state = telemetry.STATE_MAP.get(status, ("active", 1))
    server.status = status
    server.vm_state = vm_state
    server.power_state = power_state
    server.task_state = task_state
    # Any explicit state change cancels a pending transition. Without this, stopping
    # an instance that is still inside its build window leaves the old deadline armed
    # and the next read resurrects it as ACTIVE. Callers that want a *new* window
    # (reboot, unshelve) set one immediately after this call.
    server.transition_until = None
    server.transition_target = None
    server.updated_at = now_utc()


def resolve_server(server: Server) -> Server:
    """Stateless polling: flip pending -> ready once the stored deadline has passed."""
    target = settle_transition(server)
    if target is None:
        return server
    _set_state(server, target)
    if target == "ACTIVE" and server.launched_at is None:
        server.launched_at = now_utc()
    return server


def flavor_dict(flavor: Flavor) -> dict[str, Any]:
    return {
        "id": flavor.id,
        "name": flavor.name,
        "vcpus": flavor.vcpus,
        "ram": flavor.ram,
        "disk": flavor.disk,
        "swap": flavor.swap or "",
        "OS-FLV-EXT-DATA:ephemeral": flavor.ephemeral,
        "OS-FLV-DISABLED:disabled": flavor.disabled,
        "os-flavor-access:is_public": flavor.is_public,
        "rxtx_factor": flavor.rxtx_factor,
        "description": flavor.description,
        "extra_specs": dict(flavor.extra_specs or {}),
        "links": _links("flavors", flavor.id),
    }


def keypair_dict(keypair: Keypair) -> dict[str, Any]:
    return {
        "name": keypair.name,
        "public_key": keypair.public_key,
        "fingerprint": keypair.fingerprint,
        "type": keypair.type,
        "user_id": keypair.user_id,
        "created_at": iso(keypair.created_at),
        "deleted": False,
        "deleted_at": None,
        "id": keypair.id,
        "updated_at": None,
    }


def _addresses(ports: list[Port], networks: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for port in ports:
        label = networks.get(port.network_id, "private")
        entry = {
            "version": 4,
            "addr": port.ip_address,
            "OS-EXT-IPS:type": "fixed",
            "OS-EXT-IPS-MAC:mac_addr": port.mac_address,
        }
        result.setdefault(label, []).append(entry)
    return result


def server_dict(
    server: Server,
    flavor: Flavor | None,
    ports: list[Port],
    networks: dict[str, str],
    attachments: list[VolumeAttachment],
    detail: bool = True,
    request: Request | None = None,
) -> dict[str, Any]:
    """The server body, rendered at the microversion this request negotiated.

    Nova grew this response one field at a time, and a client pinned to an old version
    must not see a field that did not exist then -- that is the whole point of asking for
    a version. ``request`` carries the negotiated one; without it every field is rendered,
    which is what internal callers with no request in hand want.
    """
    if not detail:
        return {
            "id": server.id,
            "name": server.name,
            "links": _links("servers", server.id),
        }
    host_hash = hashlib.sha224((server.host + server.project_id).encode()).hexdigest()
    body: dict[str, Any] = {
        "id": server.id,
        "name": server.name,
        "status": server.status,
        "tenant_id": server.project_id,
        "user_id": server.user_id,
        "created": iso(server.created_at),
        "updated": iso(server.updated_at),
        "hostId": host_hash,
        "addresses": _addresses(ports, networks),
        "accessIPv4": "",
        "accessIPv6": "",
        "progress": 100 if server.status == "ACTIVE" else 0,
        "key_name": server.key_name,
        "metadata": dict(server.metadata_ or {}),
        "config_drive": server.config_drive,
        "links": _links("servers", server.id),
        "image": {"id": server.image_id, "links": _links("images", server.image_id)}
        if server.image_id
        else "",
        "security_groups": [
            {"name": name} for name in (server.security_group_names or ["default"])
        ],
        "OS-DCF:diskConfig": "MANUAL",
        "OS-EXT-AZ:availability_zone": server.availability_zone,
        "OS-EXT-STS:task_state": server.task_state,
        "OS-EXT-STS:vm_state": server.vm_state,
        "OS-EXT-STS:power_state": server.power_state,
        "OS-EXT-SRV-ATTR:host": server.host,
        "OS-EXT-SRV-ATTR:hypervisor_hostname": server.host,
        "OS-EXT-SRV-ATTR:instance_name": f"instance-{server.id[:8]}",
        "OS-SRV-USG:launched_at": iso(server.launched_at),
        "OS-SRV-USG:terminated_at": iso(server.terminated_at),
        "os-extended-volumes:volumes_attached": [
            {"id": a.volume_id, "delete_on_termination": a.delete_on_termination}
            for a in attachments
        ],
    }
    # Each of these arrived in a specific microversion, and is absent before it.
    for since, key, value in (
        ("2.9", "locked", server.locked),
        ("2.16", "host_status", "UP"),
        ("2.19", "description", None),
        ("2.26", "tags", list(server.tags or [])),
        ("2.63", "trusted_image_certificates", None),
        ("2.71", "server_groups", []),
    ):
        if request is None or at_least(request, since):
            body[key] = value

    if flavor is not None:
        if request is None or at_least(request, "2.47"):
            # 2.47 embeds the flavor; before it the server only linked to one, which is
            # why a deleted flavor used to make old servers unreadable.
            body["flavor"] = {
                "vcpus": server.allocated_vcpus or flavor.vcpus,
                "ram": server.allocated_ram_mb or flavor.ram,
                "disk": server.allocated_disk_gb or flavor.disk,
                "ephemeral": flavor.ephemeral,
                "swap": flavor.swap or 0,
                "original_name": flavor.name,
                "extra_specs": dict(flavor.extra_specs or {}),
            }
        else:
            body["flavor"] = {"id": flavor.id, "links": _links("flavors", flavor.id)}
    if server.fault:
        body["fault"] = server.fault
    return body


async def _server_context(
    session: AsyncSession, servers: list[Server]
) -> tuple[dict[str, Flavor], dict[str, list[Port]], dict[str, str], dict[str, list[VolumeAttachment]]]:
    """Batch-load flavors, ports, network labels and attachments for a page of servers."""
    if not servers:
        return {}, {}, {}, {}
    ids = [s.id for s in servers]
    flavor_ids = {s.flavor_id for s in servers}
    flavors = {
        f.id: f
        for f in (
            await session.execute(select(Flavor).where(Flavor.id.in_(flavor_ids)))
        ).scalars()
    }
    port_rows = (
        await session.execute(select(Port).where(Port.device_id.in_(ids)))
    ).scalars().all()
    ports: dict[str, list[Port]] = {}
    for port in port_rows:
        ports.setdefault(port.device_id, []).append(port)
    networks = {
        n.id: n.name
        for n in (
            await session.execute(
                select(Network).where(
                    Network.id.in_({p.network_id for p in port_rows} or {""})
                )
            )
        ).scalars()
    }
    attach_rows = (
        await session.execute(
            select(VolumeAttachment).where(VolumeAttachment.server_id.in_(ids))
        )
    ).scalars().all()
    attachments: dict[str, list[VolumeAttachment]] = {}
    for attachment in attach_rows:
        attachments.setdefault(attachment.server_id, []).append(attachment)
    return flavors, ports, networks, attachments


async def _get_server(session: AsyncSession, server_id: str) -> Server:
    server = await session.get(Server, server_id)
    if server is None or server.deleted:
        raise fault(SERVICE, 404, f"Instance {server_id} could not be found.")
    return resolve_server(server)


async def _resolve_flavor(
    session: AsyncSession, ref: str | None, missing: int = 400
) -> Flavor:
    """Look a flavor up by id, name or href.

    A flavorRef the caller made up is a bad request (400); addressing the flavor
    resource itself at ``/flavors/{id}`` is a 404, which is how Nova splits it.
    """
    if not ref:
        raise fault(SERVICE, 400, "Missing flavorRef attribute.")
    ref = ref.rstrip("/").split("/")[-1]
    flavor = await session.get(Flavor, ref)
    if flavor is None:
        flavor = (
            await session.execute(select(Flavor).where(Flavor.name == ref))
        ).scalar_one_or_none()
    if flavor is None:
        raise fault(SERVICE, missing, f"Flavor {ref} could not be found.")
    return flavor


# --------------------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------------------


def _version_doc(status: str = "CURRENT") -> dict[str, Any]:
    return {
        "id": "v2.1",
        "status": status,
        "version": MAX_VERSION,
        "min_version": MIN_VERSION,
        "updated": "2013-07-23T11:33:21Z",
        "links": [{"rel": "self", "href": service_url(SERVICE, "/v2.1/")}],
        "media-types": [
            {"base": "application/json", "type": "application/vnd.openstack.compute+json;version=2.1"}
        ],
    }


@router.get("/", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {"versions": [_version_doc("CURRENT")]}


@router.get("/v2.1", include_in_schema=False)
@router.get("/v2.1/", include_in_schema=False)
async def version_detail() -> dict[str, Any]:
    return {"version": _version_doc("CURRENT")}


@router.get("/v2.1/extensions")
async def extensions(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"extensions": []}


# --------------------------------------------------------------------------------------
# Flavors
# --------------------------------------------------------------------------------------


@router.get("/v2.1/flavors")
async def list_flavors(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    page = page_request(request.query_params, SERVICE)
    # Flavors page smallest-first, the order the collection is already sorted in.
    stmt = await paginate(
        session, select(Flavor), Flavor, page, sort_column=Flavor.ram, descending=False
    )
    flavors = list((await session.execute(stmt)).scalars().all())
    return {
        "flavors": [
            {"id": f.id, "name": f.name, "description": f.description,
             "links": _links("flavors", f.id)}
            for f in flavors
        ],
        **collection_links(request, "flavors", flavors, page),
    }


@router.get("/v2.1/flavors/detail")
async def list_flavors_detail(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(
        session, select(Flavor), Flavor, page, sort_column=Flavor.ram, descending=False
    )
    flavors = list((await session.execute(stmt)).scalars().all())
    return {
        "flavors": [flavor_dict(f) for f in flavors],
        **collection_links(request, "flavors", flavors, page),
    }


@router.post("/v2.1/flavors", status_code=200)
async def create_flavor(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = FlavorPayload(**(body.get("flavor") or {}))
    flavor = Flavor(
        id=payload.id or gen_id(),
        name=payload.name,
        vcpus=payload.vcpus,
        ram=payload.ram,
        disk=payload.disk,
        ephemeral=payload.ephemeral,
        swap=int(payload.swap or 0),
        rxtx_factor=payload.rxtx_factor,
        is_public=payload.is_public,
        description=payload.description,
    )
    session.add(flavor)
    await session.commit()
    return {"flavor": flavor_dict(flavor)}


@router.get("/v2.1/flavors/{flavor_id}")
async def get_flavor(
    flavor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    flavor = await _resolve_flavor(session, flavor_id, missing=404)
    return {"flavor": flavor_dict(flavor)}


@router.get("/v2.1/flavors/{flavor_id}/os-extra_specs")
async def flavor_extra_specs(
    flavor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    flavor = await _resolve_flavor(session, flavor_id, missing=404)
    return {"extra_specs": dict(flavor.extra_specs or {})}


@router.delete("/v2.1/flavors/{flavor_id}", status_code=202)
async def delete_flavor(
    flavor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    flavor = await _resolve_flavor(session, flavor_id, missing=404)
    await session.delete(flavor)
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Servers
# --------------------------------------------------------------------------------------


async def _list_servers(
    session: AsyncSession, request: Request, auth: AuthContext, detail: bool
) -> dict[str, Any]:
    params = request.query_params
    stmt = select(Server).where(Server.deleted.is_(False))
    if params.get("all_tenants") not in ("1", "True", "true"):
        stmt = stmt.where(Server.project_id == auth.project_id)
    if "name" in params:
        stmt = stmt.where(Server.name.like(f"%{params['name']}%"))
    if "status" in params:
        stmt = stmt.where(Server.status == params["status"].upper())
    page = page_request(params, SERVICE)
    stmt = await paginate(session, stmt, Server, page, sort_column=Server.created_at)
    servers = list((await session.execute(stmt)).scalars().all())
    for server in servers:
        resolve_server(server)
    await session.commit()

    flavors, ports, networks, attachments = await _server_context(session, servers)
    return {
        "servers": [
            server_dict(
                s,
                flavors.get(s.flavor_id),
                ports.get(s.id, []),
                networks,
                attachments.get(s.id, []),
                detail=detail,
                request=request,
            )
            for s in servers
        ],
        **collection_links(request, "servers", servers, page),
    }


@router.get("/v2.1/servers")
async def list_servers(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _list_servers(session, request, auth, detail=False)


@router.get("/v2.1/servers/detail")
async def list_servers_detail(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _list_servers(session, request, auth, detail=True)


@router.post("/v2.1/servers", status_code=202)
async def create_server(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Book the flavor's footprint against the node, then return BUILD for 10-60s."""
    payload = ServerPayload(**(body.get("server") or {}))
    flavor = await _resolve_flavor(session, payload.flavorRef)

    image: Image | None = None
    if payload.imageRef:
        image = await session.get(Image, payload.imageRef)
        if image is None or image.deleted:
            raise fault(SERVICE, 400, f"Image {payload.imageRef} could not be found.")
        if image.min_ram and flavor.ram < image.min_ram:
            raise fault(
                SERVICE,
                400,
                f"Flavor's memory is too small for requested image. "
                f"Flavor RAM {flavor.ram}MB < image minimum {image.min_ram}MB.",
            )
        if image.min_disk and flavor.disk < image.min_disk:
            raise fault(SERVICE, 400, "Flavor's disk is too small for requested image.")
    elif not payload.block_device_mapping_v2:
        raise fault(SERVICE, 400, "Missing imageRef attribute.")

    # -- depletion check ---------------------------------------------------------------
    try:
        await check_instance_capacity(session, flavor.vcpus, flavor.ram, flavor.disk)
    except CapacityError as exc:
        raise fault(SERVICE, 403, f"Quota exceeded for {exc.resource}: {exc}")

    host = await get_host(session)
    admin_pass = "".join(random.choices(string.ascii_letters + string.digits, k=12))
    server = Server(
        id=gen_id(),
        name=payload.name,
        project_id=auth.project_id,
        user_id=auth.user_id,
        flavor_id=flavor.id,
        image_id=image.id if image else None,
        key_name=payload.key_name,
        host=host.hostname,
        availability_zone=payload.availability_zone or "nova",
        status="BUILD",
        vm_state="building",
        task_state="spawning",
        power_state=0,
        allocated_vcpus=flavor.vcpus,
        allocated_ram_mb=flavor.ram,
        allocated_disk_gb=flavor.disk,
        overhead_ram_mb=settings.qemu_overhead_mb,
        metadata_=payload.metadata,
        user_data=payload.user_data,
        config_drive=bool(payload.config_drive),
        security_group_names=[
            g.get("name", "default") for g in payload.security_groups
        ] or ["default"],
        tags=payload.tags,
        transition_until=transition_deadline(),
        transition_target="ACTIVE",
        admin_pass=admin_pass,
    )
    session.add(server)
    await session.flush()

    await _attach_networks(session, server, payload, auth)
    await session.commit()

    return {
        "server": {
            "id": server.id,
            "links": _links("servers", server.id),
            "OS-DCF:diskConfig": "MANUAL",
            "adminPass": admin_pass,
            "security_groups": [{"name": n} for n in server.security_group_names],
        }
    }


async def _attach_networks(
    session: AsyncSession, server: Server, payload: ServerPayload, auth: AuthContext
) -> None:
    """Bind ports for the requested networks ("auto", "none" or an explicit list)."""
    requested = payload.networks
    if requested == "none":
        return
    specs: list[dict[str, Any]] = []
    if isinstance(requested, list):
        specs = [s for s in requested if isinstance(s, dict)]
    if not specs:  # "auto" or omitted
        network = await pick_network(session, auth.project_id)
        if network is not None:
            try:
                await create_port_record(
                    session,
                    network,
                    auth.project_id,
                    device_id=server.id,
                    device_owner="compute:nova",
                )
            except AddressPoolExhausted as exc:
                raise fault(SERVICE, 400, f"Cannot allocate a fixed IP: {exc}")
        return

    for spec in specs:
        if spec.get("port"):
            port = await session.get(Port, spec["port"])
            if port is None:
                raise fault(SERVICE, 400, f"Port {spec['port']} could not be found.")
            port.device_id = server.id
            port.device_owner = "compute:nova"
            port.status = "ACTIVE"
            continue
        network = await pick_network(session, auth.project_id, spec.get("uuid"))
        if network is None:
            raise fault(SERVICE, 400, f"Network {spec.get('uuid')} could not be found.")
        try:
            await create_port_record(
                session,
                network,
                auth.project_id,
                device_id=server.id,
                device_owner="compute:nova",
                fixed_ip=spec.get("fixed_ip"),
            )
        except AddressPoolExhausted as exc:
            raise fault(SERVICE, 400, f"Cannot allocate a fixed IP: {exc}")


@router.get("/v2.1/servers/{server_id}")
async def get_server(
    server_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    await session.commit()
    flavors, ports, networks, attachments = await _server_context(session, [server])
    return {
        "server": server_dict(
            server,
            flavors.get(server.flavor_id),
            ports.get(server.id, []),
            networks,
            attachments.get(server.id, []),
            request=request,
        )
    }


@router.put("/v2.1/servers/{server_id}")
async def update_server(
    server_id: str,
    body: dict[str, Any],
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    payload = body.get("server") or {}
    if "name" in payload:
        server.name = payload["name"]
    server.updated_at = now_utc()
    await session.commit()
    flavors, ports, networks, attachments = await _server_context(session, [server])
    return {
        "server": server_dict(
            server,
            flavors.get(server.flavor_id),
            ports.get(server.id, []),
            networks,
            attachments.get(server.id, []),
            request=request,
        )
    }


@router.delete("/v2.1/servers/{server_id}", status_code=204)
async def delete_server(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Release the booking, drop the ports and detach any volumes."""
    server = await _get_server(session, server_id)
    if server.locked:
        raise fault(SERVICE, 409, f"Instance {server_id} is locked.")
    _accrue(server)
    server.deleted = True
    server.status = "DELETED"
    server.vm_state = "deleted"
    server.power_state = 0
    server.task_state = None
    server.terminated_at = now_utc()
    server.updated_at = server.terminated_at

    ports = (
        await session.execute(select(Port).where(Port.device_id == server.id))
    ).scalars().all()
    for port in ports:
        await session.delete(port)

    attachments = (
        await session.execute(
            select(VolumeAttachment).where(VolumeAttachment.server_id == server.id)
        )
    ).scalars().all()
    for attachment in attachments:
        volume = await session.get(Volume, attachment.volume_id)
        if volume is not None:
            if attachment.delete_on_termination:
                volume.deleted = True
                volume.status = "deleted"
                volume.deleted_at = now_utc()
            else:
                volume.status = "available"
        await session.delete(attachment)

    await session.commit()
    return Response(status_code=204)


@router.get("/v2.1/servers/{server_id}/ips")
async def server_ips(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    _, ports, networks, _ = await _server_context(session, [server])
    return {"addresses": _addresses(ports.get(server.id, []), networks)}


@router.get("/v2.1/servers/{server_id}/metadata")
async def get_server_metadata(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    await session.commit()
    return {"metadata": dict(server.metadata_ or {})}


@router.put("/v2.1/servers/{server_id}/metadata")
async def replace_server_metadata(
    server_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    server.metadata_ = dict(body.get("metadata") or {})
    server.updated_at = now_utc()
    await session.commit()
    return {"metadata": dict(server.metadata_)}


@router.post("/v2.1/servers/{server_id}/metadata")
async def merge_server_metadata(
    server_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """POST merges keys; PUT replaces the whole map. `server set --property` uses this."""
    server = await _get_server(session, server_id)
    server.metadata_ = {**(server.metadata_ or {}), **(body.get("metadata") or {})}
    server.updated_at = now_utc()
    await session.commit()
    return {"metadata": dict(server.metadata_)}


@router.get("/v2.1/servers/{server_id}/metadata/{key}")
async def get_server_metadata_item(
    server_id: str,
    key: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    await session.commit()
    metadata = server.metadata_ or {}
    if key not in metadata:
        raise fault(SERVICE, 404, f"Metadata item {key} was not found.")
    return {"meta": {key: metadata[key]}}


@router.put("/v2.1/servers/{server_id}/metadata/{key}")
async def set_server_metadata_item(
    server_id: str,
    key: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    value = (body.get("meta") or {}).get(key)
    server.metadata_ = {**(server.metadata_ or {}), key: value}
    server.updated_at = now_utc()
    await session.commit()
    return {"meta": {key: value}}


@router.delete("/v2.1/servers/{server_id}/metadata/{key}", status_code=204)
async def delete_server_metadata_item(
    server_id: str,
    key: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    server = await _get_server(session, server_id)
    metadata = dict(server.metadata_ or {})
    if key not in metadata:
        raise fault(SERVICE, 404, f"Metadata item {key} was not found.")
    metadata.pop(key)
    server.metadata_ = metadata
    server.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


@router.get("/v2.1/servers/{server_id}/os-security-groups")
async def server_security_groups(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    names = server.security_group_names or []
    groups = (
        await session.execute(
            select(SecurityGroup).where(
                SecurityGroup.name.in_(names or [""]),
                SecurityGroup.project_id == server.project_id,
            )
        )
    ).scalars().all()
    return {
        "security_groups": [
            {
                "id": g.id,
                "name": g.name,
                "description": g.description,
                "tenant_id": g.project_id,
                "rules": [
                    {
                        "id": r.id,
                        "ip_protocol": r.protocol,
                        "from_port": r.port_range_min,
                        "to_port": r.port_range_max,
                        "ip_range": {"cidr": r.remote_ip_prefix} if r.remote_ip_prefix else {},
                        "parent_group_id": g.id,
                        "group": {},
                    }
                    for r in g.rules
                ],
            }
            for g in groups
        ]
    }


@router.get("/v2.1/servers/{server_id}/os-interface")
async def server_interfaces(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    ports = (
        await session.execute(select(Port).where(Port.device_id == server.id))
    ).scalars().all()
    return {
        "interfaceAttachments": [
            {
                "port_state": port.status,
                "fixed_ips": [
                    {"subnet_id": port.subnet_id, "ip_address": port.ip_address}
                ],
                "net_id": port.network_id,
                "port_id": port.id,
                "mac_addr": port.mac_address,
            }
            for port in ports
        ]
    }


@router.get("/v2.1/servers/{server_id}/diagnostics")
async def server_diagnostics(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    await session.commit()
    flavor = await session.get(Flavor, server.flavor_id)
    if flavor is None:
        raise fault(SERVICE, 404, f"Flavor {server.flavor_id} could not be found.")
    return telemetry.diagnostics(server, flavor)


# --------------------------------------------------------------------------------------
# Server actions
# --------------------------------------------------------------------------------------


@router.post("/v2.1/servers/{server_id}/action")
async def server_action(
    server_id: str,
    body: dict[str, Any],
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    server = await _get_server(session, server_id)
    if not body:
        raise fault(SERVICE, 400, "The server action request body is empty.")
    action = next(iter(body))
    argument = body[action]

    if action == "os-getConsoleOutput":
        flavor = await session.get(Flavor, server.flavor_id)
        image = await session.get(Image, server.image_id) if server.image_id else None
        port = (
            await session.execute(select(Port).where(Port.device_id == server.id))
        ).scalars().first()
        length = None
        if isinstance(argument, dict) and argument.get("length") is not None:
            length = int(argument["length"])
        output = telemetry.console_output(
            server,
            flavor,  # type: ignore[arg-type]
            image_name=image.name if image else "cirros",
            ip_address=port.ip_address if port else None,
            length=length,
        )
        await session.commit()
        return JSONResponse({"output": output})

    if action in ("os-start", "start"):
        if server.status not in ("SHUTOFF", "STOPPED"):
            raise fault(SERVICE, 409, f"Cannot 'os-start' instance {server_id} "
                                      f"while it is in vm_state {server.vm_state}")
        _set_state(server, "ACTIVE")
    elif action in ("os-stop", "stop"):
        if server.status != "ACTIVE":
            raise fault(SERVICE, 409, f"Cannot 'os-stop' instance {server_id} "
                                      f"while it is in vm_state {server.vm_state}")
        # SHUTOFF keeps the full compute + RAM booking on the host.
        _set_state(server, "SHUTOFF")
    elif action == "reboot":
        _set_state(server, "ACTIVE", task_state="rebooting")
        server.transition_until = transition_deadline()
        server.transition_target = "ACTIVE"
    elif action == "pause":
        _set_state(server, "PAUSED")
    elif action == "unpause":
        _set_state(server, "ACTIVE")
    elif action == "suspend":
        _set_state(server, "SUSPENDED")
    elif action == "resume":
        _set_state(server, "ACTIVE")
    elif action == "lock":
        server.locked = True
    elif action == "unlock":
        server.locked = False
    elif action == "shelve":
        _set_state(server, "SHELVED")
    elif action in ("shelveOffload", "os-shelveOffload"):
        # Offloading hands vCPU/RAM back to the free pool, storage stays booked.
        _set_state(server, "SHELVED_OFFLOADED")
    elif action == "unshelve":
        try:
            await check_instance_capacity(
                session, server.allocated_vcpus, server.allocated_ram_mb, 0
            )
        except CapacityError as exc:
            raise fault(SERVICE, 409, f"Cannot unshelve instance: {exc}")
        _set_state(server, "BUILD", task_state="spawning")
        server.transition_until = transition_deadline()
        server.transition_target = "ACTIVE"
    elif action == "resize":
        flavor_ref = (argument or {}).get("flavorRef")
        new_flavor = await _resolve_flavor(session, flavor_ref)
        delta_vcpus = max(new_flavor.vcpus - server.allocated_vcpus, 0)
        delta_ram = max(new_flavor.ram - server.allocated_ram_mb, 0)
        delta_disk = max(new_flavor.disk - server.allocated_disk_gb, 0)
        try:
            await check_instance_capacity(session, delta_vcpus, delta_ram, delta_disk)
        except CapacityError as exc:
            raise fault(SERVICE, 403, f"Quota exceeded for {exc.resource}: {exc}")
        server.flavor_id = new_flavor.id
        server.allocated_vcpus = new_flavor.vcpus
        server.allocated_ram_mb = new_flavor.ram
        server.allocated_disk_gb = new_flavor.disk
        _set_state(server, "VERIFY_RESIZE", task_state="resize_finish")
    elif action in ("confirmResize", "revertResize"):
        _set_state(server, "ACTIVE")
    elif action == "createImage":
        name = (argument or {}).get("name", f"{server.name}-snapshot")
        image = Image(
            id=gen_id(),
            name=name,
            owner=auth.project_id,
            status="active",
            visibility="private",
            container_format="bare",
            disk_format="qcow2",
            min_disk=server.allocated_disk_gb,
            min_ram=0,
            size=server.allocated_disk_gb * 1024 * 1024 * 1024 // 4,
            properties={"instance_uuid": server.id, "image_type": "snapshot"},
        )
        session.add(image)
        await session.commit()
        return Response(
            status_code=202,
            headers={"Location": service_url("glance", f"/v2/images/{image.id}")},
        )
    elif action == "rebuild":
        spec = argument or {}
        image_ref = spec.get("imageRef") or spec.get("image_ref")
        if not image_ref:
            raise fault(SERVICE, 400, "Missing imageRef attribute in rebuild request.")
        image = await session.get(Image, image_ref)
        if image is None or image.deleted:
            raise fault(SERVICE, 400, f"Image {image_ref} could not be found.")
        flavor = await session.get(Flavor, server.flavor_id)
        if flavor is not None and image.min_ram and flavor.ram < image.min_ram:
            raise fault(
                SERVICE,
                400,
                f"Flavor's memory is too small for requested image. "
                f"Flavor RAM {flavor.ram}MB < image minimum {image.min_ram}MB.",
            )
        server.image_id = image.id
        if spec.get("name"):
            server.name = spec["name"]
        if "metadata" in spec:
            server.metadata_ = spec["metadata"]
        # A rebuild reimages in place: the booking is unchanged, but the instance
        # goes back through BUILD like a fresh boot.
        _set_state(server, "BUILD", task_state="rebuilding")
        server.transition_until = transition_deadline()
        server.transition_target = "ACTIVE"
        await session.commit()
        flavors, ports, networks, attachments = await _server_context(session, [server])
        return JSONResponse(
            {
                "server": server_dict(
                    server, flavors.get(server.flavor_id), ports.get(server.id, []),
                    networks, attachments.get(server.id, []), request=request,
                )
            },
            status_code=202,
        )
    elif action in ("addSecurityGroup", "removeSecurityGroup"):
        name = (argument or {}).get("name")
        group = (
            await session.execute(
                select(SecurityGroup).where(
                    SecurityGroup.name == name,
                    SecurityGroup.project_id == auth.project_id,
                )
            )
        ).scalars().first()
        if group is None:
            raise fault(SERVICE, 404, f"Security group {name} not found.")
        attached = list(server.security_group_names or [])
        ports = (
            await session.execute(select(Port).where(Port.device_id == server.id))
        ).scalars().all()
        if action == "addSecurityGroup":
            if name not in attached:
                attached.append(name)
            for port in ports:  # Nova applies the group to the instance's ports
                if group.id not in (port.security_group_ids or []):
                    port.security_group_ids = [*(port.security_group_ids or []), group.id]
        else:
            if name not in attached:
                raise fault(
                    SERVICE, 400, f"Security group {name} is not associated with the instance."
                )
            attached.remove(name)
            for port in ports:
                port.security_group_ids = [
                    g for g in (port.security_group_ids or []) if g != group.id
                ]
        server.security_group_names = attached
        server.updated_at = now_utc()
    elif action == "os-resetState":
        _set_state(server, (argument or {}).get("state", "active").upper())
    else:
        raise fault(SERVICE, 400, f"Unsupported server action: {action}")

    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Volume attachments (the Nova side of the Cinder handshake)
# --------------------------------------------------------------------------------------


def attachment_dict(attachment: VolumeAttachment) -> dict[str, Any]:
    return {
        "id": attachment.volume_id,
        "volumeId": attachment.volume_id,
        "serverId": attachment.server_id,
        "device": attachment.device,
        "tag": None,
        "delete_on_termination": attachment.delete_on_termination,
        "attachment_id": attachment.id,
        "bdm_uuid": attachment.id,
    }


@router.get("/v2.1/servers/{server_id}/os-volume_attachments")
async def list_volume_attachments(
    server_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    rows = (
        await session.execute(
            select(VolumeAttachment).where(VolumeAttachment.server_id == server.id)
        )
    ).scalars().all()
    return {"volumeAttachments": [attachment_dict(a) for a in rows]}


@router.post("/v2.1/servers/{server_id}/os-volume_attachments", status_code=200)
async def attach_volume(
    server_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    server = await _get_server(session, server_id)
    payload = body.get("volumeAttachment") or {}
    volume = await session.get(Volume, payload.get("volumeId", ""))
    if volume is None or volume.deleted:
        raise fault(SERVICE, 404, f"Volume {payload.get('volumeId')} could not be found.")
    # The volume may still read as "creating" if nobody has polled it since its
    # transition window expired; settle that before judging whether it is attachable.
    settle_transition(volume)
    if volume.status != "available" and not volume.multiattach:
        raise fault(
            SERVICE,
            400,
            f"Invalid volume: volume {volume.id} status must be available, "
            f"currently {volume.status}.",
        )

    existing = (
        await session.execute(
            select(VolumeAttachment).where(VolumeAttachment.server_id == server.id)
        )
    ).scalars().all()
    device = payload.get("device") or f"/dev/vd{DEVICE_LETTERS[len(existing) % len(DEVICE_LETTERS)]}"
    attachment = VolumeAttachment(
        id=gen_id(),
        volume_id=volume.id,
        server_id=server.id,
        device=device,
        host_name=server.host,
        delete_on_termination=bool(payload.get("delete_on_termination", False)),
    )
    volume.status = "in-use"
    volume.updated_at = now_utc()
    session.add(attachment)
    await session.commit()
    return {"volumeAttachment": attachment_dict(attachment)}


@router.delete("/v2.1/servers/{server_id}/os-volume_attachments/{volume_id}", status_code=202)
async def detach_volume(
    server_id: str,
    volume_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    server = await _get_server(session, server_id)
    attachment = (
        await session.execute(
            select(VolumeAttachment).where(
                VolumeAttachment.server_id == server.id,
                VolumeAttachment.volume_id == volume_id,
            )
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise fault(SERVICE, 404, f"Volume {volume_id} is not attached to {server_id}.")
    volume = await session.get(Volume, volume_id)
    if volume is not None:
        volume.status = "available"
        volume.updated_at = now_utc()
    await session.delete(attachment)
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Keypairs
# --------------------------------------------------------------------------------------


def _fingerprint(public_key: str) -> str:
    body = public_key.split()[1] if len(public_key.split()) > 1 else public_key
    try:
        raw = base64.b64decode(body)
    except Exception:
        raw = public_key.encode()
    digest = hashlib.md5(raw).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


@router.get("/v2.1/os-keypairs")
async def list_keypairs(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (
        await session.execute(select(Keypair).where(Keypair.user_id == auth.user_id))
    ).scalars().all()
    return {"keypairs": [{"keypair": keypair_dict(k)} for k in rows]}


@router.post("/v2.1/os-keypairs", status_code=201)
async def create_keypair(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Import a public key, or synthesise a placeholder pair when none is supplied."""
    payload = KeypairPayload(**(body.get("keypair") or {}))
    existing = (
        await session.execute(
            select(Keypair).where(
                Keypair.name == payload.name, Keypair.user_id == auth.user_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise fault(SERVICE, 409, f"Key pair '{payload.name}' already exists.")

    generated_private: str | None = None
    public_key = payload.public_key
    if not public_key:
        seed = hashlib.sha512(f"{payload.name}:{auth.user_id}".encode()).digest()
        blob = base64.b64encode(seed * 4).decode()
        public_key = f"ssh-rsa {blob} simulated@{settings.host_name}"
        generated_private = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + "\n".join(
                base64.b64encode(seed * 8).decode()[i : i + 64] for i in range(0, 512, 64)
            )
            + "\n-----END RSA PRIVATE KEY-----\n"
        )

    keypair = Keypair(
        id=gen_id(),
        name=payload.name,
        user_id=payload.user_id or auth.user_id,
        public_key=public_key,
        fingerprint=_fingerprint(public_key),
        type=payload.type,
    )
    session.add(keypair)
    await session.commit()
    result = keypair_dict(keypair)
    if generated_private:
        # Simulated material: it identifies the keypair but unlocks nothing.
        result["private_key"] = generated_private
    return {"keypair": result}


@router.get("/v2.1/os-keypairs/{name}")
async def get_keypair(
    name: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    keypair = (
        await session.execute(select(Keypair).where(Keypair.name == name))
    ).scalars().first()
    if keypair is None:
        raise fault(SERVICE, 404, f"Keypair {name} not found.")
    return {"keypair": keypair_dict(keypair)}


@router.delete("/v2.1/os-keypairs/{name}", status_code=202)
async def delete_keypair(
    name: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    keypair = (
        await session.execute(select(Keypair).where(Keypair.name == name))
    ).scalars().first()
    if keypair is None:
        raise fault(SERVICE, 404, f"Keypair {name} not found.")
    await session.delete(keypair)
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Hypervisors, limits, availability zones, usage
# --------------------------------------------------------------------------------------


async def _hypervisor_dict(session: AsyncSession, detail: bool = True) -> dict[str, Any]:
    host = await get_host(session)
    usage = await get_usage(session, host)
    body: dict[str, Any] = {
        "id": host.id,
        "hypervisor_hostname": host.hostname,
        "state": host.state,
        "status": host.status,
    }
    if not detail:
        return body
    body.update(
        {
            "host_ip": host.host_ip,
            "hypervisor_type": host.hypervisor_type,
            "hypervisor_version": host.hypervisor_version,
            "vcpus": host.vcpus,
            "vcpus_used": usage.vcpus_used,
            "memory_mb": host.memory_mb,
            "memory_mb_used": usage.ram_used_mb,
            "free_ram_mb": int(host.memory_mb - usage.ram_used_mb),
            "local_gb": host.local_gb,
            "local_gb_used": usage.disk_used_gb,
            "free_disk_gb": int(host.local_gb - usage.disk_used_gb),
            "disk_available_least": int(usage.disk_free_gb),
            "running_vms": usage.running_vms,
            "current_workload": 0,
            "cpu_info": host.cpu_info or {},
            "service": {
                "id": 1,
                "host": host.hostname,
                "disabled_reason": None,
            },
        }
    )
    return body


@router.get("/v2.1/os-hypervisors")
async def list_hypervisors(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {"hypervisors": [await _hypervisor_dict(session, detail=False)]}


@router.get("/v2.1/os-hypervisors/detail")
async def list_hypervisors_detail(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    return {"hypervisors": [await _hypervisor_dict(session, detail=True)]}


@router.get("/v2.1/os-hypervisors/statistics")
async def hypervisor_statistics(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    host = await get_host(session)
    usage = await get_usage(session, host)
    return {
        "hypervisor_statistics": {
            "count": 1,
            "current_workload": 0,
            "disk_available_least": int(usage.disk_free_gb),
            "free_disk_gb": int(host.local_gb - usage.disk_used_gb),
            "free_ram_mb": int(host.memory_mb - usage.ram_used_mb),
            "local_gb": host.local_gb,
            "local_gb_used": usage.disk_used_gb,
            "memory_mb": host.memory_mb,
            "memory_mb_used": usage.ram_used_mb,
            "running_vms": usage.running_vms,
            "vcpus": host.vcpus,
            "vcpus_used": usage.vcpus_used,
        }
    }


@router.get("/v2.1/os-hypervisors/{hypervisor_id}")
async def get_hypervisor(
    hypervisor_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"hypervisor": await _hypervisor_dict(session, detail=True)}


@router.get("/v2.1/limits")
async def limits(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    usage = await get_usage(session)
    return {
        "limits": {
            "rate": [],
            "absolute": {
                "maxTotalCores": int(usage.vcpus_allocatable),
                "totalCoresUsed": usage.vcpus_used,
                "maxTotalRAMSize": int(usage.ram_allocatable_mb),
                "totalRAMUsed": usage.ram_used_mb,
                "maxTotalInstances": -1,
                "totalInstancesUsed": usage.total_instances,
                "maxTotalKeypairs": 100,
                "maxServerMeta": 128,
                "maxImageMeta": 128,
                "maxPersonality": 5,
                "maxPersonalitySize": 10240,
                "maxSecurityGroups": 100,
                "maxSecurityGroupRules": usage.conntrack_max,
                "maxServerGroups": 10,
                "maxServerGroupMembers": 10,
                "totalFloatingIpsUsed": 0,
                "maxTotalFloatingIps": 50,
                "totalSecurityGroupsUsed": 0,
            },
        }
    }


@router.get("/v2.1/os-availability-zone")
@router.get("/v2.1/os-availability-zone/detail")
async def availability_zones(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    host = await get_host(session)
    return {
        "availabilityZoneInfo": [
            {
                "zoneName": "nova",
                "zoneState": {"available": True},
                "hosts": {
                    host.hostname: {
                        "nova-compute": {
                            "available": True,
                            "active": True,
                            "updated_at": iso(now_utc()),
                        }
                    }
                },
            }
        ]
    }


@router.get("/v2.1/os-services")
async def services(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    host = await get_host(session)
    names = ("nova-conductor", "nova-scheduler", "nova-compute")
    return {
        "services": [
            {
                "id": index + 1,
                "binary": binary,
                "host": host.hostname,
                "state": "up",
                "status": "enabled",
                "zone": "internal" if binary != "nova-compute" else "nova",
                "updated_at": iso(now_utc()),
                "disabled_reason": None,
                "forced_down": False,
            }
            for index, binary in enumerate(names)
        ]
    }


@router.get("/v2.1/os-quota-sets/{project_id}")
async def quota_set(
    project_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Compute quotas. `openstack quota show` reads this alongside Cinder and Neutron."""
    usage = await get_usage(session)
    limits: dict[str, Any] = {
        "cores": int(usage.vcpus_allocatable),
        "ram": int(usage.ram_allocatable_mb),
        "instances": -1,
        "key_pairs": 100,
        "metadata_items": 128,
        "server_groups": 10,
        "server_group_members": 10,
    }
    # Nova stopped proxying the network quotas at 2.36 and dropped the personality-file
    # quotas with that feature at 2.57. Reporting them at a version that removed them is
    # how code ends up reading a key the real cloud will not send.
    if not at_least(request, "2.57"):
        limits |= {
            "injected_files": 5,
            "injected_file_content_bytes": 10240,
            "injected_file_path_bytes": 255,
        }
    if not at_least(request, "2.36"):
        limits |= {
            "fixed_ips": -1,
            "floating_ips": 50,
            "security_groups": 100,
            "security_group_rules": usage.conntrack_max,
        }
    if request.query_params.get("usage", "").lower() in ("true", "1"):
        in_use = {
            "cores": usage.vcpus_used,
            "ram": usage.ram_used_mb,
            "instances": usage.total_instances,
        }
        body: dict[str, Any] = {
            key: {"limit": value, "in_use": in_use.get(key, 0), "reserved": 0}
            for key, value in limits.items()
        }
    else:
        body = dict(limits)
    body["id"] = project_id
    return {"quota_set": body}


@router.get("/v2.1/os-simple-tenant-usage/{project_id}")
async def tenant_usage_detail(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-project usage, as `openstack usage show` requests it."""
    servers = (
        await session.execute(
            select(Server).where(
                Server.deleted.is_(False), Server.project_id == project_id
            )
        )
    ).scalars().all()
    now = now_utc()
    server_usages = []
    total_hours = 0.0
    for server in servers:
        hours = max((now - server.created_at).total_seconds(), 0) / 3600.0
        total_hours += hours
        server_usages.append(
            {
                "instance_id": server.id,
                "name": server.name,
                "hours": round(hours, 4),
                "flavor": server.flavor_id,
                "vcpus": server.allocated_vcpus,
                "memory_mb": server.allocated_ram_mb,
                "local_gb": server.allocated_disk_gb,
                "state": server.status.lower(),
                "uptime": int((now - server.created_at).total_seconds()),
                "started_at": iso(server.launched_at or server.created_at),
                "ended_at": iso(server.terminated_at),
                "tenant_id": server.project_id,
            }
        )
    return {
        "tenant_usage": {
            "tenant_id": project_id,
            "total_hours": round(total_hours, 4),
            "total_vcpus_usage": round(
                sum(s.allocated_vcpus for s in servers) * total_hours, 4
            ),
            "total_memory_mb_usage": round(
                sum(s.allocated_ram_mb for s in servers) * total_hours, 4
            ),
            "total_local_gb_usage": round(
                sum(s.allocated_disk_gb for s in servers) * total_hours, 4
            ),
            "server_usages": server_usages,
            "start": iso(min((s.created_at for s in servers), default=now)),
            "stop": iso(now),
        }
    }


@router.get("/v2.1/os-simple-tenant-usage")
async def tenant_usage(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    servers = (
        await session.execute(
            select(Server).where(
                Server.deleted.is_(False), Server.project_id == auth.project_id
            )
        )
    ).scalars().all()
    total_hours = sum(
        max((now_utc() - s.created_at).total_seconds(), 0) / 3600.0 for s in servers
    )
    return {
        "tenant_usages": [
            {
                "tenant_id": auth.project_id,
                "total_hours": round(total_hours, 4),
                "total_vcpus_usage": round(
                    sum(s.allocated_vcpus for s in servers) * total_hours, 4
                ),
                "total_memory_mb_usage": round(
                    sum(s.allocated_ram_mb for s in servers) * total_hours, 4
                ),
                "total_local_gb_usage": round(
                    sum(s.allocated_disk_gb for s in servers) * total_hours, 4
                ),
                "server_usages": [],
                "start": iso(min((s.created_at for s in servers), default=now_utc())),
                "stop": iso(now_utc()),
            }
        ]
    }
