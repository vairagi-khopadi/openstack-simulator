"""Nova Compute v2.1 API tests."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _ids(api) -> tuple[dict[str, str], dict[str, str]]:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    return flavors, images


async def _boot(api, name="vm", flavor="m1.small", image="cirros", **extra) -> str:
    flavors, images = await _ids(api)
    body = {"server": {"name": name, "flavorRef": flavors[flavor],
                       "imageRef": images[image], **extra}}
    response = await api["nova"].post("/v2.1/servers", json=body)
    assert response.status_code == 202, response.text
    return response.json()["server"]["id"]


# -- discovery -------------------------------------------------------------------------


async def test_version_documents(raw_clients) -> None:
    root = (await raw_clients["nova"].get("/")).json()
    assert root["versions"][0]["id"] == "v2.1"
    detail = (await raw_clients["nova"].get("/v2.1")).json()["version"]
    assert detail["version"] == "2.79" and detail["min_version"] == "2.1"


async def test_microversion_headers_are_echoed(api) -> None:
    headers = (await api["nova"].get("/v2.1/flavors")).headers
    assert headers["openstack-api-version"] == "compute 2.79"
    assert headers["x-openstack-nova-api-version"] == "2.79"
    assert headers["vary"] == "OpenStack-API-Version"


# -- flavors ---------------------------------------------------------------------------


async def test_flavor_listing_and_detail(api) -> None:
    brief = (await api["nova"].get("/v2.1/flavors")).json()["flavors"]
    assert {f["name"] for f in brief} == {"m1.tiny", "m1.small", "m1.medium"}
    assert "links" in brief[0] and "vcpus" not in brief[0]

    detail = (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]
    medium = next(f for f in detail if f["name"] == "m1.medium")
    assert (medium["vcpus"], medium["ram"], medium["disk"]) == (2, 4096, 40)


async def test_flavor_lookup_by_id_and_name(api) -> None:
    assert (await api["nova"].get("/v2.1/flavors/1")).json()["flavor"]["name"] == "m1.tiny"
    assert (await api["nova"].get("/v2.1/flavors/m1.tiny")).json()["flavor"]["id"] == "1"
    assert (await api["nova"].get("/v2.1/flavors/nope")).status_code == 404


async def test_flavor_create_and_delete(api) -> None:
    created = await api["nova"].post("/v2.1/flavors", json={"flavor": {
        "name": "m1.custom", "vcpus": 8, "ram": 16384, "disk": 100, "id": "99"}})
    assert created.status_code == 200
    assert created.json()["flavor"]["vcpus"] == 8
    assert (await api["nova"].get("/v2.1/flavors/99/os-extra_specs")).json() == {"extra_specs": {}}
    assert (await api["nova"].delete("/v2.1/flavors/99")).status_code == 202
    assert (await api["nova"].get("/v2.1/flavors/99")).status_code == 404


# -- boot ------------------------------------------------------------------------------


async def test_boot_returns_202_with_admin_password(api) -> None:
    flavors, images = await _ids(api)
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "vm1", "flavorRef": flavors["m1.small"], "imageRef": images["cirros"]}})
    assert response.status_code == 202
    body = response.json()["server"]
    assert body["adminPass"] and body["links"]
    assert body["security_groups"] == [{"name": "default"}]


async def test_server_detail_shape(api, cloud) -> None:
    server_id = await _boot(api, name="detailed")
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["name"] == "detailed"
    assert body["status"] == "ACTIVE"
    assert body["tenant_id"] == cloud.project_id
    assert body["OS-EXT-SRV-ATTR:host"] == "node-01"
    assert body["OS-EXT-STS:vm_state"] == "active"
    assert body["OS-EXT-STS:power_state"] == 1
    assert body["OS-SRV-USG:launched_at"] is not None
    assert body["OS-EXT-AZ:availability_zone"] == "nova"
    # Microversion >= 2.47 embeds the flavor rather than linking to it.
    assert body["flavor"]["original_name"] == "m1.small"
    assert body["flavor"]["vcpus"] == 1


async def test_boot_allocates_a_port_on_the_tenant_network(api) -> None:
    server_id = await _boot(api, name="netted")
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    addresses = body["addresses"]["private"]
    assert addresses[0]["addr"].startswith("10.0.0.")
    assert addresses[0]["OS-EXT-IPS:type"] == "fixed"
    assert addresses[0]["OS-EXT-IPS-MAC:mac_addr"].startswith("fa:16:3e:")

    ports = (await api["neutron"].get(f"/v2.0/ports?device_id={server_id}")).json()["ports"]
    assert len(ports) == 1 and ports[0]["device_owner"] == "compute:nova"


async def test_boot_with_no_network(api) -> None:
    server_id = await _boot(api, name="isolated", networks="none")
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["addresses"] == {}


async def test_boot_onto_an_explicit_network(api) -> None:
    networks = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"]
    server_id = await _boot(api, name="explicit", networks=[{"uuid": networks[0]["id"]}])
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert "private" in body["addresses"]


async def test_boot_onto_a_pre_created_port(api) -> None:
    networks = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"]
    port = (await api["neutron"].post("/v2.0/ports", json={
        "port": {"network_id": networks[0]["id"], "name": "preallocated"}})).json()["port"]
    server_id = await _boot(api, name="ported", networks=[{"port": port["id"]}])
    refreshed = (await api["neutron"].get(f"/v2.0/ports/{port['id']}")).json()["port"]
    assert refreshed["device_id"] == server_id
    assert refreshed["status"] == "ACTIVE"


async def test_boot_rejects_a_missing_port(api) -> None:
    flavors, images = await _ids(api)
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "bad", "flavorRef": flavors["m1.tiny"], "imageRef": images["cirros"],
        "networks": [{"port": "nope"}]}})
    assert response.status_code == 400


async def test_boot_requires_an_image(api) -> None:
    flavors, _ = await _ids(api)
    response = await api["nova"].post("/v2.1/servers",
                                      json={"server": {"name": "x",
                                                       "flavorRef": flavors["m1.tiny"]}})
    assert response.status_code == 400
    assert "imageRef" in response.json()["badRequest"]["message"]


async def test_boot_requires_a_flavor(api) -> None:
    _, images = await _ids(api)
    response = await api["nova"].post("/v2.1/servers",
                                      json={"server": {"name": "x",
                                                       "imageRef": images["cirros"]}})
    assert response.status_code == 400


async def test_flavor_must_satisfy_the_image_minimums(api) -> None:
    flavors, images = await _ids(api)
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "toosmall", "flavorRef": flavors["m1.tiny"],
        "imageRef": images["ubuntu-24.04"]}})
    assert response.status_code == 400
    assert "memory is too small" in response.json()["badRequest"]["message"]


async def test_metadata_tags_and_userdata_round_trip(api) -> None:
    server_id = await _boot(api, name="meta", metadata={"role": "web"},
                            tags=["prod"], user_data="IyEvYmluL3No",
                            key_name=None, config_drive=True)
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["metadata"] == {"role": "web"}
    assert body["tags"] == ["prod"]
    assert body["config_drive"] is True


# -- depletion -------------------------------------------------------------------------


async def test_boot_deducts_the_full_footprint(api) -> None:
    before = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    await _boot(api, flavor="m1.medium")
    after = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert after["vcpus_used"] - before["vcpus_used"] == 2
    assert after["memory_mb_used"] - before["memory_mb_used"] == 4096 + 256
    assert after["local_gb_used"] - before["local_gb_used"] == 40
    assert after["running_vms"] == 1


async def test_node_refuses_to_overcommit_ram(api) -> None:
    flavors, images = await _ids(api)
    created = 0
    for index in range(80):
        response = await api["nova"].post("/v2.1/servers", json={"server": {
            "name": f"fill-{index}", "flavorRef": flavors["m1.medium"],
            "imageRef": images["cirros"], "networks": "none"}})
        if response.status_code != 202:
            break
        created += 1
    assert response.status_code == 403
    assert "ram" in response.json()["forbidden"]["message"]
    assert 55 <= created <= 62, "256 GB of RAM at 4.25 GB per VM"

    usage = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert usage["memory_mb_used"] <= 261632
    assert usage["vcpus_used"] < 192, "vCPU still has headroom under 3x overcommit"


async def test_delete_releases_the_booking(api) -> None:
    server_id = await _boot(api, flavor="m1.medium")
    before = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert (await api["nova"].delete(f"/v2.1/servers/{server_id}")).status_code == 204
    after = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert before["vcpus_used"] - after["vcpus_used"] == 2
    assert before["memory_mb_used"] - after["memory_mb_used"] == 4352
    assert before["local_gb_used"] - after["local_gb_used"] == 40
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).status_code == 404


async def test_delete_removes_the_ports(api) -> None:
    server_id = await _boot(api)
    await api["nova"].delete(f"/v2.1/servers/{server_id}")
    ports = (await api["neutron"].get(f"/v2.0/ports?device_id={server_id}")).json()["ports"]
    assert ports == []


# -- state machine ---------------------------------------------------------------------


async def test_stop_keeps_the_booking_start_restores_it(api) -> None:
    server_id = await _boot(api, flavor="m1.medium")
    before = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]

    assert (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"os-stop": None})).status_code == 202
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "SHUTOFF" and body["OS-EXT-STS:power_state"] == 4

    after = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert after["vcpus_used"] == before["vcpus_used"], "a stopped VM still holds its cores"
    assert after["memory_mb_used"] == before["memory_mb_used"]

    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"os-start": None})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"


async def test_start_and_stop_reject_the_wrong_state(api) -> None:
    server_id = await _boot(api)
    conflict = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"os-start": None})
    assert conflict.status_code == 409
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"os-stop": None})
    conflict = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"os-stop": None})
    assert conflict.status_code == 409


async def test_shelve_offload_frees_compute_but_keeps_disk(api) -> None:
    server_id = await _boot(api, flavor="m1.medium")
    before = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]

    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"shelveOffload": None})
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "SHELVED_OFFLOADED"

    after = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert before["vcpus_used"] - after["vcpus_used"] == 2
    assert before["memory_mb_used"] - after["memory_mb_used"] == 4352
    assert after["local_gb_used"] == before["local_gb_used"], "storage stays booked"


async def test_unshelve_rebooks_capacity(api) -> None:
    server_id = await _boot(api, flavor="m1.medium")
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"shelveOffload": None})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"unshelve": None})
    usage = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert usage["vcpus_used"] == 2 and usage["memory_mb_used"] == 4352


@pytest.mark.parametrize(
    "action, expected",
    [("pause", "PAUSED"), ("suspend", "SUSPENDED"), ("shelve", "SHELVED")],
)
async def test_pause_suspend_shelve(api, action, expected) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={action: None})
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == expected


async def test_unpause_and_resume_return_to_active(api) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"pause": None})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"unpause": None})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"suspend": None})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"resume": None})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"


async def test_lock_blocks_deletion(api) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"lock": None})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["locked"] is True
    assert (await api["nova"].delete(f"/v2.1/servers/{server_id}")).status_code == 409
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"unlock": None})
    assert (await api["nova"].delete(f"/v2.1/servers/{server_id}")).status_code == 204


async def test_resize_rebooks_the_new_flavor(api) -> None:
    server_id = await _boot(api, flavor="m1.tiny")
    flavors, _ = await _ids(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                           json={"resize": {"flavorRef": flavors["m1.medium"]}})
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "VERIFY_RESIZE"
    assert body["flavor"]["original_name"] == "m1.medium"
    usage = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert usage["vcpus_used"] == 2 and usage["memory_mb_used"] == 4352

    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"confirmResize": None})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"


async def test_create_image_snapshots_into_glance(api) -> None:
    server_id = await _boot(api)
    response = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"createImage": {"name": "snap-1"}})
    assert response.status_code == 202
    assert "/v2/images/" in response.headers["Location"]
    images = (await api["glance"].get("/v2/images?name=snap-1")).json()["images"]
    assert images[0]["properties"] if "properties" in images[0] else True
    assert images[0]["status"] == "active"
    assert images[0]["instance_uuid"] == server_id


async def test_reset_state(api) -> None:
    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                           json={"os-resetState": {"state": "error"}})
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ERROR"


async def test_unknown_and_empty_actions_are_rejected(api) -> None:
    server_id = await _boot(api)
    assert (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"warpDrive": {}})).status_code == 400
    assert (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={})).status_code == 400


# -- telemetry -------------------------------------------------------------------------


async def test_diagnostics_endpoint(api) -> None:
    server_id = await _boot(api, flavor="m1.medium")
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}/diagnostics")).json()
    assert body["driver"] == "libvirt"
    assert len(body["cpu_details"]) == 2
    assert body["memory_details"]["maximum"] == 4096 * 1024


async def test_console_output_action(api) -> None:
    server_id = await _boot(api, name="console_vm", key_name=None)
    body = (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"os-getConsoleOutput": {"length": 0}})).json()
    assert "cloud-init" in body["output"]
    assert "console_vm".replace("_", "-") in body["output"]

    tail = (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"os-getConsoleOutput": {"length": 3}})).json()
    assert len(tail["output"].strip().splitlines()) <= 3


async def test_interfaces_and_ips_endpoints(api) -> None:
    server_id = await _boot(api)
    interfaces = (await api["nova"].get(f"/v2.1/servers/{server_id}/os-interface")).json()
    assert interfaces["interfaceAttachments"][0]["net_id"]
    addresses = (await api["nova"].get(f"/v2.1/servers/{server_id}/ips")).json()["addresses"]
    assert "private" in addresses


# -- volumes, keypairs, listings --------------------------------------------------------


async def test_volume_attach_detach_cycle(api, cloud) -> None:
    server_id = await _boot(api)
    volume = (await api["cinder"].post(f"/v3/{cloud.project_id}/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]

    attached = await api["nova"].post(f"/v2.1/servers/{server_id}/os-volume_attachments",
                                      json={"volumeAttachment": {"volumeId": volume["id"]}})
    assert attached.status_code == 200
    assert attached.json()["volumeAttachment"]["device"] == "/dev/vdb"

    second = (await api["cinder"].post(f"/v3/{cloud.project_id}/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]
    again = await api["nova"].post(f"/v2.1/servers/{server_id}/os-volume_attachments",
                                   json={"volumeAttachment": {"volumeId": second["id"]}})
    assert again.json()["volumeAttachment"]["device"] == "/dev/vdc"

    listed = (await api["nova"].get(f"/v2.1/servers/{server_id}/os-volume_attachments")).json()
    assert len(listed["volumeAttachments"]) == 2

    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert len(body["os-extended-volumes:volumes_attached"]) == 2

    detached = await api["nova"].delete(
        f"/v2.1/servers/{server_id}/os-volume_attachments/{volume['id']}")
    assert detached.status_code == 202
    refreshed = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert refreshed["status"] == "available"


async def test_attaching_an_unknown_volume_is_a_404(api) -> None:
    server_id = await _boot(api)
    response = await api["nova"].post(f"/v2.1/servers/{server_id}/os-volume_attachments",
                                      json={"volumeAttachment": {"volumeId": "nope"}})
    assert response.status_code == 404


async def test_deleting_a_server_frees_its_volumes(api, cloud) -> None:
    server_id = await _boot(api)
    volume = (await api["cinder"].post(f"/v3/{cloud.project_id}/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]
    await api["nova"].post(f"/v2.1/servers/{server_id}/os-volume_attachments",
                           json={"volumeAttachment": {"volumeId": volume["id"]}})
    await api["nova"].delete(f"/v2.1/servers/{server_id}")
    refreshed = (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]
    assert refreshed["status"] == "available"


async def test_keypair_import_and_lookup(api) -> None:
    public = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQ test@example"
    created = await api["nova"].post("/v2.1/os-keypairs",
                                     json={"keypair": {"name": "imported",
                                                       "public_key": public}})
    assert created.status_code == 201
    body = created.json()["keypair"]
    assert body["public_key"] == public
    assert len(body["fingerprint"].split(":")) == 16
    assert "private_key" not in body, "nothing to hand back when the key was supplied"

    assert (await api["nova"].get("/v2.1/os-keypairs/imported")).status_code == 200
    assert len((await api["nova"].get("/v2.1/os-keypairs")).json()["keypairs"]) == 1
    assert (await api["nova"].delete("/v2.1/os-keypairs/imported")).status_code == 202
    assert (await api["nova"].get("/v2.1/os-keypairs/imported")).status_code == 404


async def test_generated_keypair_returns_private_material(api) -> None:
    created = await api["nova"].post("/v2.1/os-keypairs",
                                     json={"keypair": {"name": "generated"}})
    body = created.json()["keypair"]
    assert body["public_key"].startswith("ssh-rsa ")
    assert "BEGIN RSA PRIVATE KEY" in body["private_key"]


async def test_duplicate_keypair_conflicts(api) -> None:
    await api["nova"].post("/v2.1/os-keypairs", json={"keypair": {"name": "dup"}})
    again = await api["nova"].post("/v2.1/os-keypairs", json={"keypair": {"name": "dup"}})
    assert again.status_code == 409


async def test_server_listing_and_filters(api) -> None:
    await _boot(api, name="web-1")
    await _boot(api, name="db-1")
    brief = (await api["nova"].get("/v2.1/servers")).json()["servers"]
    assert len(brief) == 2 and "status" not in brief[0]

    detail = (await api["nova"].get("/v2.1/servers/detail")).json()["servers"]
    assert {s["name"] for s in detail} == {"web-1", "db-1"}

    filtered = (await api["nova"].get("/v2.1/servers/detail?name=web")).json()["servers"]
    assert [s["name"] for s in filtered] == ["web-1"]

    by_status = (await api["nova"].get("/v2.1/servers/detail?status=ACTIVE")).json()["servers"]
    assert len(by_status) == 2


async def test_server_rename(api) -> None:
    server_id = await _boot(api, name="before")
    renamed = await api["nova"].put(f"/v2.1/servers/{server_id}",
                                    json={"server": {"name": "after"}})
    assert renamed.json()["server"]["name"] == "after"


async def test_unknown_server_is_a_404(api) -> None:
    for path in ("", "/diagnostics", "/ips", "/os-interface", "/os-volume_attachments"):
        assert (await api["nova"].get(f"/v2.1/servers/missing{path}")).status_code == 404


# -- host introspection ----------------------------------------------------------------


async def test_hypervisor_endpoints(api, cloud) -> None:
    brief = (await api["nova"].get("/v2.1/os-hypervisors")).json()["hypervisors"]
    assert brief[0]["hypervisor_hostname"] == "node-01"
    assert "vcpus" not in brief[0]

    detail = (await api["nova"].get("/v2.1/os-hypervisors/detail")).json()["hypervisors"][0]
    assert detail["vcpus"] == 64 and detail["memory_mb"] == 262144
    assert detail["cpu_info"]["topology"]["sockets"] == 2

    single = (await api["nova"].get(f"/v2.1/os-hypervisors/{cloud.host_id}")).json()
    assert single["hypervisor"]["hypervisor_hostname"] == "node-01"


async def test_limits_report_the_quota_not_the_node(api, cloud) -> None:
    """`/limits` answers "how many more may I boot?", so it has to be the quota.

    This is the endpoint most clients size themselves against, and nothing else. It used
    to report the host envelope -- 192 cores, 256 GB -- which is a different question and,
    for any project with a quota, the wrong answer.
    """
    await _boot(api, flavor="m1.medium")

    # The seeded admin project is unlimited, and -1 is what real Nova reports for that.
    absolute = (await api["nova"].get("/v2.1/limits")).json()["limits"]["absolute"]
    assert absolute["maxTotalInstances"] == -1
    assert absolute["maxTotalCores"] == -1 and absolute["maxTotalRAMSize"] == -1

    # Usage is the quota's, which charges the flavor -- not capacity's, which adds the
    # 256 MB QEMU overhead per VM on top. 4096 here, 4352 if this ever reads capacity.
    assert absolute["totalInstancesUsed"] == 1
    assert absolute["totalCoresUsed"] == 2
    assert absolute["totalRAMUsed"] == 4096

    # Set a quota and the ceilings follow it, which is the whole point.
    await api["nova"].put(
        f"/v2.1/os-quota-sets/{cloud.project_id}",
        json={"quota_set": {"instances": 4, "cores": 8, "ram": 16384}},
    )
    absolute = (await api["nova"].get("/v2.1/limits")).json()["limits"]["absolute"]
    assert absolute["maxTotalInstances"] == 4
    assert absolute["maxTotalCores"] == 8
    assert absolute["maxTotalRAMSize"] == 16384
    assert absolute["totalCoresUsed"] == 2  # unchanged; the boot above still counts


async def test_limits_network_fields_come_from_neutrons_quota(api, cloud) -> None:
    """Nova proxied these from Neutron until 2.36, so they have to be read from it.

    They were hardcoded literals copied from the defaults, which agreed with Neutron
    until the day someone edited `NEUTRON_DEFAULTS` and then silently did not.
    """
    await api["neutron"].put(
        f"/v2.0/quotas/{cloud.project_id}", json={"quota": {"floatingip": 7}}
    )
    headers = {"OpenStack-API-Version": "compute 2.35"}
    absolute = (await api["nova"].get(
        "/v2.1/limits", headers=headers)).json()["limits"]["absolute"]
    assert absolute["maxTotalFloatingIps"] == 7

    # 2.36 removed them; reporting them past that is how code reads a key real Nova
    # will not send.
    headers = {"OpenStack-API-Version": "compute 2.36"}
    absolute = (await api["nova"].get(
        "/v2.1/limits", headers=headers)).json()["limits"]["absolute"]
    assert "maxTotalFloatingIps" not in absolute
    assert "maxSecurityGroups" not in absolute


async def test_availability_zones_and_services(api) -> None:
    zones = (await api["nova"].get("/v2.1/os-availability-zone")).json()["availabilityZoneInfo"]
    assert zones[0]["zoneName"] == "nova"
    assert "node-01" in zones[0]["hosts"]
    services = (await api["nova"].get("/v2.1/os-services")).json()["services"]
    assert {s["binary"] for s in services} == {"nova-conductor", "nova-scheduler", "nova-compute"}
    assert all(s["state"] == "up" for s in services)


async def test_simple_tenant_usage(api, cloud) -> None:
    await _boot(api, flavor="m1.medium")
    usage = (await api["nova"].get("/v2.1/os-simple-tenant-usage")).json()["tenant_usages"][0]
    assert usage["tenant_id"] == cloud.project_id
    assert usage["total_hours"] >= 0


async def test_boot_reports_address_exhaustion_as_a_nova_error(api) -> None:
    """A Neutron-layer failure must surface in Nova's own error dialect, not a 500."""
    network = (await api["neutron"].post("/v2.0/networks",
                                         json={"network": {"name": "tiny"}})).json()["network"]
    await api["neutron"].post("/v2.0/subnets", json={
        "subnet": {"network_id": network["id"], "cidr": "10.99.0.0/30"}})  # one usable IP
    flavors, images = await _ids(api)
    body = {"server": {"name": "vm", "flavorRef": flavors["m1.tiny"],
                       "imageRef": images["cirros"], "networks": [{"uuid": network["id"]}]}}
    assert (await api["nova"].post("/v2.1/servers", json=body)).status_code == 202
    second = await api["nova"].post("/v2.1/servers", json=body)
    assert second.status_code == 400
    assert "badRequest" in second.json(), "Nova speaks its own error format"
    assert "fixed IP" in second.json()["badRequest"]["message"]


