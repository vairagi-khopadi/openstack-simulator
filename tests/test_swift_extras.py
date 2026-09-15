"""Swift bulk delete, COPY and expiring objects.

These are the Swift features a client reaches for at scale, and each one changes what a
*subsequent* request sees — which is the part a stub gets wrong. An expired object in
particular must stop being readable even though nothing has reaped it yet.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
async def account(api: dict[str, Any], cloud: Any) -> str:
    return f"AUTH_{cloud.project_id}"


async def _container(api: dict[str, Any], account: str, name: str) -> None:
    assert (await api["swift"].put(f"/v1/{account}/{name}")).status_code in (201, 202)


async def _object(
    api: dict[str, Any], account: str, container: str, name: str, body: bytes = b"data",
    headers: dict[str, str] | None = None,
) -> Any:
    return await api["swift"].put(
        f"/v1/{account}/{container}/{name}", content=body, headers=headers or {}
    )


# --------------------------------------------------------------------------------------
# Bulk delete
# --------------------------------------------------------------------------------------


async def test_bulk_delete_removes_many_objects(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "bulk")
    for index in range(5):
        await _object(api, account, "bulk", f"obj-{index}")

    body = "\n".join(f"/bulk/obj-{i}" for i in range(5))
    response = await api["swift"].post(f"/v1/{account}?bulk-delete=1", content=body)
    assert response.status_code == 200
    assert response.json()["Number Deleted"] == 5

    listing = (await api["swift"].get(f"/v1/{account}/bulk")).text
    assert listing.strip() == ""


async def test_bulk_delete_reports_misses_without_failing(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    """A partial failure still answers 200 with a summary, which is Swift's contract."""
    await _container(api, account, "partial")
    await _object(api, account, "partial", "real")

    response = await api["swift"].post(
        f"/v1/{account}?bulk-delete=1", content="/partial/real\n/partial/imaginary"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["Number Deleted"] == 1
    assert body["Number Not Found"] == 1
    assert body["Errors"] == [["/partial/imaginary", "404 Not Found"]]


async def test_account_post_without_the_flag_is_still_metadata(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    """The route is overloaded, so the plain metadata POST must keep working."""
    response = await api["swift"].post(
        f"/v1/{account}", headers={"X-Account-Meta-Team": "platform"}
    )
    assert response.status_code == 204
    head = await api["swift"].head(f"/v1/{account}")
    assert head.headers.get("x-account-meta-team") == "platform"


# --------------------------------------------------------------------------------------
# COPY
# --------------------------------------------------------------------------------------


async def test_copy_duplicates_an_object(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "src")
    await _container(api, account, "dst")
    original = await _object(api, account, "src", "file", b"payload")
    etag = original.headers["etag"]

    copied = await api["swift"].request(
        "COPY",
        f"/v1/{account}/src/file",
        headers={"Destination": "/dst/copy"},
    )
    assert copied.status_code == 201
    assert copied.headers["etag"] == etag

    head = await api["swift"].head(f"/v1/{account}/dst/copy")
    assert head.status_code == 200
    assert head.headers["etag"] == etag
    # The source is untouched.
    assert (await api["swift"].head(f"/v1/{account}/src/file")).status_code == 200


async def test_copy_carries_the_metadata(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "meta-src")
    await _container(api, account, "meta-dst")
    await _object(api, account, "meta-src", "f", headers={"X-Object-Meta-Colour": "red"})

    await api["swift"].request(
        "COPY", f"/v1/{account}/meta-src/f", headers={"Destination": "/meta-dst/f"}
    )
    head = await api["swift"].head(f"/v1/{account}/meta-dst/f")
    assert head.headers.get("x-object-meta-colour") == "red"


async def test_copy_needs_a_destination(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "nodest")
    await _object(api, account, "nodest", "f")
    refused = await api["swift"].request("COPY", f"/v1/{account}/nodest/f")
    assert refused.status_code == 412


async def test_copy_rejects_a_malformed_destination(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "badly")
    await _object(api, account, "badly", "f")
    refused = await api["swift"].request(
        "COPY", f"/v1/{account}/badly/f", headers={"Destination": "/justacontainer"}
    )
    assert refused.status_code == 412


async def test_copy_to_a_missing_container_is_404(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "have")
    await _object(api, account, "have", "f")
    refused = await api["swift"].request(
        "COPY", f"/v1/{account}/have/f", headers={"Destination": "/havenot/f"}
    )
    assert refused.status_code == 404


async def test_copying_a_missing_object_is_404(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "empty")
    refused = await api["swift"].request(
        "COPY", f"/v1/{account}/empty/ghost", headers={"Destination": "/empty/copy"}
    )
    assert refused.status_code == 404


# --------------------------------------------------------------------------------------
# Expiring objects
# --------------------------------------------------------------------------------------


async def test_x_delete_after_sets_an_expiry(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "ttl")
    await _object(api, account, "ttl", "later", headers={"X-Delete-After": "3600"})
    head = await api["swift"].head(f"/v1/{account}/ttl/later")
    assert head.status_code == 200
    assert int(head.headers["x-delete-at"]) > int(time.time())


async def test_an_expired_object_is_gone_on_read(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    """Swift's reaper runs on its own schedule; a read must not wait for it."""
    await _container(api, account, "expired")
    past = str(int(time.time()) - 60)
    await _object(api, account, "expired", "stale", headers={"X-Delete-At": past})

    assert (await api["swift"].head(f"/v1/{account}/expired/stale")).status_code == 404
    assert (await api["swift"].get(f"/v1/{account}/expired/stale")).status_code == 404


async def test_an_unexpired_object_is_untouched(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "fresh")
    future = str(int(time.time()) + 3600)
    await _object(api, account, "fresh", "keep", headers={"X-Delete-At": future})
    assert (await api["swift"].head(f"/v1/{account}/fresh/keep")).status_code == 200


async def test_an_expiry_can_be_set_and_removed_with_post(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    await _container(api, account, "mutable")
    await _object(api, account, "mutable", "f")

    await api["swift"].post(
        f"/v1/{account}/mutable/f", headers={"X-Delete-After": "3600"}
    )
    assert "x-delete-at" in (await api["swift"].head(f"/v1/{account}/mutable/f")).headers

    await api["swift"].post(
        f"/v1/{account}/mutable/f", headers={"X-Remove-Delete-At": "yes"}
    )
    assert "x-delete-at" not in (
        await api["swift"].head(f"/v1/{account}/mutable/f")
    ).headers


async def test_a_nonsense_expiry_is_ignored_rather_than_fatal(
    api: dict[str, Any], cloud: Any, account: str
) -> None:
    """Storing the object matters more than the header the client got wrong."""
    await _container(api, account, "garbage")
    created = await _object(
        api, account, "garbage", "f", headers={"X-Delete-After": "soon"}
    )
    assert created.status_code == 201
    assert (await api["swift"].head(f"/v1/{account}/garbage/f")).status_code == 200
