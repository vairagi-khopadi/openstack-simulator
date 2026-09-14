"""Per-project quota overrides.

A quota is not capacity. The node's envelope (``app/services/capacity.py``) is physics --
shared by everyone, and the same however many projects exist. A quota is policy: a number
an admin sets for one project, which binds long before the hardware does.

Only *overrides* live here. A project with no row for a resource uses the service default,
which is what ``DELETE /os-quota-sets/{project}`` goes back to -- so an untouched cloud
stores nothing at all, and "has this been changed?" is answerable by the row existing.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import gen_id, now_utc
from app.core.database import Base

# A quota of -1 is unlimited everywhere in OpenStack -- not "none".
UNLIMITED = -1


class Quota(Base):
    """One project's limit for one resource of one service."""

    __tablename__ = "quotas"
    __table_args__ = (
        # One limit per (project, service, resource); a PUT updates rather than stacks.
        UniqueConstraint("project_id", "service", "resource", name="uq_quota_scope"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=gen_id)
    project_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # Which service owns this limit. Nova, Cinder and Neutron each keep their own
    # quotas for their own resources; there is no central quota service to defer to.
    service: Mapped[str] = mapped_column(String, index=True, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    hard_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc
    )
