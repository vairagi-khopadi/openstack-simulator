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
    sort_key: str | None = None
    sort_dir: str = "desc"


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
    # Neutron spells the direction per key ("sort_dir" repeated); Nova and Cinder send
    # one of each. Taking the first of each covers both without pretending to support
    # multi-key sorting, which nothing here needs.
    sort_dir = (params.get("sort_dir") or "").lower()
    return Page(
        limit=min(limit, MAX_LIMIT),
        marker=params.get("marker") or None,
        requested_limit=raw is not None,
        sort_key=params.get("sort_key") or None,
        sort_dir=sort_dir if sort_dir in ("asc", "desc") else "",
    )


def sort_column_for(model: Any, page: Page, default: Any) -> tuple[Any, bool]:
    """Resolve ``?sort_key=`` against the model, falling back to the collection default.

    An unknown key is ignored rather than refused: it is how clients probe for support,
    and a 400 there would break a listing that works perfectly well unsorted.
    """
    column = default
    if page.sort_key:
        candidate = getattr(model, page.sort_key, None)
        # Only real mapped columns -- getattr would happily return a method otherwise.
        if candidate is not None and hasattr(candidate, "asc"):
            column = candidate
    return column, page.sort_dir


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
    # An explicit ?sort_dir wins over the collection's own default direction.
    sort_column, requested_dir = sort_column_for(model, page, sort_column)
    if requested_dir:
        descending = requested_dir == "desc"

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


# --------------------------------------------------------------------------------------
# Field selection
# --------------------------------------------------------------------------------------


def requested_fields(params: Any) -> set[str]:
    """The ``?fields=`` set, empty when the client wants everything.

    Neutron repeats the parameter (``?fields=id&fields=name``); the others comma-separate
    it. Both are accepted, because a client that guesses wrong should still get a useful
    answer rather than a silently unfiltered one.
    """
    raw: list[str] = []
    if hasattr(params, "getlist"):
        raw = list(params.getlist("fields"))
    elif params.get("fields"):
        raw = [params["fields"]]
    wanted = {
        part.strip()
        for value in raw
        for part in str(value).split(",")
        if part.strip()
    }
    return wanted


def trim(body: Any, fields: set[str]) -> Any:
    """Keep only ``fields`` in a resource dict, or in every dict of a list.

    ``id`` is always kept: a listing whose entries cannot be identified is not a useful
    saving, and every client that pages needs it for the marker.
    """
    if not fields:
        return body
    keep = fields | {"id"}
    if isinstance(body, list):
        return [trim(item, fields) for item in body]
    if isinstance(body, dict):
        return {key: value for key, value in body.items() if key in keep}
    return body
