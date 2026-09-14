"""Cinder Block Storage v3 (port 8776): volumes, types, snapshots and attachments."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import (
    API_VERSIONS,
    gen_id,
    iso_us,
    now_utc,
    service_url,
    settle_transition,
    transition_deadline,
)
from app.core.database import get_session
from app.core.pagination import collection_links, page_request, paginate
from app.core.middleware import AuthContext, OSPayload, fault, require
from app.models.compute import Server
from app.models.storage import Backup, Snapshot, Volume, VolumeAttachment, VolumeType
from app.services import quotas
from app.services.capacity import CapacityError, check_volume_capacity, get_usage

SERVICE = "cinder"
router = APIRouter()
auth_dep = require(SERVICE)

_, MIN_VERSION, MAX_VERSION = API_VERSIONS["cinder"]

# Top-level collections that must never be mistaken for a project id in the URL.
COLLECTIONS: frozenset[str] = frozenset(
    {
        "volumes",
        "types",
        "snapshots",
        "attachments",
        "backups",
        "limits",
        "extensions",
        "os-quota-sets",
        "os-volume-transfer",
        "group_types",
        "groups",
        "messages",
        "qos-specs",
        "scheduler-stats",
        "capabilities",
        "clusters",
        "default-types",
    }
)


class ProjectPathMiddleware:
    """Accept both ``/v3/volumes`` and the catalog's ``/v3/{project_id}/volumes``.

    The tenant-scoped form is what Keystone advertises and what older clients build,
    so the segment is simply dropped before routing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            parts = scope["path"].split("/")
            if len(parts) > 3 and parts[1] == "v3" and parts[2] not in COLLECTIONS:
                scope = dict(scope)
                scope["path"] = "/" + "/".join(["v3"] + parts[3:])
                scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------


class VolumePayload(OSPayload):
    size: int | None = None
    name: str | None = None
    description: str | None = None
    volume_type: str | None = None
    availability_zone: str = "nova"
    snapshot_id: str | None = None
    source_volid: str | None = None
    imageRef: str | None = None
    multiattach: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotPayload(OSPayload):
    volume_id: str
    name: str | None = None
    description: str | None = None
    force: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class VolumeTypePayload(OSPayload):
    name: str
    description: str = ""
    extra_specs: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def resolve_volume(volume: Volume) -> Volume:
    """creating -> available once the stored transition deadline elapses."""
    if settle_transition(volume):
        volume.updated_at = now_utc()
    return volume


def resolve_snapshot(snapshot: Snapshot) -> Snapshot:
    if settle_transition(snapshot):
        snapshot.updated_at = now_utc()
    return snapshot


def volume_dict(volume: Volume, attachments: list[VolumeAttachment]) -> dict[str, Any]:
    return {
        "id": volume.id,
        "name": volume.name,
        "description": volume.description,
        "status": volume.status,
        "size": volume.size,
        "volume_type": volume.volume_type,
        "volume_type_id": volume.volume_type,
        "availability_zone": volume.availability_zone,
        "bootable": "true" if volume.bootable else "false",
        "encrypted": volume.encrypted,
        "multiattach": volume.multiattach,
        "replication_status": volume.replication_status,
        "snapshot_id": volume.snapshot_id,
        "source_volid": volume.source_volid,
        "group_id": None,
        "consistencygroup_id": None,
        "user_id": volume.user_id,
        "os-vol-tenant-attr:tenant_id": volume.project_id,
        "os-vol-host-attr:host": volume.host,
        "metadata": dict(volume.metadata_ or {}),
        "created_at": iso_us(volume.created_at),
        "updated_at": iso_us(volume.updated_at),
        "attachments": [
            {
                "id": volume.id,
                "attachment_id": a.id,
                "volume_id": volume.id,
                "server_id": a.server_id,
                "host_name": a.host_name,
                "device": a.device,
                "attached_at": iso_us(a.attached_at),
            }
            for a in attachments
        ],
        "links": [
            {"rel": "self", "href": service_url(SERVICE, f"/v3/volumes/{volume.id}")},
            {"rel": "bookmark", "href": service_url(SERVICE, f"/volumes/{volume.id}")},
        ],
    }


def snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "name": snapshot.name,
        "description": snapshot.description,
        "volume_id": snapshot.volume_id,
        "status": snapshot.status,
        "size": snapshot.size,
        "created_at": iso_us(snapshot.created_at),
        "updated_at": iso_us(snapshot.updated_at),
        "metadata": dict(snapshot.metadata_ or {}),
        "os-extended-snapshot-attributes:project_id": snapshot.project_id,
        "os-extended-snapshot-attributes:progress": "100%",
    }


def volume_type_dict(vtype: VolumeType) -> dict[str, Any]:
    return {
        "id": vtype.id,
        "name": vtype.name,
        "description": vtype.description,
        "is_public": vtype.is_public,
        "os-volume-type-access:is_public": vtype.is_public,
        "extra_specs": dict(vtype.extra_specs or {}),
        "qos_specs_id": None,
    }


async def _get_volume(session: AsyncSession, volume_id: str) -> Volume:
    volume = await session.get(Volume, volume_id)
    if volume is None or volume.deleted:
        raise fault(SERVICE, 404, f"Volume {volume_id} could not be found.")
    return resolve_volume(volume)


async def _attachments_for(
    session: AsyncSession, volume_ids: list[str]
) -> dict[str, list[VolumeAttachment]]:
    if not volume_ids:
        return {}
    rows = (
        await session.execute(
            select(VolumeAttachment).where(VolumeAttachment.volume_id.in_(volume_ids))
        )
    ).scalars().all()
    grouped: dict[str, list[VolumeAttachment]] = {}
    for row in rows:
        grouped.setdefault(row.volume_id, []).append(row)
    return grouped


# --------------------------------------------------------------------------------------
# Version discovery
# --------------------------------------------------------------------------------------


def _version_doc() -> dict[str, Any]:
    return {
        "id": "v3.0",
        "status": "CURRENT",
        "version": MAX_VERSION,
        "min_version": MIN_VERSION,
        "updated": "2018-07-17T00:00:00Z",
        "links": [{"rel": "self", "href": service_url(SERVICE, "/v3/")}],
        "media-types": [{"base": "application/json", "type": "application/vnd.openstack.volume+json;version=3"}],
    }


