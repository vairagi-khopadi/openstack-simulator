"""Seed the simulator database: bare-metal node, identity, catalog, flavors, images,
networks and a default security group.

Idempotent -- rerunning tops up anything missing. ``--reset`` starts from scratch.
``--seed-data`` swaps the built-in flavor and image lists for ones read from a JSON file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    CATALOG_LAYOUT,
    DOMAIN_ID,
    PORTS,
    database_label,
    deterministic_hashes,
    deterministic_id,
    gen_id,
    service_url,
    settings,
)
from app import SCHEMA_VERSION, __version__
from app.core.database import SessionLocal, dispose_db, init_db, use_database
from app.core.schema import SchemaVersionError
from app.models.compute import Flavor, Hypervisor
from app.models.identity import Endpoint, Project, Role, RoleAssignment, Service, User
from app.models.network import Network, SecurityGroup, SecurityGroupRule, Subnet
from app.models.quota import UNLIMITED, Quota
from app.models.storage import Image, VolumeType
from app.services import quotas as quota_service
from app.services.networking import allocation_pool

FLAVORS: list[dict[str, Any]] = [
    {"id": "1", "name": "m1.tiny", "vcpus": 1, "ram": 512, "disk": 1},
    {"id": "2", "name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20},
    {"id": "3", "name": "m1.medium", "vcpus": 2, "ram": 4096, "disk": 40},
]

IMAGES: list[dict[str, Any]] = [
    {
        "name": "cirros",
        "min_ram": 128,
        "min_disk": 1,
        "size": 21430272,
        "disk_format": "qcow2",
        "properties": {"os_type": "linux", "os_distro": "cirros", "architecture": "x86_64"},
    },
    {
        "name": "ubuntu-24.04",
        "min_ram": 2048,
        "min_disk": 20,
        "size": 596513280,
        "disk_format": "qcow2",
        "properties": {"os_type": "linux", "os_distro": "ubuntu", "architecture": "x86_64"},
    },
]

ROLES = ("admin", "member", "reader")

# --------------------------------------------------------------------------------------
# --seed-data: flavors and images from a JSON file
# --------------------------------------------------------------------------------------
# The file is an object with a "flavors" and/or an "images" key, each a list of the same
# shape as the literals above:
#
#   {"flavors": [{"id": "4", "name": "m1.large", "vcpus": 4, "ram": 8192, "disk": 80}],
#    "images":  [{"name": "debian-12", "min_ram": 512, "min_disk": 10, "size": 1024,
#                 "disk_format": "qcow2", "properties": {"os_distro": "debian"}}]}
#
# A key that is present REPLACES that built-in list -- so a file with only "images" keeps
# m1.tiny and friends, and "flavors": [] seeds no flavors at all. Everything is checked
# before the first row is written, because a bad key would otherwise surface as a
# TypeError halfway through a transaction.

FLAVOR_FIELDS: dict[str, type | tuple[type, ...]] = {
    "id": str, "name": str, "vcpus": int, "ram": int, "disk": int, "ephemeral": int,
    "swap": int, "rxtx_factor": (int, float), "is_public": bool, "disabled": bool,
    "description": str, "extra_specs": dict,
}
FLAVOR_REQUIRED = ("name", "vcpus", "ram", "disk")

IMAGE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "name": str, "min_ram": int, "min_disk": int, "size": int, "disk_format": str,
    "properties": dict,
}
IMAGE_REQUIRED = ("name", "min_ram", "min_disk", "size", "disk_format")


def _check_specs(
    specs: Any,
    section: str,
    fields: dict[str, type | tuple[type, ...]],
    required: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Validate one section of a --seed-data file, reporting the first thing wrong."""
    if not isinstance(specs, list):
        raise ValueError(f"{section!r} must be a list, not {type(specs).__name__}")
    seen: set[str] = set()
    for index, spec in enumerate(specs):
        where = f"{section}[{index}]"
        if not isinstance(spec, dict):
            raise ValueError(f"{where} must be an object, not {type(spec).__name__}")
        for key in required:
            if key not in spec:
                raise ValueError(f"{where} is missing {key!r}")
        for key, value in spec.items():
            if key not in fields:
                raise ValueError(
                    f"{where} has unknown key {key!r}; "
                    f"allowed: {', '.join(sorted(fields))}"
                )
            # bool is an int subclass, so the second half keeps 'vcpus': true from
            # passing as an integer -- and keeps 'is_public': 1 from passing as a bool.
            expected = fields[key]
            if not isinstance(value, expected) or isinstance(value, bool) != (expected is bool):
                wanted = expected.__name__ if isinstance(expected, type) else "number"
                raise ValueError(
                    f"{where}[{key!r}] must be {wanted}, not {type(value).__name__}"
                )
        name = spec["name"]
        if name in seen:
            raise ValueError(f"{where} repeats the name {name!r}")
        seen.add(name)
    return specs


