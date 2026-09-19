"""Keystone Identity v3 API tests."""
from __future__ import annotations

import pytest

from app.core.database import SessionLocal
from app.models.identity import RoleAssignment

pytestmark = pytest.mark.anyio

PASSWORD_AUTH = {
    "auth": {
        "identity": {
            "methods": ["password"],
            "password": {"user": {"name": "admin", "domain": {"name": "Default"},
                                  "password": "secret"}},
        },
        "scope": {"project": {"name": "admin", "domain": {"name": "Default"}}},
    }
}


async def test_root_offers_multiple_choices(raw_clients) -> None:
    response = await raw_clients["keystone"].get("/")
    assert response.status_code == 300
    assert response.json()["versions"]["values"][0]["id"].startswith("v3")


async def test_v3_version_document(raw_clients) -> None:
    body = (await raw_clients["keystone"].get("/v3")).json()
    assert body["version"]["status"] == "stable"


async def test_password_auth_issues_a_scoped_token(raw_clients, cloud) -> None:
    response = await raw_clients["keystone"].post("/v3/auth/tokens", json=PASSWORD_AUTH)
    assert response.status_code == 201
    subject = response.headers["X-Subject-Token"]
    assert len(subject) == 32
    body = response.json()["token"]
    assert body["project"]["id"] == cloud.project_id
    assert body["user"]["name"] == "admin"
    assert {r["name"] for r in body["roles"]} == {"admin", "member", "reader"}
    assert body["expires_at"] > body["issued_at"]


async def test_catalog_covers_every_simulated_service(raw_clients, cloud) -> None:
    body = (await raw_clients["keystone"].post("/v3/auth/tokens", json=PASSWORD_AUTH)).json()
    catalog = body["token"]["catalog"]
    types = {entry["type"] for entry in catalog}
    assert types == {"identity", "compute", "volumev3", "block-storage", "image",
                     "network", "placement", "load-balancer", "object-store", "rating"}
    for entry in catalog:
        assert {e["interface"] for e in entry["endpoints"]} == {"public", "internal", "admin"}
    cinder = next(e for e in catalog if e["type"] == "volumev3")
    assert cinder["endpoints"][0]["url"].endswith(f"/v3/{cloud.project_id}")
    swift = next(e for e in catalog if e["type"] == "object-store")
    assert swift["endpoints"][0]["url"].endswith(f"/v1/AUTH_{cloud.project_id}")


async def test_wrong_password_is_rejected(raw_clients, cloud) -> None:
    payload = {"auth": {"identity": {"methods": ["password"], "password": {
        "user": {"name": "admin", "domain": {"name": "Default"}, "password": "nope"}}}}}
    response = await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == 401


async def test_unknown_user_is_rejected(raw_clients, cloud) -> None:
    payload = {"auth": {"identity": {"methods": ["password"], "password": {
        "user": {"name": "ghost", "domain": {"name": "Default"}, "password": "x"}}}}}
    assert (await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)).status_code == 401


async def test_unscoped_auth_falls_back_to_the_default_project(raw_clients, cloud) -> None:
    payload = {"auth": {"identity": {"methods": ["password"], "password": {
        "user": {"name": "admin", "domain": {"name": "Default"}, "password": "secret"}}}}}
    body = (await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)).json()
    assert body["token"]["project"]["id"] == cloud.project_id


async def test_token_can_be_exchanged_for_another_token(raw_clients, token) -> None:
    payload = {"auth": {"identity": {"methods": ["token"], "token": {"id": token}}}}
    response = await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)
    assert response.status_code == 201
    assert response.headers["X-Subject-Token"] != token


async def test_scope_to_a_missing_project_is_rejected(raw_clients, cloud) -> None:
    payload = dict(PASSWORD_AUTH)
    payload = {"auth": {**PASSWORD_AUTH["auth"], "scope": {"project": {"name": "nope"}}}}
    assert (await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)).status_code == 401


async def test_token_validation_and_revocation(raw_clients, token) -> None:
    client = raw_clients["keystone"]
    headers = {"X-Auth-Token": token, "X-Subject-Token": token}
    assert (await client.get("/v3/auth/tokens", headers=headers)).status_code == 200
    assert (await client.head("/v3/auth/tokens", headers=headers)).status_code == 200
    assert (await client.delete("/v3/auth/tokens", headers=headers)).status_code == 204
    # Once revoked the token no longer validates, and no longer opens other services.
    assert (await client.get("/v3/auth/tokens", headers=headers)).status_code == 404
    nova = raw_clients["nova"]
    assert (await nova.get("/v2.1/servers", headers={"X-Auth-Token": token})).status_code == 401