@router.get("/", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {"versions": [_version_doc()]}


@router.get("/v3", include_in_schema=False)
@router.get("/v3/", include_in_schema=False)
async def version_detail() -> dict[str, Any]:
    return {"versions": [_version_doc()]}


# --------------------------------------------------------------------------------------
# Volumes
# --------------------------------------------------------------------------------------


async def _list_volumes(
    session: AsyncSession, request: Request, auth: AuthContext, detail: bool
) -> dict[str, Any]:
    stmt = select(Volume).where(Volume.deleted.is_(False))
    params = request.query_params
    if params.get("all_tenants") not in ("1", "true", "True"):
        stmt = stmt.where(Volume.project_id == auth.project_id)
    if "name" in params:
        stmt = stmt.where(Volume.name == params["name"])
    if "status" in params:
        stmt = stmt.where(Volume.status == params["status"])
    page = page_request(params, SERVICE)
    stmt = await paginate(session, stmt, Volume, page, sort_column=Volume.created_at)
    volumes = list((await session.execute(stmt)).scalars().all())
    for volume in volumes:
        resolve_volume(volume)
    await session.commit()

    if not detail:
        return {
            "volumes": [
                {
                    "id": v.id,
                    "name": v.name,
                    "links": [
                        {"rel": "self", "href": service_url(SERVICE, f"/v3/volumes/{v.id}")}
                    ],
                }
                for v in volumes
            ],
            **collection_links(request, "volumes", volumes, page),
        }
    attachments = await _attachments_for(session, [v.id for v in volumes])
    return {
        "volumes": [volume_dict(v, attachments.get(v.id, [])) for v in volumes],
        **collection_links(request, "volumes", volumes, page),
    }


@router.get("/v3/volumes")
async def list_volumes(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _list_volumes(session, request, auth, detail=False)


@router.get("/v3/volumes/detail")
async def list_volumes_detail(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _list_volumes(session, request, auth, detail=True)


@router.post("/v3/volumes", status_code=202)
async def create_volume(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = VolumePayload(**(body.get("volume") or {}))
    size = payload.size
    if size is None and payload.source_volid:
        source = await _get_volume(session, payload.source_volid)
        size = source.size
    if size is None and payload.snapshot_id:
        snapshot = await session.get(Snapshot, payload.snapshot_id)
        size = snapshot.size if snapshot else None
    if not size or size <= 0:
        raise fault(SERVICE, 400, "Invalid input received: 'size' must be a positive integer.")

    # The project's own limits first, then the shared storage pool.
    try:
        await quotas.enforce_all(
            session, SERVICE, auth.project_id, {"volumes": 1, "gigabytes": int(size)}
        )
    except quotas.QuotaError as exc:
        raise fault(
            SERVICE, 413,
            f"VolumeLimitExceeded: Maximum number of volumes allowed "
            f"({exc.limit}) exceeded for quota '{exc.resource}'."
            if exc.resource == "volumes" else f"VolumeSizeExceedsAvailableQuota: {exc}",
        )
    try:
        await check_volume_capacity(session, int(size))
    except CapacityError as exc:
        raise fault(SERVICE, 413, f"VolumeSizeExceedsAvailableQuota: {exc}")

    default_type = (
        await session.execute(select(VolumeType).where(VolumeType.is_default.is_(True)))
    ).scalars().first()
    volume = Volume(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        project_id=auth.project_id,
        user_id=auth.user_id,
        size=int(size),
        status="creating",
        volume_type=payload.volume_type
        or (default_type.name if default_type else "__DEFAULT__"),
        availability_zone=payload.availability_zone,
        bootable=bool(payload.imageRef),
        image_id=payload.imageRef,
        multiattach=payload.multiattach,
        snapshot_id=payload.snapshot_id,
        source_volid=payload.source_volid,
        metadata_=payload.metadata,
        host="cinder@lvm#LVM_iSCSI",
        transition_until=transition_deadline(),
        transition_target="available",
    )
    session.add(volume)
    await session.commit()
    return {"volume": volume_dict(volume, [])}


@router.get("/v3/volumes/{volume_id}")
async def get_volume(
    volume_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    volume = await _get_volume(session, volume_id)
    await session.commit()
    attachments = await _attachments_for(session, [volume.id])
    return {"volume": volume_dict(volume, attachments.get(volume.id, []))}


@router.put("/v3/volumes/{volume_id}")
async def update_volume(
    volume_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    volume = await _get_volume(session, volume_id)
    for key, value in (body.get("volume") or {}).items():
        if key in ("name", "description"):
            setattr(volume, key, value)
        elif key == "metadata":
            volume.metadata_ = value
    volume.updated_at = now_utc()
    await session.commit()
    attachments = await _attachments_for(session, [volume.id])
    return {"volume": volume_dict(volume, attachments.get(volume.id, []))}


@router.delete("/v3/volumes/{volume_id}", status_code=202)
async def delete_volume(
    volume_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    volume = await _get_volume(session, volume_id)
    if volume.status == "in-use":
        raise fault(SERVICE, 400, f"Invalid volume: Volume {volume_id} is still attached.")
    volume.deleted = True
    volume.status = "deleted"
    volume.deleted_at = now_utc()
    volume.updated_at = volume.deleted_at
    await session.commit()
    return Response(status_code=202)


@router.post("/v3/volumes/{volume_id}/action", status_code=202)
async def volume_action(
    volume_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    volume = await _get_volume(session, volume_id)
    if not body:
        raise fault(SERVICE, 400, "The volume action request body is empty.")
    action = next(iter(body))
    argument = body[action] or {}

    if action == "os-extend":
        new_size = int(argument.get("new_size", volume.size))
        if new_size <= volume.size:
            raise fault(SERVICE, 400, "New size must be greater than the current size.")
        try:
            await check_volume_capacity(session, new_size - volume.size)
        except CapacityError as exc:
            raise fault(SERVICE, 413, f"VolumeSizeExceedsAvailableQuota: {exc}")
        volume.size = new_size
        volume.status = "extending"
        volume.transition_until = transition_deadline()
        volume.transition_target = "available"
    elif action == "os-reset_status":
        volume.status = argument.get("status", "available")
        volume.transition_until = None
    elif action == "os-set_bootable":
        volume.bootable = str(argument.get("bootable", "false")).lower() == "true"
    elif action == "os-attach":
        server_id = argument.get("instance_uuid", "")
        session.add(
            VolumeAttachment(
                id=gen_id(),
                volume_id=volume.id,
                server_id=server_id,
                device=argument.get("mountpoint", "/dev/vdb"),
            )
        )
        volume.status = "in-use"
    elif action == "os-detach":
        rows = (
            await session.execute(
                select(VolumeAttachment).where(VolumeAttachment.volume_id == volume.id)
            )
        ).scalars().all()
        for row in rows:
            await session.delete(row)
        volume.status = "available"
    elif action == "revert":
        volume.status = "reverting"
        volume.transition_until = transition_deadline()
        volume.transition_target = "available"
    else:
        raise fault(SERVICE, 400, f"Unsupported volume action: {action}")

    volume.updated_at = now_utc()
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Attachments (microversion 3.27 style)
# --------------------------------------------------------------------------------------


def attachment_dict(attachment: VolumeAttachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "volume_id": attachment.volume_id,
        "instance": attachment.server_id,
        "status": attachment.attach_status,
        "attach_mode": attachment.attach_mode,
        "attached_at": iso_us(attachment.attached_at),
        "detached_at": None,
        "connection_info": {
            "driver_volume_type": "iscsi",
            "target_iqn": f"iqn.2010-10.org.openstack:volume-{attachment.volume_id}",
            "target_portal": "127.0.0.1:3260",
            "target_lun": 0,
            "device_path": attachment.device,
        },
    }


@router.get("/v3/attachments")
async def list_attachments(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    rows = (await session.execute(select(VolumeAttachment))).scalars().all()
    return {"attachments": [attachment_dict(a) for a in rows]}


@router.post("/v3/attachments", status_code=200)
async def create_attachment(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = body.get("attachment") or {}
    volume = await _get_volume(session, payload.get("volume_uuid", ""))
    server_id = payload.get("instance_uuid", "")
    if server_id:
        server = await session.get(Server, server_id)
        if server is None or server.deleted:
            raise fault(SERVICE, 404, f"Instance {server_id} could not be found.")
    attachment = VolumeAttachment(
        id=gen_id(),
        volume_id=volume.id,
        server_id=server_id,
        device=(payload.get("connector") or {}).get("mountpoint", "/dev/vdb"),
    )
    volume.status = "in-use"
    session.add(attachment)
    await session.commit()
    return {"attachment": attachment_dict(attachment)}


@router.get("/v3/attachments/{attachment_id}")
async def get_attachment(
    attachment_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    attachment = await session.get(VolumeAttachment, attachment_id)
    if attachment is None:
        raise fault(SERVICE, 404, f"Attachment {attachment_id} could not be found.")
    return {"attachment": attachment_dict(attachment)}


@router.delete("/v3/attachments/{attachment_id}", status_code=200)
async def delete_attachment(
    attachment_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    attachment = await session.get(VolumeAttachment, attachment_id)
    if attachment is None:
        raise fault(SERVICE, 404, f"Attachment {attachment_id} could not be found.")
    volume = await session.get(Volume, attachment.volume_id)
    if volume is not None:
        volume.status = "available"
    await session.delete(attachment)
    await session.commit()
    return Response(status_code=200)


# --------------------------------------------------------------------------------------
# Volume types
# --------------------------------------------------------------------------------------


@router.get("/v3/types")
async def list_types(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    types = (await session.execute(select(VolumeType))).scalars().all()
    return {"volume_types": [volume_type_dict(t) for t in types]}


@router.post("/v3/types", status_code=202)
async def create_type(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = VolumeTypePayload(**(body.get("volume_type") or {}))
    vtype = VolumeType(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        extra_specs=payload.extra_specs,
    )
    session.add(vtype)
    await session.commit()
    return {"volume_type": volume_type_dict(vtype)}


@router.get("/v3/types/{type_id}")
async def get_type(
    type_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    vtype = await session.get(VolumeType, type_id)
    if vtype is None:
        vtype = (
            await session.execute(select(VolumeType).where(VolumeType.name == type_id))
        ).scalars().first()
    if vtype is None:
        raise fault(SERVICE, 404, f"Volume type {type_id} could not be found.")
    return {"volume_type": volume_type_dict(vtype)}


@router.delete("/v3/types/{type_id}", status_code=202)
async def delete_type(
    type_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    vtype = await session.get(VolumeType, type_id)
    if vtype is None:
        raise fault(SERVICE, 404, f"Volume type {type_id} could not be found.")
    await session.delete(vtype)
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------


@router.get("/v3/snapshots")
@router.get("/v3/snapshots/detail")
async def list_snapshots(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Snapshot).where(
        Snapshot.deleted.is_(False), Snapshot.project_id == auth.project_id
    )
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(session, stmt, Snapshot, page, sort_column=Snapshot.created_at)
    rows = list((await session.execute(stmt)).scalars().all())
    for snapshot in rows:
        resolve_snapshot(snapshot)
    await session.commit()
    return {
        "snapshots": [snapshot_dict(s) for s in rows],
        **collection_links(request, "snapshots", rows, page),
    }


@router.post("/v3/snapshots", status_code=202)
async def create_snapshot(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = SnapshotPayload(**(body.get("snapshot") or {}))
    volume = await _get_volume(session, payload.volume_id)
    if volume.status == "in-use" and not payload.force:
        raise fault(
            SERVICE,
            400,
            f"Invalid volume: Volume {volume.id} is currently attached; use force.",
        )
    snapshot = Snapshot(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        volume_id=volume.id,
        project_id=auth.project_id,
        size=volume.size,
        status="creating",
        force=payload.force,
        metadata_=payload.metadata,
        transition_until=transition_deadline(),
        transition_target="available",
    )
    session.add(snapshot)
    await session.commit()
    return {"snapshot": snapshot_dict(snapshot)}


@router.get("/v3/snapshots/{snapshot_id}")
async def get_snapshot(
    snapshot_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.deleted:
        raise fault(SERVICE, 404, f"Snapshot {snapshot_id} could not be found.")
    resolve_snapshot(snapshot)
    await session.commit()
    return {"snapshot": snapshot_dict(snapshot)}


@router.delete("/v3/snapshots/{snapshot_id}", status_code=202)
async def delete_snapshot(
    snapshot_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    snapshot = await session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.deleted:
        raise fault(SERVICE, 404, f"Snapshot {snapshot_id} could not be found.")
    snapshot.deleted = True
    snapshot.status = "deleted"
    await session.commit()
    return Response(status_code=202)


# --------------------------------------------------------------------------------------
# Limits / quotas / pools
# --------------------------------------------------------------------------------------


@router.get("/v3/limits")
async def limits(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    usage = await get_usage(session)
    return {
        "limits": {
            "rate": [],
            "absolute": {
                "totalSnapshotsUsed": 0,
                "maxTotalBackups": 10,
                "maxTotalVolumeGigabytes": int(usage.disk_allocatable_gb),
                "maxTotalSnapshots": 100,
                "maxTotalBackupGigabytes": 1000,
                "totalBackupGigabytesUsed": 0,
                "maxTotalVolumes": 100,
                "totalVolumesUsed": 0,
                "totalBackupsUsed": 0,
                "totalGigabytesUsed": usage.disk_used_volumes_gb,
            },
        }
    }


@router.get("/v3/os-quota-sets/{project_id}")
async def quota_sets(
    project_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Block-storage quotas. ``?usage=True`` adds usage, as Cinder's own API does."""
    if request.query_params.get("usage", "").lower() in ("true", "1"):
        body: dict[str, Any] = dict(await quotas.detail(session, SERVICE, project_id))
    else:
        body = dict(await quotas.limits(session, SERVICE, project_id))
    body["id"] = project_id
    return {"quota_set": body}


@router.get("/v3/os-quota-sets/{project_id}/defaults")
async def quota_set_defaults(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return {"quota_set": {**quotas.CINDER_DEFAULTS, "id": project_id}}


@router.put("/v3/os-quota-sets/{project_id}")
async def update_quota_set(
    project_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Set this project's storage limits, enforced on the next volume create."""
    try:
        values = quotas.parse_limits(SERVICE, body.get("quota_set") or {})
    except quotas.InvalidLimit as exc:
        raise fault(SERVICE, 400, exc.message)
    effective = await quotas.set_limits(session, SERVICE, project_id, values)
    await session.commit()
    return {"quota_set": {**effective, "id": project_id}}


@router.delete("/v3/os-quota-sets/{project_id}")
async def delete_quota_set(
    project_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await quotas.clear_limits(session, SERVICE, project_id)
    await session.commit()
    return Response(status_code=200)


@router.get("/v3/scheduler-stats/get_pools")
async def get_pools(
    auth: AuthContext = auth_dep, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    usage = await get_usage(session)
    return {
        "pools": [
            {
                "name": f"{usage.host}@lvm#LVM_iSCSI",
                "capabilities": {
                    "total_capacity_gb": usage.disk_total_gb,
                    "free_capacity_gb": usage.disk_free_gb,
                    "allocated_capacity_gb": usage.disk_used_volumes_gb,
                    "volume_backend_name": "LVM_iSCSI",
                    "storage_protocol": "iSCSI",
                    "vendor_name": "OpenStack-Simulator",
                    "driver_version": "1.0.0",
                    "timestamp": iso_us(now_utc()),
                },
            }
        ]
    }


# --------------------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------------------


class BackupPayload(OSPayload):
    volume_id: str
    name: str | None = None
    description: str | None = None
    container: str | None = None
    incremental: bool = False
    force: bool = False
    snapshot_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def resolve_backup(backup: Backup) -> Backup:
    if settle_transition(backup):
        backup.updated_at = now_utc()
    return backup


def backup_dict(backup: Backup) -> dict[str, Any]:
    return {
        "id": backup.id,
        "name": backup.name,
        "description": backup.description,
        "volume_id": backup.volume_id,
        "snapshot_id": backup.snapshot_id,
        "status": backup.status,
        "size": backup.size,
        "object_count": max(backup.size, 1),
        "container": backup.container,
        "availability_zone": backup.availability_zone,
        "has_dependent_backups": False,
        "is_incremental": backup.is_incremental,
        "fail_reason": backup.fail_reason,
        "data_timestamp": iso_us(backup.created_at),
        "created_at": iso_us(backup.created_at),
        "updated_at": iso_us(backup.updated_at),
        "metadata": dict(backup.metadata_ or {}),
        "os-backup-project-attr:project_id": backup.project_id,
        "user_id": backup.project_id,
        "links": [
            {"rel": "self", "href": service_url(SERVICE, f"/v3/backups/{backup.id}")}
        ],
    }


async def _get_backup(session: AsyncSession, backup_id: str) -> Backup:
    backup = await session.get(Backup, backup_id)
    if backup is None or backup.deleted:
        raise fault(SERVICE, 404, f"Backup {backup_id} could not be found.")
    return resolve_backup(backup)


async def _dependent_backups(session: AsyncSession, backup_id: str) -> bool:
    """True when an incremental backup was taken on top of this one."""
    return bool(
        (
            await session.execute(
                select(Backup.id).where(
                    Backup.parent_id == backup_id, Backup.deleted.is_(False)
                )
            )
        ).first()
    )


@router.get("/v3/backups")
@router.get("/v3/backups/detail")
async def list_backups(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Backup).where(
        Backup.deleted.is_(False), Backup.project_id == auth.project_id
    )
    if "volume_id" in request.query_params:
        stmt = stmt.where(Backup.volume_id == request.query_params["volume_id"])
    if "status" in request.query_params:
        stmt = stmt.where(Backup.status == request.query_params["status"])
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(session, stmt, Backup, page, sort_column=Backup.created_at)
    rows = list((await session.execute(stmt)).scalars().all())
    for backup in rows:
        resolve_backup(backup)
    await session.commit()

    detail = request.url.path.endswith("/detail")
    if detail:
        body: list[dict[str, Any]] = [backup_dict(b) for b in rows]
    else:
        body = [
            {
                "id": b.id,
                "name": b.name,
                "links": [
                    {"rel": "self", "href": service_url(SERVICE, f"/v3/backups/{b.id}")}
                ],
            }
            for b in rows
        ]
    return {"backups": body, **collection_links(request, "backups", rows, page)}


@router.post("/v3/backups", status_code=202)
async def create_backup(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    payload = BackupPayload(**(body.get("backup") or {}))
    volume = await _get_volume(session, payload.volume_id)
    # Cinder refuses to back up an attached volume unless forced: the filesystem is live
    # and the copy would be crash-consistent at best.
    if volume.status == "in-use" and not payload.force:
        raise fault(
            SERVICE,
            400,
            f"Invalid volume: Volume {volume.id} to be backed up must be available "
            f"or must be forced.",
        )
    if volume.status not in ("available", "in-use"):
        raise fault(
            SERVICE,
            400,
            f"Invalid volume: Volume {volume.id} is in {volume.status} status.",
        )

    try:
        await quotas.enforce_all(
            session,
            SERVICE,
            auth.project_id,
            {"backups": 1, "backup_gigabytes": volume.size},
        )
    except quotas.QuotaError as exc:
        raise fault(SERVICE, 413, f"BackupLimitExceeded: {exc}")

    parent_id: str | None = None
    if payload.incremental:
        # Status is settled lazily on read, so filtering on it in SQL would miss a
        # backup whose window has elapsed but which nothing has looked at yet.
        candidates = (
            await session.execute(
                select(Backup)
                .where(Backup.volume_id == volume.id, Backup.deleted.is_(False))
                .order_by(Backup.created_at.desc())
            )
        ).scalars().all()
        parent = next(
            (b for b in candidates if resolve_backup(b).status == "available"), None
        )
        if parent is None:
            raise fault(
                SERVICE,
                400,
                "Invalid backup: No backups available to do an incremental backup.",
            )
        parent_id = parent.id

    backup = Backup(
        id=gen_id(),
        name=payload.name,
        description=payload.description,
        volume_id=volume.id,
        snapshot_id=payload.snapshot_id,
        project_id=auth.project_id,
        size=volume.size,
        status="creating",
        container=payload.container or "volumebackups",
        availability_zone="nova",
        is_incremental=payload.incremental,
        parent_id=parent_id,
        metadata_=payload.metadata,
        transition_until=transition_deadline(),
        transition_target="available",
    )
    session.add(backup)
    # The volume is held in backing-up until the copy finishes, which is what stops a
    # second backup or a delete from racing it.
    if volume.status == "available":
        volume.status = "backing-up"
        volume.transition_until = backup.transition_until
        volume.transition_target = "available"
    await session.commit()
    return {"backup": {
        "id": backup.id,
        "name": backup.name,
        "links": backup_dict(backup)["links"],
    }}


@router.get("/v3/backups/{backup_id}")
async def get_backup(
    backup_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    backup = await _get_backup(session, backup_id)
    await session.commit()
    body = backup_dict(backup)
    body["has_dependent_backups"] = await _dependent_backups(session, backup.id)
    return {"backup": body}


@router.put("/v3/backups/{backup_id}")
async def update_backup(
    backup_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    backup = await _get_backup(session, backup_id)
    payload = body.get("backup") or {}
    if "name" in payload:
        backup.name = payload["name"]
    if "description" in payload:
        backup.description = payload["description"]
    if "metadata" in payload:
        backup.metadata_ = payload["metadata"]
    backup.updated_at = now_utc()
    await session.commit()
    return {"backup": backup_dict(backup)}


@router.post("/v3/backups/{backup_id}/restore")
async def restore_backup(
    backup_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Restore into an existing volume, or into a new one created for the purpose."""
    backup = await _get_backup(session, backup_id)
    if backup.status != "available":
        raise fault(
            SERVICE,
            400,
            f"Invalid backup: Backup status must be available, is {backup.status}.",
        )
    payload = body.get("restore") or {}
    volume_id = payload.get("volume_id")

    if volume_id:
        volume = await _get_volume(session, volume_id)
        if volume.status != "available":
            raise fault(
                SERVICE,
                400,
                f"Invalid volume: Volume to be restored to must be available, "
                f"is {volume.status}.",
            )
        if volume.size < backup.size:
            raise fault(
                SERVICE,
                400,
                f"Invalid volume: volume size {volume.size} is smaller than "
                f"backup size {backup.size}.",
            )
    else:
        # No target given: Cinder creates one the size of the backup.
        try:
            await quotas.enforce_all(
                session,
                SERVICE,
                auth.project_id,
                {"volumes": 1, "gigabytes": backup.size},
            )
        except quotas.QuotaError as exc:
            raise fault(SERVICE, 413, f"VolumeSizeExceedsAvailableQuota: {exc}")
        try:
            await check_volume_capacity(session, backup.size)
        except CapacityError as exc:
            raise fault(SERVICE, 413, f"VolumeSizeExceedsAvailableQuota: {exc}")
        default_type = (
            await session.execute(select(VolumeType).where(VolumeType.is_default.is_(True)))
        ).scalars().first()
        volume = Volume(
            id=gen_id(),
            name=payload.get("name") or f"restore_backup_{backup.id}",
            description=f"Restored from backup {backup.id}",
            project_id=auth.project_id,
            user_id=auth.user_id,
            size=backup.size,
            status="creating",
            volume_type=default_type.name if default_type else "__DEFAULT__",
            availability_zone="nova",
        )
        session.add(volume)

    volume.status = "restoring-backup"
    volume.transition_until = transition_deadline()
    volume.transition_target = "available"
    backup.status = "restoring"
    backup.transition_until = volume.transition_until
    backup.transition_target = "available"
    await session.commit()
    return {
        "restore": {
            "backup_id": backup.id,
            "volume_id": volume.id,
            "volume_name": volume.name,
        }
    }


@router.post("/v3/backups/{backup_id}/action")
async def backup_action(
    backup_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    backup = await _get_backup(session, backup_id)
    if "os-reset_status" in body:
        backup.status = (body["os-reset_status"] or {}).get("status", "available")
        backup.transition_until = None
        backup.transition_target = None
    elif "os-force_delete" in body:
        backup.deleted = True
        backup.status = "deleted"
    else:
        raise fault(SERVICE, 400, f"Invalid backup action: {list(body)[:1]}")
    backup.updated_at = now_utc()
    await session.commit()
    return Response(status_code=202)


@router.delete("/v3/backups/{backup_id}", status_code=202)
async def delete_backup(
    backup_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    backup = await _get_backup(session, backup_id)
    if backup.status not in ("available", "error"):
        raise fault(
            SERVICE,
            400,
            f"Invalid backup: Backup status must be available or error, "
            f"is {backup.status}.",
        )
    # An incremental backup is a diff against its parent, so removing the parent would
    # leave the child unrestorable.
    if await _dependent_backups(session, backup.id):
        raise fault(
            SERVICE,
            400,
            "Invalid backup: Incremental backups exist for this backup.",
        )
    backup.deleted = True
    backup.status = "deleting"
    backup.updated_at = now_utc()
    await session.commit()
    return Response(status_code=202)
