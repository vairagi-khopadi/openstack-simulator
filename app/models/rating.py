"""CloudKitty rating configuration -- the hashmap module's rules.

Rates were settable only through ``OPENSTACK_SIMULATOR_RATE_*`` environment variables,
which means a restart to change a price and no way at all to price one flavor differently
from another. The hashmap module is how a real CloudKitty is configured, and these are its
rules.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import gen_id, now_utc
from app.core.database import Base

# What a hashmap entry can be. A service holds fields; both hold mappings and thresholds.
KINDS = ("service", "field", "mapping", "threshold", "group")

# flat adds its cost once; rate multiplies what is already there.
MAP_TYPES = ("flat", "rate")


class HashMapEntry(Base):
    """One node of the hashmap configuration tree.

    A *service* (compute, volume, ...) holds *fields* (flavor_id, volume_type, ...), and
    either can carry *mappings* -- the actual rates -- and *thresholds*. One table with a
    ``kind`` and a parent covers all of them rather than four near-identical ones, because
    every kind is addressed and nested the same way.
    """

    __tablename__ = "hashmap_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Set on mappings and thresholds.
    value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    map_type: Mapped[str] = mapped_column(String(32), default="flat")
    level: Mapped[float | None] = mapped_column(Float, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
