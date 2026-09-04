"""The RunPod GPU catalogue, as a set the artist picks from.

``gpu_priority`` used to be a comma-separated string typed from memory, and
its semantics were "first of these that exists". The owner wants a set --
"any of these" -- so the ordering stops being something a person has to reason
about and becomes something we compute: cheapest first, because
``gpuTypePriority`` is ``custom`` and RunPod walks our order, so "any of these"
becomes "the cheapest of these that exists".

Everything here is pure except :func:`catalogue`, which fetches and caches, so
the selection, filtering and ordering can be tested without a network or a
Houdini.

A caution that came out of building this, and that the price sort inherits:
the catalogue's ``lowestPrice`` is NOT what we are billed. Measured the same
day, EU-RO-1:

    RTX 4090                     catalogue 0.34   billed 0.740
    RTX PRO 4000 Blackwell       catalogue 0.50   billed 0.250

It is wrong in both directions, so it cannot be presented to anyone as "what
this will cost". It is still the only comparable number RunPod gives per type,
so it is what the sort uses -- as a relative hint, labelled as such.
"""

from __future__ import annotations

import time

# `lowestPrice.stockStatus` is None when the type has no capacity in the
# datacenter right now -- confirmed live, it is not a missing field. So an
# unpriced card is not a card that does not exist; it is one you cannot rent
# this minute. It stays selectable (stock returns) and is labelled.
OUT_OF_STOCK = "out of stock here now"

# Every consumer card RunPod lists carries "GeForce" in its type id, and no
# professional or datacenter one does. Checked against all 48 types in the
# catalogue: GeForce covers 3070/3080/3080 Ti/3090/3090 Ti/4070 Ti/4080/
# 4080 SUPER/4090/5080/5090, and excludes RTX A*, RTX * Ada, RTX PRO *, L4,
# L40, L40S, A100, H100, H200, B200, B300, V100 and MI300X.
_CONSUMER_MARKER = "geforce"

_CACHE = {"at": 0.0, "dc": None, "rows": None}
CACHE_TTL_S = 300.0


def is_consumer(gpu) -> bool:
    """Is this a gaming card rather than a professional/datacenter one?"""
    return _CONSUMER_MARKER in str(gpu.get("id", "")).lower()


def price_of(gpu):
    """On-demand price for one of these, or ``None`` when out of stock."""
    low = gpu.get("lowestPrice") or {}
    price = low.get("uninterruptablePrice")
    if price is None:
        price = low.get("minimumBidPrice")
    return float(price) if price is not None else None


def stock_of(gpu):
    return (gpu.get("lowestPrice") or {}).get("stockStatus")


def normalise(raw):
    """The catalogue as plain rows, newest API shape kept in one place."""
    rows = []
    for gpu in raw or []:
        gpu_id = gpu.get("id") or ""
        if not gpu_id or gpu_id == "unknown":
            continue
        rows.append({
            "id": gpu_id,
            "display": gpu.get("displayName") or gpu_id,
            "price": price_of(gpu),
            "stock": stock_of(gpu),
            "consumer": is_consumer(gpu),
        })
    return rows


def catalogue(api, dc, now=None, ttl=CACHE_TTL_S, force=False):
    """Catalogue rows for ``dc``, cached for ``ttl`` seconds.

    A menu is built every time a parameter dialog opens, and RunPod's GraphQL
    is not fast enough to sit in that path -- without the cache, opening the
    node's parameters would stall Houdini's UI on a network round trip. Never
    raises: a menu that cannot be built must degrade to whatever is already
    selected, not break the node.
    """
    now = time.time() if now is None else now
    if (not force and _CACHE["rows"] is not None and _CACHE["dc"] == dc
            and now - _CACHE["at"] < ttl):
        return _CACHE["rows"]
    try:
        rows = normalise(api.gpu_types(dc))
    except Exception:  # noqa: BLE001 - a stale menu beats a broken node
        return _CACHE["rows"] if _CACHE["dc"] == dc else []
    _CACHE.update({"at": now, "dc": dc, "rows": rows})
    return rows


def menu_rows(rows, consumer_only=True, selected=()):
    """Rows to offer, gaming cards first-class and the rest behind a toggle.

    Anything already selected is always offered even when the filter would
    hide it -- otherwise turning the filter on makes part of your own set
    invisible, which reads as the set having been silently changed.
    """
    selected = set(selected or ())
    out = [r for r in rows
           if (not consumer_only) or r["consumer"] or r["id"] in selected]
    out.sort(key=lambda r: (r["price"] is None, r["price"] or 0.0, r["display"]))
    return out


def menu_label(row, selected=()):
    """One menu line: name, relative price, stock, and whether it is in the set."""
    mark = "* " if row["id"] in set(selected or ()) else "  "
    if row["price"] is None:
        tail = OUT_OF_STOCK
    else:
        tail = "~${:.2f}/h, stock {}".format(row["price"], (row["stock"] or "?").lower())
    return "{}{} ({})".format(mark, row["display"], tail)


def parse_selection(text):
    """The stored set, from the comma-separated string the parm holds."""
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def format_selection(ids):
    return ", ".join(ids)


def add_to_selection(text, gpu_id):
    """Add one type to the set. Idempotent -- picking it twice is not an error."""
    current = parse_selection(text)
    if gpu_id and gpu_id not in current:
        current.append(gpu_id)
    return format_selection(current)


def order_for_request(ids, rows):
    """The set, ordered cheapest-first, for ``gpuTypeIds``.

    This is what turns "any of these" into "the cheapest of these that is
    available": ``gpuTypePriority`` is ``custom``, so RunPod tries our order
    and takes the first with capacity.

    Types the catalogue has no price for go last but are NOT dropped: no price
    means no stock *right now*, and by the time the pod is created that may
    have changed. An id the catalogue does not know at all is also kept, at the
    end -- refusing to send it would turn a typo into silence instead of into
    RunPod's own error message.
    """
    price = {r["id"]: r["price"] for r in rows}
    known = [i for i in ids if i in price]
    unknown = [i for i in ids if i not in price]
    known.sort(key=lambda i: (price[i] is None, price[i] or 0.0, i))
    return known + unknown