async def test_security_group_attach_and_detach(api) -> None:
    server_id = await _boot(api, name="sg-vm")
    group = (await api["neutron"].post("/v2.0/security-groups",
                                       json={"security_group": {"name": "web"}})).json()
    group_id = group["security_group"]["id"]

    added = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"addSecurityGroup": {"name": "web"}})
    assert added.status_code == 202

    listed = (await api["nova"].get(f"/v2.1/servers/{server_id}/os-security-groups")).json()
    # the instance keeps its default group and gains the new one
    assert {g["name"] for g in listed["security_groups"]} == {"default", "web"}
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert {g["name"] for g in body["security_groups"]} == {"default", "web"}

    # the group is pushed down onto the instance's ports, as Nova does
    ports = (await api["neutron"].get(f"/v2.0/ports?device_id={server_id}")).json()["ports"]
    assert group_id in ports[0]["security_groups"]

    removed = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                     json={"removeSecurityGroup": {"name": "web"}})
    assert removed.status_code == 202
    ports = (await api["neutron"].get(f"/v2.0/ports?device_id={server_id}")).json()["ports"]
    assert group_id not in ports[0]["security_groups"]


async def test_attaching_an_unknown_group_is_a_404(api) -> None:
    server_id = await _boot(api)
    response = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"addSecurityGroup": {"name": "ghost"}})
    assert response.status_code == 404


