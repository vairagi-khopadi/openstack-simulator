"""Swift Object Store v1 (port 8080).

Containers and object metadata are tracked in SQLite; object bodies are streamed and
dropped, so uploading a 10 GB blob costs a few hundred bytes of database and no IOPS.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import gen_id, now_utc
from app.core.database import get_session
from app.core.middleware import AuthContext, fault, require
from app.models.objectstore import Container, ObjectMetadata, SwiftAccount

SERVICE = "swift"
router = APIRouter()
auth_dep = require(SERVICE)


def _http_date(value: Any) -> str:
    return value.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _timestamp(value: Any) -> str:
    return f"{value.timestamp():.5f}"


def _collect_meta(request: Request, prefix: str) -> dict[str, str]:
    return {
        key[len(prefix) :]: value
        for key, value in request.headers.items()
        if key.lower().startswith(prefix)
    }


def _meta_headers(metadata: dict[str, Any], prefix: str) -> dict[str, str]:
    return {f"{prefix}{key}": str(value) for key, value in (metadata or {}).items()}


async def _account(session: AsyncSession, project_id: str) -> SwiftAccount:
    account = (
        await session.execute(
            select(SwiftAccount).where(SwiftAccount.project_id == project_id)
        )
    ).scalar_one_or_none()
    if account is None:
        account = SwiftAccount(id=gen_id(), project_id=project_id, metadata_={})
        session.add(account)
        await session.flush()
    return account


async def _container(
    session: AsyncSession, project_id: str, name: str
) -> Container | None:
    return (
        await session.execute(
            select(Container).where(
                Container.project_id == project_id, Container.name == name
            )
        )
    ).scalar_one_or_none()


async def _container_stats(session: AsyncSession, container_id: str) -> tuple[int, int]:
    row = (
        await session.execute(
            select(
                func.count(ObjectMetadata.id),
                func.coalesce(func.sum(ObjectMetadata.bytes), 0),
            ).where(ObjectMetadata.container_id == container_id)
        )
    ).one()
    return int(row[0]), int(row[1])


def _check_account(auth: AuthContext, account: str) -> str:
    """``/v1/AUTH_{project}`` -- the project id is embedded in the path."""
    project_id = account[5:] if account.startswith("AUTH_") else account
    if project_id != auth.project_id and not auth.is_admin:
        raise fault(SERVICE, 403, "Access denied to this storage account.")
    return project_id


# --------------------------------------------------------------------------------------
# Account
# --------------------------------------------------------------------------------------


@router.get("/info")
async def info() -> dict[str, Any]:
    """Unauthenticated capability document, as served by real Swift proxies."""
    return {
        "swift": {
            "version": "2.33.0",
            "max_file_size": 5368709122,
            "max_meta_count": 90,
            "max_container_name_length": 256,
            "max_object_name_length": 1024,
            "policies": [{"name": "Policy-0", "default": True}],
            "strict_cors_mode": True,
        },
        "slo": {"max_manifest_segments": 1000},
        "tempurl": {"methods": ["GET", "HEAD", "PUT", "POST", "DELETE"]},
        "bulk_delete": {"max_deletes_per_request": 10000},
    }


async def _account_response(
    session: AsyncSession, project_id: str, request: Request, head: bool
) -> Response:
    account = await _account(session, project_id)
    containers = (
        await session.execute(
            select(Container).where(Container.project_id == project_id).order_by(Container.name)
        )
    ).scalars().all()

    listing: list[dict[str, Any]] = []
    total_objects = 0
    total_bytes = 0
    for container in containers:
        count, size = await _container_stats(session, container.id)
        total_objects += count
        total_bytes += size
        listing.append(
            {
                "name": container.name,
                "count": count,
                "bytes": size,
                "last_modified": container.updated_at.isoformat(),
            }
        )

    headers = {
        "X-Account-Container-Count": str(len(containers)),
        "X-Account-Object-Count": str(total_objects),
        "X-Account-Bytes-Used": str(total_bytes),
        "X-Timestamp": _timestamp(account.created_at),
        "Accept-Ranges": "bytes",
        "X-Account-Storage-Policy-Policy-0-Container-Count": str(len(containers)),
    }
    headers.update(_meta_headers(account.metadata_, "X-Account-Meta-"))
    await session.commit()

    if head:
        return Response(status_code=204, headers=headers)
    if request.query_params.get("format") == "json":
        return JSONResponse(listing, headers=headers)
    if not listing:
        return Response(status_code=204, headers=headers)
    body = "\n".join(entry["name"] for entry in listing) + "\n"
    return PlainTextResponse(body, headers=headers)


@router.get("/v1/{account}")
async def get_account(
    account: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    return await _account_response(session, project_id, request, head=False)


@router.head("/v1/{account}")
async def head_account(
    account: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    return await _account_response(session, project_id, request, head=True)


@router.post("/v1/{account}", status_code=204)
async def post_account(
    account: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    # Swift overloads POST on the account: with ?bulk-delete it is a batch delete, and
    # without it a metadata update.
    if {"bulk-delete", "bulk_delete"} & set(request.query_params):
        return await _bulk_delete(request, session, project_id)
    record = await _account(session, project_id)
    metadata = dict(record.metadata_ or {})
    metadata.update(_collect_meta(request, "x-account-meta-"))
    record.metadata_ = {k: v for k, v in metadata.items() if v != ""}
    record.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Containers
# --------------------------------------------------------------------------------------


async def _container_response(
    session: AsyncSession,
    project_id: str,
    name: str,
    request: Request,
    head: bool,
) -> Response:
    container = await _container(session, project_id, name)
    if container is None:
        raise fault(SERVICE, 404, "Container not found.")
    count, size = await _container_stats(session, container.id)
    headers = {
        "X-Container-Object-Count": str(count),
        "X-Container-Bytes-Used": str(size),
        "X-Timestamp": _timestamp(container.created_at),
        "X-Storage-Policy": container.storage_policy,
        "Accept-Ranges": "bytes",
        "Last-Modified": _http_date(container.updated_at),
    }
    if container.read_acl:
        headers["X-Container-Read"] = container.read_acl
    if container.write_acl:
        headers["X-Container-Write"] = container.write_acl
    headers.update(_meta_headers(container.metadata_, "X-Container-Meta-"))

    if head:
        return Response(status_code=204, headers=headers)

    params = request.query_params
    stmt = select(ObjectMetadata).where(ObjectMetadata.container_id == container.id)
    if "prefix" in params:
        stmt = stmt.where(ObjectMetadata.name.like(f"{params['prefix']}%"))
    limit = int(params.get("limit", 10000))
    objects = (
        await session.execute(stmt.order_by(ObjectMetadata.name).limit(limit))
    ).scalars().all()

    if params.get("format") == "json":
        return JSONResponse(
            [
                {
                    "name": o.name,
                    "hash": o.etag,
                    "bytes": o.bytes,
                    "content_type": o.content_type,
                    "last_modified": o.last_modified.isoformat(),
                }
                for o in objects
            ],
            headers=headers,
        )
    if not objects:
        return Response(status_code=204, headers=headers)
    return PlainTextResponse("\n".join(o.name for o in objects) + "\n", headers=headers)


@router.get("/v1/{account}/{container}")
async def get_container(
    account: str,
    container: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    return await _container_response(session, project_id, container, request, head=False)


@router.head("/v1/{account}/{container}")
async def head_container(
    account: str,
    container: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    return await _container_response(session, project_id, container, request, head=True)


@router.put("/v1/{account}/{container}")
async def put_container(
    account: str,
    container: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    existing = await _container(session, project_id, container)
    status = 202 if existing is not None else 201
    record = existing or Container(
        id=gen_id(), name=container, project_id=project_id, metadata_={}
    )
    metadata = dict(record.metadata_ or {})
    metadata.update(_collect_meta(request, "x-container-meta-"))
    record.metadata_ = {k: v for k, v in metadata.items() if v != ""}
    record.read_acl = request.headers.get("X-Container-Read", record.read_acl)
    record.write_acl = request.headers.get("X-Container-Write", record.write_acl)
    record.updated_at = now_utc()
    session.add(record)
    await session.commit()
    return Response(status_code=status)


@router.post("/v1/{account}/{container}", status_code=204)
async def post_container(
    account: str,
    container: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    if record is None:
        raise fault(SERVICE, 404, "Container not found.")
    metadata = dict(record.metadata_ or {})
    metadata.update(_collect_meta(request, "x-container-meta-"))
    record.metadata_ = {k: v for k, v in metadata.items() if v != ""}
    record.updated_at = now_utc()
    await session.commit()
    return Response(status_code=204)


@router.delete("/v1/{account}/{container}", status_code=204)
async def delete_container(
    account: str,
    container: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    if record is None:
        raise fault(SERVICE, 404, "Container not found.")
    count, _ = await _container_stats(session, record.id)
    if count:
        raise fault(SERVICE, 409, "There was a conflict when trying to complete "
                                  "your request: container is not empty.")
    await session.delete(record)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Objects -- payloads are hashed for the ETag, then discarded
# --------------------------------------------------------------------------------------


async def _object(
    session: AsyncSession, container: Container, name: str
) -> ObjectMetadata | None:
    """The object, or None if it is gone -- including expired but not yet reaped.

    Swift's object-expirer deletes on its own schedule, so an expired object can still
    be on disk. What a client must never see is a successful read of it, which is why
    expiry is checked here rather than in a sweep.
    """
    obj = (
        await session.execute(
            select(ObjectMetadata).where(
                ObjectMetadata.container_id == container.id, ObjectMetadata.name == name
            )
        )
    ).scalar_one_or_none()
    if obj is not None and obj.delete_at is not None and obj.delete_at <= now_utc():
        await session.delete(obj)
        await session.commit()
        return None
    return obj


def _expiry_from(headers: Any) -> datetime | None:
    """Read X-Delete-After (seconds from now) or X-Delete-At (a unix timestamp)."""
    after = headers.get("X-Delete-After")
    if after:
        try:
            return now_utc() + timedelta(seconds=int(after))
        except ValueError:
            return None
    at = headers.get("X-Delete-At")
    if at:
        try:
            # Naive UTC, matching how every other datetime is stored here.
            return datetime.fromtimestamp(int(at), tz=timezone.utc).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    return None


@router.put("/v1/{account}/{container}/{object_name:path}", status_code=201)
async def put_object(
    account: str,
    container: str,
    object_name: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    if record is None:
        raise fault(SERVICE, 404, "Container not found.")

    md5 = hashlib.md5()
    total = 0
    # The stream is consumed slice by slice and never buffered or written to disk.
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        md5.update(chunk)
    etag = md5.hexdigest()

    supplied_etag = request.headers.get("ETag")
    if supplied_etag and supplied_etag.strip('"').lower() != etag:
        raise fault(SERVICE, 422, "Unprocessable Entity: ETag does not match the payload.")

    obj = await _object(session, record, object_name)
    if obj is None:
        obj = ObjectMetadata(
            id=gen_id(),
            container_id=record.id,
            name=object_name,
            project_id=project_id,
        )
        session.add(obj)
    obj.bytes = total
    obj.etag = etag
    obj.content_type = request.headers.get("Content-Type", "application/octet-stream")
    obj.metadata_ = _collect_meta(request, "x-object-meta-")
    obj.delete_at = _expiry_from(request.headers)
    obj.last_modified = now_utc()
    record.updated_at = obj.last_modified
    await session.commit()

    return Response(
        status_code=201,
        headers={
            "ETag": etag,
            "Content-Length": "0",
            "Last-Modified": _http_date(obj.last_modified),
            "X-OpenStack-Simulator-Discarded-Bytes": str(total),
        },
    )


def _object_headers(obj: ObjectMetadata) -> dict[str, str]:
    headers = {
        "Content-Type": obj.content_type,
        "Content-Length": str(obj.bytes),
        "ETag": obj.etag,
        "Last-Modified": _http_date(obj.last_modified),
        "X-Timestamp": _timestamp(obj.last_modified),
        "Accept-Ranges": "bytes",
    }
    if obj.delete_at is not None:
        headers["X-Delete-At"] = str(int(obj.delete_at.replace(tzinfo=timezone.utc).timestamp()))
    headers.update(_meta_headers(obj.metadata_, "X-Object-Meta-"))
    return headers


@router.head("/v1/{account}/{container}/{object_name:path}")
async def head_object(
    account: str,
    container: str,
    object_name: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    obj = await _object(session, record, object_name) if record else None
    if obj is None:
        raise fault(SERVICE, 404, "Object not found.")
    return Response(status_code=200, headers=_object_headers(obj))


@router.get("/v1/{account}/{container}/{object_name:path}")
async def get_object(
    account: str,
    container: str,
    object_name: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Metadata is real; the body is empty because the bytes were never stored."""
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    obj = await _object(session, record, object_name) if record else None
    if obj is None:
        raise fault(SERVICE, 404, "Object not found.")
    headers = _object_headers(obj)
    headers["Content-Length"] = "0"
    headers["X-Object-Sim-Original-Length"] = str(obj.bytes)
    headers["X-OpenStack-Simulator-Zero-Storage"] = "true"
    return Response(status_code=200, content=b"", headers=headers)


