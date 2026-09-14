"""Cinder Block Storage v3 API tests."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _volume(api, size=10, **extra) -> dict:
    response = await api["cinder"].post("/v3/volumes",
                                        json={"volume": {"size": size, **extra}})
    assert response.status_code == 202, response.text
    return response.json()["volume"]


async def test_version_document(raw_clients) -> None:
    body = (await raw_clients["cinder"].get("/")).json()["versions"][0]
    assert body["version"] == "3.70" and body["min_version"] == "3.0"


async def test_microversion_header(api) -> None:
    headers = (await api["cinder"].get("/v3/volumes")).headers
    assert headers["openstack-api-version"] == "volume 3.70"


async def test_tenant_scoped_and_bare_paths_are_equivalent(api, cloud) -> None:
    """The catalog advertises /v3/{project_id}; older clients and curl use /v3."""
    volume = await _volume(api)
    scoped = await api["cinder"].get(f"/v3/{cloud.project_id}/volumes/{volume['id']}")
    bare = await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    assert scoped.status_code == bare.status_code == 200
    assert scoped.json() == bare.json()

    listed = await api["cinder"].get(f"/v3/{cloud.project_id}/volumes/detail")
    assert len(listed.json()["volumes"]) == 1


async def test_volume_create_shape(api, cloud) -> None:
    volume = await _volume(api, size=25, name="data", description="scratch")
    assert volume["status"] == "creating"
    assert volume["size"] == 25
    assert volume["name"] == "data"
    assert volume["volume_type"] == "__DEFAULT__"
    assert volume["bootable"] == "false"
    assert volume["os-vol-tenant-attr:tenant_id"] == cloud.project_id
    assert volume["attachments"] == []


async def test_volume_becomes_available_after_its_window(api) -> None:
    volume = await _volume(api)
    fetched = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert fetched["status"] == "available"


async def test_volume_size_is_validated(api) -> None:
    for size in (0, -5, None):
        response = await api["cinder"].post("/v3/volumes", json={"volume": {"size": size}})
        assert response.status_code == 400


async def test_volume_from_snapshot_inherits_the_size(api) -> None:
    source = await _volume(api, size=30)
    await api["cinder"].get(f"/v3/volumes/{source['id']}")           # settle to available
    snapshot = (await api["cinder"].post("/v3/snapshots", json={
        "snapshot": {"volume_id": source["id"], "name": "snap"}})).json()["snapshot"]
    clone = (await api["cinder"].post("/v3/volumes", json={
        "volume": {"snapshot_id": snapshot["id"]}})).json()["volume"]
    assert clone["size"] == 30


async def test_volume_from_source_volume_inherits_the_size(api) -> None:
    source = await _volume(api, size=45)
    clone = (await api["cinder"].post("/v3/volumes", json={
        "volume": {"source_volid": source["id"]}})).json()["volume"]
    assert clone["size"] == 45


async def test_bootable_flag_when_created_from_an_image(api) -> None:
    images = (await api["glance"].get("/v2/images?name=cirros")).json()["images"]
    volume = await _volume(api, size=5, imageRef=images[0]["id"])
    assert volume["bootable"] == "true"


async def test_volume_update_and_metadata(api) -> None:
    volume = await _volume(api)
    updated = await api["cinder"].put(f"/v3/volumes/{volume['id']}", json={
        "volume": {"name": "renamed", "description": "d", "metadata": {"tier": "gold"}}})
    body = updated.json()["volume"]
    assert body["name"] == "renamed" and body["metadata"] == {"tier": "gold"}


async def test_volume_listing_brief_and_detail(api) -> None:
    await _volume(api, name="one")
    await _volume(api, name="two")
    brief = (await api["cinder"].get("/v3/volumes")).json()["volumes"]
    assert len(brief) == 2 and "size" not in brief[0]
    detail = (await api["cinder"].get("/v3/volumes/detail")).json()["volumes"]
    assert {v["name"] for v in detail} == {"one", "two"}
    filtered = (await api["cinder"].get("/v3/volumes/detail?name=one")).json()["volumes"]
    assert len(filtered) == 1


async def test_volume_delete_and_404(api) -> None:
    volume = await _volume(api)
    assert (await api["cinder"].delete(f"/v3/volumes/{volume['id']}")).status_code == 202
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).status_code == 404
    assert (await api["cinder"].delete(f"/v3/volumes/{volume['id']}")).status_code == 404


async def test_capacity_is_enforced_against_the_host_pool(api) -> None:
    response = await api["cinder"].post("/v3/volumes", json={"volume": {"size": 5000}})
    assert response.status_code == 413
    assert "VolumeSizeExceedsAvailableQuota" in response.json()["overLimit"]["message"]


async def test_volumes_consume_the_shared_disk_pool(api) -> None:
    await _volume(api, size=500)
    stats = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert stats["local_gb_used"] == 500


async def test_extend_action(api) -> None:
    volume = await _volume(api, size=10)
    await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    extended = await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                        json={"os-extend": {"new_size": 40}})
    assert extended.status_code == 202
    body = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert body["size"] == 40 and body["status"] == "available"


async def test_extend_must_grow_the_volume(api) -> None:
    volume = await _volume(api, size=10)
    response = await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                        json={"os-extend": {"new_size": 5}})
    assert response.status_code == 400


async def test_extend_respects_capacity(api) -> None:
    volume = await _volume(api, size=10)
    response = await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                        json={"os-extend": {"new_size": 5000}})
    assert response.status_code == 413


async def test_reset_status_and_set_bootable(api) -> None:
    volume = await _volume(api)
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-reset_status": {"status": "error"}})
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "error"
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-set_bootable": {"bootable": "true"}})
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]["bootable"] == "true"


async def test_attach_and_detach_actions(api) -> None:
    volume = await _volume(api)
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-attach": {"instance_uuid": "vm-1",
                                                 "mountpoint": "/dev/vdz"}})
    body = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert body["status"] == "in-use"
    assert body["attachments"][0]["device"] == "/dev/vdz"

    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action", json={"os-detach": {}})
    body = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert body["status"] == "available" and body["attachments"] == []


async def test_revert_action(api) -> None:
    volume = await _volume(api)
    response = await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                        json={"revert": {"snapshot_id": "s"}})
    assert response.status_code == 202


async def test_unknown_and_empty_actions_are_rejected(api) -> None:
    volume = await _volume(api)
    assert (await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                     json={"os-teleport": {}})).status_code == 400
    assert (await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                                     json={})).status_code == 400


async def test_attached_volume_cannot_be_deleted(api) -> None:
    volume = await _volume(api)
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-attach": {"instance_uuid": "vm-1"}})
    response = await api["cinder"].delete(f"/v3/volumes/{volume['id']}")
    assert response.status_code == 400
    assert "still attached" in response.json()["badRequest"]["message"]


async def test_attachments_api(api) -> None:
    volume = await _volume(api)
    await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    created = await api["cinder"].post("/v3/attachments", json={
        "attachment": {"volume_uuid": volume["id"], "connector": {"mountpoint": "/dev/vdb"}}})
    assert created.status_code == 200
    attachment = created.json()["attachment"]
    assert attachment["connection_info"]["driver_volume_type"] == "iscsi"
    assert volume["id"] in attachment["connection_info"]["target_iqn"]

    assert (await api["cinder"].get(f"/v3/attachments/{attachment['id']}")).status_code == 200
    assert len((await api["cinder"].get("/v3/attachments")).json()["attachments"]) == 1
    assert (await api["cinder"].delete(f"/v3/attachments/{attachment['id']}")).status_code == 200
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "available"
    assert (await api["cinder"].get("/v3/attachments/nope")).status_code == 404


async def test_attachment_to_a_missing_instance_is_a_404(api) -> None:
    volume = await _volume(api)
    response = await api["cinder"].post("/v3/attachments", json={
        "attachment": {"volume_uuid": volume["id"], "instance_uuid": "ghost"}})
    assert response.status_code == 404


async def test_volume_types(api) -> None:
    types = (await api["cinder"].get("/v3/types")).json()["volume_types"]
    assert {t["name"] for t in types} == {"__DEFAULT__", "lvm-ssd"}

    created = await api["cinder"].post("/v3/types", json={
        "volume_type": {"name": "nvme", "extra_specs": {"speed": "fast"}}})
    assert created.status_code == 202
    type_id = created.json()["volume_type"]["id"]
    assert (await api["cinder"].get(f"/v3/types/{type_id}")).status_code == 200
    assert (await api["cinder"].get("/v3/types/nvme")).json()["volume_type"]["id"] == type_id
    assert (await api["cinder"].delete(f"/v3/types/{type_id}")).status_code == 202
    assert (await api["cinder"].get("/v3/types/nvme")).status_code == 404


async def test_snapshot_lifecycle(api) -> None:
    volume = await _volume(api, size=20)
    await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    created = await api["cinder"].post("/v3/snapshots", json={
        "snapshot": {"volume_id": volume["id"], "name": "nightly"}})
    assert created.status_code == 202
    snapshot = created.json()["snapshot"]
    assert snapshot["status"] == "creating" and snapshot["size"] == 20

    fetched = (await api["cinder"].get(f"/v3/snapshots/{snapshot['id']}")).json()["snapshot"]
    assert fetched["status"] == "available"
    assert len((await api["cinder"].get("/v3/snapshots")).json()["snapshots"]) == 1
    assert len((await api["cinder"].get("/v3/snapshots/detail")).json()["snapshots"]) == 1
    assert (await api["cinder"].delete(f"/v3/snapshots/{snapshot['id']}")).status_code == 202
    assert (await api["cinder"].get(f"/v3/snapshots/{snapshot['id']}")).status_code == 404


async def test_snapshot_of_an_attached_volume_needs_force(api) -> None:
    volume = await _volume(api)
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-attach": {"instance_uuid": "vm-1"}})
    refused = await api["cinder"].post("/v3/snapshots",
                                       json={"snapshot": {"volume_id": volume["id"]}})
    assert refused.status_code == 400
    forced = await api["cinder"].post("/v3/snapshots",
                                      json={"snapshot": {"volume_id": volume["id"],
                                                         "force": True}})
    assert forced.status_code == 202


async def test_limits_quotas_and_pools(api, cloud) -> None:
    await _volume(api, size=100)
    absolute = (await api["cinder"].get("/v3/limits")).json()["limits"]["absolute"]
    assert absolute["maxTotalVolumeGigabytes"] == 4096
    assert absolute["totalGigabytesUsed"] == 100

    # The absolute limits above are the node's; the quota is the project's, and the
    # seeded admin project has none -- so storage capacity is what binds for it.
    quota = (await api["cinder"].get(f"/v3/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert quota["id"] == cloud.project_id and quota["gigabytes"] == -1

    pools = (await api["cinder"].get("/v3/scheduler-stats/get_pools")).json()["pools"]
    assert pools[0]["capabilities"]["allocated_capacity_gb"] == 100
    assert pools[0]["capabilities"]["total_capacity_gb"] == 4096
