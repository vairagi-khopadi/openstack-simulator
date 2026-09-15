"""CloudKitty's hashmap rating configuration.

Rates were settable only through `OPENSTACK_SIMULATOR_RATE_*`, which meant a restart to
change a price. The endpoints matter less than the consequence: a mapping created here has
to show up in the bill, or the configuration API is decoration.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.core.config import now_utc
from app.core.database import SessionLocal
from app.models.compute import Server

pytestmark = pytest.mark.anyio

BASE = "/v1/rating/module_config/hashmap"


async def _service(api: dict[str, Any], name: str = "compute") -> str:
    created = await api["cloudkitty"].post(f"{BASE}/services", json={"name": name})
    return created.json()["service_id"]


async def _boot(api: dict[str, Any], name: str = "billed", flavor: str = "3") -> str:
    """Boot an instance and age it an hour, so there is a bill to reprice.

    Charges here accrue from wall time, so a server created a moment ago costs exactly
    zero and every ratio test below would compare 0 against 0.
    """
    images = (await api["glance"].get("/v2/images")).json()["images"]
    image = [i for i in images if i["name"] == "cirros"][0]["id"]
    networks = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    network = [n for n in networks if n["name"] == "private"][0]["id"]
    created = await api["nova"].post(
        "/v2.1/servers",
        json={"server": {"name": name, "flavorRef": flavor, "imageRef": image,
                         "networks": [{"uuid": network}]}},
    )
    server_id = created.json()["server"]["id"]
    # Settle the build, then rewind the billing clock an hour.
    await api["nova"].get(f"/v2.1/servers/{server_id}")
    async with SessionLocal() as session:
        await session.execute(
            update(Server)
            .where(Server.id == server_id)
            .values(accounted_at=now_utc() - timedelta(hours=1))
        )
        await session.commit()
    return server_id


# --------------------------------------------------------------------------------------
# Configuration CRUD
# --------------------------------------------------------------------------------------


async def test_services_are_created_and_listed(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    listed = (await api["cloudkitty"].get(f"{BASE}/services")).json()["services"]
    assert [s["name"] for s in listed] == ["compute"]
    fetched = (await api["cloudkitty"].get(f"{BASE}/services/{service_id}")).json()
    assert fetched["service_id"] == service_id


async def test_duplicate_services_conflict(api: dict[str, Any], cloud: Any) -> None:
    await _service(api)
    again = await api["cloudkitty"].post(f"{BASE}/services", json={"name": "compute"})
    assert again.status_code == 409


async def test_a_service_needs_a_name(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["cloudkitty"].post(
        f"{BASE}/services", json={})).status_code == 400


async def test_fields_hang_off_a_service(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    created = await api["cloudkitty"].post(
        f"{BASE}/fields", json={"name": "flavor_id", "service_id": service_id}
    )
    assert created.status_code == 201
    assert created.json()["service_id"] == service_id

    listed = (await api["cloudkitty"].get(
        f"{BASE}/fields?service_id={service_id}")).json()["fields"]
    assert [f["name"] for f in listed] == ["flavor_id"]


async def test_a_field_on_a_missing_service_is_404(api: dict[str, Any], cloud: Any) -> None:
    refused = await api["cloudkitty"].post(
        f"{BASE}/fields", json={"name": "x", "service_id": "ghost"}
    )
    assert refused.status_code == 404


async def test_mappings_attach_to_exactly_one_parent(
    api: dict[str, Any], cloud: Any
) -> None:
    """Both or neither would make it ambiguous which rows the rule prices."""
    service_id = await _service(api)
    field_id = (await api["cloudkitty"].post(
        f"{BASE}/fields", json={"name": "flavor_id", "service_id": service_id}
    )).json()["field_id"]

    neither = await api["cloudkitty"].post(f"{BASE}/mappings", json={"cost": "1"})
    assert neither.status_code == 400

    both = await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"cost": "1", "service_id": service_id, "field_id": field_id},
    )
    assert both.status_code == 400


async def test_a_field_mapping_needs_a_value(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    field_id = (await api["cloudkitty"].post(
        f"{BASE}/fields", json={"name": "flavor_id", "service_id": service_id}
    )).json()["field_id"]
    refused = await api["cloudkitty"].post(
        f"{BASE}/mappings", json={"field_id": field_id, "cost": "0.1"}
    )
    assert refused.status_code == 400
    assert "value" in refused.text


async def test_mapping_crud(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    created = await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "0.05", "type": "flat"},
    )
    assert created.status_code == 201
    mapping_id = created.json()["mapping_id"]
    assert created.json()["cost"] == "0.05"

    updated = await api["cloudkitty"].put(
        f"{BASE}/mappings/{mapping_id}", json={"cost": "0.10"}
    )
    assert updated.json()["cost"] == "0.1"

    assert (await api["cloudkitty"].delete(
        f"{BASE}/mappings/{mapping_id}")).status_code == 204
    assert (await api["cloudkitty"].get(
        f"{BASE}/mappings/{mapping_id}")).status_code == 404


async def test_an_unknown_map_type_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    refused = await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "1", "type": "exponential"},
    )
    assert refused.status_code == 400


async def test_a_non_numeric_cost_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    refused = await api["cloudkitty"].post(
        f"{BASE}/mappings", json={"service_id": service_id, "cost": "cheap"}
    )
    assert refused.status_code == 400


async def test_thresholds_need_a_level(api: dict[str, Any], cloud: Any) -> None:
    service_id = await _service(api)
    refused = await api["cloudkitty"].post(
        f"{BASE}/thresholds", json={"service_id": service_id, "cost": "1"}
    )
    assert refused.status_code == 400

    created = await api["cloudkitty"].post(
        f"{BASE}/thresholds",
        json={"service_id": service_id, "cost": "1", "level": "100"},
    )
    assert created.status_code == 201
    assert created.json()["level"] == "100.0"


async def test_groups_are_managed(api: dict[str, Any], cloud: Any) -> None:
    created = await api["cloudkitty"].post(f"{BASE}/groups", json={"name": "discounts"})
    assert created.status_code == 201
    group_id = created.json()["group_id"]
    assert [g["name"] for g in (await api["cloudkitty"].get(
        f"{BASE}/groups")).json()["groups"]] == ["discounts"]
    assert (await api["cloudkitty"].delete(
        f"{BASE}/groups/{group_id}")).status_code == 204


async def test_deleting_a_service_takes_its_rules(api: dict[str, Any], cloud: Any) -> None:
    """Rules left behind would price nothing and confuse the next reader of the config."""
    service_id = await _service(api)
    field_id = (await api["cloudkitty"].post(
        f"{BASE}/fields", json={"name": "flavor_id", "service_id": service_id}
    )).json()["field_id"]
    await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"field_id": field_id, "value": "m1.tiny", "cost": "0.1"},
    )

    assert (await api["cloudkitty"].delete(
        f"{BASE}/services/{service_id}")).status_code == 204
    assert (await api["cloudkitty"].get(f"{BASE}/fields")).json()["fields"] == []
    assert (await api["cloudkitty"].get(f"{BASE}/mappings")).json()["mappings"] == []


# --------------------------------------------------------------------------------------
# The configuration has to reach the bill
# --------------------------------------------------------------------------------------


async def _total(api: dict[str, Any]) -> float:
    return (await api["cloudkitty"].get("/v1/report/total")).json()["total"]


def _same(actual: float, expected: float) -> bool:
    """Charges keep accruing between two reads, so "unchanged" means "within drift"."""
    return actual == pytest.approx(expected, rel=1e-3)


async def test_no_configuration_changes_nothing(api: dict[str, Any], cloud: Any) -> None:
    """A cloud that never touches the hashmap API must bill exactly as it did before."""
    await _boot(api)
    before = await _total(api)
    await _service(api)  # a service with no mappings under it
    assert _same(await _total(api), before)


async def test_a_flat_mapping_adds_to_the_bill(api: dict[str, Any], cloud: Any) -> None:
    await _boot(api)
    baseline = await _total(api)

    service_id = await _service(api, "compute")
    await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "10", "type": "flat"},
    )
    assert await _total(api) > baseline


async def test_a_rate_mapping_multiplies_the_bill(api: dict[str, Any], cloud: Any) -> None:
    """How an operator applies a discount without restating every individual price."""
    await _boot(api)
    baseline = await _total(api)
    assert baseline > 0

    service_id = await _service(api, "compute")
    await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "0.5", "type": "rate"},
    )
    assert await _total(api) == pytest.approx(baseline * 0.5, rel=1e-3)


async def test_a_mapping_scoped_to_another_project_does_not_apply(
    api: dict[str, Any], cloud: Any
) -> None:
    await _boot(api)
    baseline = await _total(api)

    service_id = await _service(api, "compute")
    await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "0.5", "type": "rate",
              "tenant_id": "someone-else"},
    )
    assert _same(await _total(api), baseline)


async def test_a_mapping_for_another_service_does_not_apply(
    api: dict[str, Any], cloud: Any
) -> None:
    """A volume rule must not reprice compute rows."""
    await _boot(api)
    baseline = await _total(api)

    service_id = await _service(api, "volume")
    await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "0.5", "type": "rate"},
    )
    assert _same(await _total(api), baseline)


async def test_removing_the_mapping_restores_the_price(
    api: dict[str, Any], cloud: Any
) -> None:
    await _boot(api)
    baseline = await _total(api)

    service_id = await _service(api, "compute")
    mapping_id = (await api["cloudkitty"].post(
        f"{BASE}/mappings",
        json={"service_id": service_id, "cost": "0.5", "type": "rate"},
    )).json()["mapping_id"]
    assert await _total(api) < baseline / 1.5  # halved, allowing for drift

    await api["cloudkitty"].delete(f"{BASE}/mappings/{mapping_id}")
    assert _same(await _total(api), baseline)
