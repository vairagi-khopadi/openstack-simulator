"""Neutron Networking v2.0 API tests, including IPAM and conntrack accounting."""
from __future__ import annotations

import json

import pytest

from app.core.database import SessionLocal
from app.models.compute import Hypervisor
from sqlalchemy import select

pytestmark = pytest.mark.anyio


async def _network(api, name="testnet", **extra) -> dict:
    response = await api["neutron"].post("/v2.0/networks",
                                         json={"network": {"name": name, **extra}})
    assert response.status_code == 201, response.text
    return response.json()["network"]


async def _subnet(api, network_id, cidr="192.168.1.0/24", **extra) -> dict:
    response = await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network_id, "cidr": cidr, **extra}})
    assert response.status_code == 201, response.text
    return response.json()["subnet"]


async def test_version_and_extensions(raw_clients, api) -> None:
    versions = (await raw_clients["neutron"].get("/")).json()["versions"]
    assert versions[0]["id"] == "v2.0"
    aliases = {e["alias"] for e in (await api["neutron"].get("/v2.0/extensions")).json()["extensions"]}
    assert {"security-group", "external-net", "port-security"} <= aliases


async def test_seeded_networks(api, cloud) -> None:
    networks = {n["name"]: n for n in (await api["neutron"].get("/v2.0/networks")).json()["networks"]}
    assert set(networks) == {"private", "public"}
    assert networks["public"]["router:external"] is True
    assert networks["private"]["shared"] is True
    assert networks["private"]["project_id"] == cloud.project_id
    assert len(networks["private"]["subnets"]) == 1


async def test_network_crud(api) -> None:
    network = await _network(api, name="crud-net")
    assert network["status"] == "ACTIVE"
    assert network["provider:network_type"] == "vxlan"
    assert network["mtu"] == 1450

    fetched = (await api["neutron"].get(f"/v2.0/networks/{network['id']}")).json()["network"]
    assert fetched["name"] == "crud-net"

    updated = await api["neutron"].put(f"/v2.0/networks/{network['id']}",
                                       json={"network": {"name": "renamed", "mtu": 1400}})
    assert updated.json()["network"]["name"] == "renamed"
    assert updated.json()["network"]["revision_number"] == 2

    assert (await api["neutron"].delete(f"/v2.0/networks/{network['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/networks/{network['id']}")).status_code == 404


async def test_network_filters(api) -> None:
    await _network(api, name="filtered")
    assert len((await api["neutron"].get("/v2.0/networks?name=filtered")).json()["networks"]) == 1
    external = (await api["neutron"].get("/v2.0/networks?router:external=true")).json()["networks"]
    assert [n["name"] for n in external] == ["public"]


async def test_network_in_use_cannot_be_deleted(api) -> None:
    network = await _network(api, name="busy")
    await _subnet(api, network["id"])
    await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"], "device_id": "vm-1"}})
    response = await api["neutron"].delete(f"/v2.0/networks/{network['id']}")
    assert response.status_code == 409
    assert response.json()["NeutronError"]["type"] == "NetworkInUse"


async def test_subnet_cidr_math(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], cidr="10.20.30.0/24")
    assert subnet["gateway_ip"] == "10.20.30.1"
    assert subnet["allocation_pools"] == [{"start": "10.20.30.2", "end": "10.20.30.254"}]
    assert subnet["ip_version"] == 4
    assert subnet["enable_dhcp"] is True


async def test_subnet_honours_an_explicit_pool_and_gateway(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], cidr="10.9.0.0/24", gateway_ip="10.9.0.254",
                           allocation_pools=[{"start": "10.9.0.10", "end": "10.9.0.20"}])
    assert subnet["gateway_ip"] == "10.9.0.254"
    assert subnet["allocation_pools"] == [{"start": "10.9.0.10", "end": "10.9.0.20"}]


