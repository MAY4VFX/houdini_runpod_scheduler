"""The GPU catalogue as a set the artist picks from.

`gpu_priority` was a comma-separated string typed from memory whose semantics
were "first of these that exists". The owner asked for a set -- "any of these"
-- which makes the ordering ours to compute rather than his to reason about.
"""

import pytest

from rpfarm import gpus


def _gpu(gpu_id, display, price=None, stock=None):
    low = None if price is None else {
        "stockStatus": stock, "uninterruptablePrice": price, "minimumBidPrice": price}
    if price is None:
        low = {"stockStatus": None, "uninterruptablePrice": None, "minimumBidPrice": None}
    return {"id": gpu_id, "displayName": display, "lowestPrice": low}


CATALOGUE = [
    _gpu("NVIDIA GeForce RTX 4090", "RTX 4090", 0.34, "Medium"),
    _gpu("NVIDIA GeForce RTX 5090", "RTX 5090", 0.69, "Low"),
    _gpu("NVIDIA GeForce RTX 3090", "RTX 3090"),                 # out of stock
    _gpu("NVIDIA RTX PRO 4000 Blackwell", "RTX PRO 4000", 0.50, "Medium"),
    _gpu("NVIDIA RTX A4500", "RTX A4500"),                       # out of stock
    _gpu("NVIDIA H100 PCIe", "H100 PCIe"),                       # out of stock
    {"id": "unknown", "displayName": "unknown", "lowestPrice": None},
]
ROWS = gpus.normalise(CATALOGUE)


# -- what "no price" means ---------------------------------------------------


def test_an_unpriced_card_is_out_of_stock_now_not_nonexistent():
    """stockStatus None means no capacity in this datacenter this minute --
    confirmed live. A 3090 is a real card you cannot rent right now, so it
    stays selectable and gets labelled rather than hidden."""
    row = next(r for r in ROWS if r["id"] == "NVIDIA GeForce RTX 3090")

    assert row["price"] is None
    assert gpus.OUT_OF_STOCK in gpus.menu_label(row)
    assert row in gpus.menu_rows(ROWS, consumer_only=True)


def test_the_placeholder_type_is_dropped():
    assert all(r["id"] != "unknown" for r in ROWS)


# -- consumer filter ---------------------------------------------------------


def test_consumer_means_geforce_and_nothing_else():
    """Checked against all 48 types RunPod lists: every gaming card carries
    GeForce in its id and no professional or datacenter one does."""
    consumer = {r["display"] for r in ROWS if r["consumer"]}

    assert consumer == {"RTX 4090", "RTX 5090", "RTX 3090"}
    assert not any(r["consumer"] for r in ROWS
                   if r["display"] in ("RTX PRO 4000", "RTX A4500", "H100 PCIe"))


def test_the_filter_still_shows_what_you_already_picked():
    """Hiding part of someone's own set reads as the set having been changed."""
    picked = ["NVIDIA RTX PRO 4000 Blackwell"]

    shown = {r["id"] for r in gpus.menu_rows(ROWS, consumer_only=True, selected=picked)}

    assert "NVIDIA RTX PRO 4000 Blackwell" in shown
    assert "NVIDIA H100 PCIe" not in shown          # professional, not picked


def test_showing_all_includes_the_professional_cards():
    shown = {r["id"] for r in gpus.menu_rows(ROWS, consumer_only=False)}

    assert "NVIDIA H100 PCIe" in shown
    assert "NVIDIA RTX A4500" in shown


def test_the_menu_is_cheapest_first_with_the_unrentable_at_the_bottom():
    order = [r["display"] for r in gpus.menu_rows(ROWS, consumer_only=False)]

    assert order[:3] == ["RTX 4090", "RTX PRO 4000", "RTX 5090"]
    assert set(order[3:]) == {"H100 PCIe", "RTX 3090", "RTX A4500"}


# -- the set -----------------------------------------------------------------


def test_adding_builds_a_set_and_picking_twice_is_not_an_error():
    text = ""
    text = gpus.add_to_selection(text, "NVIDIA GeForce RTX 4090")
    text = gpus.add_to_selection(text, "NVIDIA GeForce RTX 5090")
    text = gpus.add_to_selection(text, "NVIDIA GeForce RTX 4090")

    assert gpus.parse_selection(text) == [
        "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090"]


def test_an_empty_selection_stays_empty():
    assert gpus.parse_selection("") == []
    assert gpus.parse_selection(None) == []
    assert gpus.parse_selection("  ,  , ") == []
    assert gpus.add_to_selection("", "") == ""
    assert gpus.order_for_request([], ROWS) == []


def test_a_selection_typed_by_hand_still_parses():
    assert gpus.parse_selection(" A , B,C ") == ["A", "B", "C"]


# -- ordering for the request ------------------------------------------------


