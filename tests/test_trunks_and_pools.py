"""Neutron trunks and subnet pools.

Both are about bookkeeping rather than packets. A trunk's rules are exclusivity ones -- a
port has one role, and a VLAN id is used once per trunk. A pool's single promise is that
two allocations never overlap, which is the thing worth actually computing rather than
stubbing.
"""
from __future__ import annotations

import ipaddress
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def _network(api: dict[str, Any], name: str = "n") -> str:
    created = await api["neutron"].post("/v2.0/networks", json={"network": {"name": name}})
    return created.json()["network"]["id"]


async def _port(api: dict[str, Any], network_id: str, name: str = "p") -> str:
    created = await api["neutron"].post(
        "/v2.0/ports", json={"port": {"network_id": network_id, "name": name}}
    )
    return created.json()["port"]["id"]


async def _pool(api: dict[str, Any], prefixes: list[str], **extra: Any) -> Any:
    return await api["neutron"].post(
        "/v2.0/subnetpools",
        json={"subnetpool": {"name": "pool", "prefixes": prefixes, **extra}},
    )


# --------------------------------------------------------------------------------------
# Subnet pools
# --------------------------------------------------------------------------------------


async def test_create_and_show_a_pool(api: dict[str, Any], cloud: Any) -> None:
    created = await _pool(api, ["10.100.0.0/16"])
    assert created.status_code == 201
    body = created.json()["subnetpool"]
    assert body["prefixes"] == ["10.100.0.0/16"]
    # Neutron returns the prefix lengths as strings, oddly but consistently.
    assert body["default_prefixlen"] == "24"
    assert body["ip_version"] == 4

    fetched = (await api["neutron"].get(
        f"/v2.0/subnetpools/{body['id']}")).json()["subnetpool"]
    assert fetched["id"] == body["id"]


async def test_a_pool_needs_a_prefix(api: dict[str, Any], cloud: Any) -> None:
    refused = await _pool(api, [])
    assert refused.status_code == 400


async def test_an_invalid_prefix_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    refused = await _pool(api, ["not-a-network"])
    assert refused.status_code == 400


async def test_allocating_from_a_pool_never_overlaps(api: dict[str, Any], cloud: Any) -> None:
    """The one promise a pool makes, so it is computed rather than faked."""
    pool_id = (await _pool(api, ["10.200.0.0/16"])).json()["subnetpool"]["id"]
    network = await _network(api)

    cidrs = []
    for _ in range(5):
        created = await api["neutron"].post(
            "/v2.0/subnets",
            json={"subnet": {"network_id": network, "subnetpool_id": pool_id,
                             "prefixlen": 24}},
        )
        assert created.status_code == 201
        cidrs.append(created.json()["subnet"]["cidr"])

    assert len(set(cidrs)) == 5
    networks = [ipaddress.ip_network(c) for c in cidrs]
    for index, first in enumerate(networks):
        for second in networks[index + 1:]:
            assert not first.overlaps(second), f"{first} overlaps {second}"


async def test_the_default_prefix_length_is_used_when_none_is_given(
    api: dict[str, Any], cloud: Any
) -> None:
    pool_id = (await _pool(
        api, ["10.210.0.0/16"], default_prefixlen=26)).json()["subnetpool"]["id"]
    network = await _network(api)
    created = await api["neutron"].post(
        "/v2.0/subnets",
        json={"subnet": {"network_id": network, "subnetpool_id": pool_id}},
    )
    assert created.json()["subnet"]["cidr"].endswith("/26")


async def test_a_prefix_length_outside_the_pool_range_is_refused(
    api: dict[str, Any], cloud: Any
) -> None:
    pool_id = (await _pool(
        api, ["10.220.0.0/16"], min_prefixlen=24, max_prefixlen=28
    )).json()["subnetpool"]["id"]
    network = await _network(api)
    refused = await api["neutron"].post(
        "/v2.0/subnets",
        json={"subnet": {"network_id": network, "subnetpool_id": pool_id, "prefixlen": 30}},
    )
    assert refused.status_code == 400
    assert "outside the pool" in refused.text


