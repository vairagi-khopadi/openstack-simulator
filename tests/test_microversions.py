"""Microversion negotiation.

The point of these is that the *default* is the service minimum, not the maximum. A
client that sends no version header must get the old response shape, because that is
what a real deployment gives it -- and an integration written against a simulator that
always served the newest shape would break the first time it met a real cloud.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.microversion import (
    NegotiationError,
    SERVICES,
    Version,
    negotiate,
)

pytestmark = pytest.mark.anyio


def _headers(value: str | None = None, legacy: str | None = None) -> dict[str, str]:
    out = {}
    if value is not None:
        out["OpenStack-API-Version"] = value
    if legacy is not None:
        out["X-OpenStack-Nova-API-Version"] = legacy
    return out


# --------------------------------------------------------------------------------------
# Parsing and ordering
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2.1", Version(2, 1)), ("2.79", Version(2, 79)), ("0.0", Version(0, 0)),
     ("  3.70  ", Version(3, 70))],
)
async def test_parse_accepts_real_versions(text: str, expected: Version) -> None:
    assert Version.parse(text) == expected


@pytest.mark.parametrize("text", ["2", "2.", "banana", "", "2.1.3", "v2.1", "2.0.1", "-1.0"])
async def test_parse_rejects_everything_else(text: str) -> None:
    assert Version.parse(text) is None


async def test_versions_order_by_minor_not_string() -> None:
    """String ordering would put 2.9 after 2.79, which is where off-by-one gates start."""
    assert Version(2, 9) < Version(2, 79)
    assert Version(2, 100) > Version(2, 79)


# --------------------------------------------------------------------------------------
# Negotiation
# --------------------------------------------------------------------------------------


async def test_no_header_means_the_minimum() -> None:
    """The headline behaviour: real Nova answers an unversioned request at 2.1."""
    assert negotiate("nova", _headers()) == SERVICES["nova"].minimum
    assert negotiate("nova", _headers()) == Version(2, 1)


async def test_latest_means_the_maximum() -> None:
    assert negotiate("nova", _headers("compute latest")) == SERVICES["nova"].maximum


async def test_standard_header_is_honoured() -> None:
    assert negotiate("nova", _headers("compute 2.47")) == Version(2, 47)


async def test_legacy_nova_header_is_honoured() -> None:
    """novaclient still sends X-OpenStack-Nova-API-Version and nothing else."""
    assert negotiate("nova", _headers(legacy="2.47")) == Version(2, 47)


async def test_standard_header_wins_over_the_legacy_one() -> None:
    assert negotiate("nova", _headers("compute 2.60", legacy="2.47")) == Version(2, 60)


async def test_a_header_for_another_service_is_ignored() -> None:
    """'volume 3.1' is addressed to Cinder; Nova falls back to its own default."""
    assert negotiate("nova", _headers("volume 3.1")) == SERVICES["nova"].minimum


async def test_out_of_range_is_406() -> None:
    with pytest.raises(NegotiationError) as caught:
        negotiate("nova", _headers("compute 2.99"))
    assert caught.value.status == 406
    assert "2.79" in caught.value.message


async def test_below_minimum_is_406() -> None:
    with pytest.raises(NegotiationError) as caught:
        negotiate("nova", _headers("compute 1.0"))
    assert caught.value.status == 406


async def test_malformed_is_400_not_406() -> None:
    """A version that is not a version is a bad request, not an unacceptable one."""
    with pytest.raises(NegotiationError) as caught:
        negotiate("nova", _headers("compute banana"))
    assert caught.value.status == 400


async def test_a_service_without_microversions_negotiates_nothing() -> None:
    assert negotiate("keystone", _headers()) is None
    assert negotiate("swift", _headers()) is None


# --------------------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------------------


async def test_response_reports_the_version_it_served(api: dict[str, Any]) -> None:
    response = await api["nova"].get(
        "/v2.1/flavors", headers={"OpenStack-API-Version": "compute 2.47"}
    )
    assert response.headers["openstack-api-version"] == "compute 2.47"
    assert response.headers["x-openstack-nova-api-version"] == "2.47"
    # The advertised range does not move with the negotiated version.
    assert response.headers["x-openstack-nova-api-maximum-version"] == "2.79"
    assert response.headers["x-openstack-nova-api-minimum-version"] == "2.1"


async def test_unsupported_version_is_refused_before_the_handler(api: dict[str, Any]) -> None:
    response = await api["nova"].get(
        "/v2.1/flavors", headers={"OpenStack-API-Version": "compute 2.99"}
    )
    assert response.status_code == 406
    assert "2.79" in response.text


async def test_negotiation_applies_to_every_microversioned_service(
    api: dict[str, Any]
) -> None:
    for service, path in (
        ("cinder", "/v3/volumes"),
        ("placement", "/resource_providers"),
        ("glance", "/v2/images"),
    ):
        response = await api[service].get(
            path, headers={"OpenStack-API-Version": f"{SERVICES[service].name} 99.99"}
        )
        assert response.status_code == 406, service


# --------------------------------------------------------------------------------------
# Version-gated response bodies
# --------------------------------------------------------------------------------------


async def _boot(api: dict[str, Any]) -> str:
    images = (await api["glance"].get("/v2/images")).json()["images"]
    image = [i for i in images if i["name"] == "cirros"][0]["id"]
    networks = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    network = [n for n in networks if n["name"] == "private"][0]["id"]
    created = await api["nova"].post(
        "/v2.1/servers",
        json={"server": {"name": "gated", "flavorRef": "1", "imageRef": image,
                         "networks": [{"uuid": network}]}},
    )
    return created.json()["server"]["id"]


async def test_flavor_is_linked_before_247_and_embedded_after(
    api: dict[str, Any], cloud: Any
) -> None:
    server_id = await _boot(api)
    path = f"/v2.1/servers/{server_id}"

    old = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": "compute 2.46"})).json()["server"]
    assert set(old["flavor"]) == {"id", "links"}

    new = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": "compute 2.47"})).json()["server"]
    assert new["flavor"]["original_name"] == "m1.tiny"


@pytest.mark.parametrize(
    ("field", "since"),
    [("locked", "2.9"), ("host_status", "2.16"), ("description", "2.19"),
     ("tags", "2.26"), ("trusted_image_certificates", "2.63"), ("server_groups", "2.71")],
)
async def test_server_fields_appear_only_from_their_own_version(
    api: dict[str, Any], cloud: Any, field: str, since: str
) -> None:
    server_id = await _boot(api)
    path = f"/v2.1/servers/{server_id}"
    major, minor = (int(part) for part in since.split("."))
    before = f"{major}.{minor - 1}"

    older = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": f"compute {before}"})).json()["server"]
    newer = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": f"compute {since}"})).json()["server"]

    assert field not in older, f"{field} must not exist at {before}"
    assert field in newer, f"{field} must exist at {since}"


async def test_unversioned_server_read_is_the_2_1_shape(api: dict[str, Any], cloud: Any) -> None:
    """What a client that forgot to pin gets -- and what it would get from a real cloud."""
    server_id = await _boot(api)
    body = (await api["nova"].get(
        f"/v2.1/servers/{server_id}",
        headers={"OpenStack-API-Version": "compute 2.1"},
    )).json()["server"]
    for modern in ("locked", "tags", "description", "host_status", "server_groups"):
        assert modern not in body
    assert set(body["flavor"]) == {"id", "links"}


async def test_quota_set_drops_the_fields_nova_removed(api: dict[str, Any], cloud: Any) -> None:
    """2.36 took the network quotas out and 2.57 took the personality-file ones."""
    path = f"/v2.1/os-quota-sets/{cloud.project_id}"
    network_quotas = {"fixed_ips", "floating_ips", "security_groups", "security_group_rules"}
    file_quotas = {"injected_files", "injected_file_content_bytes", "injected_file_path_bytes"}

    at_235 = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": "compute 2.35"})).json()["quota_set"]
    assert network_quotas <= set(at_235) and file_quotas <= set(at_235)

    at_236 = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": "compute 2.36"})).json()["quota_set"]
    assert not (network_quotas & set(at_236))
    assert file_quotas <= set(at_236)

    at_257 = (await api["nova"].get(
        path, headers={"OpenStack-API-Version": "compute 2.57"})).json()["quota_set"]
    assert not (network_quotas & set(at_257)) and not (file_quotas & set(at_257))
    # The quotas that were never removed stay put at every version.
    assert {"cores", "ram", "instances", "key_pairs"} <= set(at_257)
