"""Placement writes: custom traits, provider traits, aggregates, custom resource classes.

Provider create and delete are deliberately absent -- there is one node, and a second
resource provider nothing can ever schedule to would be a fiction. What an operator
genuinely does against a single-provider cloud is decorate it, and that is what works.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
async def provider(api: dict[str, Any], cloud: Any) -> str:
    body = (await api["placement"].get("/resource_providers")).json()
    return body["resource_providers"][0]["uuid"]


# --------------------------------------------------------------------------------------
# Custom traits
# --------------------------------------------------------------------------------------


async def test_a_custom_trait_is_created_once(api: dict[str, Any], cloud: Any) -> None:
    """201 the first time and 204 after, which is how Placement signals "already there"."""
    assert (await api["placement"].put("/traits/CUSTOM_GPU")).status_code == 201
    assert (await api["placement"].put("/traits/CUSTOM_GPU")).status_code == 204
    assert (await api["placement"].get("/traits/CUSTOM_GPU")).status_code == 204


async def test_custom_traits_appear_in_the_listing(api: dict[str, Any], cloud: Any) -> None:
    await api["placement"].put("/traits/CUSTOM_NVME")
    traits = (await api["placement"].get("/traits")).json()["traits"]
    assert "CUSTOM_NVME" in traits
    assert any(t.startswith("HW_") or t.startswith("COMPUTE_") for t in traits)


@pytest.mark.parametrize(
    "name", ["GPU", "custom_gpu", "CUSTOM_gpu", "CUSTOM-GPU", "HW_CPU_X86_AVX2"]
)
async def test_only_custom_names_can_be_created(
    api: dict[str, Any], cloud: Any, name: str
) -> None:
    """Inventing a standard-looking trait would make the cloud lie about its hardware."""
    assert (await api["placement"].put(f"/traits/{name}")).status_code == 400


async def test_an_unknown_trait_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["placement"].get("/traits/CUSTOM_ABSENT")).status_code == 404


async def test_a_custom_trait_can_be_deleted(api: dict[str, Any], cloud: Any) -> None:
    await api["placement"].put("/traits/CUSTOM_TEMP")
    assert (await api["placement"].delete("/traits/CUSTOM_TEMP")).status_code == 204
    assert (await api["placement"].get("/traits/CUSTOM_TEMP")).status_code == 404


async def test_a_standard_trait_cannot_be_deleted(api: dict[str, Any], cloud: Any) -> None:
    traits = (await api["placement"].get("/traits")).json()["traits"]
    standard = [t for t in traits if not t.startswith("CUSTOM_")][0]
    assert (await api["placement"].delete(f"/traits/{standard}")).status_code == 400


async def test_a_trait_in_use_cannot_be_deleted(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    """Deleting it would leave the provider advertising something the cloud has dropped."""
    await api["placement"].put("/traits/CUSTOM_IN_USE")
    await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_IN_USE"]}
    )
    refused = await api["placement"].delete("/traits/CUSTOM_IN_USE")
    assert refused.status_code == 409
    assert "in use" in refused.text


# --------------------------------------------------------------------------------------
# Provider traits
# --------------------------------------------------------------------------------------


async def test_provider_traits_are_set_and_reported(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    await api["placement"].put("/traits/CUSTOM_FAST_DISK")
    set_response = await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_FAST_DISK"]}
    )
    assert set_response.status_code == 200
    assert set_response.json()["traits"] == ["CUSTOM_FAST_DISK"]

    listed = (await api["placement"].get(
        f"/resource_providers/{provider}/traits")).json()["traits"]
    assert "CUSTOM_FAST_DISK" in listed
    # The hardware traits are still reported alongside the operator's.
    assert len(listed) > 1


async def test_setting_an_unknown_trait_is_refused(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    refused = await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_NEVER_MADE"]}
    )
    assert refused.status_code == 400
    assert "No such trait" in refused.text


async def test_provider_traits_are_replaced_not_merged(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    for name in ("CUSTOM_A", "CUSTOM_B"):
        await api["placement"].put(f"/traits/{name}")
    await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_A"]}
    )
    replaced = await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_B"]}
    )
    assert replaced.json()["traits"] == ["CUSTOM_B"]


async def test_provider_traits_can_be_cleared(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    await api["placement"].put("/traits/CUSTOM_CLEARED")
    await api["placement"].put(
        f"/resource_providers/{provider}/traits", json={"traits": ["CUSTOM_CLEARED"]}
    )
    assert (await api["placement"].delete(
        f"/resource_providers/{provider}/traits")).status_code == 204
    listed = (await api["placement"].get(
        f"/resource_providers/{provider}/traits")).json()["traits"]
    assert "CUSTOM_CLEARED" not in listed


async def test_traits_on_a_missing_provider_are_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["placement"].put(
        "/resource_providers/nope/traits", json={"traits": []})).status_code == 404


# --------------------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------------------


async def test_aggregates_round_trip(api: dict[str, Any], cloud: Any, provider: str) -> None:
    aggregates = ["42c9a9d4-1f1f-4f1f-8f1f-2f1f3f1f4f1f"]
    written = await api["placement"].put(
        f"/resource_providers/{provider}/aggregates", json={"aggregates": aggregates}
    )
    assert written.json()["aggregates"] == aggregates
    assert (await api["placement"].get(
        f"/resource_providers/{provider}/aggregates")).json()["aggregates"] == aggregates


async def test_aggregates_can_be_emptied(
    api: dict[str, Any], cloud: Any, provider: str
) -> None:
    await api["placement"].put(
        f"/resource_providers/{provider}/aggregates", json={"aggregates": ["a"]}
    )
    cleared = await api["placement"].put(
        f"/resource_providers/{provider}/aggregates", json={"aggregates": []}
    )
    assert cleared.json()["aggregates"] == []


# --------------------------------------------------------------------------------------
# Custom resource classes
# --------------------------------------------------------------------------------------


async def test_a_custom_resource_class_is_created(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["placement"].put(
        "/resource_classes/CUSTOM_FPGA")).status_code == 201
    assert (await api["placement"].put(
        "/resource_classes/CUSTOM_FPGA")).status_code == 204

    body = (await api["placement"].get("/resource_classes/CUSTOM_FPGA")).json()
    assert body["name"] == "CUSTOM_FPGA"

    listed = [c["name"] for c in (await api["placement"].get(
        "/resource_classes")).json()["resource_classes"]]
    assert "CUSTOM_FPGA" in listed and "VCPU" in listed


async def test_standard_resource_classes_are_protected(
    api: dict[str, Any], cloud: Any
) -> None:
    assert (await api["placement"].put("/resource_classes/VCPU")).status_code == 400
    assert (await api["placement"].delete("/resource_classes/VCPU")).status_code == 400


async def test_a_custom_resource_class_can_be_deleted(
    api: dict[str, Any], cloud: Any
) -> None:
    await api["placement"].put("/resource_classes/CUSTOM_GONE")
    assert (await api["placement"].delete(
        "/resource_classes/CUSTOM_GONE")).status_code == 204
    assert (await api["placement"].get(
        "/resource_classes/CUSTOM_GONE")).status_code == 404


async def test_an_unknown_resource_class_is_404(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["placement"].get(
        "/resource_classes/CUSTOM_NOPE")).status_code == 404
