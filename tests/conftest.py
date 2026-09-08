"""Session-wide guards that keep the suite off the developer's real machine.

Task 14 found this the hard way: `tests/test_houdini_local.py` builds a
`HoudiniInstall` over a *fake* HFS, but `user_pref_dir` was derived from
`Path.home()` and the detected version alone -- so a plain `pytest` run
resolved to the machine's REAL `~/Library/Preferences/houdini/22.0/` and
`build_and_install_hdas` overwrote all four installed `runpodfarm_*.hda`
with 17-byte `fake-hda-contents` fixtures. The next live smoke run died
with `hou.OperationFailed: Invalid node type name`.

`rpfarm.houdini_local` now honours `HOUDINI_USER_PREF_DIR` (Houdini's own
variable), and this fixture points it at a tmp dir for every test, so a
future test cannot reach the real pref dir even by accident.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_houdini_user_pref_dir(tmp_path_factory, monkeypatch):
    prefs = tmp_path_factory.mktemp("houdini-prefs")
    monkeypatch.setenv("HOUDINI_USER_PREF_DIR", str(prefs))
    return prefs


@pytest.fixture(autouse=True)
def _clear_shared_remote_index():
    """`rpfarm.sync` keeps one already-fetched listing in module-level
    state (`share_remote_index`/`take_shared_remote_index`, Ruling R64) so
    the scheduler's divergence check and the upload dialog's preflight can
    share one remote_index call within a cook. That state is process-wide,
    not per-test -- without this, a test that shares an index (or a
    fetch_farm_index test that consumes one) could leak into the next test
    that happens to ask about the same remote_project string.
    """
    from rpfarm import sync as rpsync
    rpsync.clear_shared_remote_index()
    yield
    rpsync.clear_shared_remote_index()
