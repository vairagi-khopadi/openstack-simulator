"""Shared test harness.

Everything runs in-process: httpx drives the ASGI apps directly (no ports, no background
server) against a single in-memory SQLite database that is rebuilt for every test.

The 10-60s transition windows are collapsed to zero by default so the suite finishes in
seconds; ``slow_transitions`` restores a real window for the tests that need to observe a
pending state, and ``expire_transition`` rewinds a stored deadline instead of sleeping.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

# The app reads its configuration at import time, so the environment must be set first.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENSTACK_SIMULATOR_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("OPENSTACK_SIMULATOR_REQUIRE_AUTH", "1")
os.environ.setdefault("OPENSTACK_SIMULATOR_ADVERTISE_HOST", "127.0.0.1")

import contextlib  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402

import main  # noqa: E402
from app.core.config import API_VERSIONS, now_utc, settings  # noqa: E402
from app.core.database import Base, SessionLocal, get_engine  # noqa: E402
from app.core.middleware import invalidate_scenario_cache  # noqa: E402
from seed import (  # noqa: E402
    seed_catalog,
    seed_flavors,
    seed_host,
    seed_identity,
    seed_images,
    seed_networks,
    seed_quotas,
    seed_security_group,
    seed_volume_types,
)

pytest_plugins: list[str] = []


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@dataclass(slots=True)
class Cloud:
    """Identifiers of the seeded fixtures, so tests don't have to re-query them."""

    project_id: str
    project_name: str
    user_id: str
    host_id: str
    host_name: str


@pytest.fixture(scope="session")
def apps() -> dict[str, Any]:
    """The real app wiring from main.py, including the Cinder and Octavia mounts."""
    return main.build_apps()


@pytest.fixture(autouse=True)
async def fresh_db() -> Any:
    """A clean schema per test, plus deterministic (instant) transitions."""
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    invalidate_scenario_cache()
    settings.transition_min_seconds = 0
    settings.transition_max_seconds = 0
    yield
    invalidate_scenario_cache()


@pytest.fixture
def slow_transitions() -> Any:
    """Restore a long window so pending states stay observable during a test."""
    settings.transition_min_seconds = 60
    settings.transition_max_seconds = 60
    yield
    settings.transition_min_seconds = 0
    settings.transition_max_seconds = 0


@pytest.fixture
async def cloud() -> Cloud:
    """Seed the node, identity, catalog, flavors, images, networks and default group."""
    async with SessionLocal() as session:
        host = await seed_host(session)
        project, user = await seed_identity(session)
        await seed_catalog(session)
        await seed_flavors(session)
        await seed_images(session, project.id)
        await seed_volume_types(session)
        await seed_networks(session, project.id)
        await seed_security_group(session, project.id)
        await seed_quotas(session, project.id)
        await session.commit()
        return Cloud(
            project_id=project.id,
            project_name=project.name,
            user_id=user.id,
            host_id=host.id,
            host_name=host.hostname,
        )


@pytest.fixture
async def raw_clients(apps: dict[str, Any]) -> Any:
    """One unauthenticated httpx client per service, bound straight to its ASGI app."""
    async with contextlib.AsyncExitStack() as stack:
        clients = {
            name: await stack.enter_async_context(
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url=f"http://{name}.sim",
                    timeout=30.0,
                )
            )
            for name, app in apps.items()
        }
        yield clients


@pytest.fixture
async def token(raw_clients: dict[str, httpx.AsyncClient], cloud: Cloud) -> str:
    response = await raw_clients["keystone"].post(
        "/v3/auth/tokens",
        json={
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": settings.admin_user,
                            "domain": {"name": "Default"},
                            "password": settings.admin_password,
                        }
                    },
                },
                "scope": {"project": {"name": settings.admin_project,
                                      "domain": {"name": "Default"}}},
            }
        },
    )
    assert response.status_code == 201, response.text
    return response.headers["X-Subject-Token"]


@pytest.fixture
async def api(raw_clients: dict[str, httpx.AsyncClient], token: str) -> Any:
    """Authenticated clients, keyed by service name, pinned to the newest microversion.

    Pinning matters now that negotiation is real: a client that sends no version header
    gets the service *minimum*, as it would from a real deployment, and the modern
    response shape most tests assert on would simply not be there. ``openrc.sh`` pins the
    same versions, so the suite and the shipped credentials agree.

    Tests that want an older shape override the header on the one request, and
    ``raw_clients`` stays unpinned for tests about the default.
    """
    for name, client in raw_clients.items():
        client.headers["X-Auth-Token"] = token
        entry = API_VERSIONS.get(name)
        if entry is not None:
            service_type, _, maximum = entry
            client.headers["OpenStack-API-Version"] = f"{service_type} {maximum}"
    return raw_clients


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


async def expire_transition(model: Any, resource_id: str, seconds: int = 120) -> None:
    """Rewind a stored deadline into the past -- tests the state machine without sleeping."""
    async with SessionLocal() as session:
        await session.execute(
            update(model)
            .where(model.id == resource_id)
            .values(transition_until=now_utc() - timedelta(seconds=seconds))
        )
        await session.commit()


@pytest.fixture
def expire() -> Any:
    return expire_transition