async def test_validation_can_omit_the_catalog(raw_clients, token) -> None:
    headers = {"X-Auth-Token": token, "X-Subject-Token": token}
    body = (await raw_clients["keystone"].get("/v3/auth/tokens?nocatalog", headers=headers)).json()
    assert "catalog" not in body["token"]


async def test_validating_an_unknown_token_is_a_404(raw_clients, token) -> None:
    headers = {"X-Auth-Token": token, "X-Subject-Token": "0" * 32}
    assert (await raw_clients["keystone"].get("/v3/auth/tokens", headers=headers)).status_code == 404


async def test_project_crud(api) -> None:
    client = api["keystone"]
    created = await client.post("/v3/projects", json={"project": {"name": "dev",
                                                                  "description": "team"}})
    assert created.status_code == 201
    project_id = created.json()["project"]["id"]

    assert (await client.get(f"/v3/projects/{project_id}")).json()["project"]["name"] == "dev"
    listed = (await client.get("/v3/projects?name=dev")).json()["projects"]
    assert [p["id"] for p in listed] == [project_id]

    patched = await client.patch(f"/v3/projects/{project_id}",
                                 json={"project": {"description": "renamed"}})
    assert patched.json()["project"]["description"] == "renamed"

    assert (await client.delete(f"/v3/projects/{project_id}")).status_code == 204
    assert (await client.get(f"/v3/projects/{project_id}")).status_code == 404


async def test_duplicate_project_name_conflicts(api) -> None:
    client = api["keystone"]
    await client.post("/v3/projects", json={"project": {"name": "dup"}})
    conflict = await client.post("/v3/projects", json={"project": {"name": "dup"}})
    assert conflict.status_code == 409


async def test_user_crud_and_role_grant(api, cloud) -> None:
    client = api["keystone"]
    created = await client.post("/v3/users", json={"user": {
        "name": "alice", "password": "pw", "email": "a@example.com",
        "default_project_id": cloud.project_id}})
    assert created.status_code == 201
    user_id = created.json()["user"]["id"]
    assert created.json()["user"]["email"] == "a@example.com"

    assert (await client.get(f"/v3/users/{user_id}")).json()["user"]["name"] == "alice"
    assert [u["id"] for u in (await client.get("/v3/users?name=alice")).json()["users"]] == [user_id]

    patched = await client.patch(f"/v3/users/{user_id}", json={"user": {"enabled": False}})
    assert patched.json()["user"]["enabled"] is False

    role_id = (await client.get("/v3/roles?name=member")).json()["roles"][0]["id"]
    granted = await client.put(
        f"/v3/projects/{cloud.project_id}/users/{user_id}/roles/{role_id}")
    assert granted.status_code == 204
    projects = (await client.get(f"/v3/users/{user_id}/projects")).json()["projects"]
    assert [p["id"] for p in projects] == [cloud.project_id]

    assert (await client.delete(f"/v3/users/{user_id}")).status_code == 204
    assert (await client.get(f"/v3/users/{user_id}")).status_code == 404


async def test_new_user_can_authenticate(raw_clients, api, cloud) -> None:
    await api["keystone"].post("/v3/users", json={"user": {
        "name": "bob", "password": "hunter2", "default_project_id": cloud.project_id}})
    payload = {"auth": {"identity": {"methods": ["password"], "password": {
        "user": {"name": "bob", "domain": {"name": "Default"}, "password": "hunter2"}}}}}
    response = await raw_clients["keystone"].post("/v3/auth/tokens", json=payload)
    assert response.status_code == 201
    assert response.json()["token"]["user"]["name"] == "bob"


async def test_roles_and_assignments(api, cloud) -> None:
    client = api["keystone"]
    roles = (await client.get("/v3/roles")).json()["roles"]
    assert {r["name"] for r in roles} == {"admin", "member", "reader"}

    created = await client.post("/v3/roles", json={"role": {"name": "auditor"}})
    assert created.status_code == 201

    assignments = (await client.get("/v3/role_assignments")).json()["role_assignments"]
    assert len(assignments) == 3
    assert all(a["scope"]["project"]["id"] == cloud.project_id for a in assignments)


