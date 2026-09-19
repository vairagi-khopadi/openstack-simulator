"""Per-project quotas: set them, and they bind.

The distinction these exist to hold down is quota vs capacity. Capacity is the node --
shared, physical, the same however many projects there are. A quota is one project's
policy limit, and it binds long before the hardware does. Both are checked on every
create, quota first, and the error says which one fired.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services import quotas

pytestmark = pytest.mark.anyio


async def _ids(api: dict[str, Any]) -> tuple[str, str]:
    images = (await api["glance"].get("/v2/images")).json()["images"]
    image = [i for i in images if i["name"] == "cirros"][0]["id"]
    networks = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    network = [n for n in networks if n["name"] == "private"][0]["id"]
    return image, network


async def _boot(api: dict[str, Any], name: str = "vm", flavor: str = "1") -> Any:
    image, network = await _ids(api)
    return await api["nova"].post(
        "/v2.1/servers",
        json={"server": {"name": name, "flavorRef": flavor, "imageRef": image,
                         "networks": [{"uuid": network}]}},
    )


async def _set_nova_quota(api: dict[str, Any], project: str, **values: int) -> Any:
    return await api["nova"].put(
        f"/v2.1/os-quota-sets/{project}", json={"quota_set": values}
    )


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


async def test_seeded_admin_is_unlimited(api: dict[str, Any], cloud: Any) -> None:
    """So the node's depletion model stays the thing that binds out of the box."""
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert body["instances"] == -1 and body["cores"] == -1


async def test_an_untouched_project_gets_the_service_defaults(
    api: dict[str, Any], cloud: Any
) -> None:
    """Sized for ten VMs, so a project provisioned after this one inherits room to boot."""
    body = (await api["nova"].get("/v2.1/os-quota-sets/brand-new")).json()["quota_set"]
    assert body["instances"] == 10 and body["cores"] == 20 and body["ram"] == 81920


async def test_defaults_endpoint_ignores_overrides(api: dict[str, Any], cloud: Any) -> None:
    """`openstack quota list` diffs the live quota against this to spot changes."""
    await _set_nova_quota(api, cloud.project_id, cores=4)
    defaults = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}/defaults")).json()["quota_set"]
    assert defaults["cores"] == 20


async def test_storage_and_network_defaults_cover_the_same_ten_vms(
    api: dict[str, Any], cloud: Any
) -> None:
    """Nova's limits are only a third of a bootable VM; the other two services have to agree.

    Ten VMs booted from volume take a root and a data volume each, one port and one
    floating IP -- so a `cores` ceiling that fits ten is a lie if `volumes` fits five.
    """
    storage = (await api["cinder"].get(
        f"/v3/os-quota-sets/{cloud.project_id}/defaults")).json()["quota_set"]
    assert storage["volumes"] == 25 and storage["gigabytes"] == 2000

    network = (await api["neutron"].get(
        f"/v2.0/quotas/{cloud.project_id}/default")).json()["quota"]
    assert network["port"] == 60 and network["floatingip"] == 15


async def test_detail_endpoint_reports_usage(api: dict[str, Any], cloud: Any) -> None:
    """The path `openstack quota show --usage` actually calls."""
    await _boot(api, flavor="3")  # m1.medium: 2 vcpus, 4096 MB
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}/detail")).json()["quota_set"]
    assert body["instances"]["in_use"] == 1
    assert body["cores"]["in_use"] == 2
    assert body["ram"]["in_use"] == 4096
    assert body["cores"]["reserved"] == 0


async def test_usage_query_parameter_matches_the_detail_route(
    api: dict[str, Any], cloud: Any
) -> None:
    await _boot(api)
    query = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}?usage=True")).json()
    route = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}/detail")).json()
    assert query == route


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


async def test_put_then_get_round_trips(api: dict[str, Any], cloud: Any) -> None:
    response = await _set_nova_quota(api, cloud.project_id, instances=3, cores=6)
    assert response.status_code == 200
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert body["instances"] == 3 and body["cores"] == 6
    # Untouched resources keep whatever they had.
    assert body["ram"] == -1


async def test_put_is_idempotent_and_updates_in_place(api: dict[str, Any], cloud: Any) -> None:
    await _set_nova_quota(api, cloud.project_id, instances=3)
    await _set_nova_quota(api, cloud.project_id, instances=5)
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert body["instances"] == 5


