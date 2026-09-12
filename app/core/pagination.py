"""Marker pagination, shared by every service that returns a collection.

Listings used to honour ``limit`` and nothing else, which is the failure mode that hurts
most quietly: a client asking for page after page gets the *same* first page every time,
so it either loops forever or -- if it stops when a page is short -- silently processes
only the first N resources and reports success.

Real OpenStack pages by marker. The client passes the id of the last resource it saw and
gets the ones after it, in the collection's sort order. That is keyset pagination, so it
stays correct when rows are inserted or deleted between pages, unlike an offset.

Two response shapes exist and both are produced here:

* Nova, Cinder and Neutron append ``<collection>_links`` with a ``next`` href.
* Glance uses a flat ``next`` (and ``first``) carrying a path rather than a full url.

A ``next`` link is emitted only when the page came back full. That is what tells a client
to ask again, and its absence is what ends the loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

# Ceilings mirror the services' own: a client asking for a million rows gets the cap, not
# the million, and not an error.
DEFAULT_LIMITS: dict[str, int] = {
    "nova": 1000,
    "cinder": 1000,
    "glance": 100,
    "neutron": 1000,
    "octavia": 1000,
}
MAX_LIMIT = 1000


class PaginationError(Exception):
    """The marker names a resource this collection cannot page from."""

    def __init__(self, marker: str) -> None:
        super().__init__(marker)
        self.marker = marker
        self.message = f"marker [{marker}] not found"


@dataclass(frozen=True, slots=True)
class Page:
    """What the client asked for, normalised."""

    limit: int
    marker: str | None
    requested_limit: bool  # did the client name a limit, or is this the default?


def page_request(params: Any, service: str) -> Page:
    """Read ``limit`` and ``marker`` out of a query string.

    A limit that is absent, unparseable or out of range falls back to the service
    default rather than failing the request -- which is what the real services do, and
    keeps a stray ``?limit=`` from turning into a 400.
    """
    default = DEFAULT_LIMITS.get(service, 1000)
    raw = params.get("limit")
    try:
        limit = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        limit = default
    if limit <= 0:
        limit = default
    return Page(
        limit=min(limit, MAX_LIMIT),
        marker=params.get("marker") or None,
        requested_limit=raw is not None,
    )


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    model: Any,
    page: Page,
    *,
    sort_column: Any,
    descending: bool = True,
) -> Select[Any]:
    """Order, seek past the marker, and limit -- the three parts of one page.

    The marker is a resource id, so its position has to be looked up before the rows
    after it can be selected. Ordering on ``(sort_column, id)`` rather than the sort
    column alone is what makes that position unambiguous when several rows share a
    timestamp, which at simulator speed they routinely do.
    """
    order = (
        (sort_column.desc(), model.id.desc())
        if descending
        else (sort_column.asc(), model.id.asc())
    )

    if page.marker is not None:
        anchor = await session.get(model, page.marker)
        if anchor is None:
            raise PaginationError(page.marker)
        position = (getattr(anchor, sort_column.key), anchor.id)
        keyset = tuple_(sort_column, model.id)
        # A row is "after" the marker when it sorts strictly lower (descending) or
        # strictly higher (ascending) than the marker's own position.
        stmt = stmt.where(keyset < position if descending else keyset > position)

    return stmt.order_by(*order).limit(page.limit)


def _next_query(params: Any, marker: str, limit: int, named_limit: bool) -> str:
    """The original query string with the marker advanced."""
    carried = [
        (key, value)
        for key, value in params.multi_items()
        if key not in ("marker", "limit")
    ]
    if named_limit:
        carried.append(("limit", str(limit)))
    carried.append(("marker", marker))
    return urlencode(carried)


def next_marker(items: list[Any], page: Page) -> str | None:
    """The id to page from next, or None when this page ended the collection.

    A short page means there is nothing left, so no link is emitted and the client stops.
    """
    if len(items) < page.limit or not items:
        return None
    return str(items[-1].id)


def collection_links(
    request: Any, collection: str, items: list[Any], page: Page
) -> dict[str, Any]:
    """The ``<collection>_links`` block Nova, Cinder and Neutron use. Empty when done."""
    marker = next_marker(items, page)
    if marker is None:
        return {}
    href = str(request.url.replace(
        query=_next_query(request.query_params, marker, page.limit, page.requested_limit)
    ))
    return {f"{collection}_links": [{"rel": "next", "href": href}]}


def glance_links(
    request: Any, path: str, items: list[Any], page: Page
) -> dict[str, Any]:
    """Glance's flatter shape: ``first`` always, ``next`` only while more remain."""
    body: dict[str, Any] = {"first": path}
    marker = next_marker(items, page)
    if marker is not None:
        query = _next_query(request.query_params, marker, page.limit, page.requested_limit)
        body["next"] = f"{path}?{query}"
    return body