async def test_an_exhausted_pool_is_a_409(api: dict[str, Any], cloud: Any) -> None:
    """A /24 pool holds exactly two /25s, and the third request has nowhere to go."""
    pool_id = (await _pool(api, ["192.168.77.0/24"])).json()["subnetpool"]["id"]
    network = await _network(api)
    for _ in range(2):
        created = await api["neutron"].post(
            "/v2.0/subnets",
            json={"subnet": {"network_id": network, "subnetpool_id": pool_id,
                             "prefixlen": 25}},
        )
        assert created.status_code == 201

    refused = await api["neutron"].post(
        "/v2.0/subnets",
        json={"subnet": {"network_id": network, "subnetpool_id": pool_id, "prefixlen": 25}},
    )
    assert refused.status_code == 409
    assert "no free prefix" in refused.text


async def test_a_pool_in_use_cannot_be_deleted(api: dict[str, Any], cloud: Any) -> None:
    pool_id = (await _pool(api, ["10.230.0.0/16"])).json()["subnetpool"]["id"]
    network = await _network(api)
    await api["neutron"].post(
        "/v2.0/subnets",
        json={"subnet": {"network_id": network, "subnetpool_id": pool_id}},
    )
    refused = await api["neutron"].delete(f"/v2.0/subnetpools/{pool_id}")
    assert refused.status_code == 409
    assert "in use" in refused.text


async def test_an_unused_pool_can_be_deleted(api: dict[str, Any], cloud: Any) -> None:
    pool_id = (await _pool(api, ["10.240.0.0/16"])).json()["subnetpool"]["id"]
    assert (await api["neutron"].delete(f"/v2.0/subnetpools/{pool_id}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/subnetpools/{pool_id}")).status_code == 404


async def test_updating_a_pool_only_grows_its_prefixes(
    api: dict[str, Any], cloud: Any
) -> None:
    """Shrinking would strand allocations that were already carved out of the removed space."""
    pool_id = (await _pool(api, ["10.250.0.0/16"])).json()["subnetpool"]["id"]
    updated = await api["neutron"].put(
        f"/v2.0/subnetpools/{pool_id}",
        json={"subnetpool": {"prefixes": ["10.251.0.0/16"], "name": "grown"}},
    )
    assert sorted(updated.json()["subnetpool"]["prefixes"]) == [
        "10.250.0.0/16", "10.251.0.0/16"
    ]
    assert updated.json()["subnetpool"]["name"] == "grown"


async def test_pools_are_listed(api: dict[str, Any], cloud: Any) -> None:
    await _pool(api, ["10.160.0.0/16"])
    listed = (await api["neutron"].get("/v2.0/subnetpools")).json()["subnetpools"]
    assert len(listed) == 1


async def test_subnetpool_quota_is_enforced(api: dict[str, Any], cloud: Any) -> None:
    await api["neutron"].put(
        f"/v2.0/quotas/{cloud.project_id}", json={"quota": {"subnetpool": 1}}
    )
    assert (await _pool(api, ["10.170.0.0/16"])).status_code == 201
    refused = await _pool(api, ["10.171.0.0/16"])
    assert refused.status_code == 409


# --------------------------------------------------------------------------------------
# Trunks
# --------------------------------------------------------------------------------------


async def test_create_a_trunk_with_subports(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent = await _port(api, network, "parent")
    child = await _port(api, network, "child")

    created = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"name": "t1", "port_id": parent,
                        "sub_ports": [{"port_id": child, "segmentation_type": "vlan",
                                       "segmentation_id": 101}]}},
    )
    assert created.status_code == 201
    body = created.json()["trunk"]
    assert body["port_id"] == parent
    assert body["status"] == "ACTIVE"
    assert body["sub_ports"] == [
        {"port_id": child, "segmentation_type": "vlan", "segmentation_id": 101}
    ]


async def test_a_port_can_parent_only_one_trunk(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent = await _port(api, network, "shared-parent")
    await api["neutron"].post("/v2.0/trunks", json={"trunk": {"port_id": parent}})

    refused = await api["neutron"].post("/v2.0/trunks", json={"trunk": {"port_id": parent}})
    assert refused.status_code == 409
    assert "already the parent" in refused.text


async def test_a_subport_cannot_belong_to_two_trunks(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    first_parent = await _port(api, network, "p1")
    second_parent = await _port(api, network, "p2")
    child = await _port(api, network, "c")

    await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": first_parent,
                        "sub_ports": [{"port_id": child, "segmentation_id": 10}]}},
    )
    refused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": second_parent,
                        "sub_ports": [{"port_id": child, "segmentation_id": 11}]}},
    )
    assert refused.status_code == 409
    assert "already a subport" in refused.text


