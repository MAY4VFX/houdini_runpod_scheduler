"""The volume manager: boundaries first, then the numbers on screen.

Every test here is really one question -- can this window delete something
it must not? -- asked from a different direction.
"""

from __future__ import annotations

import pytest

from rpfarm import preflight, volume


LISTING = {
    "zones": {"houdini": 8_000_000_000, "projects": 14_000_000_000,
              "ledger": 1_000, "apps": 500},
    "projects": [
        {"user": "may", "project": "demo", "bytes": 10_100_000_000,
         "outputs_pending": False},
        {"user": "may", "project": "airship", "bytes": 3_500_000_000,
         "outputs_pending": True},
        {"user": "bob", "project": "thing", "bytes": 400_000_000,
         "outputs_pending": False},
        {"user": "may", "project": "smoke-upload-9f2", "bytes": 165_000_000,
         "outputs_pending": False},
    ],
    "volume": {"used": 26_500_000_000, "total": 50_000_000_000},
}


def _roots(user="may", cooking=(), contents=None):
    rows = volume.tree_rows(LISTING, user, cooking=cooking, contents=contents)
    for row in rows:
        row.path = volume.to_local(row.path)
        row.contents = [(volume.to_local(p), b) for p, b in row.contents]
    return preflight.build_tree(rows)


# -- what can never be deleted ----------------------------------------------


@pytest.mark.parametrize("path,locked", [
    ("/workspace/projects/may/demo", False),
    ("/workspace/projects/may/demo/render", False),
    ("/workspace/projects/bob/thing", True),          # another artist
    ("/workspace/projects/bob/thing/render", True),
    ("/workspace/houdini", True),                     # the Houdini install
    ("/workspace/houdini/22.0.393", True),
    ("/workspace/ledger", True),
    ("/workspace/projects", True),                    # everyone at once
    ("/workspace/projects/may", True),                # one artist at once
    ("/workspace", True),
    ("", True),
])
def test_only_this_artists_own_projects_can_be_ticked(path, locked):
    assert volume.is_locked(path, "may") is locked


def test_a_project_being_cooked_right_now_is_locked():
    assert volume.is_locked("/workspace/projects/may/demo", "may",
                            cooking=("may/demo",)) is True
    assert volume.is_locked("/workspace/projects/may/airship", "may",
                            cooking=("may/demo",)) is False


def test_an_unrecognised_path_shape_is_locked_not_allowed():
    """The rule is written so that anything it does not understand is
    refused. A new zone, or a path nobody thought about, must not fall
    through as deletable."""
    for path in ("/somewhere/else", "/workspace/newzone/x", "relative/path"):
        assert volume.is_locked(path, "may") is True


# -- what is protected but still possible ------------------------------------


def test_a_project_with_outputs_nobody_fetched_is_flagged():
    rows = {r.path: r for r in volume.tree_rows(LISTING, "may")}
    assert rows["/workspace/projects/may/airship"].source == "pending"
    assert rows["/workspace/projects/may/demo"].source == "mine"
    assert rows["/workspace/projects/bob/thing"].source == "other"
    # The label is what the artist reads in the window, so it has to shout.
    assert "NOT FETCHED" in preflight.SOURCE_LABELS["pending"]


def test_a_cooking_project_says_so_rather_than_looking_ordinary():
    rows = {r.path: r for r in volume.tree_rows(LISTING, "may", cooking=("may/demo",))}
    assert rows["/workspace/projects/may/demo"].source == "cooking"


# -- the header --------------------------------------------------------------


def test_the_header_answers_how_much_room_would_i_have():
    roots = _roots()
    text = volume.header_text(LISTING, roots, [])
    assert "of" in text and "used" in text and "nothing ticked" in text

    airship = [n.path for n in preflight.leaves(roots) if "airship" in n.path]
    text = volume.header_text(LISTING, roots, airship)
    assert "frees {}".format(preflight.human_bytes(3_500_000_000)) in text
    assert "leaving" in text
    # And it says how many projects are holding un-fetched frames.
    assert "1 project(s) still hold outputs" in text


def test_the_selection_size_is_what_gets_deleted():
    roots = _roots()
    picked = [n.path for n in preflight.leaves(roots) if "demo" in n.path]
    assert volume.selected_bytes(roots, picked) == 10_100_000_000


# -- opening a project all the way down --------------------------------------


