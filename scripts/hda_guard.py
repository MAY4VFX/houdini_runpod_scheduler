"""The stale-``sys.modules`` guard that every HDA loaded on scene open carries.

One copy of the source, embedded into each asset at build time, because the
thing it detects makes the obvious alternative impossible: it cannot live in
``rpfarm`` and be imported, since an import is exactly what hands back the
cached module it exists to notice.

Verified in the field. A Houdini that was already open when the checkout
updated loads the NEW asset against the OLD package and the artist sees

    ImportError: cannot import name 'CLOUD_TYPE_SECURE' from 'rpfarm.runpod_api'

on scene open -- a symbol they have never heard of, and no hint that the fix
is to restart Houdini.

Which assets need it: the ones whose ``PythonModule`` imports ``rpfarm`` at
module scope, because those run while the scene is loading. Reproduced with a
poisoned ``sys.modules``: the scheduler and the stats node both throw during
the load; ``runpodfarm_upload`` and ``runpodfarm_download`` do their heavy
imports inside ``generate``, which does not run until a cook -- and the artist
will have restarted long before that.

``tests/test_hda_assets.py`` asserts every asset's embedded copy still matches
this one, so the two cannot drift apart silently.
"""

GUARD_SOURCE = """\
# -- stale-module guard ------------------------------------------------------
#
# The asset and the package ship together and are updated together, but Python
# caches modules in sys.modules for the life of the process. A Houdini that was
# already open when the checkout updated runs the NEW asset against the OLD
# package, and the artist sees either an ImportError naming a symbol they have
# never heard of, or -- worse, and this is what happened on 2026-09-05 -- a
# cook whose work items simply fail.
#
# THE CHECK IS A FACT, NOT A NUMBER. It compares the package's own
# FINGERPRINT (size + content digest of every module file, taken when this
# process imported it) against those files as they are now. If anything
# differs, the code in memory is not the code on disk, and no version needs
# to have been bumped for us to know it.
#
# That matters because the version check that used to be the whole guard
# failed exactly where it was needed: rpfarm.VERSION sat at 2.2.0 through
# seven commits that changed deps.py, preflight.py and usddeps.py, so
# "loaded >= minimum" was true while the loaded code was a week behind. A
# guard that depends on someone remembering to bump a number is a guard that
# is off whenever they forget. The version is still read -- but only to make
# the message concrete.
_MIN_RPFARM_VERSION = "2.3.0"


def _version_tuple(text):
    \"\"\"("2.1.0") -> (2, 1, 0). Unparseable parts sort as 0, never raises.\"\"\"
    parts = []
    for chunk in str(text or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _ondisk_rpfarm_version(root):
    \"\"\"rpfarm's VERSION as it is ON DISK, read without importing it.

    Importing is precisely what cannot answer this question: the import is
    what hands back the cached module. Parsed with ast, so a half-written or
    unexpected __init__ cannot execute anything or raise here.
    \"\"\"
    try:
        source = (pathlib.Path(root) / "rpfarm" / "__init__.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
                return ast.literal_eval(node.value)
    except Exception:  # noqa: BLE001 - a diagnostic must not become the failure
        return None
    return None


def _ondisk_fingerprint(package_dir):
    \"\"\"The same measurement rpfarm takes of itself, computed here.

    Only used by the bake script and the tests -- the runtime check never
    looks at the disk (see _asset_mismatch for why).
    \"\"\"
    out = {}
    try:
        names = sorted(os.listdir(package_dir))
    except Exception:
        return out
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            with open(os.path.join(package_dir, name), "rb") as handle:
                data = handle.read()
        except Exception:
            continue
        out[name] = (len(data), hashlib.sha256(data).hexdigest()[:16])
    return out


def _asset_mismatch(package, baked):
    \"\"\"Modules whose loaded content is not what this asset was built against.

    Both sides live INSIDE this Houdini: ``package.FINGERPRINT`` is what the
    modules were when this process imported them, ``baked`` is what they were
    when this asset was built. The disk is deliberately not consulted.

    That is the whole correction. Comparing against the disk answered the
    wrong question -- "has anyone touched the checkout?" -- so every push
    while an artist had Houdini open blocked their next cook, while a
    session that was genuinely broken (an asset reinstalled under a running
    Houdini, which reloads definitions without reopening the scene) could
    still look fine. Comparing the asset with the package it was built
    against answers the only question that matters to the artist: is my tool
    consistent with itself?

    An asset with no baked fingerprint predates this and gets a warning, not
    a stop -- it may well be fine, and refusing to cook on "I cannot tell"
    is how a guard gets switched off.
    \"\"\"
    loaded = getattr(package, "FINGERPRINT", None)
    if not isinstance(loaded, dict):
        return ["<no fingerprint: this rpfarm predates the check>"]
    if not baked:
        return []
    return sorted(name for name in baked if loaded.get(name) != baked[name])


def _stale_module_message(minimum, loaded, on_disk, root, changed=(), baked=True):
    \"\"\"The sentence to show the artist, or None when nothing is wrong.

    ``changed`` is the answer from :func:`_asset_mismatch`; the version
    arguments only make the message concrete. An asset that carries no baked
    fingerprint at all cannot be judged, so it says so quietly instead of
    refusing to work.
    \"\"\"
    changed = list(changed)
    if not changed:
        return None
    if not baked or any(name.startswith("<") for name in changed):
        return (
            "ВНИМАНИЕ: не могу проверить, сходится ли нода с кодом фермы.\\n"
            "\\n"
            "Это старая нода или старый пакет rpfarm. Кук пойдёт, но если он\\n"
            "упадёт странно — перезапустите Houdini, а потом обновите ноды:\\n"
            "    python3 -m rpfarm setup"
        )
    shown = ", ".join(changed[:4])
    more = " и ещё {}".format(len(changed) - 4) if len(changed) > 4 else ""
    return (
        "Нода собрана против другого кода фермы, чем сейчас в памяти Houdini.\\n"
        "\\n"
        "ПЕРЕЗАПУСТИТЕ HOUDINI. Больше ничего делать не нужно.\\n"
        "\\n"
        "Разошлись: {shown}{more}.\\n"
        "В памяти rpfarm {seen}, нода собрана против {disk}.".format(
            shown=shown, more=more, seen=loaded or "неизвестной версии",
            disk=on_disk or "неизвестной версии")
    )
"""