async def test_the_parent_cannot_be_its_own_subport(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent = await _port(api, network, "self")
    refused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": parent,
                        "sub_ports": [{"port_id": parent, "segmentation_id": 5}]}},
    )
    assert refused.status_code == 409


async def test_a_vlan_id_is_used_once_per_trunk(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent = await _port(api, network, "p")
    one, two = await _port(api, network, "a"), await _port(api, network, "b")

    refused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": parent,
                        "sub_ports": [{"port_id": one, "segmentation_id": 7},
                                      {"port_id": two, "segmentation_id": 7}]}},
    )
    assert refused.status_code == 409
    assert "already used" in refused.text


@pytest.mark.parametrize("vlan", [0, 4095, 9999, -1])
async def test_segmentation_ids_outside_the_vlan_range_are_refused(
    api: dict[str, Any], cloud: Any, vlan: int
) -> None:
    network = await _network(api)
    parent, child = await _port(api, network, "p"), await _port(api, network, "c")
    refused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": parent,
                        "sub_ports": [{"port_id": child, "segmentation_id": vlan}]}},
    )
    assert refused.status_code == 400


async def test_subports_are_added_and_removed_after_creation(
    api: dict[str, Any], cloud: Any
) -> None:
    network = await _network(api)
    parent = await _port(api, network, "p")
    child = await _port(api, network, "c")
    trunk = (await api["neutron"].post(
        "/v2.0/trunks", json={"trunk": {"port_id": parent}})).json()["trunk"]["id"]

    added = await api["neutron"].put(
        f"/v2.0/trunks/{trunk}/add_subports",
        json={"sub_ports": [{"port_id": child, "segmentation_id": 42}]},
    )
    assert added.status_code == 200
    assert added.json()["sub_ports"][0]["segmentation_id"] == 42

    removed = await api["neutron"].put(
        f"/v2.0/trunks/{trunk}/remove_subports",
        json={"sub_ports": [{"port_id": child}]},
    )
    assert removed.json()["sub_ports"] == []


async def test_removing_a_subport_frees_the_port_for_another_trunk(
    api: dict[str, Any], cloud: Any
) -> None:
    network = await _network(api)
    first, second = await _port(api, network, "p1"), await _port(api, network, "p2")
    child = await _port(api, network, "c")

    trunk = (await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": first,
                        "sub_ports": [{"port_id": child, "segmentation_id": 3}]}},
    )).json()["trunk"]["id"]
    await api["neutron"].put(
        f"/v2.0/trunks/{trunk}/remove_subports", json={"sub_ports": [{"port_id": child}]}
    )

    reused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": second,
                        "sub_ports": [{"port_id": child, "segmentation_id": 3}]}},
    )
    assert reused.status_code == 201


async def test_a_missing_port_is_404(api: dict[str, Any], cloud: Any) -> None:
    refused = await api["neutron"].post("/v2.0/trunks", json={"trunk": {"port_id": "ghost"}})
    assert refused.status_code == 404


async def test_trunks_are_listed_and_updated(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent = await _port(api, network, "p")
    trunk = (await api["neutron"].post(
        "/v2.0/trunks", json={"trunk": {"name": "old", "port_id": parent}}
    )).json()["trunk"]["id"]

    listed = (await api["neutron"].get("/v2.0/trunks")).json()["trunks"]
    assert [t["id"] for t in listed] == [trunk]

    updated = await api["neutron"].put(
        f"/v2.0/trunks/{trunk}", json={"trunk": {"name": "new"}}
    )
    assert updated.json()["trunk"]["name"] == "new"


async def test_deleting_a_trunk_frees_its_ports(api: dict[str, Any], cloud: Any) -> None:
    network = await _network(api)
    parent, child = await _port(api, network, "p"), await _port(api, network, "c")
    trunk = (await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": parent,
                        "sub_ports": [{"port_id": child, "segmentation_id": 12}]}},
    )).json()["trunk"]["id"]

    assert (await api["neutron"].delete(f"/v2.0/trunks/{trunk}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/trunks/{trunk}")).status_code == 404

    # Both ports are usable again.
    reused = await api["neutron"].post(
        "/v2.0/trunks",
        json={"trunk": {"port_id": parent,
                        "sub_ports": [{"port_id": child, "segmentation_id": 12}]}},
    )
    assert reused.status_code == 201