def test_a_project_with_a_file_listing_opens_to_the_file():
    contents = {"may/airship": [
        ("/workspace/projects/may/airship/render/a.exr", 6_000_000),
        ("/workspace/projects/may/airship/render/b.exr", 4_000_000),
        ("/workspace/projects/may/airship/scene.hip", 1_000_000),
    ]}
    roots = _roots(contents=contents)
    paths = {n.path for n in preflight.leaves(roots)}
    assert volume.to_local("/workspace/projects/may/airship/render/a.exr") in paths
    # And a project without a listing stays one honest aggregate row.
    assert volume.to_local("/workspace/projects/may/demo") in paths


def test_a_file_inside_a_pending_project_is_protected_too():
    """The un-fetched frames ARE the files in there, so protection cannot
    stop at the folder row."""
    contents = {"may/airship": [
        ("/workspace/projects/may/airship/render/a.exr", 6_000_000)]}
    captured = {}

    def fake_confirm(roots, missing, checked, **kw):
        captured.update(kw)
        return None

    volume.browse(LISTING, "may", contents=contents, confirm=fake_confirm)
    protected = captured["protected"]
    node = next(n for n in preflight.leaves(preflight.build_tree(
        volume.tree_rows(LISTING, "may", contents=contents)))
        if "a.exr" in n.path)
    assert protected(node) is True


# -- the window's contract ---------------------------------------------------


def test_browse_passes_a_delete_button_and_its_own_header():
    captured = {}

    def fake_confirm(roots, missing, checked, **kw):
        captured.update(kw)
        captured["checked"] = checked
        return None

    assert volume.browse(LISTING, "may", confirm=fake_confirm) is None
    assert captured["accept_label"] == "Delete"
    assert captured["columns"][0] == "On the farm"
    # Nothing is ticked when it opens: this button deletes.
    assert not captured["checked"]


def test_closing_the_window_deletes_nothing():
    assert volume.browse(LISTING, "may", confirm=lambda *a, **k: None) is None


def test_browse_returns_farm_paths_not_local_ones():
    chosen = {volume.to_local("/workspace/projects/may/demo")}
    got = volume.browse(LISTING, "may", confirm=lambda *a, **k: chosen)
    assert got == ["/workspace/projects/may/demo"]


def test_the_confirmation_names_the_size_and_the_count():
    roots = _roots()
    picked = [n.path for n in preflight.leaves(roots) if "demo" in n.path]
    text = volume.confirm_text(picked, roots, LISTING)
    assert "1 item(s)" in text and "cannot be undone" in text
    assert preflight.human_bytes(10_100_000_000) in text


# -- our own litter ----------------------------------------------------------


@pytest.mark.parametrize("path,litter", [
    # Two shapes, both taken from the real volume on 2026-09-07.
    ("/workspace/projects/may/smoke-upload-deps-1788447185", True),
    ("/workspace/projects/pdgtemp/41756", True),      # litter in the USER slot
    ("/workspace/projects/test_render/test1", True),
    ("/workspace/projects/may/airship", False),
    ("/workspace/projects/may/demo", False),
    # A project the artist happens to have named "smoke" is HIS.
    ("/workspace/projects/may/smoke", False),
])
def test_our_leavings_are_recognised_as_ours(path, litter):
    assert volume.is_litter(path) is litter


def test_our_leavings_never_reach_the_window():
    """They used to arrive as "another artist's projects" -- rows the artist
    could not delete and should never have been shown."""
    listing = dict(LISTING, projects=LISTING["projects"] + [
        {"user": "pdgtemp", "project": "41756", "bytes": 3_643_230,
         "outputs_pending": False},
        {"user": "test_render", "project": "test1", "bytes": 6_582_785,
         "outputs_pending": False},
    ])
    paths = {r.path for r in volume.tree_rows(listing, "may")}
    assert not [p for p in paths if "pdgtemp" in p or "test_render" in p
                or "smoke-upload" in p]


def test_both_sides_agree_on_what_litter_is():
    """The window hides it and the pod deletes it; two lists would mean the
    artist sees a row nothing will ever remove, or the reverse."""
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "pod"))
    import housekeeping as hk

    assert set(volume.LITTER_USERS) == set(hk._LITTER_USERS)
    assert set(volume.LITTER_PROJECT_PREFIXES) == set(hk._LITTER_PROJECT_PREFIXES)
