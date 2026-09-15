"""Failure-injection control plane and the middleware that enforces it."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio


async def _inject(api, **payload):
    body = {"service": "nova", "action": "500_error", "duration_seconds": 30}
    body.update(payload)
    return await api["scenarios"].post("/v1/scenarios", json=body)


async def test_index_and_action_catalogue(raw_clients) -> None:
    assert (await raw_clients["scenarios"].get("/")).json()["versions"][0]["id"] == "v1"
    body = (await raw_clients["scenarios"].get("/v1/scenarios/actions")).json()
    actions = {a["action"] for a in body["actions"]}
    assert actions == {"500_error", "503_error", "rate_limit", "latency",
                       "timeout", "quota_exhausted"}
    assert "nova" in body["services"] and "all" in body["services"]
    assert all(a["description"] for a in body["actions"])


async def test_create_and_read_back(api) -> None:
    created = await _inject(api)
    assert created.status_code == 201
    scenario = created.json()["scenario"]
    assert scenario["service"] == "nova" and scenario["action"] == "500_error"
    assert scenario["active"] is True
    assert 0 < scenario["remaining_seconds"] <= 30
    assert scenario["hits"] == 0

    fetched = await api["scenarios"].get(f"/v1/scenarios/{scenario['id']}")
    assert fetched.json()["scenario"]["id"] == scenario["id"]
    listed = (await api["scenarios"].get("/v1/scenarios")).json()["scenarios"]
    assert len(listed) == 1


async def test_500_is_injected_in_the_targets_dialect(api) -> None:
    await _inject(api, service="nova", action="500_error")
    response = await api["nova"].get("/v2.1/servers")
    assert response.status_code == 500
    assert response.json()["computeFault"]["code"] == 500
    assert response.headers["x-openstack-simulator-injected"] == "true"


async def test_injection_is_scoped_to_one_service(api) -> None:
    await _inject(api, service="nova")
    assert (await api["nova"].get("/v2.1/servers")).status_code == 500
    assert (await api["neutron"].get("/v2.0/networks")).status_code == 200
    assert (await api["glance"].get("/v2/images")).status_code == 200


async def test_the_all_service_hits_everything(api) -> None:
    await _inject(api, service="all", action="503_error")
    assert (await api["nova"].get("/v2.1/servers")).status_code == 503
    assert (await api["neutron"].get("/v2.0/networks")).status_code == 503
    assert (await api["cinder"].get("/v3/volumes")).status_code == 503


async def test_neutron_gets_a_neutron_shaped_error(api) -> None:
    await _inject(api, service="neutron", action="500_error")
    body = (await api["neutron"].get("/v2.0/networks")).json()
    assert "NeutronError" in body


async def test_rate_limit_sets_retry_after(api) -> None:
    await _inject(api, service="nova", action="rate_limit", params={"retry_after": 7})
    response = await api["nova"].get("/v2.1/servers")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json()["overLimit"]["retryAfter"] == 7


async def test_quota_exhaustion_names_the_resource(api) -> None:
    await _inject(api, service="nova", action="quota_exhausted",
                  params={"resource": "cores"})
    response = await api["nova"].get("/v2.1/servers")
    assert response.status_code == 403
    assert "cores" in response.json()["forbidden"]["message"]


async def test_timeout_returns_504(api) -> None:
    await _inject(api, service="nova", action="timeout", latency_ms=1)
    response = await api["nova"].get("/v2.1/servers")
    assert response.status_code == 504


async def test_latency_delays_but_still_succeeds(api) -> None:
    await _inject(api, service="nova", action="latency", latency_ms=1)
    response = await api["nova"].get("/v2.1/servers")
    assert response.status_code == 200, "latency slows a request, it does not fail it"


async def test_custom_message_is_used(api) -> None:
    await _inject(api, service="nova", message="the scheduler is on fire")
    body = (await api["nova"].get("/v2.1/servers")).json()
    assert body["computeFault"]["message"] == "the scheduler is on fire"


async def test_path_scoping(api) -> None:
    await _inject(api, service="nova", path_contains="/servers")
    assert (await api["nova"].get("/v2.1/servers")).status_code == 500
    assert (await api["nova"].get("/v2.1/flavors")).status_code == 200


async def test_method_scoping(api) -> None:
    await _inject(api, service="nova", method="POST")
    assert (await api["nova"].get("/v2.1/servers")).status_code == 200
    response = await api["nova"].post("/v2.1/servers", json={"server": {"name": "x"}})
    assert response.status_code == 500


async def test_zero_probability_never_fires(api) -> None:
    await _inject(api, service="nova", probability=0.0)
    for _ in range(5):
        assert (await api["nova"].get("/v2.1/servers")).status_code == 200


async def test_hits_are_counted(api) -> None:
    created = await _inject(api, service="nova")
    scenario_id = created.json()["scenario"]["id"]
    for _ in range(3):
        await api["nova"].get("/v2.1/servers")
    body = (await api["scenarios"].get(f"/v1/scenarios/{scenario_id}")).json()["scenario"]
    assert body["hits"] == 3


async def test_delete_restores_service(api) -> None:
    created = await _inject(api, service="nova")
    scenario_id = created.json()["scenario"]["id"]
    assert (await api["nova"].get("/v2.1/servers")).status_code == 500
    assert (await api["scenarios"].delete(f"/v1/scenarios/{scenario_id}")).status_code == 204
    assert (await api["nova"].get("/v2.1/servers")).status_code == 200
    assert (await api["scenarios"].get(f"/v1/scenarios/{scenario_id}")).status_code == 404


async def test_clear_removes_every_rule(api) -> None:
    await _inject(api, service="nova")
    await _inject(api, service="neutron")
    cleared = await api["scenarios"].delete("/v1/scenarios")
    assert cleared.json()["cleared"] == 2
    assert (await api["nova"].get("/v2.1/servers")).status_code == 200
    assert (await api["neutron"].get("/v2.0/networks")).status_code == 200
    assert (await api["scenarios"].get("/v1/scenarios")).json()["scenarios"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"service": "not-a-service"},
        {"action": "explode"},
        {"probability": 1.5},
        {"probability": -0.1},
        {"duration_seconds": 0},
        {"duration_seconds": -30},
    ],
)
async def test_invalid_payloads_are_rejected(api, payload) -> None:
    response = await _inject(api, **payload)
    assert response.status_code == 400


async def test_unknown_fields_are_rejected(api) -> None:
    response = await api["scenarios"].post("/v1/scenarios", json={
        "service": "nova", "action": "500_error", "typo_field": 1})
    assert response.status_code == 400


async def test_latency_and_timeout_get_sensible_defaults(api) -> None:
    latency = (await _inject(api, action="latency")).json()["scenario"]
    assert latency["latency_ms"] == 2000
    timeout = (await _inject(api, action="timeout")).json()["scenario"]
    assert timeout["latency_ms"] == 30000


async def test_scenarios_show_up_on_the_dashboard(api) -> None:
    await _inject(api, service="cinder", action="rate_limit")
    stats = (await api["dashboard"].get("/api/stats")).json()
    assert len(stats["scenarios"]) == 1
    assert stats["scenarios"][0]["service"] == "cinder"


async def test_the_catalog_skips_services_this_run_is_not_serving() -> None:
    """--service narrows PORTS, and the catalog must narrow with it.

    Advertising an endpoint for a service that is not running used to raise KeyError
    inside token issuance, so every request in a narrowed run failed with a 500 -- the
    run was unusable rather than partial.
    """
    from app.core.config import PORTS, build_catalog

    full = {entry["name"] for entry in build_catalog("p1")}
    assert "swift" in full

    removed = PORTS.pop("swift")
    try:
        narrowed = {entry["name"] for entry in build_catalog("p1")}
    finally:
        PORTS["swift"] = removed
    assert "swift" not in narrowed
    assert "nova" in narrowed
