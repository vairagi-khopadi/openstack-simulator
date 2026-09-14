"""Keystone groups and application credentials.

The load-bearing behaviours: a group's roles reach its members *through* membership
rather than being copied onto them, and an application credential's secret is readable
exactly once. Both are easy to fake in a way that passes a shallow test and teaches the
wrong thing to whatever is written against it.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.anyio


async def _group(api: dict[str, Any], name: str = "platform") -> str:
    created = await api["keystone"].post(
        "/v3/groups", json={"group": {"name": name, "description": "d"}}
    )
    return created.json()["group"]["id"]


async def _user(api: dict[str, Any], name: str) -> str:
    created = await api["keystone"].post(
        "/v3/users", json={"user": {"name": name, "password": "pw"}}
    )
    return created.json()["user"]["id"]


async def _role_id(api: dict[str, Any], name: str = "admin") -> str:
    roles = (await api["keystone"].get("/v3/roles")).json()["roles"]
    return [r for r in roles if r["name"] == name][0]["id"]


# --------------------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------------------


async def test_group_crud(api: dict[str, Any], cloud: Any) -> None:
    group_id = await _group(api, "ops")
    fetched = (await api["keystone"].get(f"/v3/groups/{group_id}")).json()["group"]
    assert fetched["name"] == "ops" and fetched["domain_id"] == "default"

    patched = await api["keystone"].patch(
        f"/v3/groups/{group_id}", json={"group": {"description": "renamed"}}
    )
    assert patched.json()["group"]["description"] == "renamed"

    assert (await api["keystone"].delete(f"/v3/groups/{group_id}")).status_code == 204
    assert (await api["keystone"].get(f"/v3/groups/{group_id}")).status_code == 404


async def test_duplicate_group_names_conflict(api: dict[str, Any], cloud: Any) -> None:
    await _group(api, "dupe")
    again = await api["keystone"].post("/v3/groups", json={"group": {"name": "dupe"}})
    assert again.status_code == 409


async def test_a_group_needs_a_name(api: dict[str, Any], cloud: Any) -> None:
    assert (await api["keystone"].post("/v3/groups", json={"group": {}})).status_code == 400


async def test_groups_are_listed_and_filtered(api: dict[str, Any], cloud: Any) -> None:
    await _group(api, "alpha")
    await _group(api, "beta")
    assert len((await api["keystone"].get("/v3/groups")).json()["groups"]) == 2
    filtered = (await api["keystone"].get("/v3/groups?name=alpha")).json()["groups"]
    assert [g["name"] for g in filtered] == ["alpha"]


async def test_membership_is_added_checked_and_removed(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = await _group(api)
    user_id = await _user(api, "joiner")

    assert (await api["keystone"].get(
        f"/v3/groups/{group_id}/users/{user_id}")).status_code == 404

    assert (await api["keystone"].put(
        f"/v3/groups/{group_id}/users/{user_id}")).status_code == 204
    assert (await api["keystone"].get(
        f"/v3/groups/{group_id}/users/{user_id}")).status_code == 204

    users = (await api["keystone"].get(f"/v3/groups/{group_id}/users")).json()["users"]
    assert [u["id"] for u in users] == [user_id]

    assert (await api["keystone"].delete(
        f"/v3/groups/{group_id}/users/{user_id}")).status_code == 204
    assert (await api["keystone"].get(f"/v3/groups/{group_id}/users")).json()["users"] == []


async def test_adding_a_member_twice_is_harmless(api: dict[str, Any], cloud: Any) -> None:
    group_id, user_id = await _group(api), await _user(api, "twice")
    await api["keystone"].put(f"/v3/groups/{group_id}/users/{user_id}")
    assert (await api["keystone"].put(
        f"/v3/groups/{group_id}/users/{user_id}")).status_code == 204
    users = (await api["keystone"].get(f"/v3/groups/{group_id}/users")).json()["users"]
    assert len(users) == 1


async def test_adding_a_missing_user_is_404(api: dict[str, Any], cloud: Any) -> None:
    group_id = await _group(api)
    assert (await api["keystone"].put(
        f"/v3/groups/{group_id}/users/ghost")).status_code == 404


async def test_a_group_role_reaches_its_members(api: dict[str, Any], cloud: Any) -> None:
    """The point of groups: the role is not copied onto the user, membership implies it."""
    group_id = await _group(api, "admins")
    user_id = await _user(api, "delegate")
    role_id = await _role_id(api, "admin")

    await api["keystone"].put(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )
    await api["keystone"].put(f"/v3/groups/{group_id}/users/{user_id}")

    token = await api["keystone"].post(
        "/v3/auth/tokens",
        json={"auth": {"identity": {"methods": ["password"],
                                    "password": {"user": {"id": user_id, "password": "pw"}}},
                       "scope": {"project": {"id": cloud.project_id}}}},
    )
    assert token.status_code == 201
    roles = [r["name"] for r in token.json()["token"]["roles"]]
    assert "admin" in roles


async def test_removing_a_user_from_the_group_takes_the_role_away(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = await _group(api, "temps")
    user_id = await _user(api, "temp")
    role_id = await _role_id(api, "admin")
    await api["keystone"].put(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )
    await api["keystone"].put(f"/v3/groups/{group_id}/users/{user_id}")
    await api["keystone"].delete(f"/v3/groups/{group_id}/users/{user_id}")

    token = await api["keystone"].post(
        "/v3/auth/tokens",
        json={"auth": {"identity": {"methods": ["password"],
                                    "password": {"user": {"id": user_id, "password": "pw"}}},
                       "scope": {"project": {"id": cloud.project_id}}}},
    )
    assert "admin" not in [r["name"] for r in token.json()["token"]["roles"]]


async def test_a_group_role_can_be_revoked(api: dict[str, Any], cloud: Any) -> None:
    group_id = await _group(api)
    role_id = await _role_id(api, "admin")
    await api["keystone"].put(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )
    assert (await api["keystone"].delete(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )).status_code == 204
    assert (await api["keystone"].delete(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )).status_code == 404


async def test_deleting_a_group_drops_its_assignments(
    api: dict[str, Any], cloud: Any
) -> None:
    group_id = await _group(api, "doomed")
    user_id = await _user(api, "survivor")
    role_id = await _role_id(api, "admin")
    await api["keystone"].put(
        f"/v3/projects/{cloud.project_id}/groups/{group_id}/roles/{role_id}"
    )
    await api["keystone"].put(f"/v3/groups/{group_id}/users/{user_id}")

    await api["keystone"].delete(f"/v3/groups/{group_id}")

    token = await api["keystone"].post(
        "/v3/auth/tokens",
        json={"auth": {"identity": {"methods": ["password"],
                                    "password": {"user": {"id": user_id, "password": "pw"}}},
                       "scope": {"project": {"id": cloud.project_id}}}},
    )
    assert "admin" not in [r["name"] for r in token.json()["token"]["roles"]]


# --------------------------------------------------------------------------------------
# Application credentials
# --------------------------------------------------------------------------------------


async def test_the_secret_is_returned_once(api: dict[str, Any], cloud: Any) -> None:
    """Its whole value is that it cannot be read back -- a simulator that showed it
    again would teach the opposite habit."""
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "ci"}},
    )
    assert created.status_code == 201
    body = created.json()["application_credential"]
    assert body["secret"]

    fetched = (await api["keystone"].get(
        f"/v3/users/{cloud.user_id}/application_credentials/{body['id']}"
    )).json()["application_credential"]
    assert "secret" not in fetched


async def test_a_supplied_secret_is_kept(api: dict[str, Any], cloud: Any) -> None:
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "chosen", "secret": "hunter2"}},
    )
    assert created.json()["application_credential"]["secret"] == "hunter2"


async def test_credentials_are_listed_and_deleted(api: dict[str, Any], cloud: Any) -> None:
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "temporary"}},
    )
    credential_id = created.json()["application_credential"]["id"]

    listed = (await api["keystone"].get(
        f"/v3/users/{cloud.user_id}/application_credentials"
    )).json()["application_credentials"]
    assert [c["id"] for c in listed] == [credential_id]
    assert all("secret" not in c for c in listed)

    assert (await api["keystone"].delete(
        f"/v3/users/{cloud.user_id}/application_credentials/{credential_id}"
    )).status_code == 204
    assert (await api["keystone"].get(
        f"/v3/users/{cloud.user_id}/application_credentials"
    )).json()["application_credentials"] == []


async def test_duplicate_names_for_one_user_conflict(
    api: dict[str, Any], cloud: Any
) -> None:
    await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "same"}},
    )
    again = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "same"}},
    )
    assert again.status_code == 409


async def test_unrestricted_defaults_to_false(api: dict[str, Any], cloud: Any) -> None:
    """A credential that cannot delegate cannot be used to mint another one."""
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "restricted"}},
    )
    assert created.json()["application_credential"]["unrestricted"] is False


async def test_an_expiry_can_be_set(api: dict[str, Any], cloud: Any) -> None:
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "expiring",
                                         "expires_at": "2030-01-01T00:00:00Z"}},
    )
    assert created.json()["application_credential"]["expires_at"].startswith("2030-01-01")


async def test_an_unparseable_expiry_is_rejected(api: dict[str, Any], cloud: Any) -> None:
    refused = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "bad", "expires_at": "next tuesday"}},
    )
    assert refused.status_code == 400


async def test_a_credential_for_a_missing_user_is_404(
    api: dict[str, Any], cloud: Any
) -> None:
    refused = await api["keystone"].post(
        "/v3/users/ghost/application_credentials",
        json={"application_credential": {"name": "orphan"}},
    )
    assert refused.status_code == 404


async def test_another_users_credential_is_not_found(
    api: dict[str, Any], cloud: Any
) -> None:
    """The id alone must not be enough; it has to belong to the user in the path."""
    created = await api["keystone"].post(
        f"/v3/users/{cloud.user_id}/application_credentials",
        json={"application_credential": {"name": "mine"}},
    )
    credential_id = created.json()["application_credential"]["id"]
    other = await _user(api, "someone-else")
    assert (await api["keystone"].get(
        f"/v3/users/{other}/application_credentials/{credential_id}"
    )).status_code == 404
