"""Glance Image v2 (port 9292).

The catalog is real; the bits are not. ``PUT /v2/images/{id}/file`` streams the payload
straight to /dev/null, recording only size and checksum so no laptop disk is consumed.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, iso, now_utc, service_url
from app.core.database import get_session
from app.core.pagination import glance_links, page_request, paginate
from app.core.middleware import AuthContext, OSPayload, fault, require
from app.models.storage import Image

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


async def _get_image(session: AsyncSession, image_id: str) -> Image:
    image = await session.get(Image, image_id)
    if image is None or image.deleted:
        raise fault(SERVICE, 404, f"No image found with ID {image_id}")
    return image


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


@router.get("/v2/images/{image_id}/members")
async def list_members(
    image_id: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_image(session, image_id)
    return {"members": [], "schema": "/v2/schemas/members"}


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
