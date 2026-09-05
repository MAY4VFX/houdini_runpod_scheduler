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

    Deliberately a second implementation: the first one lives in the module
    that may be stale, and asking a stale module to measure staleness is how
    you get a guard that agrees with itself and nothing else.
    tests/test_scheduler_glue.py holds the two to the same answer.
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


def _changed_module_files(package):
    \"\"\"Module files that differ from what this process imported, by content.

    A package too old to carry a FINGERPRINT at all is itself the answer:
    it predates this check, so it cannot be the code we just shipped.
    \"\"\"
    loaded = getattr(package, "FINGERPRINT", None)
    if not isinstance(loaded, dict):
        return ["<no fingerprint: this rpfarm predates the check>"]
    try:
        package_dir = os.path.dirname(os.path.abspath(package.__file__))
    except Exception:
        return []
    current = _ondisk_fingerprint(package_dir)
    if not current:
        return []  # cannot read the files: say nothing rather than cry wolf
    return sorted(name for name in set(current) | set(loaded)
                  if current.get(name) != loaded.get(name))


def _stale_module_message(minimum, loaded, on_disk, root, changed=()):
    \"\"\"The sentence to show the artist, or None when nothing is wrong.

    Two different causes, two different fixes, and telling them apart is the
    whole point of reading the on-disk version as well as the loaded one:

    * the files changed since this process imported them, or the loaded
      version is below the floor -> a running Houdini is holding the old
      package. Restart it.
    * on disk it is old too -> the checkout itself is behind the asset, and
      restarting will change nothing. Update it and rerun `rpfarm setup`.
    \"\"\"
    changed = list(changed)
    behind = _version_tuple(loaded) < _version_tuple(minimum)
    if not changed and not behind:
        return None
    seen = loaded or "неизвестна (старая копия без VERSION)"
    if changed or _version_tuple(on_disk) >= _version_tuple(minimum):
        detail = ""
        if changed:
            shown = ", ".join(changed[:4])
            more = " и ещё {}".format(len(changed) - 4) if len(changed) > 4 else ""
            detail = ("\\n\\nИзменились с момента загрузки: {}{}.".format(shown, more))
        return (
            "Код фермы обновился, а Houdini держит в памяти старую версию.\\n"
            "\\n"
            "ПЕРЕЗАПУСТИТЕ HOUDINI. Больше ничего делать не нужно —\\n"
            "на диске всё правильно, чинить нечего.\\n"
            "\\n"
            "Подробности: в памяти rpfarm {seen}, на диске {disk}.{detail}".format(
                seen=seen, disk=on_disk or "не найдена", detail=detail)
        )
    return (
        "Код фермы на диске старее, чем установленная нода.\\n"
        "\\n"
        "Перезапуск здесь не поможет. Обновите репозиторий и выполните\\n"
        "    python3 -m rpfarm setup\\n"
        "чтобы нода и пакет снова совпали.\\n"
        "\\n"
        "Подробности: нода требует rpfarm {min}, на диске {disk} ({root}),\\n"
        "в памяти {seen}.".format(min=minimum, disk=on_disk or "не найдена",
                                  root=root, seen=seen)
    )
"""


def guard_call(minimum):
    """The lines that run the guard, for an asset declaring ``minimum``.

    ``minimum`` is only the version floor for the message; the decision is
    the fingerprint comparison, which needs nothing to be remembered.
    """
    return (
        'import rpfarm as _rpfarm_pkg\n'
        '\n'
        '_stale = _stale_module_message(\n'
        '    _MIN_RPFARM_VERSION,\n'
        '    getattr(_rpfarm_pkg, "VERSION", None),\n'
        '    _ondisk_rpfarm_version(_RPFARM_ROOT),\n'
        '    _RPFARM_ROOT,\n'
        '    _changed_module_files(_rpfarm_pkg),\n'
        ')\n'
        'if _stale:\n'
        '    # Raised, not printed: this is the same place the ImportError used\n'
        '    # to come from, so Houdini surfaces it in the same dialog on scene\n'
        '    # open -- with the instruction as the first line the artist reads.\n'
        '    raise ImportError(_stale)\n'
    )