def test_the_set_goes_out_cheapest_first():
    """gpuTypePriority is custom, so RunPod walks our order -- which is what
    turns "any of these" into "the cheapest of these that exists"."""
    picked = ["NVIDIA GeForce RTX 5090", "NVIDIA RTX PRO 4000 Blackwell",
              "NVIDIA GeForce RTX 4090"]

    assert gpus.order_for_request(picked, ROWS) == [
        "NVIDIA GeForce RTX 4090",           # 0.34
        "NVIDIA RTX PRO 4000 Blackwell",     # 0.50
        "NVIDIA GeForce RTX 5090",           # 0.69
    ]


def test_out_of_stock_goes_last_but_is_never_dropped():
    """Stock can come back between building the menu and creating the pod."""
    picked = ["NVIDIA GeForce RTX 3090", "NVIDIA GeForce RTX 4090"]

    assert gpus.order_for_request(picked, ROWS) == [
        "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 3090"]


def test_a_type_the_catalogue_does_not_know_is_kept_at_the_end():
    """Dropping it would turn a typo into silence instead of RunPod's error."""
    picked = ["NVIDIA Fictional 9000", "NVIDIA GeForce RTX 4090"]

    assert gpus.order_for_request(picked, ROWS) == [
        "NVIDIA GeForce RTX 4090", "NVIDIA Fictional 9000"]


# -- caching -----------------------------------------------------------------


class _CountingAPI:
    def __init__(self, rows=CATALOGUE):
        self.rows = rows
        self.calls = 0
        self.asked = []

    def gpu_types(self, dc, secure_cloud=True):
        self.calls += 1
        self.asked.append((dc, secure_cloud))
        return self.rows


def test_the_catalogue_is_cached_so_opening_the_parms_does_not_hit_the_network():
    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})
    api = _CountingAPI()

    gpus.catalogue(api, "EU-RO-1", now=1000.0)
    gpus.catalogue(api, "EU-RO-1", now=1100.0)

    assert api.calls == 1


def test_the_cache_expires_and_a_different_datacenter_is_not_reused():
    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})
    api = _CountingAPI()

    gpus.catalogue(api, "EU-RO-1", now=1000.0)
    gpus.catalogue(api, "EU-RO-1", now=1000.0 + gpus.CACHE_TTL_S + 1)
    gpus.catalogue(api, "US-TX-3", now=1000.0 + gpus.CACHE_TTL_S + 1)

    assert api.calls == 3


def test_the_cloud_is_part_of_the_cache_key():
    """Same card, different price and stock per cloud -- mixing them is the
    bug that made the catalogue disagree with the bill."""
    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})
    api = _CountingAPI()

    gpus.catalogue(api, "EU-RO-1", secure_cloud=True, now=1000.0)
    gpus.catalogue(api, "EU-RO-1", secure_cloud=False, now=1000.0)

    assert api.calls == 2
    assert api.asked == [("EU-RO-1", True), ("EU-RO-1", False)]


def test_only_the_on_demand_price_is_used():
    """We never rent spot, so a bid price in a menu claiming to show cost
    would be a number nobody is ever charged."""
    bid_only = {"id": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090",
                "lowestPrice": {"stockStatus": "Low", "minimumBidPrice": 0.20,
                                "uninterruptablePrice": None}}

    assert gpus.price_of(bid_only) is None


def test_the_other_cloud_is_only_suggested_when_it_has_something():
    """In EU-RO-1, Community has 0 of 48 types priced -- confirmed live. The
    advice "switch to Community" sends someone to wait for a machine that
    cannot arrive."""
    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})

    empty = _CountingAPI(rows=[_gpu("NVIDIA GeForce RTX 4090", "RTX 4090")])
    assert gpus.other_cloud_hint(empty, "EU-RO-1", secure_cloud=True, now=1.0) == ""

    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})
    stocked = _CountingAPI(rows=[_gpu("NVIDIA GeForce RTX 4090", "RTX 4090", 0.34, "High")])
    hint = gpus.other_cloud_hint(stocked, "EU-RO-1", secure_cloud=True, now=1.0)
    assert "Community" in hint and "0.34" in hint


def test_a_catalogue_fetch_that_fails_does_not_break_the_node():
    """A menu that cannot be built degrades; it does not raise into the UI."""
    gpus._CACHE.update({"at": 0.0, "key": None, "rows": None})

    class _Broken:
        def gpu_types(self, dc, secure_cloud=True):
            raise OSError("no network in this Houdini")

    assert gpus.catalogue(_Broken(), "EU-RO-1", now=1000.0) == []

    # and with something cached, the stale answer is preferred to nothing
    api = _CountingAPI()
    gpus.catalogue(api, "EU-RO-1", now=2000.0)
    assert gpus.catalogue(_Broken(), "EU-RO-1", now=2000.0 + gpus.CACHE_TTL_S + 1)


def test_the_label_shows_a_real_price_not_an_estimate():
    """Asked for the right cloud, the catalogue matches the bill to the cent,
    so hedging it with "~" would understate what we know."""
    row = gpus.normalise([_gpu("NVIDIA GeForce RTX 4090", "RTX 4090", 0.74, "High")])[0]

    label = gpus.menu_label(row)

    assert "$0.74/h" in label
    assert "~" not in label
