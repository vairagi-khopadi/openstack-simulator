"""The stateless polling state machine.

Deadlines are rewound rather than waited out, so the whole file runs in milliseconds
while still exercising the real pending -> ready path.
"""
from __future__ import annotations

import pytest

from app.core.config import (
    Settings,
    now_utc,
    settings,
    transition_deadline,
    transition_done,
    volume_provision_deadline,
)
from app.models.compute import Server
from app.models.loadbalancer import LoadBalancer, Listener, Member, Pool
from app.models.storage import Snapshot, Volume

pytestmark = pytest.mark.anyio

LBAAS = "/v2/lbaas"


async def _boot(api, **extra) -> str:
    flavors = {f["name"]: f["id"]
               for f in (await api["nova"].get("/v2.1/flavors/detail")).json()["flavors"]}
    images = {i["name"]: i["id"]
              for i in (await api["glance"].get("/v2/images")).json()["images"]}
    response = await api["nova"].post("/v2.1/servers", json={"server": {
        "name": "pending", "flavorRef": flavors["m1.small"], "imageRef": images["cirros"],
        "networks": "none", **extra}})
    return response.json()["server"]["id"]


# -- the delay itself ------------------------------------------------------------------


async def test_deadline_lands_inside_the_configured_window() -> None:
    settings.transition_min_seconds = 10
    settings.transition_max_seconds = 60
    try:
        # The deadline is "some instant inside the call, plus the random delay", so
        # bracket the call: the widest reading bounds the delay from below, the
        # narrowest from above. That is exact regardless of how slow the call is.
        widest, narrowest = [], []
        for _ in range(200):
            before = now_utc()
            deadline = transition_deadline()
            after = now_utc()
            widest.append((deadline - before).total_seconds())
            narrowest.append((deadline - after).total_seconds())
        assert min(widest) >= 10, f"delay dipped below the floor: {min(widest)}"
        assert max(narrowest) <= 60, f"delay exceeded the ceiling: {max(narrowest)}"
        assert min(widest) < 20 and max(narrowest) > 50, "the full window should be used"
        assert len(set(round(d) for d in widest)) > 5, "the delay must be randomised"
    finally:
        settings.transition_min_seconds = 0
        settings.transition_max_seconds = 0


async def test_transition_done_compares_against_now() -> None:
    from datetime import timedelta
    assert transition_done(None) is True
    assert transition_done(now_utc() - timedelta(seconds=1)) is True
    assert transition_done(now_utc() + timedelta(seconds=30)) is False


# -- Nova ------------------------------------------------------------------------------


async def test_server_reads_build_until_the_deadline(api, slow_transitions, expire) -> None:
    server_id = await _boot(api)
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "BUILD"
    assert body["OS-EXT-STS:vm_state"] == "building"
    assert body["OS-EXT-STS:power_state"] == 0
    assert body["OS-EXT-STS:task_state"] == "spawning"
    assert body["OS-SRV-USG:launched_at"] is None

    await expire(Server, server_id)
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "ACTIVE"
    assert body["OS-EXT-STS:vm_state"] == "active"
    assert body["OS-EXT-STS:power_state"] == 1
    assert body["OS-EXT-STS:task_state"] is None
    assert body["OS-SRV-USG:launched_at"] is not None


async def test_the_flip_is_persisted_not_recomputed(api, slow_transitions, expire) -> None:
    server_id = await _boot(api)
    await expire(Server, server_id)
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"
    # A second read must be stable, and the stored deadline must have been cleared.
    again = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert again["status"] == "ACTIVE"
    from app.core.database import SessionLocal
    async with SessionLocal() as session:
        stored = await session.get(Server, server_id)
    assert stored.transition_until is None and stored.transition_target is None


async def test_a_building_server_still_holds_its_capacity(api, slow_transitions) -> None:
    await _boot(api)
    stats = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert stats["vcpus_used"] == 1, "capacity is booked at request time, not at ACTIVE"
    assert stats["memory_mb_used"] == 2048 + 256


