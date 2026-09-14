"""Sorting and field selection.

Both are cheap for a client to ask for and easy for a simulator to ignore, which is the
problem: a listing that silently returns everything in its own order looks like it
supports `?sort_key=` and `?fields=` right up until the assertions move to a real cloud.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.pagination import Page, page_request, requested_fields, trim

pytestmark = pytest.mark.anyio


class _Params(dict):
    def multi_items(self) -> list[tuple[str, str]]:
        return list(self.items())

    def getlist(self, key: str) -> list[str]:
        value = self.get(key)
        return [value] if value is not None else []


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


async def test_sort_parameters_are_read() -> None:
    page = page_request(_Params(sort_key="name", sort_dir="asc"), "neutron")
    assert page.sort_key == "name" and page.sort_dir == "asc"


async def test_an_unusable_sort_dir_is_ignored() -> None:
    """Falling back to the collection's own order beats refusing the listing."""
    assert page_request(_Params(sort_dir="sideways"), "nova").sort_dir == ""


async def test_fields_accepts_both_spellings() -> None:
    assert requested_fields(_Params(fields="id,name")) == {"id", "name"}
    assert requested_fields(_Params(fields=" name , status ")) == {"name", "status"}
    assert requested_fields(_Params()) == set()


async def test_trim_keeps_only_what_was_asked_for() -> None:
    body = {"id": "1", "name": "n", "status": "ACTIVE", "extra": "x"}
    assert trim(body, {"name"}) == {"id": "1", "name": "n"}


async def test_trim_always_keeps_the_id() -> None:
    """A listing whose entries cannot be identified saves nothing worth having."""
    assert trim({"id": "1", "name": "n"}, {"name"})["id"] == "1"


async def test_trim_without_fields_is_a_no_op() -> None:
    body = {"id": "1", "name": "n"}
    assert trim(body, set()) == body


async def test_trim_maps_over_a_list() -> None:
    rows = [{"id": "1", "name": "a", "x": 1}, {"id": "2", "name": "b", "x": 2}]
    assert trim(rows, {"name"}) == [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]


# --------------------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------------------


async def _networks(api: dict[str, Any], names: list[str]) -> None:
    for name in names:
        await api["neutron"].post("/v2.0/networks", json={"network": {"name": name}})


async def test_sorting_a_listing_by_name(api: dict[str, Any], cloud: Any) -> None:
    await _networks(api, ["zebra", "alpha", "mango"])
    ascending = (await api["neutron"].get(
        "/v2.0/networks?sort_key=name&sort_dir=asc")).json()["networks"]
    names = [n["name"] for n in ascending]
    assert names == sorted(names)

    descending = (await api["neutron"].get(
        "/v2.0/networks?sort_key=name&sort_dir=desc")).json()["networks"]
    assert [n["name"] for n in descending] == sorted(names, reverse=True)


async def test_an_unknown_sort_key_falls_back_rather_than_failing(
    api: dict[str, Any], cloud: Any
) -> None:
    """Clients probe for support; a 400 would break a listing that works fine unsorted."""
    response = await api["neutron"].get("/v2.0/networks?sort_key=favourite_colour")
    assert response.status_code == 200
    assert len(response.json()["networks"]) == 2  # the seeded pair


async def test_sorting_survives_pagination(api: dict[str, Any], cloud: Any) -> None:
    """The marker keyset has to seek on the same column the order uses."""
    await _networks(api, ["d", "a", "c", "b"])
    seen: list[str] = []
    url: str | None = "/v2.0/networks?sort_key=name&sort_dir=asc&limit=2"
    for _ in range(10):
        if url is None:
            break
        body = (await api["neutron"].get(url)).json()
        seen += [n["name"] for n in body["networks"]]
        links = body.get("networks_links") or []
        nxt = [link for link in links if link["rel"] == "next"]
        url = nxt[0]["href"].split(".sim", 1)[-1] if nxt else None
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 6  # four created plus the two seeded


async def test_fields_trims_the_response(api: dict[str, Any], cloud: Any) -> None:
    body = (await api["neutron"].get("/v2.0/networks?fields=name")).json()["networks"]
    assert all(set(n) == {"id", "name"} for n in body)


async def test_fields_applies_to_every_wired_collection(
    api: dict[str, Any], cloud: Any
) -> None:
    for path, key in (
        ("/v2.0/networks", "networks"),
        ("/v2.0/subnets", "subnets"),
        ("/v2.0/ports", "ports"),
        ("/v2.0/security-groups", "security_groups"),
        ("/v2.0/routers", "routers"),
        ("/v2.0/floatingips", "floatingips"),
    ):
        body = (await api["neutron"].get(f"{path}?fields=name")).json()[key]
        assert all(set(row) <= {"id", "name"} for row in body), path


async def test_no_fields_parameter_returns_everything(
    api: dict[str, Any], cloud: Any
) -> None:
    body = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    assert len(set(body[0])) > 2
