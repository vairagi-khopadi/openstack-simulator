"""Octavia L7 policies, rules, statistics and health-monitor updates.

A policy diverts traffic away from a listener's default pool; its rules say when. Nothing
here proxies a packet, so what is worth modelling is the *configuration* surface -- the
validation Octavia does on the way in, and the position bookkeeping it does across a
listener's policies, both of which a config tool will hit long before any traffic flows.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def _lb(api: dict[str, Any], cloud: Any) -> str:
    subnets = (await api["neutron"].get("/v2.0/subnets")).json()["subnets"]
    created = await api["octavia"].post(
        "/v2/lbaas/loadbalancers",
        json={"loadbalancer": {"name": "lb", "vip_subnet_id": subnets[0]["id"]}},
    )
    return created.json()["loadbalancer"]["id"]


async def _listener(api: dict[str, Any], lb_id: str, port: int = 80) -> str:
    created = await api["octavia"].post(
        "/v2/lbaas/listeners",
        json={"listener": {"name": f"l{port}", "loadbalancer_id": lb_id,
                           "protocol": "HTTP", "protocol_port": port}},
    )
    return created.json()["listener"]["id"]


async def _pool(api: dict[str, Any], lb_id: str, name: str = "pool") -> str:
    created = await api["octavia"].post(
        "/v2/lbaas/pools",
        json={"pool": {"name": name, "loadbalancer_id": lb_id, "protocol": "HTTP",
                       "lb_algorithm": "ROUND_ROBIN"}},
    )
    return created.json()["pool"]["id"]


async def _policy(api: dict[str, Any], listener_id: str, **extra: Any) -> Any:
    return await api["octavia"].post(
        "/v2/lbaas/l7policies",
        json={"l7policy": {"listener_id": listener_id, **extra}},
    )


# --------------------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------------------


async def test_create_a_reject_policy(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    created = await _policy(api, listener, name="block-admin", action="REJECT")
    assert created.status_code == 201
    body = created.json()["l7policy"]
    assert body["action"] == "REJECT"
    assert body["position"] == 1
    assert body["rules"] == []


async def test_redirect_to_pool_needs_a_pool(api: dict[str, Any], cloud: Any) -> None:
    lb = await _lb(api, cloud)
    listener = await _listener(api, lb)
    refused = await _policy(api, listener, action="REDIRECT_TO_POOL")
    assert refused.status_code == 400
    assert "redirect_pool_id" in refused.text

    pool = await _pool(api, lb)
    ok = await _policy(api, listener, action="REDIRECT_TO_POOL", redirect_pool_id=pool)
    assert ok.status_code == 201
    assert ok.json()["l7policy"]["redirect_pool_id"] == pool


async def test_redirect_to_url_needs_a_url(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    refused = await _policy(api, listener, action="REDIRECT_TO_URL")
    assert refused.status_code == 400

    ok = await _policy(api, listener, action="REDIRECT_TO_URL",
                       redirect_url="https://example.invalid/")
    assert ok.status_code == 201
    # A redirect without an explicit code defaults to 302, as Octavia's does.
    assert ok.json()["l7policy"]["redirect_http_code"] == 302


async def test_an_unknown_action_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    refused = await _policy(api, listener, action="TELEPORT")
    assert refused.status_code == 400


async def test_a_policy_on_a_missing_listener_is_404(api: dict[str, Any], cloud: Any) -> None:
    refused = await _policy(api, "no-such-listener")
    assert refused.status_code == 404


async def test_redirect_to_a_missing_pool_is_404(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    refused = await _policy(api, listener, action="REDIRECT_TO_POOL",
                            redirect_pool_id="ghost")
    assert refused.status_code == 404


async def test_positions_are_assigned_in_order(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    first = (await _policy(api, listener, name="a")).json()["l7policy"]
    second = (await _policy(api, listener, name="b")).json()["l7policy"]
    third = (await _policy(api, listener, name="c")).json()["l7policy"]
    assert [first["position"], second["position"], third["position"]] == [1, 2, 3]


async def test_deleting_a_policy_closes_the_position_gap(
    api: dict[str, Any], cloud: Any
) -> None:
    """Octavia renumbers on every change; a gap would make ordering ambiguous."""
    listener = await _listener(api, await _lb(api, cloud))
    first = (await _policy(api, listener, name="a")).json()["l7policy"]["id"]
    await _policy(api, listener, name="b")
    await _policy(api, listener, name="c")

    assert (await api["octavia"].delete(
        f"/v2/lbaas/l7policies/{first}")).status_code == 204

    remaining = (await api["octavia"].get(
        f"/v2/lbaas/l7policies?listener_id={listener}")).json()["l7policies"]
    assert sorted(p["position"] for p in remaining) == [1, 2]


async def test_listing_filters_by_listener(api: dict[str, Any], cloud: Any) -> None:
    lb = await _lb(api, cloud)
    one, two = await _listener(api, lb, 80), await _listener(api, lb, 8080)
    await _policy(api, one, name="on-one")
    await _policy(api, two, name="on-two")

    filtered = (await api["octavia"].get(
        f"/v2/lbaas/l7policies?listener_id={one}")).json()["l7policies"]
    assert [p["name"] for p in filtered] == ["on-one"]


async def test_update_changes_the_action(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener, name="mutable")).json()["l7policy"]["id"]
    updated = await api["octavia"].put(
        f"/v2/lbaas/l7policies/{policy}",
        json={"l7policy": {"action": "REDIRECT_TO_URL",
                           "redirect_url": "https://example.invalid/x"}},
    )
    assert updated.json()["l7policy"]["action"] == "REDIRECT_TO_URL"


async def test_unknown_policy_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["octavia"].get("/v2/lbaas/l7policies/nope")).status_code == 404


# --------------------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------------------


async def _rule(api: dict[str, Any], policy_id: str, **extra: Any) -> Any:
    payload = {"type": "PATH", "compare_type": "STARTS_WITH", "value": "/admin"}
    payload.update(extra)
    return await api["octavia"].post(
        f"/v2/lbaas/l7policies/{policy_id}/rules", json={"rule": payload}
    )


async def test_create_and_list_rules(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]

    created = await _rule(api, policy)
    assert created.status_code == 201
    assert created.json()["rule"]["value"] == "/admin"

    rules = (await api["octavia"].get(
        f"/v2/lbaas/l7policies/{policy}/rules")).json()["rules"]
    assert len(rules) == 1


async def test_the_policy_reports_its_rule_ids(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    rule_id = (await _rule(api, policy)).json()["rule"]["id"]

    body = (await api["octavia"].get(f"/v2/lbaas/l7policies/{policy}")).json()["l7policy"]
    assert body["rules"] == [{"id": rule_id}]


@pytest.mark.parametrize("rule_type", ["HEADER", "COOKIE", "SSL_DN_FIELD"])
async def test_keyed_rule_types_require_a_key(
    api: dict[str, Any], cloud: Any, rule_type: str
) -> None:
    """"the header named X contains Y" is meaningless without the name."""
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]

    refused = await _rule(api, policy, type=rule_type, compare_type="EQUAL_TO", value="v")
    assert refused.status_code == 400
    assert "key" in refused.text

    ok = await _rule(api, policy, type=rule_type, compare_type="EQUAL_TO",
                     value="v", key="X-Thing")
    assert ok.status_code == 201


async def test_an_unknown_rule_type_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    refused = await _rule(api, policy, type="VIBES")
    assert refused.status_code == 400


async def test_an_unknown_compare_type_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    refused = await _rule(api, policy, compare_type="SOUNDS_LIKE")
    assert refused.status_code == 400


async def test_rules_can_be_inverted(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    rule = (await _rule(api, policy, invert=True)).json()["rule"]
    assert rule["invert"] is True


async def test_update_and_delete_a_rule(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    rule_id = (await _rule(api, policy)).json()["rule"]["id"]

    updated = await api["octavia"].put(
        f"/v2/lbaas/l7policies/{policy}/rules/{rule_id}", json={"rule": {"value": "/new"}}
    )
    assert updated.json()["rule"]["value"] == "/new"

    assert (await api["octavia"].delete(
        f"/v2/lbaas/l7policies/{policy}/rules/{rule_id}")).status_code == 204
    assert (await api["octavia"].get(
        f"/v2/lbaas/l7policies/{policy}/rules")).json()["rules"] == []


async def test_deleting_a_policy_takes_its_rules(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    policy = (await _policy(api, listener)).json()["l7policy"]["id"]
    rule_id = (await _rule(api, policy)).json()["rule"]["id"]

    await api["octavia"].delete(f"/v2/lbaas/l7policies/{policy}")
    assert (await api["octavia"].get(
        f"/v2/lbaas/l7policies/{policy}/rules/{rule_id}")).status_code == 404


async def test_a_rule_from_another_policy_is_not_found(
    api: dict[str, Any], cloud: Any
) -> None:
    """The rule id alone must not be enough -- it has to belong to this policy."""
    listener = await _listener(api, await _lb(api, cloud))
    first = (await _policy(api, listener, name="a")).json()["l7policy"]["id"]
    second = (await _policy(api, listener, name="b")).json()["l7policy"]["id"]
    rule_id = (await _rule(api, first)).json()["rule"]["id"]

    assert (await api["octavia"].get(
        f"/v2/lbaas/l7policies/{second}/rules/{rule_id}")).status_code == 404


# --------------------------------------------------------------------------------------
# Statistics and health monitors
# --------------------------------------------------------------------------------------


async def test_loadbalancer_stats_are_stable_between_reads(
    api: dict[str, Any], cloud: Any
) -> None:
    """Synthetic, but deterministic -- a polling dashboard must not see them jitter."""
    lb = await _lb(api, cloud)
    first = (await api["octavia"].get(f"/v2/lbaas/loadbalancers/{lb}/stats")).json()["stats"]
    second = (await api["octavia"].get(f"/v2/lbaas/loadbalancers/{lb}/stats")).json()["stats"]
    assert first == second
    assert set(first) == {"active_connections", "bytes_in", "bytes_out",
                          "request_errors", "total_connections"}


async def test_listener_stats(api: dict[str, Any], cloud: Any) -> None:
    listener = await _listener(api, await _lb(api, cloud))
    stats = (await api["octavia"].get(
        f"/v2/lbaas/listeners/{listener}/stats")).json()["stats"]
    assert stats["total_connections"] >= 0


async def test_stats_for_a_missing_loadbalancer_is_404(
    api: dict[str, Any], cloud: Any
) -> None:
    assert (await api["octavia"].get(
        "/v2/lbaas/loadbalancers/nope/stats")).status_code == 404


async def test_health_monitor_can_be_retuned(api: dict[str, Any], cloud: Any) -> None:
    lb = await _lb(api, cloud)
    pool = await _pool(api, lb)
    monitor = (await api["octavia"].post(
        "/v2/lbaas/healthmonitors",
        json={"healthmonitor": {"pool_id": pool, "type": "HTTP", "delay": 5,
                                "timeout": 3, "max_retries": 3}},
    )).json()["healthmonitor"]["id"]

    updated = await api["octavia"].put(
        f"/v2/lbaas/healthmonitors/{monitor}",
        json={"healthmonitor": {"delay": 30, "timeout": 10}},
    )
    assert updated.status_code == 200
    assert updated.json()["healthmonitor"]["delay"] == 30


async def test_a_timeout_longer_than_the_delay_is_refused(
    api: dict[str, Any], cloud: Any
) -> None:
    """The next probe would start before the previous one had given up."""
    lb = await _lb(api, cloud)
    pool = await _pool(api, lb)
    monitor = (await api["octavia"].post(
        "/v2/lbaas/healthmonitors",
        json={"healthmonitor": {"pool_id": pool, "type": "HTTP", "delay": 5,
                                "timeout": 3, "max_retries": 3}},
    )).json()["healthmonitor"]["id"]

    refused = await api["octavia"].put(
        f"/v2/lbaas/healthmonitors/{monitor}", json={"healthmonitor": {"timeout": 60}}
    )
    assert refused.status_code == 400
    assert "delay" in refused.text
