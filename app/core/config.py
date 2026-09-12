"""Global configuration: host hardware envelope, overcommit ratios, billing rates,
service catalog layout and small shared helpers (time, ids, transition windows)."""
from __future__ import annotations

import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


DEFAULT_DATABASE = "openstack_simulator.db"

# Anything before "://" is a scheme, so a value carrying one is already a full url.
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def resolve_database_url(value: str) -> str:
    """Turn a ``--database`` argument into a SQLAlchemy url.

    Three shapes are accepted, because all three are things people reach for:
    a full url (``postgresql+asyncpg://...``) is passed through untouched, ``:memory:``
    becomes an ephemeral database, and anything else is a SQLite file path. A bare name
    with no extension picks up ``.db``, so ``--database prod`` and ``--database prod.db``
    land on the same file rather than quietly on two different ones.
    """
    value = value.strip()
    if not value:
        raise ValueError("database must not be empty")
    if _URL_SCHEME.match(value):
        return value
    if value in (":memory:", "memory"):
        return "sqlite+aiosqlite:///:memory:"
    path = Path(value).expanduser()
    if not path.suffix:
        path = path.with_suffix(".db")
    return f"sqlite+aiosqlite:///{path}"


def database_path(url: str | None = None) -> Path | None:
    """The file behind a SQLite url, or None for in-memory and non-SQLite databases."""
    url = settings.database_url if url is None else url
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    location = url.partition("://")[2].partition("?")[0]
    # SQLite urls carry the path after the host slot, so "sqlite:///x.db" is relative to
    # the working directory and "sqlite:////x.db" is the absolute /x.db.
    return Path(location[1:] if location.startswith("//") else location.lstrip("/"))


def database_label(url: str | None = None) -> str:
    """Short name for whichever database is in use -- for banners and status output.

    Credentials can ride in a non-SQLite url, so those are reported by backend name
    only rather than echoed to a terminal or a log file.
    """
    url = settings.database_url if url is None else url
    if ":memory:" in url:
        return ":memory: (nothing is persisted)"
    path = database_path(url)
    if path is None:
        return url.partition("://")[0]
    return str(path)


class Settings(BaseModel):
    """Runtime knobs. Every field can be overridden with an ``OPENSTACK_SIMULATOR_*`` env var."""

    # -- process / networking ---------------------------------------------------------
    bind_host: str = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_BIND_HOST", "0.0.0.0"))
    advertise_host: str = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_ADVERTISE_HOST", "127.0.0.1")
    )

    # -- persistence ------------------------------------------------------------------
    # DATABASE_URL is the full SQLAlchemy url; DATABASE is the friendly form ("dev.db",
    # "~/clouds/prod.db", ":memory:") that --database also accepts. The url wins when
    # both are set, since it is the more specific of the two.
    database_url: str = Field(
        default_factory=lambda: _env(
            "OPENSTACK_SIMULATOR_DATABASE_URL",
            resolve_database_url(_env("OPENSTACK_SIMULATOR_DATABASE", DEFAULT_DATABASE)),
        )
    )
    sql_echo: bool = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_SQL_ECHO", "0") == "1")

    # -- bare-metal host envelope (node-01) -------------------------------------------
    host_name: str = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_HOST_NAME", "node-01"))
    host_sockets: int = Field(default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_HOST_SOCKETS", 2))
    host_cores: int = Field(default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_HOST_CORES", 32))
    host_threads: int = Field(default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_HOST_THREADS", 64))
    host_ram_mb: int = Field(default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_HOST_RAM_MB", 262144))
    host_disk_gb: int = Field(default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_HOST_DISK_GB", 4096))
    host_conntrack_max: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_CONNTRACK_MAX", 65536)
    )

    # -- depletion tuning -------------------------------------------------------------
    cpu_allocation_ratio: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_CPU_ALLOCATION_RATIO", 3.0)
    )
    ram_allocation_ratio: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RAM_ALLOCATION_RATIO", 1.0)
    )
    disk_allocation_ratio: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_DISK_ALLOCATION_RATIO", 1.0)
    )
    # Per-VM QEMU/libvirt process overhead charged on top of the flavor RAM.
    qemu_overhead_mb: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_QEMU_OVERHEAD_MB", 256)
    )
    host_reserved_ram_mb: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_RESERVED_RAM_MB", 512)
    )
    host_reserved_disk_gb: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_RESERVED_DISK_GB", 0)
    )

    # -- stateless polling delays -----------------------------------------------------
    transition_min_seconds: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_TRANSITION_MIN", 10)
    )
    transition_max_seconds: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_TRANSITION_MAX", 60)
    )

    # -- identity ---------------------------------------------------------------------
    admin_project: str = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_ADMIN_PROJECT", "admin"))
    admin_user: str = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_ADMIN_USER", "admin"))
    admin_password: str = Field(default_factory=lambda: _env("OPENSTACK_SIMULATOR_ADMIN_PASSWORD", "secret"))
    token_expiry_hours: int = Field(
        default_factory=lambda: _env_int("OPENSTACK_SIMULATOR_TOKEN_EXPIRY_HOURS", 24)
    )
    require_auth: bool = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_REQUIRE_AUTH", "1") == "1"
    )

    # -- rating (CloudKitty), unit costs are per hour ----------------------------------
    rate_vcpu_hour: float = Field(default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_VCPU_HOUR", 0.02))
    rate_ram_gb_hour: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_RAM_GB_HOUR", 0.01)
    )
    rate_idle_multiplier: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_IDLE_MULTIPLIER", 0.25)
    )
    rate_volume_gb_hour: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_VOLUME_GB_HOUR", 0.0005)
    )
    rate_floating_ip_hour: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_FLOATING_IP_HOUR", 0.005)
    )
    rate_object_gb_hour: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_OBJECT_GB_HOUR", 0.0001)
    )
    rate_loadbalancer_hour: float = Field(
        default_factory=lambda: _env_float("OPENSTACK_SIMULATOR_RATE_LOADBALANCER_HOUR", 0.025)
    )

    # -- networking defaults -----------------------------------------------------------
    external_network_name: str = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_EXTERNAL_NETWORK", "public")
    )
    external_network_cidr: str = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_EXTERNAL_CIDR", "172.24.4.0/24")
    )
    private_network_name: str = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_PRIVATE_NETWORK", "private")
    )
    private_network_cidr: str = Field(
        default_factory=lambda: _env("OPENSTACK_SIMULATOR_PRIVATE_CIDR", "10.0.0.0/24")
    )

    @property
    def total_vcpus(self) -> int:
        """Physical thread count -- the un-overcommitted vCPU pool."""
        return self.host_threads