def guard_call(minimum):
    """The lines that run the guard, for an asset declaring ``minimum``.

    ``minimum`` and the versions are only for the message: the decision is
    the baked fingerprint against the loaded one, and nothing has to be
    remembered for it to be right.
    """
    return (
        'import rpfarm as _rpfarm_pkg\n'
        '\n'
        '_stale = _stale_module_message(\n'
        '    _MIN_RPFARM_VERSION,\n'
        '    getattr(_rpfarm_pkg, "VERSION", None),\n'
        '    _ASSET_BUILT_AGAINST_VERSION,\n'
        '    _RPFARM_ROOT,\n'
        '    _asset_mismatch(_rpfarm_pkg, _ASSET_FINGERPRINT),\n'
        '    bool(_ASSET_FINGERPRINT),\n'
        ')\n'
        'if _stale:\n'
        '    # Raised, not printed: this is the same place the ImportError used\n'
        '    # to come from, so Houdini surfaces it in the same dialog on scene\n'
        '    # open -- with the instruction as the first line the artist reads.\n'
        '    raise ImportError(_stale)\n'
    )


#: Written into every guarded asset by scripts/bake_asset_fingerprint.py and
#: checked by tests/test_hda_assets.py, so "rebuild the asset when the package
#: changes" is enforced rather than remembered.
BAKE_BEGIN = "# BEGIN baked by scripts/bake_asset_fingerprint.py -- do not edit"
BAKE_END = "# END baked"


def fingerprint_block(fingerprint, version):
    """The generated constant an asset carries: what it was built against."""
    lines = [BAKE_BEGIN,
             "_ASSET_BUILT_AGAINST_VERSION = {!r}".format(version),
             "_ASSET_FINGERPRINT = {"]
    for name in sorted(fingerprint):
        size, digest = fingerprint[name]
        lines.append("    {!r}: ({}, {!r}),".format(name, size, digest))
    lines.append("}")
    lines.append(BAKE_END)
    return "\n".join(lines) + "\n"
