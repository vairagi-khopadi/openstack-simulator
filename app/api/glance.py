"""Glance Image v2 (port 9292).

The catalog is real; the bits are not. ``PUT /v2/images/{id}/file`` streams the payload
straight to /dev/null, recording only size and checksum so no laptop disk is consumed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    gen_id,
    iso,
    now_utc,
    service_url,
    settle_transition,
    transition_deadline,
)
from app.core.database import get_session
from app.core.pagination import glance_links, page_request, paginate
from app.core.middleware import AuthContext, OSPayload, fault, require
from app.models.storage import Image, ImageMember, Task

SERVICE = "glance"
router = APIRouter()
auth_dep = require(SERVICE)

BASE_FIELDS = {
    "id",
    "name",
    "status",
    "visibility",
    "protected",
    "os_hidden",
    "container_format",
    "disk_format",
    "min_disk",
    "min_ram",
    "size",
    "virtual_size",
    "checksum",
    "os_hash_algo",
    "os_hash_value",
    "tags",
    "owner",
    "created_at",
    "updated_at",
    "file",
    "schema",
    "self",
    "direct_url",
}


class ImagePayload(OSPayload):
    name: str | None = None
    container_format: str | None = None
    disk_format: str | None = None
    visibility: str = "shared"
    protected: bool = False
    min_disk: int = 0
    min_ram: int = 0
    tags: list[str] = []
    id: str | None = None


def image_dict(image: Image) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": image.id,
        "name": image.name,
        "status": image.status,
        "visibility": image.visibility,
        "protected": image.protected,
        "os_hidden": image.os_hidden,
        "container_format": image.container_format,
        "disk_format": image.disk_format,
        "min_disk": image.min_disk,
        "min_ram": image.min_ram,
        "size": image.size,
        "virtual_size": image.virtual_size,
        "checksum": image.checksum,
        "os_hash_algo": image.os_hash_algo,
        "os_hash_value": image.os_hash_value,
        "tags": list(image.tags or []),
        "owner": image.owner,
        "owner_specified.openstack.md5": "",
        "owner_specified.openstack.sha256": "",
        "owner_specified.openstack.object": f"images/{image.name}",
        "created_at": iso(image.created_at),
        "updated_at": iso(image.updated_at),
        "file": f"/v2/images/{image.id}/file",
        "schema": "/v2/schemas/image",
        "self": f"/v2/images/{image.id}",
        "direct_url": f"sim://{image.id}",
    }
    body.update(dict(image.properties or {}))
    return body


def resolve_image(image: Image) -> Image:
    """Settle a finished import on read, the same way every other resource settles."""
    if settle_transition(image):
        image.updated_at = now_utc()
    return image


async def _get_image(session: AsyncSession, image_id: str) -> Image:
    image = await session.get(Image, image_id)
    if image is None or image.deleted:
        raise fault(SERVICE, 404, f"No image found with ID {image_id}")
    return resolve_image(image)


# --------------------------------------------------------------------------------------
# Version discovery / schemas
# --------------------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
@router.get("/versions", include_in_schema=False)
async def versions() -> dict[str, Any]:
    return {
        "versions": [
            {
                "id": f"v2.{minor}",
                "status": status,
                "links": [{"rel": "self", "href": service_url(SERVICE, "/v2/")}],
            }
            for minor, status in (
                (15, "CURRENT"),
                (9, "SUPPORTED"),
                (2, "SUPPORTED"),
                (1, "SUPPORTED"),
                (0, "SUPPORTED"),
            )
        ]
    }


@router.get("/v2/schemas/image")
async def image_schema(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "name": "image",
        "properties": {
            field: {"type": ["null", "string"], "description": f"Simulated {field}"}
            for field in sorted(BASE_FIELDS)
        },
        "additionalProperties": {"type": "string"},
        "links": [
            {"href": "{self}", "rel": "self"},
            {"href": "{file}", "rel": "enclosure"},
        ],
    }


@router.get("/v2/schemas/images")
async def images_schema(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "name": "images",
        "properties": {
            "images": {"type": "array", "items": {"name": "image"}},
            "first": {"type": "string"},
            "next": {"type": "string"},
            "schema": {"type": "string"},
        },
        "links": [{"href": "{first}", "rel": "first"}],
    }


@router.get("/v2/schemas/member")
@router.get("/v2/schemas/members")
async def member_schema(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"name": "member", "properties": {"image_id": {"type": "string"}}}


# --------------------------------------------------------------------------------------
# Image catalog
# --------------------------------------------------------------------------------------


@router.get("/v2/images")
async def list_images(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    params = request.query_params
    stmt = select(Image).where(Image.deleted.is_(False))
    if "name" in params:
        stmt = stmt.where(Image.name == params["name"])
    if "status" in params:
        stmt = stmt.where(Image.status == params["status"])
    if "visibility" in params:
        stmt = stmt.where(Image.visibility == params["visibility"])
    page = page_request(params, SERVICE)
    stmt = await paginate(session, stmt, Image, page, sort_column=Image.created_at)
    images = list((await session.execute(stmt)).scalars().all())
    for image in images:
        resolve_image(image)
    await session.commit()
    return {
        "images": [image_dict(i) for i in images],
        **glance_links(request, "/v2/images", images, page),
        "schema": "/v2/schemas/images",
    }


@router.post("/v2/images", status_code=201)
async def create_image(
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    payload = ImagePayload(**body)
    if not payload.name:
        raise fault(SERVICE, 400, "An image name is required.")
    extras = {
        key: value
        for key, value in body.items()
        if key not in BASE_FIELDS and key not in ("visibility", "protected", "tags")
    }
    image = Image(
        id=payload.id or gen_id(),
        name=payload.name,
        owner=auth.project_id,
        status="queued",
        visibility=payload.visibility,
        protected=payload.protected,
        container_format=payload.container_format,
        disk_format=payload.disk_format,
        min_disk=payload.min_disk,
        min_ram=payload.min_ram,
        tags=payload.tags,
        properties=extras,
    )
    session.add(image)
    await session.commit()
    return JSONResponse(
        image_dict(image),
        status_code=201,
        headers={"Location": service_url(SERVICE, f"/v2/images/{image.id}")},
    )


@router.get("/v2/images/{image_id}")
async def get_image(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return image_dict(await _get_image(session, image_id))


@router.patch("/v2/images/{image_id}")
async def patch_image(
    image_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """RFC-6902 style patch document, as sent by ``openstack image set``."""
    image = await _get_image(session, image_id)
    raw = await request.body()
    try:
        operations = json.loads(raw or b"[]")
    except json.JSONDecodeError:
        raise fault(SERVICE, 400, "Request body must be a JSON patch document.")
    if isinstance(operations, dict):
        operations = [
            {"op": "replace", "path": f"/{key}", "value": value}
            for key, value in operations.items()
        ]

    properties = dict(image.properties or {})
    for operation in operations:
        op = operation.get("op")
        field = str(operation.get("path", "")).lstrip("/").split("/")[0]
        value = operation.get("value")
        if not field:
            continue
        if op == "remove":
            properties.pop(field, None)
            continue
        if field in BASE_FIELDS and hasattr(image, field):
            setattr(image, field, value)
        elif field == "tags":
            image.tags = value if isinstance(value, list) else [value]
        else:
            properties[field] = value
    image.properties = properties
    image.updated_at = now_utc()
    await session.commit()
    return image_dict(image)


@router.delete("/v2/images/{image_id}", status_code=204)
async def delete_image(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    image = await _get_image(session, image_id)
    if image.protected:
        raise fault(SERVICE, 403, "Image is protected and cannot be deleted.")
    image.deleted = True
    image.status = "deleted"
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Zero-storage image data
# --------------------------------------------------------------------------------------


@router.put("/v2/images/{image_id}/file", status_code=204)
async def upload_image_data(
    image_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Consume the upload stream, discard every byte, keep size + checksum."""
    image = await _get_image(session, image_id)
    if image.status == "active" and image.size:
        raise fault(SERVICE, 409, "Image already has data associated with it.")

    md5 = hashlib.md5()
    sha512 = hashlib.sha512()
    total = 0
    # Each slice is hashed then dropped -- nothing is buffered and nothing is written.
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        md5.update(chunk)
        sha512.update(chunk)

    image.size = total
    image.virtual_size = total
    image.checksum = md5.hexdigest()
    image.os_hash_algo = "sha512"
    image.os_hash_value = sha512.hexdigest()
    image.status = "active"
    image.updated_at = now_utc()
    await session.commit()
    return Response(
        status_code=204,
        headers={
            "Content-Type": "application/json",
            "X-OpenStack-Simulator-Discarded-Bytes": str(total),
        },
    )