async def test_subnet_validation(api) -> None:
    network = await _network(api)
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"]}})).status_code == 400
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"], "cidr": "not-a-cidr"}})).status_code == 400
    assert (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": "ghost", "cidr": "10.0.1.0/24"}})).status_code == 404


async def test_subnet_crud(api) -> None:
    network = await _network(api)
    subnet = await _subnet(api, network["id"], name="sub", dns_nameservers=["9.9.9.9"])
    assert subnet["dns_nameservers"] == ["9.9.9.9"]
    listed = (await api["neutron"].get(f"/v2.0/subnets?network_id={network['id']}")).json()
    assert len(listed["subnets"]) == 1
    updated = await api["neutron"].put(f"/v2.0/subnets/{subnet['id']}",
                                       json={"subnet": {"name": "renamed"}})
    assert updated.json()["subnet"]["name"] == "renamed"
    assert (await api["neutron"].delete(f"/v2.0/subnets/{subnet['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/subnets/{subnet['id']}")).status_code == 404


async def test_ports_get_sequential_addresses_and_a_mac(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.50.0.0/24")
    addresses = []
    for index in range(3):
        port = (await api["neutron"].post("/v2.0/ports", json={
            "port": {"network_id": network["id"], "name": f"p{index}"}})).json()["port"]
        addresses.append(port["fixed_ips"][0]["ip_address"])
        assert port["mac_address"].startswith("fa:16:3e:")
    assert addresses == ["10.50.0.2", "10.50.0.3", "10.50.0.4"]


async def test_port_with_an_explicit_address(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.60.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"],
                 "fixed_ips": [{"ip_address": "10.60.0.99"}]}})).json()["port"]
    assert port["fixed_ips"][0]["ip_address"] == "10.60.0.99"


async def test_port_exhaustion_is_reported(api) -> None:
    network = await _network(api)
    # A /30 holds .1 and .2; .1 is the gateway, so exactly one address is allocatable.
    subnet = await _subnet(api, network["id"], cidr="10.70.0.0/30")
    assert subnet["allocation_pools"] == [{"start": "10.70.0.2", "end": "10.70.0.2"}]
    first = await api["neutron"].post("/v2.0/ports", json={"port": {"network_id": network["id"]}})
    assert first.status_code == 201
    assert first.json()["port"]["fixed_ips"][0]["ip_address"] == "10.70.0.2"
    second = await api["neutron"].post("/v2.0/ports", json={"port": {"network_id": network["id"]}})
    assert second.status_code == 409
    assert second.json()["NeutronError"]["type"] == "IpAddressGenerationFailure"


async def test_port_crud_and_filters(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.80.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": network["id"], "name": "p1", "device_id": "vm-9"}})).json()["port"]
    assert port["status"] == "ACTIVE", "a bound port comes up ACTIVE"

    by_device = (await api["neutron"].get("/v2.0/ports?device_id=vm-9")).json()["ports"]
    assert len(by_device) == 1
    by_mac = (await api["neutron"].get(f"/v2.0/ports?mac_address={port['mac_address']}")).json()
    assert len(by_mac["ports"]) == 1

    updated = await api["neutron"].put(f"/v2.0/ports/{port['id']}",
                                       json={"port": {"name": "renamed", "device_id": ""}})
    assert updated.json()["port"]["name"] == "renamed"
    assert (await api["neutron"].delete(f"/v2.0/ports/{port['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/ports/{port['id']}")).status_code == 404


async def test_unbound_port_is_down(api) -> None:
    network = await _network(api)
    await _subnet(api, network["id"], cidr="10.85.0.0/24")
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": network["id"]}})).json()["port"]
    assert port["status"] == "DOWN"


async def test_security_group_creation_costs_two_conntrack_slots(api) -> None:
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    created = await api["neutron"].post("/v2.0/security-groups",
                                        json={"security_group": {"name": "web"}})
    assert created.status_code == 201
    rules = created.json()["security_group"]["security_group_rules"]
    assert len(rules) == 2
    assert {r["direction"] for r in rules} == {"egress"}
    assert {r["ethertype"] for r in rules} == {"IPv4", "IPv6"}

    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after - before == 2


async def test_each_rule_costs_one_more_slot(api) -> None:
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "web"}})).json()["security_group"]
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    created = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"], "direction": "ingress",
                                "protocol": "tcp", "port_range_min": 22, "port_range_max": 22,
                                "remote_ip_prefix": "0.0.0.0/0"}})
    assert created.status_code == 201
    rule = created.json()["security_group_rule"]
    assert rule["port_range_min"] == 22 and rule["protocol"] == "tcp"
    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after - before == 1


