"""Cinder backups.

A backup is the one storage resource that does *not* come out of the node's disk pool --
on a real cloud it lands in object storage -- so these check that it is bounded by quota
rather than by capacity, and that the dependency rules incremental backups impose are
actually enforced.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.models.storage import Backup, Volume

pytestmark = pytest.mark.anyio


async def _volume(api: dict[str, Any], size: int = 1, name: str = "src") -> str:
    created = await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": name, "size": size}}
    )
    return created.json()["volume"]["id"]


async def _backup(api: dict[str, Any], volume_id: str, **extra: Any) -> Any:
    return await api["cinder"].post(
        "/v3/backups", json={"backup": {"volume_id": volume_id, **extra}}
    )


async def _settle(expire: Any, volume_id: str | None = None, backup_id: str | None = None) -> None:
    if volume_id:
        await expire(Volume, volume_id)
    if backup_id:
        await expire(Backup, backup_id)


async def _ready_volume(api: dict[str, Any], expire: Any, size: int = 1,
                        name: str = "src") -> str:
    """A volume that has finished creating.

    Under ``slow_transitions`` a new volume sits in ``creating``, and Cinder refuses to
    back up a volume that is not available -- so the tests about backup windows have to
    settle the source first, exactly as a script would have to wait for it.
    """
    volume_id = await _volume(api, size=size, name=name)
    await expire(Volume, volume_id)
    await api["cinder"].get(f"/v3/volumes/{volume_id}")  # read settles it
    return volume_id


# --------------------------------------------------------------------------------------
# Create and read
# --------------------------------------------------------------------------------------


async def test_create_returns_202_with_a_link(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    response = await _backup(api, volume_id, name="nightly")
    assert response.status_code == 202
    body = response.json()["backup"]
    assert body["name"] == "nightly"
    assert body["links"][0]["href"].endswith(f"/v3/backups/{body['id']}")


async def test_backup_becomes_available_after_its_window(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    volume_id = await _ready_volume(api, expire)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]

    pending = (await api["cinder"].get(f"/v3/backups/{backup_id}")).json()["backup"]
    assert pending["status"] == "creating"

    await _settle(expire, backup_id=backup_id)
    settled = (await api["cinder"].get(f"/v3/backups/{backup_id}")).json()["backup"]
    assert settled["status"] == "available"


async def test_the_source_volume_is_held_while_backing_up(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    """A volume in backing-up must not look available, or a delete would race the copy."""
    volume_id = await _ready_volume(api, expire)
    await _backup(api, volume_id)
    volume = (await api["cinder"].get(f"/v3/volumes/{volume_id}")).json()["volume"]
    assert volume["status"] == "backing-up"

    await _settle(expire, volume_id=volume_id)
    volume = (await api["cinder"].get(f"/v3/volumes/{volume_id}")).json()["volume"]
    assert volume["status"] == "available"


async def test_detail_listing_carries_the_full_body(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api, size=3)
    await _backup(api, volume_id, name="full")

    brief = (await api["cinder"].get("/v3/backups")).json()["backups"]
    assert set(brief[0]) == {"id", "name", "links"}

    detail = (await api["cinder"].get("/v3/backups/detail")).json()["backups"]
    assert detail[0]["size"] == 3
    assert detail[0]["container"] == "volumebackups"
    assert detail[0]["availability_zone"] == "nova"


async def test_listing_filters_by_volume(api: dict[str, Any], cloud: Any) -> None:
    first, second = await _volume(api, name="a"), await _volume(api, name="b")
    await _backup(api, first)
    await _backup(api, second)
    filtered = (await api["cinder"].get(f"/v3/backups?volume_id={first}")).json()["backups"]
    assert len(filtered) == 1


async def test_unknown_backup_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["cinder"].get("/v3/backups/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Rules Cinder actually enforces
# --------------------------------------------------------------------------------------


async def test_an_attached_volume_needs_force(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action", json={"os-attach": {"instance_uuid": "vm"}}
    )
    refused = await _backup(api, volume_id)
    assert refused.status_code == 400
    assert "forced" in refused.text

    assert (await _backup(api, volume_id, force=True)).status_code == 202


async def test_incremental_needs_a_parent(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    refused = await _backup(api, volume_id, incremental=True)
    assert refused.status_code == 400
    assert "No backups available" in refused.text


async def test_incremental_chains_onto_the_last_full_backup(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    parent = (await _backup(api, volume_id, name="full")).json()["backup"]["id"]
    child = (await _backup(api, volume_id, name="inc", incremental=True)).json()["backup"]

    body = (await api["cinder"].get(f"/v3/backups/{child['id']}")).json()["backup"]
    assert body["is_incremental"] is True

    parent_body = (await api["cinder"].get(f"/v3/backups/{parent}")).json()["backup"]
    assert parent_body["has_dependent_backups"] is True


async def test_a_parent_with_children_cannot_be_deleted(
    api: dict[str, Any], cloud: Any
) -> None:
    """Deleting the base of an incremental chain would leave the children unrestorable."""
    volume_id = await _volume(api)
    parent = (await _backup(api, volume_id)).json()["backup"]["id"]
    await _backup(api, volume_id, incremental=True)

    refused = await api["cinder"].delete(f"/v3/backups/{parent}")
    assert refused.status_code == 400
    assert "Incremental backups exist" in refused.text


async def test_delete_removes_it_from_the_listing(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]
    assert (await api["cinder"].delete(f"/v3/backups/{backup_id}")).status_code == 202
    assert (await api["cinder"].get("/v3/backups")).json()["backups"] == []


async def test_a_backup_still_creating_cannot_be_deleted(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    volume_id = await _ready_volume(api, expire)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]
    refused = await api["cinder"].delete(f"/v3/backups/{backup_id}")
    assert refused.status_code == 400
    assert "must be available or error" in refused.text


async def test_reset_status_unsticks_a_backup(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    volume_id = await _ready_volume(api, expire)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]
    await api["cinder"].post(
        f"/v3/backups/{backup_id}/action", json={"os-reset_status": {"status": "error"}}
    )
    body = (await api["cinder"].get(f"/v3/backups/{backup_id}")).json()["backup"]
    assert body["status"] == "error"
    # error is a deletable state, unlike creating.
    assert (await api["cinder"].delete(f"/v3/backups/{backup_id}")).status_code == 202


async def test_update_renames(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    backup_id = (await _backup(api, volume_id, name="old")).json()["backup"]["id"]
    updated = await api["cinder"].put(
        f"/v3/backups/{backup_id}", json={"backup": {"name": "new", "description": "d"}}
    )
    assert updated.json()["backup"]["name"] == "new"
    assert updated.json()["backup"]["description"] == "d"


# --------------------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------------------


async def test_restore_into_a_new_volume(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    volume_id = await _ready_volume(api, expire, size=2)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]
    await _settle(expire, backup_id=backup_id)

    restored = await api["cinder"].post(f"/v3/backups/{backup_id}/restore", json={})
    assert restored.status_code == 200
    body = restored.json()["restore"]
    assert body["backup_id"] == backup_id
    assert body["volume_id"] != volume_id

    volume = (await api["cinder"].get(f"/v3/volumes/{body['volume_id']}")).json()["volume"]
    assert volume["size"] == 2
    assert volume["status"] == "restoring-backup"


async def test_restore_into_an_existing_volume(api: dict[str, Any], cloud: Any) -> None:
    source = await _volume(api, size=2, name="source")
    target = await _volume(api, size=2, name="target")
    backup_id = (await _backup(api, source)).json()["backup"]["id"]

    restored = await api["cinder"].post(
        f"/v3/backups/{backup_id}/restore", json={"restore": {"volume_id": target}}
    )
    assert restored.json()["restore"]["volume_id"] == target


async def test_restore_refuses_a_volume_that_is_too_small(
    api: dict[str, Any], cloud: Any
) -> None:
    source = await _volume(api, size=5, name="big")
    target = await _volume(api, size=1, name="small")
    backup_id = (await _backup(api, source)).json()["backup"]["id"]

    refused = await api["cinder"].post(
        f"/v3/backups/{backup_id}/restore", json={"restore": {"volume_id": target}}
    )
    assert refused.status_code == 400
    assert "smaller than" in refused.text


async def test_restore_needs_an_available_backup(
    api: dict[str, Any], cloud: Any, slow_transitions: Any, expire: Any
) -> None:
    volume_id = await _ready_volume(api, expire)
    backup_id = (await _backup(api, volume_id)).json()["backup"]["id"]
    refused = await api["cinder"].post(f"/v3/backups/{backup_id}/restore", json={})
    assert refused.status_code == 400
    assert "must be available" in refused.text


# --------------------------------------------------------------------------------------
# Quota, not capacity
# --------------------------------------------------------------------------------------


async def test_backup_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await api["cinder"].put(
        f"/v3/os-quota-sets/{cloud.project_id}", json={"quota_set": {"backups": 1}}
    )
    volume_id = await _volume(api)
    assert (await _backup(api, volume_id, name="one")).status_code == 202
    refused = await _backup(api, volume_id, name="two", force=True)
    assert refused.status_code == 413
    assert "backups" in refused.text


async def test_backup_gigabytes_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await api["cinder"].put(
        f"/v3/os-quota-sets/{cloud.project_id}",
        json={"quota_set": {"backup_gigabytes": 5}},
    )
    volume_id = await _volume(api, size=4)
    assert (await _backup(api, volume_id, name="fits")).status_code == 202
    refused = await _backup(api, volume_id, name="over", force=True)
    assert refused.status_code == 413


async def test_backups_do_not_consume_the_node_disk_pool(
    api: dict[str, Any], cloud: Any
) -> None:
    """They live in object storage on a real cloud, so the depletion pool is untouched."""
    volume_id = await _volume(api, size=100)
    before = (await api["cinder"].get("/v3/scheduler-stats/get_pools")).json()
    used_before = before["pools"][0]["capabilities"]["allocated_capacity_gb"]

    await _backup(api, volume_id)
    after = (await api["cinder"].get("/v3/scheduler-stats/get_pools")).json()
    assert after["pools"][0]["capabilities"]["allocated_capacity_gb"] == used_before


async def test_quota_usage_counts_backups(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api, size=3)
    await _backup(api, volume_id)
    body = (await api["cinder"].get(
        f"/v3/os-quota-sets/{cloud.project_id}?usage=True")).json()["quota_set"]
    assert body["backups"]["in_use"] == 1
    assert body["backup_gigabytes"]["in_use"] == 3
