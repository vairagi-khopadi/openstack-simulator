"""Glance image import, tasks, tags and member sharing.

`PUT /v2/images/{id}/file` is the old upload path and finishes when the upload does.
The import workflow is the modern one and does not: data is staged, an import is asked
for, and the image reaches ``active`` through a task. Code written against the direct PUT
will not have the polling loop the import needs, which is the difference worth simulating.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.models.storage import Image

pytestmark = pytest.mark.anyio


async def _queued_image(api: dict[str, Any], name: str = "imported") -> str:
    created = await api["glance"].post(
        "/v2/images",
        json={"name": name, "disk_format": "qcow2", "container_format": "bare"},
    )
    return created.json()["id"]


# --------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------


async def test_import_methods_are_advertised(api: dict[str, Any], cloud: Any) -> None:
    body = (await api["glance"].get("/v2/info/import")).json()
    assert set(body["import-methods"]["value"]) == {"glance-direct", "web-download"}


async def test_stores_are_advertised(api: dict[str, Any], cloud: Any) -> None:
    stores = (await api["glance"].get("/v2/info/stores")).json()["stores"]
    assert stores[0]["default"] is True


# --------------------------------------------------------------------------------------
# glance-direct: stage, then import
# --------------------------------------------------------------------------------------


async def test_staging_moves_the_image_to_uploading(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    staged = await api["glance"].put(f"/v2/images/{image_id}/stage", content=b"x" * 2048)
    assert staged.status_code == 204

    body = (await api["glance"].get(f"/v2/images/{image_id}")).json()
    assert body["status"] == "uploading"
    assert body["size"] == 2048
    # The bytes are gone but the checksum over them is real.
    assert body["checksum"] and body["os_hash_algo"] == "sha512"


async def test_import_finishes_through_a_task(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    image_id = await _queued_image(api)
    await api["glance"].put(f"/v2/images/{image_id}/stage", content=b"x" * 16)

    started = await api["glance"].post(
        f"/v2/images/{image_id}/import",
        json={"method": {"name": "glance-direct"}},
    )
    assert started.status_code == 202
    task_id = started.headers["openstack-image-import-task"]

    # Mid-import the image is neither queued nor active.
    assert (await api["glance"].get(
        f"/v2/images/{image_id}")).json()["status"] == "importing"
    assert (await api["glance"].get(f"/v2/tasks/{task_id}")).json()["status"] == "pending"

    await expire(Image, image_id)
    from app.models.storage import Task

    await expire(Task, task_id)

    assert (await api["glance"].get(f"/v2/images/{image_id}")).json()["status"] == "active"
    task = (await api["glance"].get(f"/v2/tasks/{task_id}")).json()
    assert task["status"] == "success"
    assert task["result"] == {"image_id": image_id}


async def test_glance_direct_without_staging_is_refused(
    api: dict[str, Any], cloud: Any
) -> None:
    """There is nothing to import: the client skipped the stage step."""
    image_id = await _queued_image(api)
    refused = await api["glance"].post(
        f"/v2/images/{image_id}/import", json={"method": {"name": "glance-direct"}}
    )
    assert refused.status_code == 409
    assert "staged" in refused.text


async def test_staging_twice_is_refused(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    await api["glance"].put(f"/v2/images/{image_id}/stage", content=b"data")
    again = await api["glance"].put(f"/v2/images/{image_id}/stage", content=b"data")
    assert again.status_code == 409


# --------------------------------------------------------------------------------------
# web-download
# --------------------------------------------------------------------------------------


async def test_web_download_needs_no_upload(api: dict[str, Any], cloud: Any) -> None:
    """Glance fetches the URL itself, so the client sends no bytes at all."""
    image_id = await _queued_image(api)
    started = await api["glance"].post(
        f"/v2/images/{image_id}/import",
        json={"method": {"name": "web-download", "uri": "https://example.invalid/i.qcow2"}},
    )
    assert started.status_code == 202
    body = (await api["glance"].get(f"/v2/images/{image_id}")).json()
    assert body["status"] == "active"  # instant transitions in the suite
    assert body["size"] > 0


async def test_web_download_requires_a_uri(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    refused = await api["glance"].post(
        f"/v2/images/{image_id}/import", json={"method": {"name": "web-download"}}
    )
    assert refused.status_code == 400
    assert "uri" in refused.text


async def test_an_unknown_import_method_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    refused = await api["glance"].post(
        f"/v2/images/{image_id}/import", json={"method": {"name": "carrier-pigeon"}}
    )
    assert refused.status_code == 400


# --------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------


async def test_tasks_are_listed_for_the_project(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    await api["glance"].put(f"/v2/images/{image_id}/stage", content=b"d")
    await api["glance"].post(
        f"/v2/images/{image_id}/import", json={"method": {"name": "glance-direct"}}
    )
    body = (await api["glance"].get("/v2/tasks")).json()
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["type"] == "api_image_import"
    assert body["tasks"][0]["owner"] == cloud.project_id


async def test_unknown_task_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["glance"].get("/v2/tasks/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Tags
# --------------------------------------------------------------------------------------


async def test_tags_are_added_and_removed(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    assert (await api["glance"].put(
        f"/v2/images/{image_id}/tags/prod")).status_code == 204
    assert (await api["glance"].put(
        f"/v2/images/{image_id}/tags/golden")).status_code == 204

    body = (await api["glance"].get(f"/v2/images/{image_id}")).json()
    assert sorted(body["tags"]) == ["golden", "prod"]

    assert (await api["glance"].delete(
        f"/v2/images/{image_id}/tags/prod")).status_code == 204
    body = (await api["glance"].get(f"/v2/images/{image_id}")).json()
    assert body["tags"] == ["golden"]


async def test_adding_a_tag_twice_is_harmless(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    await api["glance"].put(f"/v2/images/{image_id}/tags/once")
    await api["glance"].put(f"/v2/images/{image_id}/tags/once")
    body = (await api["glance"].get(f"/v2/images/{image_id}")).json()
    assert body["tags"] == ["once"]


async def test_removing_a_missing_tag_is_404(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _queued_image(api)
    assert (await api["glance"].delete(
        f"/v2/images/{image_id}/tags/absent")).status_code == 404


# --------------------------------------------------------------------------------------
# Member sharing
# --------------------------------------------------------------------------------------


async def _shared_image(api: dict[str, Any]) -> str:
    created = await api["glance"].post(
        "/v2/images",
        json={"name": "shareable", "visibility": "shared", "disk_format": "qcow2",
              "container_format": "bare"},
    )
    return created.json()["id"]


async def test_sharing_starts_pending_and_is_accepted_by_the_member(
    api: dict[str, Any], cloud: Any
) -> None:
    """The handshake is the point: an image cannot be pushed into another project's list."""
    image_id = await _shared_image(api)
    added = await api["glance"].post(
        f"/v2/images/{image_id}/members", json={"member": "other-project"}
    )
    assert added.status_code == 200
    assert added.json()["status"] == "pending"

    accepted = await api["glance"].put(
        f"/v2/images/{image_id}/members/other-project", json={"status": "accepted"}
    )
    assert accepted.json()["status"] == "accepted"