async def test_deleting_a_group_returns_its_slots(api) -> None:
    before = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "temp"}})).json()["security_group"]
    await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"]}})
    assert (await api["neutron"].delete(f"/v2.0/security-groups/{group['id']}")).status_code == 204
    after = (await api["dashboard"].get("/api/stats")).json()["host"]["conntrack_used"]
    assert after == before, "rules cascade with the group"


async def test_conntrack_exhaustion_blocks_new_groups(api) -> None:
    async with SessionLocal() as session:
        host = (await session.execute(select(Hypervisor))).scalar_one()
        host.conntrack_max = 5          # the seeded default group already uses 4
        await session.commit()
    response = await api["neutron"].post("/v2.0/security-groups",
                                         json={"security_group": {"name": "overflow"}})
    assert response.status_code == 409
    assert response.json()["NeutronError"]["type"] == "SecurityGroupLimitExceeded"


async def test_conntrack_exhaustion_blocks_new_rules(api) -> None:
    group = (await api["neutron"].get("/v2.0/security-groups")).json()["security_groups"][0]
    async with SessionLocal() as session:
        host = (await session.execute(select(Hypervisor))).scalar_one()
        host.conntrack_max = 4
        await session.commit()
    response = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": group["id"]}})
    assert response.status_code == 409


async def test_security_group_crud(api) -> None:
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "app"}})).json()["security_group"]
    assert (await api["neutron"].get(f"/v2.0/security-groups/{group['id']}")).status_code == 200
    named = (await api["neutron"].get("/v2.0/security-groups?name=app")).json()["security_groups"]
    assert len(named) == 1
    updated = await api["neutron"].put(f"/v2.0/security-groups/{group['id']}",
                                       json={"security_group": {"description": "the app"}})
    assert updated.json()["security_group"]["description"] == "the app"
    assert (await api["neutron"].get("/v2.0/security-groups/ghost")).status_code == 404


async def test_security_group_rule_lookup_and_delete(api) -> None:
    group = (await api["neutron"].get("/v2.0/security-groups")).json()["security_groups"][0]
    rules = (await api["neutron"].get(
        f"/v2.0/security-group-rules?security_group_id={group['id']}")).json()["security_group_rules"]
    assert len(rules) == 4, "the seeded default group has 2 egress + 2 ingress rules"
    rule_id = rules[0]["id"]
    assert (await api["neutron"].get(f"/v2.0/security-group-rules/{rule_id}")).status_code == 200
    assert (await api["neutron"].delete(f"/v2.0/security-group-rules/{rule_id}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/security-group-rules/{rule_id}")).status_code == 404


async def test_rule_for_an_unknown_group_is_a_404(api) -> None:
    response = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {"security_group_id": "ghost"}})
    assert response.status_code == 404