async def test_delete_reverts_to_defaults(api: dict[str, Any], cloud: Any) -> None:
    await _set_nova_quota(api, cloud.project_id, instances=3)
    assert (await api["nova"].delete(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).status_code == 202
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert body["instances"] == 10  # the default, not the seeded -1


async def test_a_non_integer_limit_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    response = await _set_nova_quota(api, cloud.project_id, instances="lots")
    assert response.status_code == 400


@pytest.mark.parametrize("service", ["nova", "cinder", "neutron"])
async def test_the_padding_clients_send_is_ignored(
    api: dict[str, Any], cloud: Any, service: str
) -> None:
    """`openstack quota set` puts tenant_id in the body; parsing it as a limit is a 400.

    Every client pads the request with identifiers alongside the real limits, so a PUT
    that only knows how to read integers rejects a perfectly valid command.
    """
    path, key, resource = {
        "nova": (f"/v2.1/os-quota-sets/{cloud.project_id}", "quota_set", "cores"),
        "cinder": (f"/v3/os-quota-sets/{cloud.project_id}", "quota_set", "volumes"),
        "neutron": (f"/v2.0/quotas/{cloud.project_id}", "quota", "network"),
    }[service]
    response = await api[service].put(
        path,
        json={key: {resource: 7, "tenant_id": cloud.project_id,
                    "project_id": cloud.project_id, "id": cloud.project_id,
                    "force": True}},
    )
    assert response.status_code == 200, response.text
    assert response.json()[key][resource] == 7


# --------------------------------------------------------------------------------------
# Enforcement -- the point of the exercise
# --------------------------------------------------------------------------------------


async def test_instance_quota_binds_before_the_node_does(
    api: dict[str, Any], cloud: Any
) -> None:
    """Two m1.tiny is nothing to a 256 GB node; the project's own limit still refuses."""
    await _set_nova_quota(api, cloud.project_id, instances=2)
    assert (await _boot(api, "one")).status_code == 202
    assert (await _boot(api, "two")).status_code == 202

    refused = await _boot(api, "three")
    assert refused.status_code == 403
    message = refused.json()["forbidden"]["message"]
    assert "instances" in message and "already used 2 of 2" in message


async def test_core_quota_is_counted_in_vcpus_not_instances(
    api: dict[str, Any], cloud: Any
) -> None:
    await _set_nova_quota(api, cloud.project_id, cores=3)
    assert (await _boot(api, "medium", flavor="3")).status_code == 202  # 2 vcpus
    # A second m1.medium would be 4 of 3 cores, even though it is only the 2nd instance.
    refused = await _boot(api, "second-medium", flavor="3")
    assert refused.status_code == 403
    assert "cores" in refused.json()["forbidden"]["message"]


async def test_ram_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await _set_nova_quota(api, cloud.project_id, ram=4096)
    assert (await _boot(api, "fits", flavor="3")).status_code == 202  # 4096 MB
    refused = await _boot(api, "over", flavor="1")
    assert refused.status_code == 403
    assert "ram" in refused.json()["forbidden"]["message"]


async def test_deleting_an_instance_frees_its_quota(api: dict[str, Any], cloud: Any) -> None:
    await _set_nova_quota(api, cloud.project_id, instances=1)
    created = await _boot(api, "only")
    server_id = created.json()["server"]["id"]
    assert (await _boot(api, "blocked")).status_code == 403

    await api["nova"].delete(f"/v2.1/servers/{server_id}")
    assert (await _boot(api, "now-fits")).status_code == 202


async def test_unlimited_means_unlimited(api: dict[str, Any], cloud: Any) -> None:
    await _set_nova_quota(api, cloud.project_id, instances=1)
    assert (await _boot(api, "first")).status_code == 202
    assert (await _boot(api, "second")).status_code == 403
    await _set_nova_quota(api, cloud.project_id, instances=-1)
    assert (await _boot(api, "unbounded")).status_code == 202


async def test_capacity_still_binds_when_the_quota_is_generous(
    api: dict[str, Any], cloud: Any
) -> None:
    """A quota cannot conjure RAM: the node is the other ceiling and it is still there."""
    await _set_nova_quota(api, cloud.project_id, instances=-1, cores=-1, ram=-1)
    refused = None
    for index in range(80):  # 80 x m1.medium overruns a 256 GB node
        response = await _boot(api, f"fill-{index}", flavor="3")
        if response.status_code == 403:
            refused = response
            break
    assert refused is not None, "the node never filled up"
    # The capacity error names the host, which is how you tell the two ceilings apart.
    assert "on host" in refused.json()["forbidden"]["message"]


# --------------------------------------------------------------------------------------
# Cinder and Neutron
# --------------------------------------------------------------------------------------


async def test_volume_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await api["cinder"].put(
        f"/v3/os-quota-sets/{cloud.project_id}", json={"quota_set": {"volumes": 1}}
    )
    assert (await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": "first", "size": 1}})).status_code == 202
    refused = await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": "second", "size": 1}}
    )
    assert refused.status_code == 413
    assert "volumes" in refused.text


async def test_gigabyte_quota_is_enforced_separately_from_volume_count(
    api: dict[str, Any], cloud: Any
) -> None:
    await api["cinder"].put(
        f"/v3/os-quota-sets/{cloud.project_id}", json={"quota_set": {"gigabytes": 10}}
    )
    assert (await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": "8gb", "size": 8}})).status_code == 202
    refused = await api["cinder"].post(
        "/v3/volumes", json={"volume": {"name": "another-8gb", "size": 8}}
    )
    assert refused.status_code == 413


async def test_network_quota_is_enforced_as_a_409_overquota(
    api: dict[str, Any], cloud: Any
) -> None:
    """Neutron's dialect for this is a 409 naming the resource, not Nova's 403."""
    await api["neutron"].put(
        f"/v2.0/quotas/{cloud.project_id}", json={"quota": {"network": 3}}
    )
    # Two networks are already seeded, so exactly one more fits.
    assert (await api["neutron"].post(
        "/v2.0/networks", json={"network": {"name": "third"}})).status_code == 201
    refused = await api["neutron"].post(
        "/v2.0/networks", json={"network": {"name": "fourth"}}
    )
    assert refused.status_code == 409
    body = refused.json()["NeutronError"]
    assert body["type"] == "OverQuota"
    assert "network" in body["message"]


async def test_neutron_quota_details_reports_usage(api: dict[str, Any], cloud: Any) -> None:
    body = (await api["neutron"].get(
        f"/v2.0/quotas/{cloud.project_id}/details")).json()["quota"]
    assert body["network"]["in_use"] == 2  # private + public, both seeded
    assert body["network"]["reserved"] == 0


async def test_neutron_lists_only_projects_with_overrides(
    api: dict[str, Any], cloud: Any
) -> None:
    empty = (await api["neutron"].get("/v2.0/quotas")).json()["quotas"]
    assert [q for q in empty if q["project_id"] == "listed-project"] == []
    await api["neutron"].put("/v2.0/quotas/listed-project", json={"quota": {"port": 5}})
    listed = (await api["neutron"].get("/v2.0/quotas")).json()["quotas"]
    assert [q for q in listed if q["project_id"] == "listed-project"][0]["port"] == 5


# --------------------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------------------


async def test_enforcement_can_be_turned_off(
    api: dict[str, Any], cloud: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPENSTACK_SIMULATOR_ENFORCE_QUOTAS=0 leaves the APIs readable but non-binding."""
    from app.core.config import settings

    await _set_nova_quota(api, cloud.project_id, instances=1)
    assert (await _boot(api, "first")).status_code == 202
    assert (await _boot(api, "blocked")).status_code == 403

    monkeypatch.setattr(settings, "enforce_quotas", False)
    assert (await _boot(api, "allowed-now")).status_code == 202
    # The limit is still reported; it just does not bind.
    body = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert body["instances"] == 1


# --------------------------------------------------------------------------------------
# The service layer
# --------------------------------------------------------------------------------------


async def test_limits_layer_defaults_under_overrides(api: dict[str, Any], cloud: Any) -> None:
    from app.core.database import SessionLocal

    async with SessionLocal() as session:
        await quotas.set_limits(session, "nova", "layered", {"cores": 7})
        await session.commit()
        effective = await quotas.limits(session, "nova", "layered")
    assert effective["cores"] == 7
    assert effective["instances"] == quotas.NOVA_DEFAULTS["instances"]


async def test_unknown_resources_are_ignored_rather_than_stored(
    api: dict[str, Any], cloud: Any
) -> None:
    """A typo'd resource must not become a quota nothing ever checks."""
    from app.core.database import SessionLocal

    async with SessionLocal() as session:
        await quotas.set_limits(session, "nova", "typo", {"coress": 7})
        await session.commit()
        assert await quotas.overrides(session, "nova", "typo") == {}