async def test_members_are_listed(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _shared_image(api)
    await api["glance"].post(f"/v2/images/{image_id}/members", json={"member": "a"})
    await api["glance"].post(f"/v2/images/{image_id}/members", json={"member": "b"})
    members = (await api["glance"].get(f"/v2/images/{image_id}/members")).json()["members"]
    assert sorted(m["member_id"] for m in members) == ["a", "b"]


async def test_a_member_can_be_removed(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _shared_image(api)
    await api["glance"].post(f"/v2/images/{image_id}/members", json={"member": "gone"})
    assert (await api["glance"].delete(
        f"/v2/images/{image_id}/members/gone")).status_code == 204
    assert (await api["glance"].get(f"/v2/images/{image_id}/members")).json()["members"] == []


async def test_only_a_shared_image_takes_members(api: dict[str, Any], cloud: Any) -> None:
    """Visibility defaults to `shared`, so this needs an image explicitly made private."""
    created = await api["glance"].post(
        "/v2/images",
        json={"name": "mine-only", "visibility": "private", "disk_format": "qcow2",
              "container_format": "bare"},
    )
    refused = await api["glance"].post(
        f"/v2/images/{created.json()['id']}/members", json={"member": "someone"}
    )
    assert refused.status_code == 403


async def test_the_owner_cannot_be_a_member(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _shared_image(api)
    refused = await api["glance"].post(
        f"/v2/images/{image_id}/members", json={"member": cloud.project_id}
    )
    assert refused.status_code == 403


async def test_duplicate_members_are_rejected(api: dict[str, Any], cloud: Any) -> None:
    image_id = await _shared_image(api)
    await api["glance"].post(f"/v2/images/{image_id}/members", json={"member": "twice"})
    again = await api["glance"].post(
        f"/v2/images/{image_id}/members", json={"member": "twice"}
    )
    assert again.status_code == 409


async def test_an_invalid_membership_status_is_rejected(
    api: dict[str, Any], cloud: Any
) -> None:
    image_id = await _shared_image(api)
    await api["glance"].post(f"/v2/images/{image_id}/members", json={"member": "m"})
    refused = await api["glance"].put(
        f"/v2/images/{image_id}/members/m", json={"status": "maybe"}
    )
    assert refused.status_code == 400