async def test_floating_ip_allocation_and_association(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": private["id"]}})).json()["port"]

    created = await api["neutron"].post("/v2.0/floatingips",
                                        json={"floatingip": {"floating_network_id": public["id"]}})
    assert created.status_code == 201
    fip = created.json()["floatingip"]
    assert fip["floating_ip_address"].startswith("172.24.4.")
    assert fip["status"] == "DOWN" and fip["port_id"] is None

    associated = await api["neutron"].put(f"/v2.0/floatingips/{fip['id']}",
                                          json={"floatingip": {"port_id": port["id"]}})
    body = associated.json()["floatingip"]
    assert body["status"] == "ACTIVE"
    assert body["fixed_ip_address"] == port["fixed_ips"][0]["ip_address"]

    disassociated = await api["neutron"].put(f"/v2.0/floatingips/{fip['id']}",
                                             json={"floatingip": {"port_id": None}})
    assert disassociated.json()["floatingip"]["status"] == "DOWN"
    assert disassociated.json()["floatingip"]["fixed_ip_address"] is None


async def test_floating_ip_can_be_created_already_associated(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    port = (await api["neutron"].post("/v2.0/ports",
                                      json={"port": {"network_id": private["id"]}})).json()["port"]
    fip = (await api["neutron"].post("/v2.0/floatingips", json={
        "floatingip": {"floating_network_id": public["id"], "port_id": port["id"]}})).json()["floatingip"]
    assert fip["status"] == "ACTIVE"


async def test_floating_ips_are_unique_and_listable(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    addresses = set()
    for _ in range(3):
        fip = (await api["neutron"].post("/v2.0/floatingips", json={
            "floatingip": {"floating_network_id": public["id"]}})).json()["floatingip"]
        addresses.add(fip["floating_ip_address"])
    assert len(addresses) == 3
    listed = (await api["neutron"].get("/v2.0/floatingips")).json()["floatingips"]
    assert len(listed) == 3


async def test_floating_ip_release_hides_it(api) -> None:
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    fip = (await api["neutron"].post("/v2.0/floatingips", json={
        "floatingip": {"floating_network_id": public["id"]}})).json()["floatingip"]
    assert (await api["neutron"].delete(f"/v2.0/floatingips/{fip['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/floatingips/{fip['id']}")).status_code == 404
    assert (await api["neutron"].get("/v2.0/floatingips")).json()["floatingips"] == []


async def test_floating_ip_on_a_non_external_network_still_needs_a_subnet(api) -> None:
    empty = await _network(api, name="no-subnets")
    response = await api["neutron"].post("/v2.0/floatingips",
                                         json={"floatingip": {"floating_network_id": empty["id"]}})
    assert response.status_code == 400
    response = await api["neutron"].post("/v2.0/floatingips",
                                         json={"floatingip": {"floating_network_id": "ghost"}})
    assert response.status_code == 404


async def test_quotas_and_availability_zones(api, cloud) -> None:
    """The seeded admin project is unlimited; the conntrack envelope still binds."""
    quota = (await api["neutron"].get(f"/v2.0/quotas/{cloud.project_id}")).json()["quota"]
    assert quota["security_group_rule"] == -1
    # A project that has never been touched gets upstream's defaults instead.
    fresh = (await api["neutron"].get("/v2.0/quotas/some-other-project")).json()["quota"]
    assert fresh["security_group_rule"] == 100 and fresh["network"] == 100
    zones = (await api["neutron"].get("/v2.0/availability_zones")).json()["availability_zones"]
    assert {z["resource"] for z in zones} == {"network", "router"}


async def test_missing_body_object_is_rejected(api) -> None:
    assert (await api["neutron"].post("/v2.0/networks", json={"nope": {}})).status_code == 400


# --------------------------------------------------------------------------------------
# Tenancy: one project must not see another's resources
# --------------------------------------------------------------------------------------


async def _second_project(raw_clients, api, name="tenant-b") -> str:
    """Create a project + member user and return a token scoped to it."""
    keystone = api["keystone"]
    project = (await keystone.post("/v3/projects",
                                   json={"project": {"name": name}})).json()["project"]
    user = (await keystone.post("/v3/users", json={"user": {
        "name": f"{name}-user", "password": "pw",
        "default_project_id": project["id"]}})).json()["user"]
    role = (await keystone.get("/v3/roles?name=member")).json()["roles"][0]
    await keystone.put(f"/v3/projects/{project['id']}/users/{user['id']}/roles/{role['id']}")
    issued = await raw_clients["keystone"].post("/v3/auth/tokens", json={"auth": {
        "identity": {"methods": ["password"], "password": {"user": {
            "name": f"{name}-user", "domain": {"name": "Default"}, "password": "pw"}}},
        "scope": {"project": {"id": project["id"]}}}})
    assert "admin" not in [r["name"] for r in issued.json()["token"]["roles"]]
    return issued.headers["X-Subject-Token"]


async def test_security_groups_are_scoped_to_the_project(raw_clients, api) -> None:
    token = await _second_project(raw_clients, api)
    other = {"X-Auth-Token": token}

    await api["neutron"].post("/v2.0/security-groups",
                              json={"security_group": {"name": "admin-only"}})
    mine = await raw_clients["neutron"].post(
        "/v2.0/security-groups", json={"security_group": {"name": "tenant-b-sg"}},
        headers=other)
    assert mine.status_code == 201

    seen = (await raw_clients["neutron"].get("/v2.0/security-groups",
                                             headers=other)).json()["security_groups"]
    names = {g["name"] for g in seen}
    assert "tenant-b-sg" in names
    assert "admin-only" not in names, "another project's group must not be listed"


async def test_a_tenant_cannot_fetch_another_projects_group(raw_clients, api) -> None:
    token = await _second_project(raw_clients, api)
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "private"}})).json()
    group_id = group["security_group"]["id"]
    response = await raw_clients["neutron"].get(f"/v2.0/security-groups/{group_id}",
                                                headers={"X-Auth-Token": token})
    assert response.status_code == 404, "hidden behind a 404, as Neutron does"
    deleted = await raw_clients["neutron"].delete(f"/v2.0/security-groups/{group_id}",
                                                  headers={"X-Auth-Token": token})
    assert deleted.status_code == 404