async def test_detaching_a_group_that_is_not_attached_is_a_400(api) -> None:
    server_id = await _boot(api)
    await api["neutron"].post("/v2.0/security-groups",
                              json={"security_group": {"name": "unused"}})
    response = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"removeSecurityGroup": {"name": "unused"}})
    assert response.status_code == 400


# --------------------------------------------------------------------------------------
# Rebuild, metadata, quotas, per-project usage
# --------------------------------------------------------------------------------------


async def test_rebuild_reimages_in_place(api) -> None:
    server_id = await _boot(api, name="rebuildable", flavor="m1.medium", image="cirros")
    _, images = await _ids(api)
    before = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]

    response = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                      json={"rebuild": {"imageRef": images["ubuntu-24.04"]}})
    assert response.status_code == 202
    assert response.json()["server"]["id"] == server_id, "rebuild returns the server body"

    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["image"]["id"] == images["ubuntu-24.04"]

    after = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert after["vcpus_used"] == before["vcpus_used"], "a rebuild does not re-book capacity"
    assert after["memory_mb_used"] == before["memory_mb_used"]


async def test_rebuild_validates_its_image(api) -> None:
    server_id = await _boot(api, flavor="m1.tiny")
    _, images = await _ids(api)
    assert (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"rebuild": {}})).status_code == 400
    assert (await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                   json={"rebuild": {"imageRef": "ghost"}})).status_code == 400
    # m1.tiny cannot satisfy ubuntu's 2048 MB minimum
    too_big = await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                                     json={"rebuild": {"imageRef": images["ubuntu-24.04"]}})
    assert too_big.status_code == 400


