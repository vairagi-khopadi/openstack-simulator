"""Cinder's remaining gaps: metadata, transfers, and the volume actions that were missing.

Transfers are the interesting one. The auth key is the whole mechanism -- knowing a
transfer id must not be enough to take someone's volume -- and getting that wrong is the
kind of thing a simulator papers over by accepting anything.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def _volume(api: dict[str, Any], size: int = 1, name: str = "v") -> str:
    created = await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": name, "size": size}}
    )
    return created.json()["volume"]["id"]


async def _snapshot(api: dict[str, Any], volume_id: str, name: str = "s") -> str:
    created = await api["cinder"].post(
        "/v3/snapshots", json={"snapshot": {"volume_id": volume_id, "name": name}}
    )
    return created.json()["snapshot"]["id"]


# --------------------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------------------


async def test_volume_metadata_round_trips(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    replaced = await api["cinder"].put(
        f"/v3/volumes/{volume_id}/metadata", json={"metadata": {"tier": "gold"}}
    )
    assert replaced.json()["metadata"] == {"tier": "gold"}
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}/metadata")).json()["metadata"] == {"tier": "gold"}


async def test_post_merges_and_put_replaces(api: dict[str, Any], cloud: Any) -> None:
    """The distinction matters: a client using POST expects to keep the other keys."""
    volume_id = await _volume(api)
    await api["cinder"].put(
        f"/v3/volumes/{volume_id}/metadata", json={"metadata": {"a": "1", "b": "2"}}
    )
    merged = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/metadata", json={"metadata": {"b": "9", "c": "3"}}
    )
    assert merged.json()["metadata"] == {"a": "1", "b": "9", "c": "3"}

    replaced = await api["cinder"].put(
        f"/v3/volumes/{volume_id}/metadata", json={"metadata": {"only": "this"}}
    )
    assert replaced.json()["metadata"] == {"only": "this"}


async def test_a_single_metadata_item(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    await api["cinder"].put(
        f"/v3/volumes/{volume_id}/metadata/tier", json={"meta": {"tier": "silver"}}
    )
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}/metadata/tier")).json()["meta"] == {"tier": "silver"}

    assert (await api["cinder"].delete(
        f"/v3/volumes/{volume_id}/metadata/tier")).status_code == 200
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}/metadata/tier")).status_code == 404


async def test_a_key_that_disagrees_with_the_uri_is_rejected(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    refused = await api["cinder"].put(
        f"/v3/volumes/{volume_id}/metadata/tier", json={"meta": {"other": "x"}}
    )
    assert refused.status_code == 400


async def test_snapshot_metadata(api: dict[str, Any], cloud: Any) -> None:
    snapshot_id = await _snapshot(api, await _volume(api))
    await api["cinder"].put(
        f"/v3/snapshots/{snapshot_id}/metadata", json={"metadata": {"keep": "yes"}}
    )
    assert (await api["cinder"].get(
        f"/v3/snapshots/{snapshot_id}/metadata")).json()["metadata"] == {"keep": "yes"}


# --------------------------------------------------------------------------------------
# Transfers
# --------------------------------------------------------------------------------------


async def test_a_transfer_hands_the_volume_over(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api, name="handover")
    created = await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id, "name": "t"}}
    )
    assert created.status_code == 202
    transfer = created.json()["transfer"]
    assert transfer["auth_key"]

    # Parked while ownership is in flight.
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}")).json()["volume"]["status"] == "awaiting-transfer"

    accepted = await api["cinder"].post(
        f"/v3/volume-transfers/{transfer['id']}/accept",
        json={"accept": {"auth_key": transfer["auth_key"]}},
    )
    assert accepted.status_code == 202
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}")).json()["volume"]["status"] == "available"


async def test_the_auth_key_is_required_to_accept(api: dict[str, Any], cloud: Any) -> None:
    """Knowing the transfer id must not be enough to take someone's volume."""
    volume_id = await _volume(api)
    transfer = (await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id}}
    )).json()["transfer"]

    refused = await api["cinder"].post(
        f"/v3/volume-transfers/{transfer['id']}/accept",
        json={"accept": {"auth_key": "guessed"}},
    )
    assert refused.status_code == 400
    assert "auth key" in refused.text.lower()


async def test_the_auth_key_is_shown_only_at_creation(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    transfer = (await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id}}
    )).json()["transfer"]
    assert "auth_key" in transfer

    fetched = (await api["cinder"].get(
        f"/v3/volume-transfers/{transfer['id']}")).json()["transfer"]
    assert "auth_key" not in fetched


