"""Glance Image v2 API tests, including the zero-storage upload path."""
from __future__ import annotations

import hashlib

import pytest

pytestmark = pytest.mark.anyio


async def _image(api, name="blank", **extra) -> dict:
    response = await api["glance"].post("/v2/images", json={
        "name": name, "disk_format": "qcow2", "container_format": "bare", **extra})
    assert response.status_code == 201, response.text
    return response.json()


async def test_version_documents(raw_clients) -> None:
    for path in ("/", "/versions"):
        versions = (await raw_clients["glance"].get(path)).json()["versions"]
        assert versions[0]["id"] == "v2.15" and versions[0]["status"] == "CURRENT"


async def test_seeded_images(api) -> None:
    images = {i["name"]: i for i in (await api["glance"].get("/v2/images")).json()["images"]}
    assert set(images) == {"cirros", "ubuntu-24.04"}
    assert images["cirros"]["min_ram"] == 128
    assert images["ubuntu-24.04"]["min_ram"] == 2048
    assert images["cirros"]["status"] == "active"
    assert images["cirros"]["os_distro"] == "cirros", "custom properties are flattened"
    for image in images.values():
        assert image["os_hash_algo"] == "sha512"
        assert len(image["checksum"]) == 32 and len(image["os_hash_value"]) == 128
    assert images["cirros"]["checksum"] != images["ubuntu-24.04"]["checksum"]


async def test_image_create_starts_queued(api, cloud) -> None:
    body = await _image(api, name="fresh")
    assert body["status"] == "queued"
    assert body["owner"] == cloud.project_id
    assert body["size"] is None
    assert body["file"] == f"/v2/images/{body['id']}/file"
    assert body["schema"] == "/v2/schemas/image"


async def test_image_create_returns_a_location_header(api) -> None:
    response = await api["glance"].post("/v2/images", json={"name": "located"})
    assert "/v2/images/" in response.headers["Location"]


async def test_image_create_requires_a_name(api) -> None:
    assert (await api["glance"].post("/v2/images", json={})).status_code == 400


async def test_custom_properties_are_preserved(api) -> None:
    body = await _image(api, name="tagged", os_distro="alpine", hw_rng_model="virtio")
    assert body["os_distro"] == "alpine" and body["hw_rng_model"] == "virtio"
    fetched = (await api["glance"].get(f"/v2/images/{body['id']}")).json()
    assert fetched["hw_rng_model"] == "virtio"


async def test_upload_is_discarded_but_measured(api) -> None:
    image = await _image(api, name="discarded")
    payload = b"\xde\xad\xbe\xef" * 262144          # 1 MiB
    response = await api["glance"].put(f"/v2/images/{image['id']}/file", content=payload)
    assert response.status_code == 204
    assert response.headers["x-openstack-simulator-discarded-bytes"] == str(len(payload))

    body = (await api["glance"].get(f"/v2/images/{image['id']}")).json()
    assert body["status"] == "active"
    assert body["size"] == len(payload)
    assert body["checksum"] == hashlib.md5(payload).hexdigest()
    assert body["os_hash_algo"] == "sha512"
    assert body["os_hash_value"] == hashlib.sha512(payload).hexdigest()


async def test_empty_upload_is_accepted(api) -> None:
    image = await _image(api, name="empty")
    response = await api["glance"].put(f"/v2/images/{image['id']}/file", content=b"")
    assert response.status_code == 204
    body = (await api["glance"].get(f"/v2/images/{image['id']}")).json()
    assert body["size"] == 0 and body["status"] == "active"


async def test_second_upload_conflicts(api) -> None:
    image = await _image(api, name="once")
    await api["glance"].put(f"/v2/images/{image['id']}/file", content=b"data")
    again = await api["glance"].put(f"/v2/images/{image['id']}/file", content=b"more")
    assert again.status_code == 409


async def test_download_returns_no_content(api) -> None:
    image = await _image(api, name="nodata")
    await api["glance"].put(f"/v2/images/{image['id']}/file", content=b"payload")
    response = await api["glance"].get(f"/v2/images/{image['id']}/file")
    assert response.status_code == 204
    assert response.headers["x-openstack-simulator-zero-storage"] == "true"
    assert response.content == b""