async def test_listing_resolves_pending_servers_too(api, slow_transitions, expire) -> None:
    server_id = await _boot(api)
    listed = (await api["nova"].get("/v2.1/servers/detail")).json()["servers"]
    assert listed[0]["status"] == "BUILD"
    await expire(Server, server_id)
    listed = (await api["nova"].get("/v2.1/servers/detail")).json()["servers"]
    assert listed[0]["status"] == "ACTIVE"


async def test_reboot_opens_a_new_window(api, slow_transitions, expire) -> None:
    server_id = await _boot(api)
    await expire(Server, server_id)
    await api["nova"].get(f"/v2.1/servers/{server_id}")

    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"reboot": {"type": "SOFT"}})
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["OS-EXT-STS:task_state"] == "rebooting"
    await expire(Server, server_id)
    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "ACTIVE" and body["OS-EXT-STS:task_state"] is None


async def test_unshelve_goes_back_through_build(api, slow_transitions, expire) -> None:
    server_id = await _boot(api)
    await expire(Server, server_id)
    await api["nova"].get(f"/v2.1/servers/{server_id}")
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"shelveOffload": None})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"unshelve": None})

    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "BUILD"
    await expire(Server, server_id)
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"


# -- Cinder ----------------------------------------------------------------------------


async def test_volume_reads_creating_until_the_deadline(api, slow_transitions, expire) -> None:
    volume = (await api["cinder"].post("/v3/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "creating"
    await expire(Volume, volume["id"])
    assert (await api["cinder"].get(f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "available"


async def test_a_volume_provisions_on_its_own_short_window() -> None:
    """The shipped defaults: no wait for a volume, up to a minute for an instance.

    A client that attaches a volume it has just created -- the usual sequence -- depends
    on Cinder clearing "creating" before its next call lands, and one second is already
    enough to lose that race against a CLI round trip. Putting a volume back on the
    instance window is what this guards against.
    """
    shipped = Settings()
    assert shipped.volume_provision_max_seconds == 0, (
        "a volume must be attachable within a client's next call, not a minute later"
    )
    assert shipped.transition_max_seconds >= 10, "instances still build slowly"

    settings.volume_provision_min_seconds = 0
    settings.volume_provision_max_seconds = 1
    settings.transition_min_seconds = 60
    settings.transition_max_seconds = 60
    try:
        assert (volume_provision_deadline() - now_utc()).total_seconds() <= 1
    finally:
        settings.volume_provision_min_seconds = 0
        settings.volume_provision_max_seconds = 0
        settings.transition_min_seconds = 0
        settings.transition_max_seconds = 0


async def test_a_new_volume_attaches_while_instances_still_build(api, expire) -> None:
    """`volume create` then `server add volume`, the sequence every client writes.

    The instance window stays long here: what makes the attach work is the volume's own
    window. While the two were shared, Nova refused the attach with "status must be
    available, currently creating", and a client that rolls a half-finished provision
    back answers that by deleting the instance it had just booted.
    """
    settings.transition_min_seconds = 60
    settings.transition_max_seconds = 60
    try:
        server_id = await _boot(api)
        await expire(Server, server_id)
        await api["nova"].get(f"/v2.1/servers/{server_id}")

        volume = (await api["cinder"].post("/v3/volumes",
                                           json={"volume": {"size": 5}})).json()["volume"]
        attached = await api["nova"].post(
            f"/v2.1/servers/{server_id}/os-volume_attachments",
            json={"volumeAttachment": {"volumeId": volume["id"]}},
        )
        assert attached.status_code == 200, attached.text
    finally:
        settings.transition_min_seconds = 0
        settings.transition_max_seconds = 0


async def test_a_creating_volume_already_consumes_disk(api, slow_transitions) -> None:
    await api["cinder"].post("/v3/volumes", json={"volume": {"size": 300}})
    stats = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert stats["local_gb_used"] == 300


async def test_snapshot_transitions(api, slow_transitions, expire) -> None:
    volume = (await api["cinder"].post("/v3/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]
    await expire(Volume, volume["id"])
    await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    snapshot = (await api["cinder"].post("/v3/snapshots", json={
        "snapshot": {"volume_id": volume["id"]}})).json()["snapshot"]
    assert (await api["cinder"].get(
        f"/v3/snapshots/{snapshot['id']}")).json()["snapshot"]["status"] == "creating"
    await expire(Snapshot, snapshot["id"])
    assert (await api["cinder"].get(
        f"/v3/snapshots/{snapshot['id']}")).json()["snapshot"]["status"] == "available"


async def test_extend_reopens_the_window(api, slow_transitions, expire) -> None:
    volume = (await api["cinder"].post("/v3/volumes",
                                       json={"volume": {"size": 5}})).json()["volume"]
    await expire(Volume, volume["id"])
    await api["cinder"].get(f"/v3/volumes/{volume['id']}")
    await api["cinder"].post(f"/v3/volumes/{volume['id']}/action",
                             json={"os-extend": {"new_size": 20}})
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "extending"
    await expire(Volume, volume["id"])
    assert (await api["cinder"].get(
        f"/v3/volumes/{volume['id']}")).json()["volume"]["status"] == "available"


# -- Octavia ---------------------------------------------------------------------------


async def _lb(api) -> dict:
    networks = (await api["neutron"].get("/v2.0/networks?name=private")).json()["networks"]
    return (await api["octavia"].post(f"{LBAAS}/loadbalancers", json={
        "loadbalancer": {"name": "lb", "vip_subnet_id": networks[0]["subnets"][0]}})).json()["loadbalancer"]


async def test_loadbalancer_pending_create_to_active(api, slow_transitions, expire) -> None:
    lb = await _lb(api)
    body = (await api["octavia"].get(f"{LBAAS}/loadbalancers/{lb['id']}")).json()["loadbalancer"]
    assert body["provisioning_status"] == "PENDING_CREATE"
    assert body["operating_status"] == "OFFLINE"

    await expire(LoadBalancer, lb["id"])
    body = (await api["octavia"].get(f"{LBAAS}/loadbalancers/{lb['id']}")).json()["loadbalancer"]
    assert body["provisioning_status"] == "ACTIVE"
    assert body["operating_status"] == "ONLINE"


async def test_listener_and_pool_transition(api, slow_transitions, expire) -> None:
    lb = await _lb(api)
    listener = (await api["octavia"].post(f"{LBAAS}/listeners", json={
        "listener": {"loadbalancer_id": lb["id"], "protocol": "HTTP",
                     "protocol_port": 80}})).json()["listener"]
    pool = (await api["octavia"].post(f"{LBAAS}/pools", json={
        "pool": {"listener_id": listener["id"], "protocol": "HTTP",
                 "lb_algorithm": "ROUND_ROBIN"}})).json()["pool"]

    assert (await api["octavia"].get(
        f"{LBAAS}/listeners/{listener['id']}")).json()["listener"]["provisioning_status"] == "PENDING_CREATE"
    await expire(Listener, listener["id"])
    await expire(Pool, pool["id"])
    assert (await api["octavia"].get(
        f"{LBAAS}/listeners/{listener['id']}")).json()["listener"]["operating_status"] == "ONLINE"
    assert (await api["octavia"].get(
        f"{LBAAS}/pools/{pool['id']}")).json()["pool"]["provisioning_status"] == "ACTIVE"


async def test_member_settles_to_no_monitor(api, slow_transitions, expire) -> None:
    """An ACTIVE member with no health monitor reports NO_MONITOR, as Octavia does."""
    lb = await _lb(api)
    pool = (await api["octavia"].post(f"{LBAAS}/pools", json={
        "pool": {"loadbalancer_id": lb["id"], "protocol": "HTTP",
                 "lb_algorithm": "ROUND_ROBIN"}})).json()["pool"]
    member = (await api["octavia"].post(f"{LBAAS}/pools/{pool['id']}/members", json={
        "member": {"address": "10.0.0.90", "protocol_port": 80}})).json()["member"]

    assert member["provisioning_status"] == "PENDING_CREATE"
    await expire(Member, member["id"])
    body = (await api["octavia"].get(
        f"{LBAAS}/pools/{pool['id']}/members/{member['id']}")).json()["member"]
    assert body["provisioning_status"] == "ACTIVE"
    assert body["operating_status"] == "NO_MONITOR"


async def test_status_tree_reflects_pending_children(api, slow_transitions) -> None:
    lb = await _lb(api)
    await api["octavia"].post(f"{LBAAS}/listeners", json={
        "listener": {"loadbalancer_id": lb["id"], "protocol": "HTTP", "protocol_port": 80}})
    tree = (await api["octavia"].get(
        f"{LBAAS}/loadbalancers/{lb['id']}/status")).json()["statuses"]["loadbalancer"]
    assert tree["provisioning_status"] == "PENDING_CREATE"
    assert tree["listeners"][0]["provisioning_status"] == "PENDING_CREATE"


async def test_dashboard_reports_pending_deadlines(api, slow_transitions) -> None:
    await _boot(api)
    stats = (await api["dashboard"].get("/api/stats")).json()
    assert stats["servers"][0]["status"] == "BUILD"
    assert stats["servers"][0]["pending_until"] is not None


async def test_stopping_during_the_build_window_sticks(api, slow_transitions) -> None:
    """An explicit action cancels the pending transition instead of being undone by it.

    Without this, stopping an instance that is still inside its 10-60s build window
    leaves the old deadline armed, and the next read resurrects it as ACTIVE.
    """
    from app.core.database import SessionLocal

    server_id = await _boot(api)
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "BUILD"

    await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                           json={"os-resetState": {"state": "active"}})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"os-stop": None})

    async with SessionLocal() as session:
        stored = await session.get(Server, server_id)
    assert stored.status == "SHUTOFF"
    assert stored.transition_until is None, "the build deadline must have been cancelled"
    assert stored.transition_target is None

    # Nothing left to fire, so the instance stays stopped however often it is polled.
    for _ in range(3):
        body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
        assert body["status"] == "SHUTOFF"


async def test_shelve_offload_during_the_build_window_sticks(api, slow_transitions) -> None:
    from app.core.database import SessionLocal

    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                           json={"os-resetState": {"state": "active"}})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"shelveOffload": None})

    async with SessionLocal() as session:
        stored = await session.get(Server, server_id)
    assert stored.transition_until is None

    body = (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]
    assert body["status"] == "SHELVED_OFFLOADED"
    stats = (await api["nova"].get("/v2.1/os-hypervisors/statistics")).json()["hypervisor_statistics"]
    assert stats["vcpus_used"] == 0, "and its vCPU stays released"


async def test_reboot_still_opens_a_fresh_window(api, slow_transitions, expire) -> None:
    """Cancelling on state change must not break actions that want a new window."""
    from app.core.database import SessionLocal

    server_id = await _boot(api)
    await api["nova"].post(f"/v2.1/servers/{server_id}/action",
                           json={"os-resetState": {"state": "active"}})
    await api["nova"].post(f"/v2.1/servers/{server_id}/action", json={"reboot": {"type": "SOFT"}})

    async with SessionLocal() as session:
        stored = await session.get(Server, server_id)
    assert stored.transition_until is not None, "reboot re-arms a deadline of its own"
    assert (await api["nova"].get(
        f"/v2.1/servers/{server_id}")).json()["server"]["OS-EXT-STS:task_state"] == "rebooting"
    await expire(Server, server_id)
    assert (await api["nova"].get(f"/v2.1/servers/{server_id}")).json()["server"]["status"] == "ACTIVE"
