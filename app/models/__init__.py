"""Import every mapper so ``Base.metadata`` is complete before ``create_all``."""
from __future__ import annotations

from app.models.compute import (
    STATES_HOLDING_COMPUTE,
    STATES_HOLDING_DISK,
    Flavor,
    Hypervisor,
    Keypair,
    Server,
)
from app.models.failure import VALID_ACTIONS, VALID_SERVICES, FailureInjection
from app.models.identity import (
    Endpoint,
    Project,
    Role,
    RoleAssignment,
    Service,
    Token,
    User,
)
from app.models.loadbalancer import (
    HealthMonitor,
    Listener,
    LoadBalancer,
    Member,
    Pool,
)
from app.models.network import (
    FloatingIP,
    Network,
    Port,
    Router,
    SecurityGroup,
    SecurityGroupRule,
    Subnet,
)
from app.models.objectstore import Container, ObjectMetadata, SwiftAccount
from app.models.quota import UNLIMITED, Quota
from app.models.storage import (
    VOLUME_STATES_HOLDING_DISK,
    Image,
    Snapshot,
    Volume,
    VolumeAttachment,
    VolumeType,
)

__all__ = [
    "STATES_HOLDING_COMPUTE",
    "STATES_HOLDING_DISK",
    "VOLUME_STATES_HOLDING_DISK",
    "VALID_ACTIONS",
    "UNLIMITED",
    "VALID_SERVICES",
    "Container",
    "Endpoint",
    "FailureInjection",
    "Flavor",
    "FloatingIP",
    "HealthMonitor",
    "Hypervisor",
    "Image",
    "Keypair",
    "Listener",
    "LoadBalancer",
    "Member",
    "Network",
    "ObjectMetadata",
    "Pool",
    "Port",
    "Project",
    "Quota",
    "Role",
    "Router",
    "RoleAssignment",
    "SecurityGroup",
    "SecurityGroupRule",
    "Server",
    "Service",
    "Snapshot",
    "Subnet",
    "SwiftAccount",
    "Token",
    "User",
    "Volume",
    "VolumeAttachment",
    "VolumeType",
]
