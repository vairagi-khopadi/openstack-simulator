"""Simulated IPAM and port allocation.

This lives in the service layer because Nova and Octavia both need to bind ports, and
neither should have to reach into Neutron's API module to do it. Failures are raised as
domain errors; translating them into HTTP is each API module's job.
"""
from __future__ import annotations

import ipaddress
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import deterministic_id, gen_id
from app.models.network import (
    FloatingIP,
    Network,
    Port,
    SecurityGroup,
    SecurityGroupRule,
    Subnet,
)


class AddressPoolExhausted(Exception):
    """No address left in the subnet's allocation pool."""

    def __init__(self, subnet_id: str) -> None:
        self.subnet_id = subnet_id
        super().__init__(f"No more IP addresses available on subnet {subnet_id}.")


def gen_mac() -> str:
    return "fa:16:3e:%02x:%02x:%02x" % (
        random.randint(0, 255),
        random.randint(0, 255),
        random.randint(0, 255),
    )


def allocation_pool(cidr: str) -> tuple[str, str, str]:
    """(gateway, pool_start, pool_end) using the usual OpenStack convention."""
    net = ipaddress.ip_network(cidr, strict=False)
    hosts = list(net.hosts()) if net.num_addresses <= 65536 else None
    if hosts:
        gateway = str(hosts[0])
        # The gateway is never handed out, so the pool starts one past it unless the
        # network is so small that the gateway is the only address there is.
        start = str(hosts[1]) if len(hosts) > 1 else str(hosts[0])
        end = str(hosts[-1])
    else:  # very large network: derive arithmetically instead of materialising hosts
        base = int(net.network_address)
        gateway = str(ipaddress.ip_address(base + 1))
        start = str(ipaddress.ip_address(base + 2))
        end = str(ipaddress.ip_address(int(net.broadcast_address) - 1))
    return gateway, start, end


async def next_free_ip(session: AsyncSession, subnet: Subnet) -> str:
    """Hand out the next address from the pool, skipping anything already bound."""
    start = int(ipaddress.ip_address(subnet.allocation_start))
    end = int(ipaddress.ip_address(subnet.allocation_end))
    taken = set(
        (
            await session.execute(
                select(Port.ip_address).where(Port.subnet_id == subnet.id)
            )
        )
        .scalars()
        .all()
    )
    taken |= set(
        (
            await session.execute(
                select(FloatingIP.floating_ip_address).where(
                    FloatingIP.released.is_(False)
                )
            )
        )
        .scalars()
        .all()
    )
    candidate = start + subnet.next_ip_offset
    while candidate <= end:
        address = str(ipaddress.ip_address(candidate))
        if address not in taken:
            subnet.next_ip_offset = candidate - start + 1
            return address
        candidate += 1
    raise AddressPoolExhausted(subnet.id)


async def pick_network(
    session: AsyncSession, project_id: str, network_id: str | None = None
) -> Network | None:
    """Resolve an explicit network id, else the project's first internal network."""
    if network_id:
        return await session.get(Network, network_id)
    stmt = (
        select(Network)
        .where(Network.external.is_(False))
        .where((Network.project_id == project_id) | (Network.shared.is_(True)))
        .order_by(Network.created_at)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_port_record(
    session: AsyncSession,
    network: Network,
    project_id: str,
    device_id: str = "",
    device_owner: str = "",
    name: str = "",
    security_group_ids: list[str] | None = None,
    fixed_ip: str | None = None,
) -> Port:
    """Create a port on the network's first subnet with an allocated IP + MAC."""
    subnet = (
        await session.execute(
            select(Subnet).where(Subnet.network_id == network.id).order_by(Subnet.created_at)
        )
    ).scalars().first()
    ip_address = fixed_ip
    if subnet is not None and ip_address is None:
        ip_address = await next_free_ip(session, subnet)
    port = Port(
        id=gen_id(),
        name=name,
        network_id=network.id,
        subnet_id=subnet.id if subnet else None,
        project_id=project_id,
        mac_address=gen_mac(),
        ip_address=ip_address,
        device_id=device_id,
        device_owner=device_owner,
        security_group_ids=security_group_ids or [],
        status="ACTIVE" if device_id else "DOWN",
    )
    session.add(port)
    return port


async def ensure_default_security_group(
    session: AsyncSession, project_id: str
) -> SecurityGroup:
    """Return the project's ``default`` group, creating it the first time it is needed.

    Neutron gives every project a ``default`` group with two egress rules and two
    ingress rules from the group itself; it materialises on first use rather than at
    project creation, which is why this is a lookup-or-create rather than something the
    identity API does. Only the bootstrap project used to get one, so a project created
    through the API had no ``default`` to boot against and a client counting on it saw
    one group where a real cloud has two.
    """
    group = (
        await session.execute(
            select(SecurityGroup).where(
                SecurityGroup.name == "default", SecurityGroup.project_id == project_id
            )
        )
    ).scalars().first()
    if group is not None:
        return group
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
    await session.flush()
    return group


async def resolve_security_groups(
    session: AsyncSession, references: list[str], project_id: str
) -> list[SecurityGroup]:
    """Resolve boot-time security group references -- a name or a UUID, as Nova does.

    Nova accepts either spelling and reports the group by *name* afterwards, so a client
    that passes UUIDs (the unambiguous choice under an admin identity, where a name can
    match another tenant's group) still reads its own group's name back. Unknown
    references raise ``KeyError`` for the caller to turn into the service's own 400.
    """
    groups: list[SecurityGroup] = []
    for reference in references:
        group = (
            await session.execute(
                select(SecurityGroup).where(
                    SecurityGroup.name == reference,
                    SecurityGroup.project_id == project_id,
                )
            )
        ).scalars().first()
        if group is None:
            candidate = await session.get(SecurityGroup, reference)
            if candidate is not None and candidate.project_id == project_id:
                group = candidate
        if group is None:
            if reference == "default":
                group = await ensure_default_security_group(session, project_id)
            else:
                raise KeyError(reference)
        if group.id not in {g.id for g in groups}:
            groups.append(group)
    return groups
