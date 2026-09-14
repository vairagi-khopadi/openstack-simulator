"""Nova's remaining gaps: interface attach/detach, server tags, flavor update and access.

Each of these was a listing or a read with no write behind it — the shape that lets code
appear to work against the simulator right up until it tries to change something.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def _ids(api: dict[str, Any]) -> tuple[str, str]:
    images = (await api["glance"].get("/v2/images")).json()["images"]
    image = [i for i in images if i["name"] == "cirros"][0]["id"]
    networks = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    network = [n for n in networks if n["name"] == "private"][0]["id"]
    return image, network


async def _boot(api: dict[str, Any], name: str = "vm") -> str:
    image, network = await _ids(api)
    created = await api["nova"].post(
        "/v2.1/servers",
        json={"server": {"name": name, "flavorRef": "1", "imageRef": image,
                         "networks": [{"uuid": network}]}},
    )
    return created.json()["server"]["id"]


# --------------------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------------------


async def test_attach_a_new_interface(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    before = (await api["nova"].get(
        f"/v2.1/servers/{server_id}/os-interface")).json()["interfaceAttachments"]

    attached = await api["nova"].post(
        f"/v2.1/servers/{server_id}/os-interface", json={"interfaceAttachment": {}}
    )
    assert attached.status_code == 200
    body = attached.json()["interfaceAttachment"]
    assert body["port_id"] and body["fixed_ips"][0]["ip_address"]

    after = (await api["nova"].get(
        f"/v2.1/servers/{server_id}/os-interface")).json()["interfaceAttachments"]
    assert len(after) == len(before) + 1


async def test_attach_an_existing_port(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    _, network = await _ids(api)
    port = (await api["neutron"].post(
        "/v2.0/ports", json={"port": {"network_id": network, "name": "spare"}}
    )).json()["port"]["id"]

    attached = await api["nova"].post(
        f"/v2.1/servers/{server_id}/os-interface",
        json={"interfaceAttachment": {"port_id": port}},
    )
    assert attached.json()["interfaceAttachment"]["port_id"] == port


async def test_a_port_already_in_use_is_refused(api: dict[str, Any], cloud: Any) -> None:
    first, second = await _boot(api, "one"), await _boot(api, "two")
    port = (await api["nova"].get(
        f"/v2.1/servers/{first}/os-interface")).json()["interfaceAttachments"][0]["port_id"]

    refused = await api["nova"].post(
        f"/v2.1/servers/{second}/os-interface",
        json={"interfaceAttachment": {"port_id": port}},
    )
    assert refused.status_code == 409
    assert "already attached" in refused.text


async def test_show_one_interface(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    port = (await api["nova"].get(
        f"/v2.1/servers/{server_id}/os-interface")).json()["interfaceAttachments"][0]["port_id"]
    body = (await api["nova"].get(
        f"/v2.1/servers/{server_id}/os-interface/{port}")).json()["interfaceAttachment"]
    assert body["port_id"] == port


async def test_detach_removes_the_interface(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    attached = (await api["nova"].post(
        f"/v2.1/servers/{server_id}/os-interface", json={"interfaceAttachment": {}}
    )).json()["interfaceAttachment"]["port_id"]

    assert (await api["nova"].delete(
        f"/v2.1/servers/{server_id}/os-interface/{attached}")).status_code == 202
    remaining = (await api["nova"].get(
        f"/v2.1/servers/{server_id}/os-interface")).json()["interfaceAttachments"]
    assert attached not in [i["port_id"] for i in remaining]


async def test_detaching_a_foreign_port_is_404(api: dict[str, Any], cloud: Any) -> None:
    first, second = await _boot(api, "a"), await _boot(api, "b")
    port = (await api["nova"].get(
        f"/v2.1/servers/{first}/os-interface")).json()["interfaceAttachments"][0]["port_id"]
    assert (await api["nova"].delete(
        f"/v2.1/servers/{second}/os-interface/{port}")).status_code == 404


async def test_attaching_to_a_pending_instance_is_refused(
    api: dict[str, Any], cloud: Any, slow_transitions: Any
) -> None:
    """A vNIC cannot be added to an instance that is still being built."""
    server_id = await _boot(api)
    refused = await api["nova"].post(
        f"/v2.1/servers/{server_id}/os-interface", json={"interfaceAttachment": {}}
    )
    assert refused.status_code == 409


# --------------------------------------------------------------------------------------
# Server tags
# --------------------------------------------------------------------------------------


async def test_tags_are_added_listed_and_removed(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    assert (await api["nova"].put(
        f"/v2.1/servers/{server_id}/tags/prod")).status_code == 201
    assert (await api["nova"].put(
        f"/v2.1/servers/{server_id}/tags/web")).status_code == 201

    tags = (await api["nova"].get(f"/v2.1/servers/{server_id}/tags")).json()["tags"]
    assert tags == ["prod", "web"]

    assert (await api["nova"].delete(
        f"/v2.1/servers/{server_id}/tags/prod")).status_code == 204
    assert (await api["nova"].get(
        f"/v2.1/servers/{server_id}/tags")).json()["tags"] == ["web"]


async def test_adding_an_existing_tag_is_204_not_201(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    assert (await api["nova"].put(f"/v2.1/servers/{server_id}/tags/x")).status_code == 201
    assert (await api["nova"].put(f"/v2.1/servers/{server_id}/tags/x")).status_code == 204


async def test_tags_can_be_replaced_wholesale(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    await api["nova"].put(f"/v2.1/servers/{server_id}/tags/old")
    replaced = await api["nova"].put(
        f"/v2.1/servers/{server_id}/tags", json={"tags": ["new", "newer"]}
    )
    assert replaced.json()["tags"] == ["new", "newer"]


async def test_all_tags_can_be_cleared(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    await api["nova"].put(f"/v2.1/servers/{server_id}/tags/a")
    assert (await api["nova"].delete(f"/v2.1/servers/{server_id}/tags")).status_code == 204
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}/tags")).json()["tags"] == []


async def test_removing_a_missing_tag_is_404(api: dict[str, Any], cloud: Any) -> None:
    server_id = await _boot(api)
    assert (await api["nova"].delete(
        f"/v2.1/servers/{server_id}/tags/absent")).status_code == 404


async def test_tags_need_microversion_226(api: dict[str, Any], cloud: Any) -> None:
    """The endpoint did not exist before 2.26, so an older client must get a 404."""
    server_id = await _boot(api)
    refused = await api["nova"].get(
        f"/v2.1/servers/{server_id}/tags",
        headers={"OpenStack-API-Version": "compute 2.25"},
    )
    assert refused.status_code == 404


# --------------------------------------------------------------------------------------
# Flavor update and access
# --------------------------------------------------------------------------------------


async def _private_flavor(api: dict[str, Any], name: str = "private.small") -> str:
    created = await api["nova"].post(
        "/v2.1/flavors",
        json={"flavor": {"name": name, "vcpus": 1, "ram": 512, "disk": 1,
                         "os-flavor-access:is_public": False}},
    )
    return created.json()["flavor"]["id"]


async def test_the_description_can_be_updated(api: dict[str, Any], cloud: Any) -> None:
    updated = await api["nova"].put(
        "/v2.1/flavors/1", json={"flavor": {"description": "the small one"}}
    )
    assert updated.status_code == 200
    assert updated.json()["flavor"]["description"] == "the small one"


async def test_resizing_a_flavor_is_refused(api: dict[str, Any], cloud: Any) -> None:
    """Editing vcpus would invalidate the booking of every instance already using it."""
    refused = await api["nova"].put("/v2.1/flavors/1", json={"flavor": {"vcpus": 64}})
    assert refused.status_code == 400
    assert "description" in refused.text


async def test_flavor_update_needs_microversion_255(api: dict[str, Any], cloud: Any) -> None:
    refused = await api["nova"].put(
        "/v2.1/flavors/1",
        json={"flavor": {"description": "x"}},
        headers={"OpenStack-API-Version": "compute 2.54"},
    )
    assert refused.status_code == 404


async def test_flavor_access_is_granted_and_revoked(api: dict[str, Any], cloud: Any) -> None:
    flavor_id = await _private_flavor(api)
    assert (await api["nova"].get(
        f"/v2.1/flavors/{flavor_id}/os-flavor-access")).json()["flavor_access"] == []

    granted = await api["nova"].post(
        f"/v2.1/flavors/{flavor_id}/action",
        json={"addTenantAccess": {"tenant": "team-b"}},
    )
    assert granted.json()["flavor_access"] == [
        {"flavor_id": flavor_id, "tenant_id": "team-b"}
    ]

    revoked = await api["nova"].post(
        f"/v2.1/flavors/{flavor_id}/action",
        json={"removeTenantAccess": {"tenant": "team-b"}},
    )
    assert revoked.json()["flavor_access"] == []


async def test_granting_access_twice_is_a_conflict(api: dict[str, Any], cloud: Any) -> None:
    flavor_id = await _private_flavor(api, "dup.flavor")
    await api["nova"].post(
        f"/v2.1/flavors/{flavor_id}/action", json={"addTenantAccess": {"tenant": "t"}}
    )
    again = await api["nova"].post(
        f"/v2.1/flavors/{flavor_id}/action", json={"addTenantAccess": {"tenant": "t"}}
    )
    assert again.status_code == 409


async def test_a_public_flavor_has_no_access_list(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["nova"].get("/v2.1/flavors/1/os-flavor-access")).status_code == 404
    refused = await api["nova"].post(
        "/v2.1/flavors/1/action", json={"addTenantAccess": {"tenant": "t"}}
    )
    assert refused.status_code == 409


async def test_revoking_access_that_was_never_granted_is_404(
    api: dict[str, Any], cloud: Any
) -> None:
    flavor_id = await _private_flavor(api, "never.granted")
    refused = await api["nova"].post(
        f"/v2.1/flavors/{flavor_id}/action",
        json={"removeTenantAccess": {"tenant": "nobody"}},
    )
    assert refused.status_code == 404