SeedSpecs = list[dict[str, Any]] | None


def load_seed_data(path: str | Path) -> tuple[SeedSpecs, SeedSpecs]:
    """Read a --seed-data file into (flavors, images); None means 'keep the built-in list'.

    Raises ``ValueError`` -- including for unreadable or malformed JSON -- with a message
    naming the offending entry, so the caller can print it and exit rather than traceback.
    """
    try:
        raw = Path(path).expanduser().read_text()
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"top level must be an object with 'flavors' and/or 'images', "
            f"not {type(data).__name__}"
        )
    unknown = set(data) - {"flavors", "images"}
    if unknown:
        raise ValueError(f"unknown top-level key(s): {', '.join(sorted(unknown))}")
    if not data:
        raise ValueError("no 'flavors' or 'images' key, so there is nothing to seed")
    flavors = images = None
    if "flavors" in data:
        flavors = _check_specs(data["flavors"], "flavors", FLAVOR_FIELDS, FLAVOR_REQUIRED)
    if "images" in data:
        images = _check_specs(data["images"], "images", IMAGE_FIELDS, IMAGE_REQUIRED)
    return flavors, images


async def seed_host(session: AsyncSession) -> Hypervisor:
    host = (
        await session.execute(
            select(Hypervisor).where(Hypervisor.hostname == settings.host_name)
        )
    ).scalar_one_or_none()
    if host is not None:
        return host
    host = Hypervisor(
        id=deterministic_id(f"hypervisor-{settings.host_name}"),
        hostname=settings.host_name,
        sockets=settings.host_sockets,
        cores=settings.host_cores,
        threads=settings.host_threads,
        vcpus=settings.host_threads,
        memory_mb=settings.host_ram_mb,
        local_gb=settings.host_disk_gb,
        cpu_allocation_ratio=settings.cpu_allocation_ratio,
        ram_allocation_ratio=settings.ram_allocation_ratio,
        disk_allocation_ratio=settings.disk_allocation_ratio,
        reserved_memory_mb=settings.host_reserved_ram_mb,
        reserved_disk_gb=settings.host_reserved_disk_gb,
        conntrack_max=settings.host_conntrack_max,
        host_ip=settings.advertise_host,
        cpu_info={
            "arch": "x86_64",
            "model": "Cascadelake-Server",
            "vendor": "Intel",
            "topology": {
                "sockets": settings.host_sockets,
                "cores": settings.host_cores // max(settings.host_sockets, 1),
                "threads": settings.host_threads // max(settings.host_cores, 1),
            },
            "features": ["vmx", "avx2", "aes", "sse4.2"],
        },
    )
    session.add(host)
    return host


async def seed_identity(session: AsyncSession) -> tuple[Project, User]:
    project = (
        await session.execute(
            select(Project).where(Project.name == settings.admin_project)
        )
    ).scalar_one_or_none()
    if project is None:
        project = Project(
            id=deterministic_id(f"project-{settings.admin_project}"),
            name=settings.admin_project,
            description="Bootstrap administrative project",
            domain_id=DOMAIN_ID,
        )
        session.add(project)
        await session.flush()

    roles: dict[str, Role] = {}
    for name in ROLES:
        role = (
            await session.execute(select(Role).where(Role.name == name))
        ).scalar_one_or_none()
        if role is None:
            role = Role(id=deterministic_id(f"role-{name}"), name=name)
            session.add(role)
        roles[name] = role
    await session.flush()

    user = (
        await session.execute(select(User).where(User.name == settings.admin_user))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            id=deterministic_id(f"user-{settings.admin_user}"),
            name=settings.admin_user,
            password=settings.admin_password,
            email=f"{settings.admin_user}@openstack-simulator.local",
            domain_id=DOMAIN_ID,
            default_project_id=project.id,
            description="Bootstrap administrator",
        )
        session.add(user)
        await session.flush()

    for name, role in roles.items():
        exists = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user.id,
                    RoleAssignment.project_id == project.id,
                    RoleAssignment.role_id == role.id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                RoleAssignment(user_id=user.id, project_id=project.id, role_id=role.id)
            )
    return project, user