async def test_ports_and_floating_ips_are_scoped(raw_clients, api) -> None:
    token = await _second_project(raw_clients, api)
    other = {"X-Auth-Token": token}
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    await api["neutron"].post("/v2.0/ports", json={"port": {"network_id": private["id"]}})
    await api["neutron"].post("/v2.0/floatingips",
                              json={"floatingip": {"floating_network_id": public["id"]}})

    assert (await raw_clients["neutron"].get("/v2.0/ports", headers=other)).json()["ports"] == []
    assert (await raw_clients["neutron"].get("/v2.0/floatingips",
                                             headers=other)).json()["floatingips"] == []


async def test_shared_and_external_networks_stay_visible(raw_clients, api) -> None:
    """A tenant with no networks of its own must still see the shared ones to boot on."""
    token = await _second_project(raw_clients, api)
    seen = (await raw_clients["neutron"].get(
        "/v2.0/networks", headers={"X-Auth-Token": token})).json()["networks"]
    assert {n["name"] for n in seen} == {"private", "public"}


async def test_admin_sees_every_project(raw_clients, api) -> None:
    token = await _second_project(raw_clients, api)
    await raw_clients["neutron"].post(
        "/v2.0/security-groups", json={"security_group": {"name": "tenant-b-sg"}},
        headers={"X-Auth-Token": token})
    seen = (await api["neutron"].get("/v2.0/security-groups")).json()["security_groups"]
    assert "tenant-b-sg" in {g["name"] for g in seen}


async def test_a_new_project_gets_its_own_default_group(raw_clients, api) -> None:
    """Neutron gives every project a `default` group; it appears on first use.

    A project created through the API used to have none, so a client that counts on the
    pair every real cloud shows -- `default` plus whatever it creates itself -- saw one.
    """
    token = await _second_project(raw_clients, api)
    other = {"X-Auth-Token": token}
    groups = (await raw_clients["neutron"].get("/v2.0/security-groups",
                                               headers=other)).json()["security_groups"]
    assert [g["name"] for g in groups] == ["default"]
    default = groups[0]
    assert len(default["security_group_rules"]) == 4, "2 egress + 2 ingress from itself"
    assert {r["direction"] for r in default["security_group_rules"]} == {"egress", "ingress"}

    again = (await raw_clients["neutron"].get("/v2.0/security-groups",
                                              headers=other)).json()["security_groups"]
    assert [g["id"] for g in again] == [default["id"]], "materialised once, not per call"