async def test_upload_to_a_missing_image_is_a_404(api) -> None:
    assert (await api["glance"].put("/v2/images/ghost/file", content=b"x")).status_code == 404


async def test_json_patch_replaces_and_adds(api) -> None:
    image = await _image(api, name="patchable")
    patched = await api["glance"].patch(
        f"/v2/images/{image['id']}",
        json=[
            {"op": "replace", "path": "/name", "value": "renamed"},
            {"op": "add", "path": "/os_version", "value": "24.04"},
            {"op": "replace", "path": "/min_ram", "value": 1024},
        ],
        headers={"Content-Type": "application/openstack-images-v2.1-json-patch"},
    )
    body = patched.json()
    assert body["name"] == "renamed"
    assert body["os_version"] == "24.04"
    assert body["min_ram"] == 1024


async def test_json_patch_removes_a_property(api) -> None:
    image = await _image(api, name="removable", temporary="yes")
    patched = await api["glance"].patch(f"/v2/images/{image['id']}",
                                        json=[{"op": "remove", "path": "/temporary"}])
    assert "temporary" not in patched.json()


async def test_patch_accepts_a_plain_object(api) -> None:
    image = await _image(api, name="objectpatch")
    patched = await api["glance"].patch(f"/v2/images/{image['id']}",
                                        json={"name": "flat"})
    assert patched.json()["name"] == "flat"


async def test_patch_rejects_a_malformed_document(api) -> None:
    image = await _image(api, name="badpatch")
    response = await api["glance"].patch(f"/v2/images/{image['id']}", content=b"not json")
    assert response.status_code == 400


async def test_image_delete_and_protection(api) -> None:
    image = await _image(api, name="doomed")
    assert (await api["glance"].delete(f"/v2/images/{image['id']}")).status_code == 204
    assert (await api["glance"].get(f"/v2/images/{image['id']}")).status_code == 404

    protected = await _image(api, name="keepme", protected=True)
    refused = await api["glance"].delete(f"/v2/images/{protected['id']}")
    assert refused.status_code == 403


async def test_listing_filters(api) -> None:
    await _image(api, name="private-one", visibility="private")
    assert len((await api["glance"].get("/v2/images?name=cirros")).json()["images"]) == 1
    assert len((await api["glance"].get("/v2/images?status=queued")).json()["images"]) == 1
    shared = (await api["glance"].get("/v2/images?visibility=private")).json()["images"]
    assert [i["name"] for i in shared] == ["private-one"]
    assert len((await api["glance"].get("/v2/images?limit=1")).json()["images"]) == 1


async def test_deactivate_and_reactivate(api) -> None:
    image = await _image(api, name="toggle")
    await api["glance"].put(f"/v2/images/{image['id']}/file", content=b"x")
    assert (await api["glance"].post(
        f"/v2/images/{image['id']}/actions/deactivate")).status_code == 204
    assert (await api["glance"].get(f"/v2/images/{image['id']}")).json()["status"] == "deactivated"
    assert (await api["glance"].post(
        f"/v2/images/{image['id']}/actions/reactivate")).status_code == 204
    assert (await api["glance"].get(f"/v2/images/{image['id']}")).json()["status"] == "active"


async def test_schemas_and_members(api) -> None:
    assert (await api["glance"].get("/v2/schemas/image")).json()["name"] == "image"
    assert (await api["glance"].get("/v2/schemas/images")).json()["name"] == "images"
    assert (await api["glance"].get("/v2/schemas/member")).json()["name"] == "member"
    image = await _image(api, name="shared")
    assert (await api["glance"].get(f"/v2/images/{image['id']}/members")).json()["members"] == []


async def test_glance_error_envelope(api) -> None:
    response = await api["glance"].get("/v2/images/ghost")
    assert response.status_code == 404
    assert response.json()["code"] == 404
    assert "No image found" in response.json()["message"]
