"""Microversion negotiation.

Until now the simulator echoed each service's maximum version back whatever the client
asked for, and every handler rendered the newest response shape. That is the friendliest
possible behaviour and the wrong one: real Nova defaults to its *minimum* (2.1) when no
version header is sent, so code that forgets to pin a microversion gets an old response
shape from the real cloud and a new one from a simulator that ignores the header. The
bug then surfaces in production rather than in the test that was supposed to catch it.

So negotiation is real here: the header is parsed, validated against the service's
advertised range, and the negotiated version is what handlers branch on and what the
response reports. Two headers carry it --  the standard

    OpenStack-API-Version: compute 2.79

and the older per-service spelling (``X-OpenStack-Nova-API-Version``), which novaclient
still sends. The standard one wins when both are present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.core.config import API_VERSIONS

# "2.79", with no leading zeros or extra parts -- Nova rejects anything else as malformed
# rather than rounding it to something workable.
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")

LATEST = "latest"


@dataclass(frozen=True, order=True)
class Version:
    """A microversion. Ordered, so ``version >= Version(2, 47)`` reads naturally."""

    major: int
    minor: int

    @classmethod
    def parse(cls, text: str) -> Version | None:
        """The version in ``text``, or None if it is not a microversion at all."""
        match = _VERSION.match(text.strip())
        if match is None:
            return None
        return cls(int(match.group(1)), int(match.group(2)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


class NegotiationError(Exception):
    """The client asked for a version this service cannot serve.

    Carries the status the service would return: 400 for a version that is not a version,
    406 for a well-formed one outside the advertised range.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class ServiceVersions:
    """What one service advertises, parsed once at import."""

    name: str  # the service type as it appears in the header ("compute", "volume", ...)
    minimum: Version
    maximum: Version

    def clamp(self, requested: str) -> Version:
        """Resolve a header value against this range, or raise :class:`NegotiationError`."""
        if requested.strip().lower() == LATEST:
            return self.maximum
        version = Version.parse(requested)
        if version is None:
            raise NegotiationError(
                400,
                f"Invalid format for microversion '{requested.strip()}'. "
                f"Expected 'major.minor' or 'latest'.",
            )
        if not (self.minimum <= version <= self.maximum):
            raise NegotiationError(
                406,
                f"Version {version} is not supported by the API. "
                f"Minimum is {self.minimum} and maximum is {self.maximum}.",
            )
        return version


SERVICES: dict[str, ServiceVersions] = {
    service: ServiceVersions(
        name,
        Version.parse(minimum) or Version(0, 0),
        Version.parse(maximum) or Version(0, 0),
    )
    for service, (name, minimum, maximum) in API_VERSIONS.items()
}

# The per-service header novaclient and cinderclient still send alongside the standard
# one. Keyed by simulator service name.
_LEGACY_HEADERS: dict[str, str] = {
    "nova": "X-OpenStack-Nova-API-Version",
    "cinder": "X-OpenStack-Volume-API-Version",
}


def _requested(service: str, versions: ServiceVersions, headers: Any) -> str | None:
    """The version string the client asked for, from whichever header carries it."""
    standard = headers.get("OpenStack-API-Version")
    if standard:
        # "compute 2.79" -- the service type is part of the value, and a header naming
        # some other service is not addressed to us.
        parts = standard.split()
        if len(parts) == 2:
            if parts[0].lower() == versions.name.lower():
                return parts[1]
        elif len(parts) == 1:
            # Not the documented form, but clients do send a bare version. Honour it
            # rather than silently falling back to the minimum.
            return parts[0]

    legacy = _LEGACY_HEADERS.get(service)
    if legacy:
        value = headers.get(legacy)
        if value:
            return value
    return None


def negotiate(service: str, headers: Any) -> Version | None:
    """The version this request is served at, or None for a service without microversions.

    Absent or unreadable headers give the service **minimum**, which is what a real
    deployment does -- not the maximum, however much more useful the newest shape is.
    """
    versions = SERVICES.get(service)
    if versions is None:
        return None
    requested = _requested(service, versions, headers)
    if requested is None:
        return versions.minimum
    return versions.clamp(requested)


def at_least(request: Any, version: str) -> bool:
    """True when this request was negotiated at ``version`` or newer.

    The guard for every version-gated field: ``if at_least(request, "2.47")``. A service
    with no microversions answers True, since its one shape is always current.
    """
    current = getattr(request.state, "microversion", None)
    if current is None:
        return True
    wanted = Version.parse(version)
    return wanted is None or current >= wanted