async def test_an_identical_rule_is_a_conflict(api, cloud) -> None:
    """Neutron answers a duplicate with 409 SecurityGroupRuleExists.

    Clients make rule setup idempotent by creating the rule and reading that 409 as
    "already there"; accepting the duplicate instead grew a fresh copy of the same rule
    on every run.
    """
    group = (await api["neutron"].post(
        "/v2.0/security-groups",
        json={"security_group": {"name": "dupes"}})).json()["security_group"]
    rule = {"security_group_id": group["id"], "direction": "ingress",
            "protocol": "icmp", "remote_ip_prefix": "172.26.8.0/23"}
    first = await api["neutron"].post("/v2.0/security-group-rules",
                                      json={"security_group_rule": rule})
    assert first.status_code == 201
    second = await api["neutron"].post("/v2.0/security-group-rules",
                                       json={"security_group_rule": rule})
    assert second.status_code == 409, second.text
    body = second.json()["NeutronError"]
    assert body["type"] == "SecurityGroupRuleExists"
    assert first.json()["security_group_rule"]["id"] in body["message"]

    # Same group, one field different: a rule in its own right, not a duplicate.
    other = await api["neutron"].post("/v2.0/security-group-rules", json={
        "security_group_rule": {**rule, "remote_ip_prefix": "10.0.0.0/8"}})
    assert other.status_code == 201


async def test_project_id_filter_narrows_the_listing(api, cloud) -> None:
    seen = (await api["neutron"].get(
        f"/v2.0/security-groups?project_id={cloud.project_id}")).json()["security_groups"]
    assert all(g["project_id"] == cloud.project_id for g in seen)
    assert (await api["neutron"].get(
        "/v2.0/security-groups?project_id=nobody")).json()["security_groups"] == []


# --------------------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------------------


async def test_router_crud(api) -> None:
    created = await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "r1"}})
    assert created.status_code == 201
    router = created.json()["router"]
    assert router["status"] == "ACTIVE"
    assert router["external_gateway_info"] is None, "no gateway until one is attached"

    assert (await api["neutron"].get(f"/v2.0/routers/{router['id']}")).status_code == 200
    listed = (await api["neutron"].get("/v2.0/routers?name=r1")).json()["routers"]
    assert [r["id"] for r in listed] == [router["id"]]

    renamed = await api["neutron"].put(f"/v2.0/routers/{router['id']}",
                                       json={"router": {"name": "r1-renamed"}})
    assert renamed.json()["router"]["name"] == "r1-renamed"

    assert (await api["neutron"].delete(f"/v2.0/routers/{router['id']}")).status_code == 204
    assert (await api["neutron"].get(f"/v2.0/routers/{router['id']}")).status_code == 404


async def test_external_gateway_set_and_unset(api) -> None:
    """`router show` must not mention network_id until a gateway is attached."""
    public = (await api["neutron"].get("/v2.0/networks?name=public")).json()["networks"][0]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "gw"}})).json()["router"]
    assert "network_id" not in json.dumps(router)

    updated = await api["neutron"].put(f"/v2.0/routers/{router['id']}", json={
        "router": {"external_gateway_info": {"network_id": public["id"]}}})
    gateway = updated.json()["router"]["external_gateway_info"]
    assert gateway["network_id"] == public["id"]
    assert gateway["enable_snat"] is True
    assert gateway["external_fixed_ips"][0]["ip_address"].startswith("172.24.4.")

    cleared = await api["neutron"].put(f"/v2.0/routers/{router['id']}",
                                       json={"router": {"external_gateway_info": None}})
    assert cleared.json()["router"]["external_gateway_info"] is None


async def test_gateway_must_be_an_external_network(api) -> None:
    private = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "bad-gw"}})).json()["router"]
    response = await api["neutron"].put(f"/v2.0/routers/{router['id']}", json={
        "router": {"external_gateway_info": {"network_id": private["id"]}}})
    assert response.status_code == 400