async def test_role_assignment_filters(api, cloud) -> None:
    """The CLI sends these as dotted query params; ignoring them returns another
    project's rows, which reads as a real answer rather than as a missing filter."""
    client = api["keystone"]
    roles = {r["name"]: r["id"] for r in (await client.get("/v3/roles")).json()["roles"]}
    empty = await client.post("/v3/projects", json={"project": {"name": "empty-proj"}})
    empty_id = empty.json()["project"]["id"]

    async def listed(**params: str) -> list[dict]:
        response = await client.get("/v3/role_assignments", params=params)
        assert response.status_code == 200
        return response.json()["role_assignments"]

    assert await listed(**{"scope.project.id": empty_id}) == []
    assert await listed(**{"user.id": "no-such-user"}) == []
    assert len(await listed(**{
        "scope.project.id": cloud.project_id, "user.id": cloud.user_id})) == 3
    assert [r["role"]["id"] for r in await listed(**{"role.id": roles["reader"]})] == [
        roles["reader"]
    ]

    # Assignments are only ever project-scoped here, so a domain- or system-scoped
    # query matches nothing rather than falling back to the project list.
    assert await listed(**{"scope.domain.id": "default"}) == []
    assert await listed(**{"scope.system": "all"}) == []


async def test_role_assignment_include_names(api, cloud) -> None:
    """``--names`` reads scope.project.name straight off the body: an id-only
    response makes the client raise KeyError instead of printing a table."""
    client = api["keystone"]
    user_name = (await client.get(f"/v3/users/{cloud.user_id}")).json()["user"]["name"]

    response = await client.get("/v3/role_assignments", params={
        "scope.project.id": cloud.project_id, "include_names": "True"})
    rows = response.json()["role_assignments"]
    assert len(rows) == 3
    for row in rows:
        assert row["role"]["name"]
        assert row["scope"]["project"]["name"] == cloud.project_name
        assert row["scope"]["project"]["domain"]["name"] == "Default"
        assert row["user"]["name"] == user_name
        assert row["user"]["domain"]["name"] == "Default"

    # Omitting it keeps the id-only shape the rest of the suite expects.
    plain = (await client.get("/v3/role_assignments")).json()["role_assignments"]
    assert all("name" not in r["scope"]["project"] for r in plain)


async def test_effective_expands_group_assignments(api, cloud) -> None:
    """A role held through a group only shows up against the user under ``effective``."""
    client = api["keystone"]
    auditor = (await client.post(
        "/v3/roles", json={"role": {"name": "auditor"}})).json()["role"]["id"]
    group = (await client.post(
        "/v3/groups", json={"group": {"name": "ops"}})).json()["group"]
    assert (await client.put(
        f"/v3/groups/{group['id']}/users/{cloud.user_id}")).status_code == 204

    async with SessionLocal() as session:
        session.add(RoleAssignment(
            group_id=group["id"], project_id=cloud.project_id, role_id=auditor))
        await session.commit()

    direct = (await client.get("/v3/role_assignments", params={
        "user.id": cloud.user_id})).json()["role_assignments"]
    assert auditor not in [r["role"]["id"] for r in direct]

    effective = (await client.get("/v3/role_assignments", params={
        "user.id": cloud.user_id, "effective": "True"})).json()["role_assignments"]
    assert auditor in [r["role"]["id"] for r in effective]

    # Unexpanded, the assignment belongs to the group rather than to any user.
    grouped = (await client.get("/v3/role_assignments", params={
        "group.id": group["id"]})).json()["role_assignments"]
    assert [r["group"]["id"] for r in grouped] == [group["id"]]
    assert "user" not in grouped[0]


async def test_domains_and_regions(api) -> None:
    client = api["keystone"]
    domains = (await client.get("/v3/domains")).json()["domains"]
    assert domains[0]["id"] == "default" and domains[0]["name"] == "Default"
    assert (await client.get("/v3/domains/default")).status_code == 200
    assert (await client.get("/v3/domains/nope")).status_code == 404
    regions = (await client.get("/v3/regions")).json()["regions"]
    assert regions[0]["id"] == "RegionOne"


async def test_seeded_services_and_endpoints_are_listed(api, cloud) -> None:
    client = api["keystone"]
    services = (await client.get("/v3/services")).json()["services"]
    assert len(services) == 10
    assert (await client.get("/v3/services?type=compute")).json()["services"][0]["name"] == "nova"

    endpoints = (await client.get("/v3/endpoints")).json()["endpoints"]
    assert len(endpoints) == 30, "10 services x 3 interfaces"
    # Stored templates are rendered against the caller's project.
    object_store = [e for e in endpoints if "AUTH_" in e["url"]]
    assert object_store and all(cloud.project_id in e["url"] for e in object_store)


async def test_auth_helper_endpoints(api, cloud) -> None:
    client = api["keystone"]
    assert len((await client.get("/v3/auth/catalog")).json()["catalog"]) == 10
    projects = (await client.get("/v3/auth/projects")).json()["projects"]
    assert cloud.project_id in [p["id"] for p in projects]
    assert (await client.get("/v3/auth/domains")).json()["domains"][0]["id"] == "default"


async def test_protected_endpoints_require_a_token(raw_clients, cloud) -> None:
    response = await raw_clients["keystone"].get("/v3/projects")
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers
