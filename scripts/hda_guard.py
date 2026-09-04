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
# already open when the checkout updated loads the NEW asset against the OLD
# package, and the only thing the artist sees is
#
#     ImportError: cannot import name 'CLOUD_TYPE_SECURE' from 'rpfarm.runpod_api'
#
# which happened in the field on scene open. That message names a symbol the
# artist has never heard of and says nothing about the fix, which is simply to
# restart Houdini. So: check first, in words.
#
# A FLOOR, not an equality. The asset declares the oldest package it can work
# with, so a newer package with an older asset is fine (that direction never
# breaks) and bumping rpfarm.VERSION on its own cannot make every scene shout.
_MIN_RPFARM_VERSION = "2.1.0"


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


def _stale_module_message(minimum, loaded, on_disk, root):
    \"\"\"The sentence to show the artist, or None when nothing is wrong.

    Two different causes, two different fixes, and telling them apart is the
    whole point of reading the on-disk version as well as the loaded one:

    * on disk it is new enough, in memory it is not -> a running Houdini is
      holding the old package. Restart it.
    * on disk it is old too -> the checkout itself is behind the asset, and
      restarting will change nothing. Update it and rerun `rpfarm setup`.
    \"\"\"
    if _version_tuple(loaded) >= _version_tuple(minimum):
        return None
    seen = loaded or "неизвестна (старая копия без VERSION)"
    if _version_tuple(on_disk) >= _version_tuple(minimum):
        return (
            "Код фермы обновился, а Houdini держит в памяти старую версию.\\n"
            "\\n"
            "ПЕРЕЗАПУСТИТЕ HOUDINI. Больше ничего делать не нужно —\\n"
            "на диске всё правильно, чинить нечего.\\n"
            "\\n"
            "Подробности: нода фермы требует rpfarm {min}, на диске лежит {disk},\\n"
            "а в памяти этого Houdini осталась {seen}. Python загружает модуль\\n"
            "один раз за запуск, поэтому обновление кода не подхватывается\\n"
            "открытым Houdini.".format(min=minimum, disk=on_disk, seen=seen)
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
    """The lines that run the guard, for an asset declaring ``minimum``."""
    return (
        'import rpfarm as _rpfarm_pkg\n'
        '\n'
        '_stale = _stale_module_message(\n'
        '    _MIN_RPFARM_VERSION,\n'
        '    getattr(_rpfarm_pkg, "VERSION", None),\n'
        '    _ondisk_rpfarm_version(_RPFARM_ROOT),\n'
        '    _RPFARM_ROOT,\n'
        ')\n'
        'if _stale:\n'
        '    # Raised, not printed: this is the same place the ImportError used\n'
        '    # to come from, so Houdini surfaces it in the same dialog on scene\n'
        '    # open -- with the instruction as the first line the artist reads.\n'
        '    raise ImportError(_stale)\n'
    )