async def test_router_interface_is_a_router_owned_port(api) -> None:
    network = (await api["neutron"].post("/v2.0/networks",
                                         json={"network": {"name": "n"}})).json()["network"]
    subnet = (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"], "cidr": "10.44.0.0/24"}})).json()["subnet"]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "r"}})).json()["router"]

    added = await api["neutron"].put(f"/v2.0/routers/{router['id']}/add_router_interface",
                                     json={"subnet_id": subnet["id"]})
    assert added.status_code == 200
    assert added.json()["subnet_id"] == subnet["id"]

    # the interface exists as a port the router owns, holding the subnet's gateway IP
    ports = (await api["neutron"].get(
        f"/v2.0/ports?device_id={router['id']}")).json()["ports"]
    assert len(ports) == 1
    assert ports[0]["device_owner"] == "network:router_interface"
    assert ports[0]["fixed_ips"][0]["ip_address"] == subnet["gateway_ip"]

    shown = (await api["neutron"].get(f"/v2.0/routers/{router['id']}")).json()["router"]
    assert shown["interfaces_info"][0]["subnet_id"] == subnet["id"]

    removed = await api["neutron"].put(
        f"/v2.0/routers/{router['id']}/remove_router_interface",
        json={"subnet_id": subnet["id"]})
    assert removed.status_code == 200
    assert (await api["neutron"].get(
        f"/v2.0/ports?device_id={router['id']}")).json()["ports"] == []


async def test_adding_the_same_subnet_twice_is_a_self_overlap_400(api) -> None:
    """Neutron rejects a redundant add with a 400 the caller treats as idempotent."""
    network = (await api["neutron"].post("/v2.0/networks",
                                         json={"network": {"name": "n"}})).json()["network"]
    subnet = (await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"], "cidr": "10.45.0.0/24"}})).json()["subnet"]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "r"}})).json()["router"]
    await api["neutron"].put(f"/v2.0/routers/{router['id']}/add_router_interface",
                             json={"subnet_id": subnet["id"]})
    again = await api["neutron"].put(f"/v2.0/routers/{router['id']}/add_router_interface",
                                     json={"subnet_id": subnet["id"]})
    assert again.status_code == 400
    assert "overlaps with cidr" in again.json()["NeutronError"]["message"]


async def test_router_with_interfaces_cannot_be_deleted(api) -> None:
    network = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    subnet_id = network["subnets"][0]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "busy"}})).json()["router"]
    await api["neutron"].put(f"/v2.0/routers/{router['id']}/add_router_interface",
                             json={"subnet_id": subnet_id})
    assert (await api["neutron"].delete(f"/v2.0/routers/{router['id']}")).status_code == 409


async def test_routers_are_scoped_to_the_project(raw_clients, api) -> None:
    token = await _second_project(raw_clients, api)
    await api["neutron"].post("/v2.0/routers", json={"router": {"name": "admin-router"}})
    seen = (await raw_clients["neutron"].get(
        "/v2.0/routers", headers={"X-Auth-Token": token})).json()["routers"]
    assert seen == []


async def test_port_list_filters_by_fixed_ip_subnet(api) -> None:
    """`port list --fixed-ip subnet=<id>` is how the caller checks for an interface."""
    network = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"][0]
    subnet_id = network["subnets"][0]
    router = (await api["neutron"].post("/v2.0/routers",
                                        json={"router": {"name": "r"}})).json()["router"]
    await api["neutron"].put(f"/v2.0/routers/{router['id']}/add_router_interface",
                             json={"subnet_id": subnet_id})

    match = await api["neutron"].get(
        f"/v2.0/ports?device_id={router['id']}&fixed_ips=subnet_id%3D{subnet_id}")
    assert len(match.json()["ports"]) == 1
    miss = await api["neutron"].get(
        f"/v2.0/ports?device_id={router['id']}&fixed_ips=subnet_id%3Dnope")
    assert miss.json()["ports"] == []
