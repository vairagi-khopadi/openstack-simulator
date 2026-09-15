"""Seed the simulator database: bare-metal node, identity, catalog, flavors, images,
networks and a default security group.

Idempotent -- rerunning tops up anything missing. ``--reset`` starts from scratch.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    CATALOG_LAYOUT,
    DOMAIN_ID,
    PORTS,
    database_label,
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


async def seed_flavors(session: AsyncSession) -> None:
    for spec in FLAVORS:
        existing = (
            await session.execute(select(Flavor).where(Flavor.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is None:
            session.add(Flavor(description=f"Simulated {spec['name']}", **spec))


async def seed_images(session: AsyncSession, owner: str) -> None:
    for spec in IMAGES:
        existing = (
            await session.execute(select(Image).where(Image.name == spec["name"]))
        ).scalar_one_or_none()
        if existing is not None:
            continue
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
                properties=spec["properties"],
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


async def seed(reset: bool = False) -> None:
    schema = await init_db(drop=reset)
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
        print(f"  flavors       {', '.join(f['name'] for f in FLAVORS)}")
        print(f"  images        {', '.join(i['name'] for i in IMAGES)}")
        print(f"  networks      {settings.private_network_name} ({settings.private_network_cidr}), "
              f"{settings.external_network_name} ({settings.external_network_cidr})")
        print(f"  auth url      {service_url('keystone', '/v3')}")


async def _seed_and_close(reset: bool = False) -> None:
    """Seeding as a one-shot command: do the work, then let go of the engine.

    ``seed`` itself leaves the engine open, because ``main.py`` calls it in-process to
    populate an in-memory run -- and disposing there would close the one connection the
    whole database lives in.
    """
    try:
        await seed(reset=reset)
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
        "--version",
        action="version",
        version=f"OpenStack-Simulator {__version__} (database schema v{SCHEMA_VERSION})",
    )
    args = parser.parse_args(argv)
    if args.database:
        try:
            use_database(args.database)
        except (ValueError, OSError) as exc:
            print(f"Cannot use database {args.database!r}: {exc}", file=sys.stderr)
            return 1
    try:
        asyncio.run(_seed_and_close(reset=args.reset))
    except SchemaVersionError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