@router.get("/v2/images/{image_id}/file")
async def download_image_data(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """No bytes were ever stored, so this is always an empty 204 (a valid Glance reply)."""
    await _get_image(session, image_id)
    return Response(status_code=204, headers={"X-OpenStack-Simulator-Zero-Storage": "true"})


# --------------------------------------------------------------------------------------
# Members and lifecycle actions
# --------------------------------------------------------------------------------------


def member_dict(member: ImageMember) -> dict[str, Any]:
    return {
        "image_id": member.image_id,
        "member_id": member.member_id,
        "status": member.status,
        "created_at": iso(member.created_at),
        "updated_at": iso(member.updated_at),
        "schema": "/v2/schemas/member",
    }


async def _get_member(session: AsyncSession, image_id: str, member_id: str) -> ImageMember:
    member = (
        await session.execute(
            select(ImageMember).where(
                ImageMember.image_id == image_id, ImageMember.member_id == member_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise fault(
            SERVICE, 404, f"No image member found with ID {member_id} for image {image_id}"
        )
    return member


@router.get("/v2/images/{image_id}/members")
async def list_members(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_image(session, image_id)
    members = (
        await session.execute(
            select(ImageMember)
            .where(ImageMember.image_id == image_id)
            .order_by(ImageMember.created_at)
        )
    ).scalars().all()
    return {
        "members": [member_dict(m) for m in members],
        "schema": "/v2/schemas/members",
    }


@router.post("/v2/images/{image_id}/members", status_code=200)
async def create_member(
    image_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Share an image with another project. It arrives ``pending`` until accepted."""
    image = await _get_image(session, image_id)
    if image.visibility != "shared":
        raise fault(
            SERVICE,
            403,
            f"Image {image_id} is not shared; only a shared image can have members.",
        )
    member_id = (body or {}).get("member")
    if not member_id:
        raise fault(SERVICE, 400, "Member to be added not specified.")
    if member_id == image.owner:
        raise fault(SERVICE, 403, "Membership cannot be granted to the image owner.")
    existing = (
        await session.execute(
            select(ImageMember).where(
                ImageMember.image_id == image_id, ImageMember.member_id == member_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise fault(SERVICE, 409, f"The member {member_id} is duplicated for image {image_id}")

    member = ImageMember(id=gen_id(), image_id=image_id, member_id=member_id)
    session.add(member)
    await session.commit()
    return member_dict(member)


@router.get("/v2/images/{image_id}/members/{member_id}")
async def get_member(
    image_id: str,
    member_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_image(session, image_id)
    return member_dict(await _get_member(session, image_id, member_id))


@router.put("/v2/images/{image_id}/members/{member_id}")
async def update_member(
    image_id: str,
    member_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """The member's own accept/reject. Only the member may set this, not the owner."""
    await _get_image(session, image_id)
    member = await _get_member(session, image_id, member_id)
    status = (body or {}).get("status")
    if status not in ("accepted", "rejected", "pending"):
        raise fault(
            SERVICE,
            400,
            f"Invalid membership status {status!r}: must be accepted, rejected or pending.",
        )
    member.status = status
    member.updated_at = now_utc()
    await session.commit()
    return member_dict(member)


@router.delete("/v2/images/{image_id}/members/{member_id}", status_code=204)
async def delete_member(
    image_id: str,
    member_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _get_image(session, image_id)
    member = await _get_member(session, image_id, member_id)
    await session.delete(member)
    await session.commit()
    return Response(status_code=204)


@router.post("/v2/images/{image_id}/actions/deactivate", status_code=204)
async def deactivate(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    image = await _get_image(session, image_id)
    image.status = "deactivated"
    await session.commit()
    return Response(status_code=204)


@router.post("/v2/images/{image_id}/actions/reactivate", status_code=204)
async def reactivate(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    image = await _get_image(session, image_id)
    image.status = "active"
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------------------


@router.put("/v2/images/{image_id}/tags/{tag}", status_code=204)
async def add_tag(
    image_id: str,
    tag: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    image = await _get_image(session, image_id)
    tags = list(image.tags or [])
    if tag not in tags:
        # Reassigned rather than appended: SQLAlchemy only notices a JSON column changed
        # when the attribute itself is set.
        image.tags = [*tags, tag]
        image.updated_at = now_utc()
        await session.commit()
    return Response(status_code=204)


@router.delete("/v2/images/{image_id}/tags/{tag}", status_code=204)
async def remove_tag(
    image_id: str,
    tag: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    image = await _get_image(session, image_id)
    tags = list(image.tags or [])
    if tag not in tags:
        raise fault(SERVICE, 404, f"Image {image_id} has no tag {tag}")
    image.tags = [t for t in tags if t != tag]
    image.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Import workflow
# --------------------------------------------------------------------------------------

# The two methods worth simulating. glance-direct uploads to the staging area first;
# web-download has Glance fetch a URL itself, so nothing is uploaded at all.
IMPORT_METHODS = ("glance-direct", "web-download")

# Not a real store, but a client that asks which stores exist expects an answer, and
# "there is one and it is the default" is the shape of a single-store deployment.
STORES = [
    {
        "id": "simulator",
        "description": "Simulated store -- bytes are hashed and discarded",
        "default": True,
    }
]


@router.get("/v2/info/import")
async def import_info(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "import-methods": {
            "description": "Set of methods available to import image data.",
            "type": "array",
            "value": list(IMPORT_METHODS),
        }
    }


@router.get("/v2/info/stores")
async def store_info(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {"stores": STORES}


def task_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "owner": task.owner,
        "input": dict(task.input_ or {}),
        "result": task.result,
        "message": task.message,
        "created_at": iso(task.created_at),
        "updated_at": iso(task.updated_at),
        "expires_at": iso(task.expires_at),
        "self": f"/v2/tasks/{task.id}",
        "schema": "/v2/schemas/task",
    }


def resolve_task(task: Task) -> Task:
    """Settle a pending import once its window has passed, and finish the image with it."""
    if settle_transition(task, "status"):
        task.updated_at = now_utc()
        task.result = {"image_id": task.image_id} if task.image_id else None
    return task


@router.put("/v2/images/{image_id}/stage", status_code=204)
async def stage_image_data(
    image_id: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Upload into the staging area ahead of a glance-direct import.

    Same zero-storage treatment as a direct PUT to /file: every slice is hashed and
    dropped. What differs is the state machine -- the image is only *uploading* here, and
    becomes active when the import is asked for.
    """
    image = await _get_image(session, image_id)
    if image.status != "queued":
        raise fault(
            SERVICE,
            409,
            f"Image {image_id} is in {image.status} status; it must be queued to stage data.",
        )

    md5 = hashlib.md5()
    sha512 = hashlib.sha512()
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        md5.update(chunk)
        sha512.update(chunk)

    image.size = total
    image.virtual_size = total
    image.checksum = md5.hexdigest()
    image.os_hash_algo = "sha512"
    image.os_hash_value = sha512.hexdigest()
    image.status = "uploading"
    image.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


@router.post("/v2/images/{image_id}/import", status_code=202)
async def import_image(
    image_id: str,
    body: dict[str, Any],
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Start an import. The image reaches active through a task, not immediately."""
    image = await _get_image(session, image_id)
    method = ((body or {}).get("method") or {}).get("name")
    if method not in IMPORT_METHODS:
        raise fault(
            SERVICE,
            400,
            f"Unknown import method {method!r}: must be one of {list(IMPORT_METHODS)}.",
        )

    if method == "glance-direct":
        # Nothing was staged, so there is nothing to import.
        if image.status != "uploading":
            raise fault(
                SERVICE,
                409,
                f"Image {image_id} is in {image.status} status; data must be staged "
                f"before a glance-direct import.",
            )
    else:  # web-download
        if image.status != "queued":
            raise fault(
                SERVICE,
                409,
                f"Image {image_id} is in {image.status} status; it must be queued for "
                f"a web-download import.",
            )
        uri = ((body or {}).get("method") or {}).get("uri")
        if not uri:
            raise fault(SERVICE, 400, "The web-download import method requires a uri.")
        # Glance fetches the URL itself; no bytes reach us, so the size is synthetic.
        image.size = image.size or 21430272
        image.virtual_size = image.size

    image.status = "importing"
    image.transition_until = transition_deadline()
    image.transition_target = "active"
    image.updated_at = now_utc()

    task = Task(
        id=gen_id(),
        type="api_image_import",
        status="pending",
        owner=auth.project_id,
        image_id=image.id,
        input_={"image_id": image.id, "import_req": body or {}},
        transition_until=image.transition_until,
        transition_target="success",
        expires_at=now_utc() + timedelta(hours=48),
    )
    session.add(task)
    await session.commit()
    return Response(status_code=202, headers={"OpenStack-image-import-task": task.id})


@router.get("/v2/tasks")
async def list_tasks(
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    stmt = select(Task).where(Task.owner == auth.project_id)
    page = page_request(request.query_params, SERVICE)
    stmt = await paginate(session, stmt, Task, page, sort_column=Task.created_at)
    tasks = list((await session.execute(stmt)).scalars().all())
    for task in tasks:
        resolve_task(task)
    await session.commit()
    return {
        "tasks": [task_dict(t) for t in tasks],
        "schema": "/v2/schemas/tasks",
        **glance_links(request, "/v2/tasks", tasks, page),
    }


@router.get("/v2/tasks/{task_id}")
async def get_task(
    task_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await session.get(Task, task_id)
    if task is None:
        raise fault(SERVICE, 404, f"No task found with ID {task_id}")
    resolve_task(task)
    await session.commit()
    return task_dict(task)


@router.get("/v2/schemas/task")
async def task_schema(auth: AuthContext = auth_dep) -> dict[str, Any]:
    return {
        "name": "task",
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string"},
            "status": {"type": "string"},
            "image_id": {"type": "string"},
        },
    }