settings = Settings()

DOMAIN_ID = "default"
DOMAIN_NAME = "Default"

# --------------------------------------------------------------------------------------
# Ports / service catalog
# --------------------------------------------------------------------------------------

PORTS: dict[str, int] = {
    "keystone": 5000,
    "nova": 8774,
    "cinder": 8776,
    "glance": 9292,
    "neutron": 9696,
    "placement": 8778,
    "octavia": 9876,
    "swift": 8080,
    "cloudkitty": 8889,
    "scenarios": 8999,
    "dashboard": 10000,
}

# service key -> (catalog type, catalog name, url suffix template)
CATALOG_LAYOUT: list[tuple[str, str, str, str]] = [
    ("keystone", "identity", "keystone", "/v3"),
    ("nova", "compute", "nova", "/v2.1"),
    ("cinder", "volumev3", "cinderv3", "/v3/%(project_id)s"),
    ("cinder", "block-storage", "cinder", "/v3/%(project_id)s"),
    ("glance", "image", "glance", "/v2"),
    ("neutron", "network", "neutron", ""),
    ("placement", "placement", "placement", ""),
    ("octavia", "load-balancer", "octavia", ""),
    ("swift", "object-store", "swift", "/v1/AUTH_%(project_id)s"),
    ("cloudkitty", "rating", "cloudkitty", "/v1"),
]

# The microversion range each service advertises. app/core/microversion.py
# negotiates within it -- a request with no version header is served at the minimum,
# as a real deployment would serve it.
API_VERSIONS: dict[str, tuple[str, str, str]] = {
    # service key -> (service name used in OpenStack-API-Version, min, max)
    "nova": ("compute", "2.1", "2.79"),
    "cinder": ("volume", "3.0", "3.70"),
    "placement": ("placement", "1.0", "1.36"),
    "octavia": ("load-balancer", "2.0", "2.27"),
    "glance": ("image", "2.0", "2.15"),
}


def service_url(service: str, path: str = "") -> str:
    return f"http://{settings.advertise_host}:{PORTS[service]}{path}"


def build_catalog(project_id: str) -> list[dict[str, Any]]:
    """Full Keystone v3 service catalog pointing at the loopback simulator ports."""
    catalog: list[dict[str, Any]] = []
    for service_key, ctype, cname, suffix in CATALOG_LAYOUT:
        url = service_url(service_key, suffix % {"project_id": project_id})
        endpoints = [
            {
                "id": deterministic_id(f"endpoint-{cname}-{iface}"),
                "interface": iface,
                "region": "RegionOne",
                "region_id": "RegionOne",
                "url": url,
            }
            for iface in ("public", "internal", "admin")
        ]
        catalog.append(
            {
                "id": deterministic_id(f"service-{cname}"),
                "type": ctype,
                "name": cname,
                "endpoints": endpoints,
            }
        )
    return catalog


# --------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------


def now_utc() -> datetime:
    """Timezone-naive UTC "now" -- SQLite stores naive datetimes, we normalise on read."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def iso(value: datetime | None) -> str | None:
    """Nova/Glance flavour of ISO-8601: second precision with a trailing Z."""
    if value is None:
        return None
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_us(value: datetime | None) -> str | None:
    """Cinder/Neutron flavour of ISO-8601: microsecond precision, no suffix."""
    if value is None:
        return None
    return as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%f")


def gen_id() -> str:
    return str(uuid.uuid4())


def deterministic_id(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def gen_token() -> str:
    """32-character opaque UUID token as handed back in X-Subject-Token."""
    return uuid.uuid4().hex


def transition_deadline(minimum: int | None = None, maximum: int | None = None) -> datetime:
    """A random 10-60s deadline; while now() < deadline the resource reads as pending."""
    low = settings.transition_min_seconds if minimum is None else minimum
    high = settings.transition_max_seconds if maximum is None else maximum
    return now_utc() + timedelta(seconds=random.randint(low, high))


def transition_done(deadline: datetime | None) -> bool:
    return deadline is None or now_utc() >= deadline


def settle_transition(entity: Any, status_field: str = "status") -> str | None:
    """Apply a stored transition once its deadline has passed.

    Returns the new status if the entity just became ready (so the caller can apply
    whatever else that state implies), or None if it is still pending or had no
    transition armed. Clearing the deadline here is what stops a settled resource from
    being flipped a second time.
    """
    deadline = getattr(entity, "transition_until", None)
    if deadline is None or not transition_done(deadline):
        return None
    target = entity.transition_target or "ACTIVE"
    entity.transition_until = None
    entity.transition_target = None
    setattr(entity, status_field, target)
    return target