async def test_server_metadata_endpoints(api) -> None:
    server_id = await _boot(api, metadata={"role": "web"})
    assert (await api["nova"].get(
        f"/v2.1/servers/{server_id}/metadata")).json()["metadata"] == {"role": "web"}

    # POST merges (this is what `server set --property` uses)
    merged = await api["nova"].post(f"/v2.1/servers/{server_id}/metadata",
                                    json={"metadata": {"tier": "gold"}})
    assert merged.json()["metadata"] == {"role": "web", "tier": "gold"}

    # PUT replaces wholesale
    replaced = await api["nova"].put(f"/v2.1/servers/{server_id}/metadata",
                                     json={"metadata": {"only": "this"}})
    assert replaced.json()["metadata"] == {"only": "this"}

    single = await api["nova"].put(f"/v2.1/servers/{server_id}/metadata/env",
                                   json={"meta": {"env": "prod"}})
    assert single.json() == {"meta": {"env": "prod"}}
    assert (await api["nova"].get(
        f"/v2.1/servers/{server_id}/metadata/env")).json() == {"meta": {"env": "prod"}}

    assert (await api["nova"].delete(
        f"/v2.1/servers/{server_id}/metadata/env")).status_code == 204
    assert (await api["nova"].get(
        f"/v2.1/servers/{server_id}/metadata/env")).status_code == 404
    assert (await api["nova"].delete(
        f"/v2.1/servers/{server_id}/metadata/env")).status_code == 404

    # metadata shows up on the server itself
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["metadata"] == {"only": "this"}


