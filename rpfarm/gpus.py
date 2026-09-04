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

The price IS the bill, provided you ask the right question. ``lowestPrice``
without ``secureCloud`` is the lowest across all clouds, which nobody is
charged; that made a 4090 look like $0.34/h while every cook was billed
$0.740/h. Asked for the cloud the pods are actually created in, the catalogue
matches the ledger to the cent:

    RTX 4090                     SECURE catalogue 0.74   billed 0.740
    RTX PRO 4000 Blackwell       SECURE catalogue 0.57   billed 0.570

(The $0.250 once attributed to the PRO 4000 was the A4500's rate, misread off
a `farm status` line.) So the price shown in the menu is a real hourly cost and
the sort orders by real money -- as long as the catalogue is fetched for the
same cloud type the scheduler will use.
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

_CACHE = {"at": 0.0, "key": None, "rows": None}
CACHE_TTL_S = 300.0


def is_consumer(gpu) -> bool:
    """Is this a gaming card rather than a professional/datacenter one?"""
    return _CONSUMER_MARKER in str(gpu.get("id", "")).lower()


def price_of(gpu):
    """On-demand price for one of these, or ``None`` when out of stock.

    ``uninterruptablePrice`` only -- never ``minimumBidPrice``. We do not rent
    spot (``interruptible`` is deliberately never set, see R32), so a bid price
    would be a number nobody will ever be charged sitting in a menu that claims
    to show cost.
    """
    low = gpu.get("lowestPrice") or {}
    price = low.get("uninterruptablePrice")
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


def catalogue(api, dc, secure_cloud=True, now=None, ttl=CACHE_TTL_S, force=False):
    """Catalogue rows for ``dc``, cached for ``ttl`` seconds.

    A menu is built every time a parameter dialog opens, and RunPod's GraphQL
    is not fast enough to sit in that path -- without the cache, opening the
    node's parameters would stall Houdini's UI on a network round trip. Never
    raises: a menu that cannot be built must degrade to whatever is already
    selected, not break the node.
    """
    now = time.time() if now is None else now
    # The cloud is part of the key, not a detail: the same card has different
    # prices and different stock in each, and mixing them is exactly the bug
    # that made the catalogue disagree with the bill.
    key = (dc, bool(secure_cloud))
    if (not force and _CACHE["rows"] is not None and _CACHE["key"] == key
            and now - _CACHE["at"] < ttl):
        return _CACHE["rows"]
    try:
        rows = normalise(api.gpu_types(dc, secure_cloud=secure_cloud))
    except Exception:  # noqa: BLE001 - a stale menu beats a broken node
        return _CACHE["rows"] if _CACHE["key"] == key else []
    _CACHE.update({"at": now, "key": key, "rows": rows})
    return rows


def other_cloud_hint(api, dc, secure_cloud, now=None):
    """"...or try Community", but only when Community actually has anything.

    The advice was being handed out unconditionally, and in EU-RO-1 it is
    false: asked with ``secureCloud: false``, **0 of 48** types have a price
    or any stock there. Telling someone to switch clouds when the other cloud
    is empty in their datacenter sends them to wait for a machine that cannot
    arrive. Returns "" when there is nothing honest to say.
    """
    try:
        other = catalogue(api, dc, secure_cloud=not secure_cloud, now=now)
    except Exception:  # noqa: BLE001
        return ""
    available = [r for r in other if r["price"] is not None]
    if not available:
        return ""
    name = "Community" if secure_cloud else "Secure"
    cheapest = min(available, key=lambda r: r["price"])
    return ("{} has {} type(s) available in {} right now, cheapest {} at "
            "${:.2f}/h -- switching Cloud Type may find a machine sooner."
            .format(name, len(available), dc, cheapest["display"], cheapest["price"]))


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
        # No "~": asked for the right cloud this is the on-demand rate we are
        # actually charged, matched against the ledger to the cent.
        tail = "${:.2f}/h, stock {}".format(row["price"], (row["stock"] or "?").lower())
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