async def test_only_an_available_volume_can_be_transferred(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action", json={"os-attach": {"instance_uuid": "vm"}}
    )
    refused = await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id}}
    )
    assert refused.status_code == 400


async def test_cancelling_a_transfer_gives_the_volume_back(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    transfer = (await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id}}
    )).json()["transfer"]

    assert (await api["cinder"].delete(
        f"/v3/volume-transfers/{transfer['id']}")).status_code == 202
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}")).json()["volume"]["status"] == "available"
    assert (await api["cinder"].get(
        f"/v3/volume-transfers/{transfer['id']}")).status_code == 404


async def test_transfers_are_listed_until_accepted(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    transfer = (await api["cinder"].post(
        "/v3/volume-transfers", json={"transfer": {"volume_id": volume_id}}
    )).json()["transfer"]
    assert len((await api["cinder"].get("/v3/volume-transfers")).json()["transfers"]) == 1

    await api["cinder"].post(
        f"/v3/volume-transfers/{transfer['id']}/accept",
        json={"accept": {"auth_key": transfer["auth_key"]}},
    )
    assert (await api["cinder"].get("/v3/volume-transfers")).json()["transfers"] == []


async def test_the_legacy_transfer_path_works_too(api: dict[str, Any], cloud: Any) -> None:
    """Older clients call /os-volume-transfer; both spellings reach the same handler."""
    volume_id = await _volume(api)
    created = await api["cinder"].post(
        "/v3/os-volume-transfer", json={"transfer": {"volume_id": volume_id}}
    )
    assert created.status_code == 202


# --------------------------------------------------------------------------------------
# Volume actions
# --------------------------------------------------------------------------------------


async def test_retype_changes_the_volume_type(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    retyped = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action",
        json={"os-retype": {"new_type": "lvm-ssd", "migration_policy": "on-demand"}},
    )
    assert retyped.status_code == 202
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume_id}")).json()["volume"]["volume_type"] == "lvm-ssd"


async def test_retyping_to_the_same_type_is_refused(api: dict[str, Any], cloud: Any) -> None:
    volume_id = await _volume(api)
    current = (await api["cinder"].get(
        f"/v3/volumes/{volume_id}")).json()["volume"]["volume_type"]
    refused = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action", json={"os-retype": {"new_type": current}}
    )
    assert refused.status_code == 400


async def test_retyping_to_an_unknown_type_is_refused(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api)
    refused = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action", json={"os-retype": {"new_type": "unobtainium"}}
    )
    assert refused.status_code == 400


async def test_upload_to_image_creates_a_glance_image(
    api: dict[str, Any], cloud: Any
) -> None:
    volume_id = await _volume(api, size=4)
    uploaded = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action",
        json={"os-volume_upload_image": {"image_name": "from-volume",
                                         "disk_format": "qcow2"}},
    )
    assert uploaded.status_code == 202
    body = uploaded.json()["os-volume_upload_image"]
    assert body["image_name"] == "from-volume"

    image = (await api["glance"].get(f"/v2/images/{body['image_id']}")).json()
    assert image["name"] == "from-volume"
    assert image["min_disk"] == 4


async def test_revert_to_snapshot_only_takes_the_latest(
    api: dict[str, Any], cloud: Any
) -> None:
    """Older snapshots would need deltas that no longer exist once a newer one was taken."""
    volume_id = await _volume(api)
    older = await _snapshot(api, volume_id, "older")
    newer = await _snapshot(api, volume_id, "newer")

    refused = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action",
        json={"revert_to_snapshot": {"snapshot_id": older}},
    )
    assert refused.status_code == 400
    assert "latest" in refused.text

    accepted = await api["cinder"].post(
        f"/v3/volumes/{volume_id}/action",
        json={"revert_to_snapshot": {"snapshot_id": newer}},
    )
    assert accepted.status_code == 202


async def test_reverting_to_another_volumes_snapshot_is_refused(
    api: dict[str, Any], cloud: Any
) -> None:
    first, second = await _volume(api, name="a"), await _volume(api, name="b")
    foreign = await _snapshot(api, second)
    refused = await api["cinder"].post(
        f"/v3/volumes/{first}/action", json={"revert_to_snapshot": {"snapshot_id": foreign}}
    )
    assert refused.status_code == 400
    assert "does not belong" in refused.text