async def test_quota_set_endpoint(api, cloud) -> None:
    """The seeded admin project is unlimited, so the node's capacity is what binds."""
    await _boot(api, flavor="m1.medium")
    quota = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}")).json()["quota_set"]
    assert quota["id"] == cloud.project_id
    assert quota["cores"] == -1
    assert quota["ram"] == -1

    # Usage is counted for real whether or not a limit binds on it.
    with_usage = (await api["nova"].get(
        f"/v2.1/os-quota-sets/{cloud.project_id}?usage=True")).json()["quota_set"]
    assert with_usage["cores"] == {"limit": -1, "in_use": 2, "reserved": 0}
    assert with_usage["ram"]["in_use"] == 4096
    assert with_usage["instances"]["in_use"] == 1


async def test_per_project_tenant_usage(api, cloud) -> None:
    server_id = await _boot(api, name="billed", flavor="m1.medium")
    usage = (await api["nova"].get(
        f"/v2.1/os-simple-tenant-usage/{cloud.project_id}")).json()["tenant_usage"]
    assert usage["tenant_id"] == cloud.project_id
    assert len(usage["server_usages"]) == 1
    entry = usage["server_usages"][0]
    assert entry["instance_id"] == server_id
    assert entry["name"] == "billed"
    assert entry["vcpus"] == 2 and entry["memory_mb"] == 4096
    assert usage["start"] and usage["stop"]

    empty = (await api["nova"].get(
        "/v2.1/os-simple-tenant-usage/nobody")).json()["tenant_usage"]
    assert empty["server_usages"] == []
