"""Nova server groups.

The scheduler is the part of Nova this simulator cannot have -- there is one host, so
there is nowhere to place anything. What it *can* model faithfully is the refusal:
a second anti-affinity member has no other host to go to, which is the error a real
multi-node cloud produces once every host already holds a member.
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


async def _group(api: dict[str, Any], name: str = "web", policy: str = "anti-affinity") -> Any:
    return await api["nova"].post(
        "/v2.1/os-server-groups", json={"server_group": {"name": name, "policy": policy}}
    )


async def _boot(api: dict[str, Any], name: str, group: str | None = None) -> Any:
    image, network = await _ids(api)
    body: dict[str, Any] = {
        "server": {"name": name, "flavorRef": "1", "imageRef": image,
                   "networks": [{"uuid": network}]}
    }
    if group:
        body["os:scheduler_hints"] = {"group": group}
    return await api["nova"].post("/v2.1/servers", json=body)


# --------------------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------------------


async def test_create_and_show(api: dict[str, Any], cloud: Any) -> None:
    created = await _group(api, "web-tier")
    assert created.status_code == 200
    group = created.json()["server_group"]
    assert group["name"] == "web-tier"
    assert group["policy"] == "anti-affinity"
    assert group["members"] == []
    assert group["project_id"] == cloud.project_id

    fetched = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group['id']}")).json()["server_group"]
    assert fetched["id"] == group["id"]


@pytest.mark.parametrize(
    "policy", ["anti-affinity", "affinity", "soft-anti-affinity", "soft-affinity"]
)
async def test_every_policy_is_accepted(api: dict[str, Any], cloud: Any, policy: str) -> None:
    created = await _group(api, f"g-{policy}", policy)
    assert created.status_code == 200
    assert created.json()["server_group"]["policy"] == policy


async def test_an_unknown_policy_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    refused = await _group(api, "bad", "sometimes-affinity")
    assert refused.status_code == 400
    assert "policy" in refused.text


async def test_a_group_needs_a_name(api: dict[str, Any], cloud: Any) -> None:
    refused = await api["nova"].post(
        "/v2.1/os-server-groups", json={"server_group": {"policy": "affinity"}}
    )
    assert refused.status_code == 400


async def test_listing_is_scoped_to_the_project(api: dict[str, Any], cloud: Any) -> None:
    await _group(api, "mine")
    listed = (await api["nova"].get("/v2.1/os-server-groups")).json()["server_groups"]
    assert [g["name"] for g in listed] == ["mine"]
    assert all(g["project_id"] == cloud.project_id for g in listed)


async def test_delete_removes_the_group(api: dict[str, Any], cloud: Any) -> None:
    group_id = (await _group(api)).json()["server_group"]["id"]
    assert (await api["nova"].delete(
        f"/v2.1/os-server-groups/{group_id}")).status_code == 204
    assert (await api["nova"].get(f"/v2.1/os-server-groups/{group_id}")).status_code == 404
    assert (await api["nova"].get("/v2.1/os-server-groups")).json()["server_groups"] == []


async def test_unknown_group_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["nova"].get("/v2.1/os-server-groups/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Microversion 2.64 changed the shape
# --------------------------------------------------------------------------------------


async def test_policy_is_a_list_before_264(api: dict[str, Any], cloud: Any) -> None:
    """Before 2.64 a group carried "policies" and "metadata"; after it, "policy"/"rules"."""
    group_id = (await _group(api, "shaped")).json()["server_group"]["id"]

    old = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group_id}",
        headers={"OpenStack-API-Version": "compute 2.63"},
    )).json()["server_group"]
    assert old["policies"] == ["anti-affinity"]
    assert old["metadata"] == {}
    assert "policy" not in old and "rules" not in old

    new = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group_id}",
        headers={"OpenStack-API-Version": "compute 2.64"},
    )).json()["server_group"]
    assert new["policy"] == "anti-affinity"
    assert new["rules"] == {}
    assert "policies" not in new


async def test_creating_with_the_old_shape_works(api: dict[str, Any], cloud: Any) -> None:
    created = await api["nova"].post(
        "/v2.1/os-server-groups",
        json={"server_group": {"name": "legacy", "policies": ["affinity"]}},
        headers={"OpenStack-API-Version": "compute 2.63"},
    )
    assert created.status_code == 200
    assert created.json()["server_group"]["policies"] == ["affinity"]


# --------------------------------------------------------------------------------------
# Membership and scheduling
# --------------------------------------------------------------------------------------


async def test_a_booted_instance_joins_the_group(api: dict[str, Any], cloud: Any) -> None:
    group_id = (await _group(api, "affine", "affinity")).json()["server_group"]["id"]
    booted = await _boot(api, "member-1", group=group_id)
    assert booted.status_code == 202
    server_id = booted.json()["server"]["id"]

    group = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group_id}")).json()["server_group"]
    assert group["members"] == [server_id]


async def test_the_server_body_reports_its_group_from_271(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = (await _group(api, "affine", "affinity")).json()["server_group"]["id"]
    server_id = (await _boot(api, "m1", group=group_id)).json()["server"]["id"]

    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["server_groups"] == [group_id]


async def test_affinity_allows_several_members_on_one_host(
    api: dict[str, Any], cloud: Any
) -> None:
    """Affinity wants them together, and together is all this node can do."""
    group_id = (await _group(api, "together", "affinity")).json()["server_group"]["id"]
    assert (await _boot(api, "a", group=group_id)).status_code == 202
    assert (await _boot(api, "b", group=group_id)).status_code == 202

    group = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group_id}")).json()["server_group"]
    assert len(group["members"]) == 2


async def test_anti_affinity_refuses_a_second_member(api: dict[str, Any], cloud: Any) -> None:
    """The one case a single-node cloud models exactly: nowhere else to put it."""
    group_id = (await _group(api, "spread", "anti-affinity")).json()["server_group"]["id"]
    assert (await _boot(api, "first", group=group_id)).status_code == 202

    refused = await _boot(api, "second", group=group_id)
    assert refused.status_code == 409
    assert "anti-affinity" in refused.text


async def test_soft_anti_affinity_is_a_preference_not_a_rule(
    api: dict[str, Any], cloud: Any
) -> None:
    """Soft policies degrade rather than fail, which is the whole point of them."""
    group_id = (await _group(api, "soft", "soft-anti-affinity")).json()["server_group"]["id"]
    assert (await _boot(api, "one", group=group_id)).status_code == 202
    assert (await _boot(api, "two", group=group_id)).status_code == 202


async def test_deleting_a_member_frees_the_anti_affinity_slot(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = (await _group(api, "spread")).json()["server_group"]["id"]
    first = (await _boot(api, "first", group=group_id)).json()["server"]["id"]
    assert (await _boot(api, "second", group=group_id)).status_code == 409

    await api["nova"].delete(f"/v2.1/servers/{first}")
    assert (await _boot(api, "replacement", group=group_id)).status_code == 202


async def test_booting_into_a_missing_group_is_rejected(
    api: dict[str, Any], cloud: Any
) -> None:
    refused = await _boot(api, "orphan", group="no-such-group")
    assert refused.status_code == 400
    assert "not found" in refused.text


async def test_deleting_a_group_leaves_its_instances_running(
    api: dict[str, Any], cloud: Any
) -> None:
    """Members outlive the group on a real cloud; they just stop being constrained."""
    group_id = (await _group(api, "temporary", "affinity")).json()["server_group"]["id"]
    server_id = (await _boot(api, "survivor", group=group_id)).json()["server"]["id"]

    await api["nova"].delete(f"/v2.1/os-server-groups/{group_id}")
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] in ("ACTIVE", "BUILD")
    assert body["server_groups"] == []


async def test_the_old_scheduler_hint_spelling_is_accepted(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = (await _group(api, "legacy-hint", "affinity")).json()["server_group"]["id"]
    image, network = await _ids(api)
    booted = await api["nova"].post(
        "/v2.1/servers",
        json={
            "server": {"name": "hinted", "flavorRef": "1", "imageRef": image,
                       "networks": [{"uuid": network}]},
            "OS-SCHEDULER-HINTS:scheduler_hints": {"group": group_id},
        },
    )
    assert booted.status_code == 202
    group = (await api["nova"].get(
        f"/v2.1/os-server-groups/{group_id}")).json()["server_group"]
    assert len(group["members"]) == 1


async def test_server_group_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await api["nova"].put(
        f"/v2.1/os-quota-sets/{cloud.project_id}", json={"quota_set": {"server_groups": 1}}
    )
    assert (await _group(api, "first")).status_code == 200
    refused = await _group(api, "second")
    assert refused.status_code == 403
    assert "server_groups" in refused.text