@router.post("/v1/{account}/{container}/{object_name:path}", status_code=202)
async def post_object(
    account: str,
    container: str,
    object_name: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    obj = await _object(session, record, object_name) if record else None
    if obj is None:
        raise fault(SERVICE, 404, "Object not found.")
    obj.metadata_ = {**(obj.metadata_ or {}), **_collect_meta(request, "x-object-meta-")}
    if "Content-Type" in request.headers:
        obj.content_type = request.headers["Content-Type"]
    expiry = _expiry_from(request.headers)
    if expiry is not None:
        obj.delete_at = expiry
    elif "X-Remove-Delete-At" in request.headers:
        obj.delete_at = None
    obj.last_modified = now_utc()
    await session.commit()
    return Response(status_code=202)


@router.delete("/v1/{account}/{container}/{object_name:path}", status_code=204)
async def delete_object(
    account: str,
    container: str,
    object_name: str,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    obj = await _object(session, record, object_name) if record else None
    if obj is None:
        raise fault(SERVICE, 404, "Object not found.")
    await session.delete(obj)
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# COPY and bulk delete
# --------------------------------------------------------------------------------------


def _split_destination(destination: str) -> tuple[str, str]:
    """``/container/object`` -- the form Swift's Destination header takes."""
    cleaned = destination.lstrip("/")
    container, _, name = cleaned.partition("/")
    if not container or not name:
        raise fault(SERVICE, 412, "Destination header must be of the form /container/object.")
    return container, name


async def _copy(
    session: AsyncSession,
    project_id: str,
    source: ObjectMetadata,
    destination: str,
    request: Request,
) -> Response:
    container_name, object_name = _split_destination(destination)
    target = await _container(session, project_id, container_name)
    if target is None:
        raise fault(SERVICE, 404, "Destination container not found.")

    existing = await _object(session, target, object_name)
    if existing is None:
        existing = ObjectMetadata(
            id=gen_id(),
            container_id=target.id,
            name=object_name,
            project_id=project_id,
        )
        session.add(existing)
    # No bytes were stored, so a copy is the metadata and the checksum over bytes that
    # were hashed on the way past -- which is all a client can verify anyway.
    existing.bytes = source.bytes
    existing.etag = source.etag
    existing.content_type = request.headers.get("Content-Type", source.content_type)
    existing.metadata_ = {
        **(source.metadata_ or {}),
        **_collect_meta(request, "x-object-meta-"),
    }
    existing.delete_at = _expiry_from(request.headers)
    existing.last_modified = now_utc()
    target.updated_at = existing.last_modified
    await session.commit()
    return Response(
        status_code=201,
        headers={
            "ETag": source.etag,
            "Content-Length": "0",
            "X-Copied-From": f"{source.container_id}/{source.name}",
            "Last-Modified": _http_date(existing.last_modified),
        },
    )


@router.api_route("/v1/{account}/{container}/{object_name:path}", methods=["COPY"])
async def copy_object(
    account: str,
    container: str,
    object_name: str,
    request: Request,
    auth: AuthContext = auth_dep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """COPY with a Destination header -- the server-side copy Swift offers."""
    project_id = _check_account(auth, account)
    record = await _container(session, project_id, container)
    source = await _object(session, record, object_name) if record else None
    if source is None:
        raise fault(SERVICE, 404, "Object not found.")
    destination = request.headers.get("Destination")
    if not destination:
        raise fault(SERVICE, 412, "The COPY method requires a Destination header.")
    return await _copy(session, project_id, source, destination, request)


async def _bulk_delete(
    request: Request, session: AsyncSession, project_id: str
) -> Response:
    """``?bulk-delete=1`` with one ``/container/object`` per line in the body.

    The reason this exists is round trips: deleting ten thousand objects one request at
    a time is the slowest thing a client can do to an object store. Swift answers with a
    summary rather than a status code per line, so a partial failure still returns 200.
    """
    body = (await request.body()).decode(errors="replace")
    targets = [line.strip() for line in body.splitlines() if line.strip()]
    deleted, not_found, errors = 0, 0, []
    for target in targets:
        container_name, _, object_name = target.lstrip("/").partition("/")
        record = await _container(session, project_id, container_name)
        obj = await _object(session, record, object_name) if record else None
        if obj is None:
            not_found += 1
            errors.append([target, "404 Not Found"])
            continue
        await session.delete(obj)
        deleted += 1
    await session.commit()
    return JSONResponse(
        {
            "Number Deleted": deleted,
            "Number Not Found": not_found,
            "Response Status": "200 OK" if not errors else "400 Bad Request",
            "Response Body": "",
            "Errors": errors,
        },
        status_code=200,
    )