async def seed_catalog(session: AsyncSession) -> None:
    """Persist the catalog Keystone advertises, one service + three interfaces each."""
    for service_key, ctype, cname, suffix in CATALOG_LAYOUT:
        service = (
            await session.execute(select(Service).where(Service.name == cname))
        ).scalar_one_or_none()
        if service is None:
            service = Service(
                id=deterministic_id(f"service-{cname}"),
                type=ctype,
                name=cname,
                description=(
                    f"Simulated {ctype} service on port "
                    f"{PORTS.get(service_key, 'not in this run')}"
                ),
            )
            session.add(service)
            await session.flush()
        for interface in ("public", "internal", "admin"):
            endpoint_id = deterministic_id(f"endpoint-{cname}-{interface}")
            if await session.get(Endpoint, endpoint_id) is not None:
                continue
            session.add(
                Endpoint(
                    id=endpoint_id,
                    service_id=service.id,
                    interface=interface,
                    url=service_url(service_key, suffix),
                )
            )


async def seed_flavors(session: AsyncSession, specs: list[dict[str, Any]] = FLAVORS) -> None:
    for spec in specs:
        existing = (
            await session.execute(select(Flavor).where(Flavor.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is None:
            # Spread second so a --seed-data entry can carry its own description.
            session.add(Flavor(**{"description": f"Simulated {spec['name']}", **spec}))


async def seed_images(
    session: AsyncSession, owner: str, specs: list[dict[str, Any]] = IMAGES
) -> None:
    for spec in specs:
        existing = (
            await session.execute(select(Image).where(Image.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        checksum, os_hash_value = deterministic_hashes(f"image-{spec['name']}")
        session.add(
            Image(
                id=deterministic_id(f"image-{spec['name']}"),
                name=spec["name"],
                owner=owner,
                status="active",
                visibility="public",
                container_format="bare",
                disk_format=spec["disk_format"],
                min_ram=spec["min_ram"],
                min_disk=spec["min_disk"],
                size=spec["size"],
                virtual_size=spec["size"],
                checksum=checksum,
                os_hash_algo="sha512",
                os_hash_value=os_hash_value,
                properties=spec.get("properties", {}),
            )
        )


async def seed_volume_types(session: AsyncSession) -> None:
    for name, is_default in (("__DEFAULT__", True), ("lvm-ssd", False)):
        existing = (
            await session.execute(select(VolumeType).where(VolumeType.name == name))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                VolumeType(
                    id=deterministic_id(f"volume-type-{name}"),
                    name=name,
                    description=f"Simulated {name} volume type",
                    is_default=is_default,
                    extra_specs={"volume_backend_name": "LVM_iSCSI"},
                )
            )


async def seed_networks(session: AsyncSession, project_id: str) -> None:
    specs = (
        (settings.private_network_name, settings.private_network_cidr, False),
        (settings.external_network_name, settings.external_network_cidr, True),
    )
    for name, cidr, external in specs:
        network = (
            await session.execute(select(Network).where(Network.name == name))
        ).scalar_one_or_none()
        if network is None:
            network = Network(
                id=deterministic_id(f"network-{name}"),
                name=name,
                project_id=project_id,
                external=external,
                shared=not external,
                description=f"Seeded {'external' if external else 'tenant'} network",
                provider_segmentation_id=1000 + len(name),
            )
            session.add(network)
            await session.flush()
        subnet = (
            await session.execute(select(Subnet).where(Subnet.network_id == network.id))
        ).scalars().first()
        if subnet is None:
            gateway, start, end = allocation_pool(cidr)
            session.add(
                Subnet(
                    id=deterministic_id(f"subnet-{name}"),
                    name=f"{name}-subnet",
                    network_id=network.id,
                    project_id=project_id,
                    cidr=cidr,
                    gateway_ip=gateway,
                    allocation_start=start,
                    allocation_end=end,
                    dns_nameservers=["1.1.1.1", "8.8.8.8"],
                )
            )


async def seed_security_group(session: AsyncSession, project_id: str) -> None:
    """The 'default' group: 2 egress + 2 remote-group ingress rules = 4 conntrack slots."""
    group = (
        await session.execute(
            select(SecurityGroup).where(
                SecurityGroup.name == "default", SecurityGroup.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if group is not None:
        return
    group = SecurityGroup(
        id=deterministic_id(f"secgroup-default-{project_id}"),
        name="default",
        description="Default security group",
        project_id=project_id,
    )
    session.add(group)
    await session.flush()
    for ethertype in ("IPv4", "IPv6"):
        session.add(
            SecurityGroupRule(
                id=gen_id(),
                security_group_id=group.id,
                project_id=project_id,
                direction="egress",
                ethertype=ethertype,
            )
        )
        session.add(
            SecurityGroupRule(
                id=gen_id(),
                security_group_id=group.id,
                project_id=project_id,
                direction="ingress",
                ethertype=ethertype,
                remote_group_id=group.id,
            )
        )


async def seed_quotas(session: AsyncSession, project_id: str) -> None:
    """Give the bootstrap admin project unlimited quotas.

    Quotas and capacity are two different ceilings, and this seeder decides which one the
    shipped cloud demonstrates. Upstream's defaults (10 instances, 20 cores, 50 GB RAM)
    would bind long before the node's 256 GB does, so the depletion model -- the thing
    this simulator exists to show -- would never be reached on a default install.

    Unlimited here is also what an operator actually does with the admin project. A
    project created afterwards gets the real defaults, so quota enforcement is one
    ``openstack quota set`` away.
    """
    for service in ("nova", "cinder", "neutron"):
        existing = await quota_service.overrides(session, service, project_id)
        for resource in quota_service.DEFAULTS[service]:
            if resource not in existing:
                session.add(
                    Quota(
                        id=deterministic_id(f"quota-{service}-{resource}-{project_id}"),
                        project_id=project_id,
                        service=service,
                        resource=resource,
                        hard_limit=UNLIMITED,
                    )
                )


async def seed(
    reset: bool = False, flavors: SeedSpecs = None, images: SeedSpecs = None
) -> None:
    flavors = FLAVORS if flavors is None else flavors
    images = IMAGES if images is None else images
    schema = await init_db(drop=reset)
    async with SessionLocal() as session:
        host = await seed_host(session)
        project, user = await seed_identity(session)
        await seed_catalog(session)
        await seed_flavors(session, flavors)
        await seed_images(session, project.id, images)
        await seed_volume_types(session)
        await seed_networks(session, project.id)
        await seed_security_group(session, project.id)
        await seed_quotas(session, project.id)
        await session.commit()

        print(f"Seeded OpenStack-Simulator {__version__} ({schema.summary()})")
        print(f"  database      {database_label()}")
        print(f"  node          {host.hostname}: {host.sockets} sockets / {host.cores} cores / "
              f"{host.threads} threads")
        print(f"                {host.memory_mb} MB RAM, {host.local_gb} GB disk, "
              f"{host.conntrack_max} conntrack entries")
        print(f"  overcommit    cpu x{host.cpu_allocation_ratio}, ram x{host.ram_allocation_ratio}"
              f" (+{settings.qemu_overhead_mb} MB per VM)")
        print(f"  project/user  {project.name}/{user.name} (password: {settings.admin_password})")
        print(f"  project id    {project.id}")
        print(f"  flavors       {', '.join(f['name'] for f in flavors) or '(none)'}")
        print(f"  images        {', '.join(i['name'] for i in images) or '(none)'}")
        print(f"  networks      {settings.private_network_name} ({settings.private_network_cidr}), "
              f"{settings.external_network_name} ({settings.external_network_cidr})")
        print(f"  auth url      {service_url('keystone', '/v3')}")


async def _seed_and_close(
    reset: bool = False, flavors: SeedSpecs = None, images: SeedSpecs = None
) -> None:
    """Seeding as a one-shot command: do the work, then let go of the engine.

    ``seed`` itself leaves the engine open, because ``main.py`` calls it in-process to
    populate an in-memory run -- and disposing there would close the one connection the
    whole database lives in.
    """
    try:
        await seed(reset=reset, flavors=flavors, images=images)
    finally:
        await dispose_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the OpenStack-Simulator database.")
    parser.add_argument(
        "--reset", action="store_true", help="drop every table before seeding"
    )
    parser.add_argument(
        "--database",
        "-D",
        metavar="PATH",
        help="database to seed: a SQLite file ('dev.db', 'prod', "
             "'~/clouds/staging.db'), ':memory:', or a full SQLAlchemy url. Seed each "
             "environment separately, with the same value you pass to main.py "
             f"(default: {settings.database_url})",
    )
    parser.add_argument(
        "--seed-data",
        "-S",
        metavar="PATH",
        help="JSON file of flavors and/or images to seed instead of the built-in ones: "
             '{"flavors": [{"name": "m1.large", "vcpus": 4, "ram": 8192, "disk": 80}], '
             '"images": [{"name": "debian-12", "min_ram": 512, "min_disk": 10, '
             '"size": 1024, "disk_format": "qcow2"}]}. A key you leave out keeps that '
             "built-in list",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"OpenStack-Simulator {__version__} (database schema v{SCHEMA_VERSION})",
    )
    args = parser.parse_args(argv)
    flavors = images = None
    if args.seed_data:
        try:
            flavors, images = load_seed_data(args.seed_data)
        except ValueError as exc:
            print(f"Cannot use seed data {args.seed_data!r}: {exc}", file=sys.stderr)
            return 1
    if args.database:
        try:
            use_database(args.database)
        except (ValueError, OSError) as exc:
            print(f"Cannot use database {args.database!r}: {exc}", file=sys.stderr)
            return 1
    try:
        asyncio.run(_seed_and_close(reset=args.reset, flavors=flavors, images=images))
    except SchemaVersionError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
