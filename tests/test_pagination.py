"""Marker pagination.

The bug worth guarding against is not a wrong page -- it is a client that pages forever,
or one that stops early and reports success. So most of these walk the links the way an
SDK does and assert that the walk terminates, covers the collection exactly once, and
matches what an unpaged listing returns.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest

from app.core.pagination import MAX_LIMIT, Page, next_marker, page_request

pytestmark = pytest.mark.anyio


class _Params(dict):
    """Enough of Starlette's QueryParams for the unit tests below."""

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self.items())


def _relative(href: str) -> str:
    parsed = urlparse(href)
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


async def _walk(client: Any, path: str, collection: str, cap: int = 50) -> list[str]:
    """Follow ``next`` links to exhaustion, exactly as an SDK would."""
    seen: list[str] = []
    url: str | None = path
    for _ in range(cap):
        if url is None:
            return seen
        body = (await client.get(url)).json()
        seen += [item["id"] for item in body[collection]]
        links = body.get(f"{collection}_links") or []
        nxt = [link for link in links if link["rel"] == "next"]
        url = _relative(nxt[0]["href"]) if nxt else None
    raise AssertionError(f"{path} did not terminate within {cap} pages")


# --------------------------------------------------------------------------------------
# Reading the request
# --------------------------------------------------------------------------------------


async def test_limit_defaults_per_service() -> None:
    assert page_request(_Params(), "glance").limit == 100
    assert page_request(_Params(), "nova").limit == 1000


async def test_limit_is_capped_not_refused() -> None:
    assert page_request(_Params(limit="999999"), "nova").limit == MAX_LIMIT


@pytest.mark.parametrize("value", ["", "banana", "0", "-5"])
async def test_unusable_limit_falls_back_to_the_default(value: str) -> None:
    """A stray ?limit= should not turn a working listing into a 400."""
    assert page_request(_Params(limit=value), "nova").limit == 1000


async def test_next_marker_is_none_on_a_short_page() -> None:
    """A short page is how a client learns the collection ended."""
    items = [type("Row", (), {"id": "a"})()]
    assert next_marker(items, Page(limit=10, marker=None, requested_limit=True)) is None


async def test_next_marker_is_the_last_id_on_a_full_page() -> None:
    items = [type("Row", (), {"id": name})() for name in ("a", "b")]
    assert next_marker(items, Page(limit=2, marker=None, requested_limit=True)) == "b"


# --------------------------------------------------------------------------------------
# Walking real collections
# --------------------------------------------------------------------------------------


async def _boot_servers(api: dict[str, Any], count: int) -> list[str]:
    images = (await api["glance"].get("/v2/images")).json()["images"]
    image = [i for i in images if i["name"] == "cirros"][0]["id"]
    networks = (await api["neutron"].get("/v2.0/networks")).json()["networks"]
    network = [n for n in networks if n["name"] == "private"][0]["id"]
    created = []
    for index in range(count):
        response = await api["nova"].post(
            "/v2.1/servers",
            json={"server": {"name": f"page-{index}", "flavorRef": "1",
                             "imageRef": image, "networks": [{"uuid": network}]}},
        )
        created.append(response.json()["server"]["id"])
    return created


async def test_servers_page_completely_and_terminate(api: dict[str, Any], cloud: Any) -> None:
    booted = await _boot_servers(api, 7)
    walked = await _walk(api["nova"], "/v2.1/servers?limit=2", "servers")
    assert sorted(walked) == sorted(booted)
    assert len(walked) == len(set(walked)), "a resource was served on two pages"


async def test_a_page_that_exactly_divides_still_terminates(
    api: dict[str, Any], cloud: Any
) -> None:
    """The off-by-one case: the last full page must be followed by one empty page."""
    booted = await _boot_servers(api, 4)
    walked = await _walk(api["nova"], "/v2.1/servers?limit=2", "servers")
    assert sorted(walked) == sorted(booted)


async def test_paged_and_unpaged_listings_agree(api: dict[str, Any], cloud: Any) -> None:
    await _boot_servers(api, 5)
    unpaged = (await api["nova"].get("/v2.1/servers")).json()
    walked = await _walk(api["nova"], "/v2.1/servers?limit=2", "servers")
    assert sorted(walked) == sorted(s["id"] for s in unpaged["servers"])
    # A listing that fits in one page carries no next link at all.
    assert "servers_links" not in unpaged


async def test_next_link_preserves_the_other_query_parameters(
    api: dict[str, Any], cloud: Any
) -> None:
    """Losing ?name= on page two would silently widen the result set."""
    await _boot_servers(api, 3)
    body = (await api["nova"].get("/v2.1/servers?name=page&limit=1")).json()
    href = body["servers_links"][0]["href"]
    assert "name=page" in href and "marker=" in href
    # And following it keeps the filter applied rather than returning the whole cloud.
    assert len(await _walk(api["nova"], "/v2.1/servers?name=page&limit=1", "servers")) == 3


async def test_an_unknown_marker_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    """An empty page would look exactly like the end of the collection."""
    response = await api["nova"].get("/v2.1/servers?marker=does-not-exist")
    assert response.status_code == 400
    assert "does-not-exist" in response.text


async def test_volumes_page(api: dict[str, Any], cloud: Any) -> None:
    for index in range(5):
        await api["cinder"].post(
            "/v3/volumes", json={"volume": {"name": f"vol-{index}", "size": 1}}
        )
    walked = await _walk(api["cinder"], "/v3/volumes?limit=2", "volumes")
    assert len(walked) == 5 and len(set(walked)) == 5


async def test_networks_page(api: dict[str, Any], cloud: Any) -> None:
    for index in range(4):
        await api["neutron"].post(
            "/v2.0/networks", json={"network": {"name": f"net-{index}"}}
        )
    walked = await _walk(api["neutron"], "/v2.0/networks?limit=2", "networks")
    # Two seeded networks plus the four created here.
    assert len(walked) == 6 and len(set(walked)) == 6


async def test_images_use_glances_own_link_shape(api: dict[str, Any], cloud: Any) -> None:
    """Glance answers with a flat ``next`` path, not a ``<collection>_links`` block."""
    body = (await api["glance"].get("/v2/images?limit=1")).json()
    assert body["first"] == "/v2/images"
    assert body["next"].startswith("/v2/images?")
    assert "images_links" not in body

    seen, url, pages = [], body["next"], 0
    seen += [i["id"] for i in body["images"]]
    while url and pages < 20:
        page = (await api["glance"].get(url)).json()
        seen += [i["id"] for i in page["images"]]
        url = page.get("next")
        pages += 1
    assert len(seen) == len(set(seen)) == 2  # the two seeded images


async def test_last_page_has_no_next(api: dict[str, Any], cloud: Any) -> None:
    body = (await api["glance"].get("/v2/images?limit=50")).json()
    assert "next" not in body
    assert body["first"] == "/v2/images"
