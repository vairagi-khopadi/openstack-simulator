"""Keystone identity models: domains, projects, users, roles, tokens, catalog."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import DOMAIN_ID, gen_id, now_utc
from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    domain_id: Mapped[str] = mapped_column(String(64), default=DOMAIN_ID)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_domain: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    users: Mapped[list["User"]] = relationship(back_populates="project")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    password: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain_id: Mapped[str] = mapped_column(String(64), default=DOMAIN_ID)
    default_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)

    project: Mapped[Project | None] = relationship(back_populates="users")

    __table_args__ = (UniqueConstraint("name", "domain_id", name="uq_user_name_domain"),)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    domain_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class RoleAssignment(Base):
    """A role granted on a project, to either a user or a group.

    Exactly one of ``user_id`` and ``group_id`` is set. A group assignment reaches every
    member without being copied onto them, which is the point: adding a user to the group
    grants the role, and removing them takes it away again.
    """

    __tablename__ = "role_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )
    group_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id", ondelete="CASCADE"))

    __table_args__ = (
        UniqueConstraint(
            "user_id", "group_id", "project_id", "role_id", name="uq_assignment"
        ),
    )


class Group(Base):
    """A set of users that role assignments can be made against.

    Assigning to a group rather than a user is how an operator avoids re-granting the
    same roles to every new joiner, so the useful half is that membership *implies* the
    group's roles without copying them onto the user.
    """

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("name", "domain_id", name="uq_group_name_domain"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    domain_id: Mapped[str] = mapped_column(String(64), default=DOMAIN_ID)
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class GroupMembership(Base):
    """One user's membership of one group."""

    __tablename__ = "group_memberships"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_member"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class ApplicationCredential(Base):
    """A long-lived credential scoped to one project, for automation.

    The secret is shown once, at creation, and is not recoverable afterwards -- which is
    the property that makes it safe to put in CI instead of a password.
    """

    __tablename__ = "application_credentials"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_app_cred_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    name: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    secret: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(1024), default="")
    # A credential that cannot delegate cannot be used to mint another one, which is
    # what keeps a leaked CI credential from escalating into a permanent foothold.
    unrestricted: Mapped[bool] = mapped_column(Boolean, default=False)
    roles: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)


class Token(Base):
    """Opaque 32-char UUID token, project-scoped, handed out via X-Subject-Token."""

    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Service(Base):
    __tablename__ = "services"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    type: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str] = mapped_column(String(1024), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Endpoint(Base):
    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=gen_id)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"))
    interface: Mapped[str] = mapped_column(String(16), default="public")
    region_id: Mapped[str] = mapped_column(String(64), default="RegionOne")
    url: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # url templates keep %(project_id)s so per-token catalogs can be rendered
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
