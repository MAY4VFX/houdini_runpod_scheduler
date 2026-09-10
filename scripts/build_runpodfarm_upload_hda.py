"""One-shot builder for hda/runpodfarm_upload.hda.

Builds a Top/subnet node containing a pythonprocessor1 (work item
generation/cook) and a localscheduler (forces this node's items to cook on
PDG's local scheduler regardless of the parent topnet's own scheduler --
see this node's Help), wires them up, sets the rpfarm_* parameter
interface (including the rpfarm_inprocess debug toggle), adds an
OnCreated event (Python, via ExtraFileOptions IsPython/IsScript -- see
main() below for how that's set) that re-asserts the scheduler-override
expression, converts the subnet to a digital asset, and saves it as a
single packed .hda file at the path given on the command line. By
default work items dispatch out of process through
rpfarm.package_runner (Ruling R22) rather than cooking in this node's own
cooktask callback.

Regenerate the checked-in asset with, e.g.::

    HYTHON=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython
    OUT=/tmp/runpodfarm_upload.hda
    "$HYTHON" scripts/build_runpodfarm_upload_hda.py "$OUT"
    rm -rf hda/runpodfarm_upload.hda
    hotl -t hda/runpodfarm_upload.hda "$OUT"          # git-tracked expanded form
    hotl -l hda/runpodfarm_upload.hda \\
        ~/Library/Preferences/houdini/22.0/otls/runpodfarm_upload.hda  # install

Only ``hotl -l`` (the counterpart of ``-t``) round-trips correctly for this
directory layout -- ``-c``/``-C`` are for the other (``-x``/``-X``) expanded
format and silently produce an interface-less asset here.
"""
import os
import pathlib
import sys

import hou

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from rpfarm import houdini_local as _hl  # noqa: E402

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_upload.hda"

# -- the family look (Task 17) ------------------------------------------------
#
# All four RunPodFarm nodes share one colour, one node shape and one icon
# family, so a farm node is recognisable at a glance among stock TOP nodes.
# Violet because it is RunPod's own colour and is essentially absent from
# stock Houdini.
#
# The icon travels INSIDE the asset as an ``IconSVG`` section referenced by
# ``opdef:.?IconSVG``, so it needs no installation. The node shape cannot:
# Houdini resolves a shape by name out of ``config/NodeShapes`` on
# HOUDINI_PATH, so ``rpfarm setup`` copies hda/nodeshapes/rpfarm.json into
# the user pref dir (rpfarm.houdini_local.install_node_shape) and ``rpfarm
# doctor`` checks it is there. Without it the nodes simply draw as plain
# rectangles -- they still work.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE_COLOR = (0.549, 0.361, 0.882)
NODE_SHAPE = "rpfarm"
ICON_SVG = (REPO_ROOT / "hda" / "icons" / "runpodfarm_upload.svg").read_text()

# Colour is applied in OnCreated because a definition carries none: a node
# coloured before createDigitalAsset comes back grey on the next instance
# (verified in Houdini 22.0.368). The shape does survive -- setUserData
# below bakes an ``opuserdata`` line into the generated CreateScript -- but
# it is re-asserted here too, so one mechanism failing is not the whole
# look failing.
FAMILY_ONCREATED = (
    'node.setColor(hou.Color((0.549, 0.361, 0.882)))\n'
    'node.setUserData("nodeshape", "rpfarm")\n'
)

PYTHON_MODULE = '''\
"""Parameter-default helpers for runpodfarm_upload.

Task 17: the fields that are read from ~/.rpfarm/config.toml now SHOW what
they will use. They used to sit empty while the code behind them did
`parm or config`, so an empty field read as "not configured" even though
everything worked, and there was no way to tell a field that still needs
filling in from one that is already answered.

The mechanism is Houdini's own: the parm's DEFAULT is an expression calling
into here. While the artist has not touched the field it evaluates live from
the config; the moment they type something, the literal replaces the
expression and wins -- exactly "an override overrides the config", with no
change needed to the `parm or cfg` code that reads it.

Two rules for everything in this module, because a parm default expression
is re-evaluated on every UI refresh and runs while the parameter dialog is
being drawn:
  * it must be cheap -- rpfarm.config.load_cached() re-reads config.toml
    only when its mtime/size change; and
  * it must never raise -- no config yet (before `rpfarm setup`), no rpfarm
    on sys.path at all, a corrupt config: every one of those has to come
    back as an empty field, not a node that throws while drawing itself.
"""

import ast
import hashlib
import os
import pathlib
import sys

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))


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
    """("2.1.0") -> (2, 1, 0). Unparseable parts sort as 0, never raises."""
    parts = []
    for chunk in str(text or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _ondisk_rpfarm_version(root):
    """rpfarm's VERSION as it is ON DISK, read without importing it.

    Importing is precisely what cannot answer this question: the import is
    what hands back the cached module. Parsed with ast, so a half-written or
    unexpected __init__ cannot execute anything or raise here.
    """
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
    """The same measurement rpfarm takes of itself, computed here.

    Only used by the bake script and the tests -- the runtime check never
    looks at the disk (see _asset_mismatch for why).
    """
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
    """Modules whose loaded content is not what this asset was built against.

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
    """
    loaded = getattr(package, "FINGERPRINT", None)
    if not isinstance(loaded, dict):
        return ["<no fingerprint: this rpfarm predates the check>"]
    if not baked:
        return []
    return sorted(name for name in baked if loaded.get(name) != baked[name])


def _stale_module_message(minimum, loaded, on_disk, root, changed=(), baked=True):
    """The sentence to show the artist, or None when nothing is wrong.

    ``changed`` is the answer from :func:`_asset_mismatch`; the version
    arguments only make the message concrete. An asset that carries no baked
    fingerprint at all cannot be judged, so it says so quietly instead of
    refusing to work.
    """
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
    package = __import__('sys').modules.get('rpfarm')
    loaded_fingerprint = getattr(package, 'FINGERPRINT', None)
    disk_fingerprint = _ondisk_fingerprint(pathlib.Path(root) / 'rpfarm')
    if loaded_fingerprint and disk_fingerprint and loaded_fingerprint == disk_fingerprint:
        action = 'Установите согласованную сборку Python и HDA. Перезапуск этой свежей сессии не исправит несовместимую сборку.'
    else:
        action = 'ПЕРЕЗАПУСТИТЕ HOUDINI, чтобы загрузить установленный пакет заново.'
    return (
        "Нода собрана против другого кода фермы, чем сейчас в памяти Houdini.\\n"
        "\\n"
        "{action}\\n"
        "\\n"
        "Разошлись: {shown}{more}.\\n"
        "В памяти rpfarm {seen}, нода собрана против {disk}.".format(
            action=action, shown=shown, more=more, seen=loaded or "неизвестной версии",
            disk=on_disk or "неизвестной версии")
    )

# BEGIN baked by scripts/bake_asset_fingerprint.py -- do not edit
_ASSET_BUILT_AGAINST_VERSION = '2.4.0'
_ASSET_FINGERPRINT = {
    '__init__.py': (2490, '84b00617a24de235'),
    '__main__.py': (52, '13a1a5b340cdcfc1'),
    'background_cook.py': (8292, '0e7f039578c949dd'),
    'cli.py': (72171, '9a2152a00e573a1c'),
    'compression.py': (23234, 'bef2f19daebbc929'),
    'config.py': (20080, '1dfcc2a15551dbe2'),
    'context.py': (4466, '2fa09a0d45931725'),
    'delivery.py': (3964, '63bb04491f97a3f3'),
    'deps.py': (38558, '2daae12f5770289a'),
    'dispatch.py': (22191, '1121a6505c88adb3'),
    'file_review.py': (16246, 'd81726007b4ba4dd'),
    'gpus.py': (8311, '7a28d5c2692b776e'),
    'host_render.py': (10983, 'a0139507d9f7fe7f'),
    'houdini_local.py': (48596, '7902f068c70d702b'),
    'jobs.py': (8732, '88bd5f01eed7897c'),
    'ledger.py': (17327, '70425e75fb216f01'),
    'monitoring.py': (6320, '941db2f923c756ae'),
    'mq.py': (2124, '174afbd9f49ad86f'),
    'package_runner.py': (9652, '939de3e067b005db'),
    'packages.py': (63842, '42a0a9d34bdf67a8'),
    'pods.py': (35551, '3b64efff30fb33d7'),
    'preflight.py': (45206, 'bea8fc057bc7e32a'),
    'progress.py': (3439, 'c364a012f5cd92e6'),
    'releases.py': (4735, 'c6b364ccfb3534b3'),
    'runpod_api.py': (14539, 'b90960f9860c97fb'),
    'scene_setup.py': (18803, 'c8031966f92b71f3'),
    'smoke.py': (42548, 'ce0c8d36fe763314'),
    'status.py': (458, '2205873427086b87'),
    'submission.py': (1621, '6b428346b41fab92'),
    'sync.py': (20962, '6ae2a7b7e1e25f58'),
    'tls.py': (3642, 'f3e50ea6ebd0308f'),
    'tools.py': (4290, 'c5d3b026f125578f'),
    'usddeps.py': (9631, '3c7192d3bd94d07f'),
    'volume.py': (10038, 'dc11b185a58c9262'),
    'worker_client.py': (10012, '407feb11016dd01c'),
}
# END baked


def farmCodeMessage():
    """Why this session cannot cook, or None. Never raises.

    The guard functions above are embedded rather than run at module scope
    on purpose: this module also backs the parameter DEFAULT expressions,
    which are evaluated while Houdini draws the parameter dialog, and a
    module that raises there turns every redraw into an error. So the check
    is a call, made where it matters -- at the start of a cook, and by the
    Preview button -- rather than an import-time explosion.
    """
    try:
        import rpfarm as _pkg

        return _stale_module_message(
            _MIN_RPFARM_VERSION,
            getattr(_pkg, "VERSION", None),
            _ASSET_BUILT_AGAINST_VERSION,
            _RPFARM_ROOT,
            _asset_mismatch(_pkg, _ASSET_FINGERPRINT),
            bool(_ASSET_FINGERPRINT),
        )
    except Exception:
        return None  # a diagnostic must never become the failure


def cfg_default(name, fallback=""):
    """The value ~/.rpfarm/config.toml gives *name*, or *fallback*."""
    try:
        from rpfarm import config as rpcfg

        return rpcfg.config_value(name, fallback)
    except Exception:
        return fallback


def project_default():
    """What onGenerate would fall back to for Project: the $JOB basename.

    Not a config field -- the project is per scene -- but the same problem:
    an empty Project field never said which folder on the volume the upload
    was actually going to.
    """
    try:
        import hou

        job = hou.getenv("JOB") or hou.expandString("$HIP") or ""
        return os.path.basename(os.path.normpath(job)) if job else ""
    except Exception:
        return ""


# -- buttons (these may raise: unlike the default expressions above, they run
#    on a click, not while the parameter dialog is being drawn) ---------------


def _say(message):
    print("[rpfarm-upload] {}".format(message))


def _warn(message):
    """A warning badge on THIS node.

    Via `self` (a pdg.Node), never hou.Node.addWarning on another node --
    that raises "Cannot set error badges on other nodes" and zeroes the
    generated work items, which is how a warning once cost a whole cook.
    """
    try:
        self.addWarning(message, False)
    except Exception:
        pass


def effectiveContext():
    try:
        import hou
        from rpfarm import context
        return context.resolve(hou.pwd()).description()
    except Exception as exc:
        return str(exc)


def previewUpload(kwargs):
    """Show the upload plan without cooking anything.

    The same single window the cook shows, forced on regardless of the
    Confirm toggle. Any choice made here is kept, so previewing IS how you
    set the selection for a batch cook that will never open a window.
    """
    node = kwargs["node"]
    import hou

    from rpfarm import config as rpcfg
    from rpfarm import deps as rpdeps
    from rpfarm import preflight as rppf
    from rpfarm import usddeps as rpusd
    from rpfarm.runpod_api import RunPodAPI

    scope = node.evalParm("rpfarm_scope") or rpdeps.SCOPE_BRANCH
    asset_regex = node.evalParm("rpfarm_assetregex") or rpdeps.ASSET_REGEX
    exclude_pattern = node.evalParm("rpfarm_excludepattern")
    scan = rpdeps.scan_refs(scope=scope, node=node, log=_say,
                            asset_regex=asset_regex, exclude_pattern=exclude_pattern)
    rops = rpdeps.cook_rops(node, log=_say)
    usd = rpusd.collect_usd_refs(
        rops, log=_say, deep=bool(node.evalParm("rpfarm_usddeep")),
        asset_regex=asset_regex) if rops else []
    usd = rpdeps.remove_pattern(usd, exclude_pattern)

    # Same farm-state lookup the generate callback makes -- best-effort,
    # this button is exactly as entitled to a wrong-scene warning as a real
    # cook is, and it is already on the main thread so there is no bridge
    # to worry about here.
    job_dir = hou.getenv("JOB") or hou.expandString("$HIP")
    project = node.evalParm("rpfarm_project") or os.path.basename(os.path.normpath(job_dir))
    try:
        from rpfarm import context as rpcontext
        farm_context = rpcontext.resolve(node, job_dir)
        cfg, project = farm_context.cfg, farm_context.project
        api = RunPodAPI(cfg.api_key)
    except Exception:
        cfg = api = None
    remote_project = "/workspace/projects/{}/{}".format(cfg.user, project) if cfg else None
    env_refs, env_problems = rpdeps.environment_refs(
        getenv=lambda name: hou.getenv(name) or os.environ.get(name), log=_say)
    env_refs = rpdeps.remove_pattern(env_refs, exclude_pattern)
    for problem in env_problems:
        _warn(problem)

    try:
        paths = rppf.choose_uploads(node, scan, usd, env_refs, ask=True, log=_say,
                                    job_dir=job_dir, remote_project=remote_project,
                                    cfg=cfg, api=api, intent="preview")
    except rppf.UploadCancelled:
        _say("preview closed with Cancel -- nothing changed")
        return
    _say("{} file(s)/directory(ies) would upload".format(len(paths)))


def clearExclusions(kwargs):
    """Forget every answer given in the window -- back to the defaults."""
    node = kwargs["node"]
    node.parm("rpfarm_exclude").set("")
    _say("window answers cleared: everything checked again except outputs")
'''

GENERATE_CODE = '''\
# Called when this node should generate new work items from upstream items.
#
# self             -   A reference to the current pdg.Node instance
# item_holder      -   A pdg.WorkItemHolder for constructing and adding work items
# upstream_items   -   The list of work items in the node above, or empty list if there are no inputs
# generation_type  -   The type of generation, e.g. pdg.generationType.Static, Dynamic, or Regenerate
#
# See this node's Help for the design: modes, the Install Houdini preset,
# why the post-command runs as one extra work item instead of once per
# package (Ruling R3), and why items dispatch out of process by default
# through rpfarm.package_runner (Ruling R22).

import json
import os
import pathlib
import shlex
import shutil
import sys
import tempfile

import hou

# generate runs in this same hython process (not a spawned one, unlike
# cooktask below), but rpfarm still is not on Houdini's own sys.path --
# same bootstrap as cooktask, see its comment for why $RPFARM_ROOT/~/.rpfarm/src.
_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

# BEFORE anything is imported from rpfarm, and before a single machine is
# rented: is the code in this process the code on disk? A Houdini that was
# open when the checkout updated runs this NEW asset against the OLD
# package, and on 2026-09-05 that cost the owner a cook -- two work items,
# both CookedFail, with nothing in the message about the real cause.
#
# The check is the package's own content fingerprint against the files
# (hou.phm() -> farmCodeMessage above), NOT a version number. The version
# check that used to be here slept through seven commits because nobody
# remembered to bump rpfarm.VERSION, which is exactly what a guard that
# depends on discipline does.
_stale = self.topNode().parent().hdaModule().farmCodeMessage()
if _stale:
    raise hou.NodeError(_stale)

from rpfarm import config as rpcfg
from rpfarm import deps as rpdeps
from rpfarm import houdini_local as rphou
from rpfarm import packages as rppkg
from rpfarm import preflight as rppf
from rpfarm import usddeps as rpusd
from rpfarm.runpod_api import RunPodAPI


def _say(message):
    print("[rpfarm-upload] {}".format(message))

node = self.topNode().parent()

mode = node.evalParm("rpfarm_mode")
preset = node.evalParm("rpfarm_preset")
job_dir = hou.getenv("JOB") or hou.expandString("$HIP")
project = node.evalParm("rpfarm_project") or os.path.basename(os.path.normpath(job_dir))
package_gb = node.evalParm("rpfarm_packagegb")
grouping = node.evalParm("rpfarm_grouping") or "packages"

from rpfarm import context as rpcontext
farm_context = rpcontext.resolve(node, job_dir)
cfg, project = farm_context.cfg, farm_context.project
context_path = rpcontext.snapshot(cfg)
user = cfg.user
# Same formula build_upload_items itself uses (rpfarm/packages.py) -- kept
# in sync by a test, not by hoping nobody edits one without the other.
remote_project = "/workspace/projects/{}/{}".format(user, project)
try:
    api = RunPodAPI(cfg.api_key)
except Exception:
    api = None

custom = []
for i in range(1, node.evalParm("rpfarm_custom") + 1):
    local = node.evalParm("rpfarm_local{}".format(i))
    remote = node.evalParm("rpfarm_remote{}".format(i))
    if local and remote:
        custom.append((local, remote))
post_command = node.evalParm("rpfarm_postcmd")

if preset == "install_houdini":
    tar = node.evalParm("rpfarm_houtar")
    ver = node.evalParm("rpfarm_houver")
    custom, post_command = rppkg.houdini_install_preset(tar, ver)
    mode = "custom"

# What this cook uploads, from two places no single Houdini API covers,
# shown in ONE window.
#
# 1. Parameter references -- hou.fileReferences(), narrowed to this cook's
#    branch. Output parameters (pdg_workingdir, outputimage) come back on
#    their own list and appear unchecked rather than dropped out of sight.
# 2. USD references -- what a stage reads that no parameter names. On
#    airship_v013.hip that is 77 files, including both .usdc layers the
#    render is made of; a farm render without them produces nothing.
#
# Houdini's own dependency dialog cannot carry (2): it is fed entirely by
# hou.fileReferences(), its rows ARE (hou.Parm, pattern) pairs, and its
# five arguments offer no way to add a row that is not one. So the window
# is ours, and it is the only one -- two windows for one question was
# rejected, correctly.
#
# Custom mode skips all of it: there the artist typed the paths.
refs = []
if mode == "deps":
    ask = rppf.wants_window(node, log=_say)
    asset_regex = node.evalParm("rpfarm_assetregex") or rpdeps.ASSET_REGEX
    exclude_pattern = node.evalParm("rpfarm_excludepattern")
    scan = rpdeps.scan_refs(
        scope=node.evalParm("rpfarm_scope") or rpdeps.SCOPE_BRANCH, node=node, log=_say,
        asset_regex=asset_regex, exclude_pattern=exclude_pattern)
    rops = rpdeps.cook_rops(node, log=_say)
    usd = rpusd.collect_usd_refs(
        rops, log=_say, deep=bool(node.evalParm("rpfarm_usddeep")),
        asset_regex=asset_regex) if rops else []
    usd = rpdeps.remove_pattern(usd, exclude_pattern)

    # Dependencies named by the ENVIRONMENT, which no scanner can see: there
    # is no Parm for $OCIO, so hou.fileReferences() has nothing to report and
    # never will. Left alone, the farm renders with Houdini's own bundled
    # colour config while the artist works in theirs -- no error, wrong
    # colour, because `auto`/`Automatic` texture handling is resolved BY the
    # config that happens to be loaded.
    env_refs, env_problems = rpdeps.environment_refs(
        getenv=lambda name: hou.getenv(name) or os.environ.get(name), log=_say)
    env_refs = rpdeps.remove_pattern(env_refs, exclude_pattern)
    for problem in env_problems:
        _say("WARNING: " + problem)
        _warn(problem)
    try:
        refs = rppf.choose_uploads(node, scan, usd, env_refs, ask=ask, log=_say,
                                   job_dir=job_dir, remote_project=remote_project,
                                   cfg=cfg, api=api)
    except rppf.UploadCancelled as e:
        # A deliberate no, not a failure -- but generation has to stop, and
        # a NodeError is the only way to stop it that PDG reports plainly.
        raise hou.NodeError(str(e))

package_gb = node.evalParm("rpfarm_packagegb")
grouping = node.evalParm("rpfarm_grouping") or "packages"
items = rppkg.build_upload_items(mode, job_dir, user, project, custom, refs, package_gb, grouping=grouping)
_say("Upload plan: {} file(s), {} item(s), {} GB limit per package ({})".format(
    sum(len(it["files"]) for it in items), len(items), package_gb, grouping))

if mode == "deps":
    # runpodfarm_scheduler's _loadPathMap merges this in -- see
    # rpfarm.packages.write_pathmap. resolve_entries is pure/cheap
    # (filesystem stats only) so recomputing it here for the path map
    # alone, rather than widening build_upload_items' return shape, is a
    # deliberate small duplication.
    remote_project = "/workspace/projects/{}/{}".format(user, project)
    _, path_map = rpdeps.resolve_entries(refs, job_dir, remote_project)
    rppkg.write_pathmap(job_dir, path_map)

# Volume auto-grow (design spec 4.1) is NOT done here. Task 12 landed the
# real check as rpfarm.packages.maybe_grow_volume, which package_runner
# calls once per item with the volume's true used-space (housekeeping's
# disk-usage against the volume's real provisioned size, Ruling R27).
# The coarse placeholder that used to sit here -- grow when THIS cook's
# own upload alone would cross 85% of capacity, sized
# ceil((total+upload)/10)*10 -- ran first and over-provisioned: a 45GB
# upload onto a 50GB volume grew it to 100GB where the real check grows
# it to 60GB. RunPod volumes never shrink and bill on allocated size, so
# that was money you could not get back. Do not reintroduce it.

compress = rppkg.resolve_compress_flag(node.evalParm("rpfarm_compress"))

# Ruling R22: uploads must not block Houdini's UI, and progress must be
# visible per package -- so items dispatch OUT of process by default
# (rpfarm_inprocess off). PDG's pythonprocessor only runs a callback-only
# item out of process when it never happens at all (see cooktask below);
# the actual out-of-process path needs a shell ".command" instead, so
# each item's command points at rpfarm.package_runner (rpfarm/package_runner.py),
# fed the item (+ this node's compress flag) as a small JSON file. That
# module is stdlib-only like the rest of rpfarm, so a plain interpreter is
# enough -- no need to pay hython's startup cost, or its licence, per
# package. sys.executable here is hython itself, so it is the fallback, not
# the choice; see the resolver below.
in_process = bool(node.evalParm("rpfarm_inprocess"))
# The interpreter is resolved explicitly and then EXECUTED to read its real
# version, never taken off PATH on faith. A Houdini launched from the macOS
# Dock inherits a minimal PATH where "python3" is Xcode's 3.9, which has no
# tomllib, so every package item died on `import rpfarm.config` before doing
# any work -- while every headless run went through a shell whose PATH started
# with a modern python and so passed for the wrong reason. The resolver
# prefers the plain python bundled with THIS running Houdini ($HFS): always
# present, modern, and no licence (hython would take one per package). If it
# cannot find anything that can import rpfarm it raises rather than handing
# the item an interpreter that will fail at the far end of a cook.
try:
    python3, python3_why = rphou.resolve_package_python(
        hfs=hou.getenv("HFS") or hou.expandString("$HFS"))
except rphou.NoUsablePythonError as e:
    raise hou.NodeError(str(e))
print("[rpfarm-upload] package runner interpreter: {} ({})".format(python3, python3_why))
items_dir = tempfile.mkdtemp(prefix="rpfarm_upload_items_")
# "python3 -m rpfarm.package_runner" has to resolve the rpfarm package
# BEFORE any of package_runner's own code (its $RPFARM_ROOT/~/.rpfarm/src
# bootstrap included) ever runs -- -m resolution happens at interpreter
# startup, off sys.path, which for a plain "python3" subprocess is not
# this checkout unless something puts it there. $RPFARM_ROOT alone does
# NOT do that (verified live: it fixed nothing here -- see the Task 9
# report); PYTHONPATH does, so it's set explicitly and unconditionally
# from where `rppkg` -- already imported into THIS process -- actually
# lives, rather than trusting cwd or an env var to happen to line up.
rpfarm_pkg_root = str(pathlib.Path(rppkg.__file__).resolve().parent.parent)


def _make_command(item_json_path):
    # No shell involved -- the scheduler runs this via shlex.split() +
    # subprocess.Popen(..., no shell=True), so a "VAR=value cmd" shell
    # prefix does NOT set an env var here: it is parsed as the literal
    # (nonexistent) executable "VAR=value" and fails instantly with no
    # output at all (verified live -- see the Task 9 report). PYTHONPATH
    # goes through the item's own environment (addEnvironmentVar) instead.
    return "{} -m rpfarm.package_runner {}".format(shlex.quote(python3), shlex.quote(item_json_path))


def _write_item_payload(name, it, compress_flag):
    path = os.path.join(items_dir, "{}.json".format(name))
    with open(path, "w") as f:
        json.dump({"item": it, "compress": compress_flag, "context_path": context_path}, f)
    return path


def _set_out_of_process(wi, item_json_path):
    wi.setCommand(_make_command(item_json_path))
    wi.addEnvironmentVar("PYTHONPATH", rpfarm_pkg_root)


pkg_items = []
for it in items:
    name = "upload_{:03d}_of_{:03d}".format(it["index"] + 1, len(items))
    wi = item_holder.addWorkItem(name=name, inProcess=in_process)
    wi.setStringAttrib("rpfarm_item", json.dumps(it))
    wi.setStringAttrib("rpfarm_role", "package")
    wi.setIntAttrib("bytes", it["bytes"])
    wi.setIntAttrib("files", len(it["files"]))
    wi.setIntAttrib("package_index", it["index"] + 1)
    wi.setIntAttrib("package_count", len(items))
    wi.setStringAttrib("package_files", "\\n".join(f[0] for f in it["files"]))
    wi.setStringAttrib("phase", "Waiting")
    wi.setFloatAttrib("percent", 0.0)
    wi.setCookPercent(0.0)
    wi.setIntAttrib("bytes_done", 0)
    wi.setIntAttrib("bytes_total", it["bytes"])
    wi.setIntAttrib("compress", 1 if compress else 0)
    if not in_process:
        _set_out_of_process(wi, _write_item_payload(name, it, compress))
    pkg_items.append(wi)

if post_command and pkg_items:
    post_dict = {
        "index": len(items),
        "local_root": "",
        "remote_root": "",
        "files": [],
        "bytes": 0,
        "post_command": post_command,
    }
    post_item = item_holder.addWorkItem(name="upload_post", inProcess=in_process)
    post_item.setStringAttrib("rpfarm_item", json.dumps(post_dict))
    post_item.setStringAttrib("rpfarm_role", "post")
    post_item.setIntAttrib("bytes", 0)
    post_item.setIntAttrib("files", 0)
    post_item.setIntAttrib("compress", 0)
    if not in_process:
        _set_out_of_process(post_item, _write_item_payload("upload_post", post_dict, False))
'''

ADDDEPS_CODE = '''\
# Called when the node has generated work items so that dependencies can
# be added between work items in this node.
#
# self              -   A reference to the current pdg.Node instance
# dependency_holder -   A pdg.WorkItemHolder for adding pairs of items that should have a dependency
# internal_items    -   The list of items, either all static items or a group of dynamic items
# is_static         -   Boolean indicating if the items list contains static items
#
# Ruling R3: the post-command work item ("upload_post", rpfarm_role=post)
# must cook after every package item, not interleaved with them. Item-to-
# item dependencies can't be wired up inline while items are still being
# created in onGenerate (PDG resolves them in this separate pass), so this
# is where the post item is made to depend on every package item.

posts = [it for it in internal_items if it.stringAttribValue("rpfarm_role") == "post"]
packages = [it for it in internal_items if it.stringAttribValue("rpfarm_role") == "package"]
for post_item in posts:
    for pkg_item in packages:
        dependency_holder.addDependency(post_item, pkg_item)
'''

COOKTASK_CODE = '''\
# Called when an in process work item needs to cook. In process work items
# are created by passing the [in_process] flag when constructing the item
# in the [onGenerate] callback -- onGenerate above only does that when the
# Cook in process toggle (rpfarm_inprocess) is on; by default (Ruling R22)
# every item instead carries a shell ".command" (rpfarm.package_runner)
# and cooks out of process through this node's own localscheduler, in
# parallel across its slots, without blocking Houdini's UI -- see this
# node's Help. This callback is the FALLBACK path for the toggle: kept
# working (and still fully unit-testable via run_upload_item) because it
# costs nothing to keep, and because it is a straightforward way to debug
# a package's upload logic directly in Houdini's own process without
# going through package_runner's subprocess + pdgcmd round trip.
#
# self              -   A reference to the current pdg.Node instance
# work_item         -   The work item being cooked by this callback

import json
import os
import pathlib
import sys
import time

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

from rpfarm import config as rpcfg
from rpfarm import packages as rppkg
from rpfarm import pods as rppods
from rpfarm import sync as rpsync
from rpfarm.runpod_api import RunPodAPI, pod_public_endpoint
from rpfarm.worker_client import WorkerClient

from rpfarm import context as rpcontext
cfg = rpcontext.resolve(self.topNode().parent()).cfg
api = RunPodAPI(cfg.api_key)
token = rpcfg.session_token()
with open(cfg.ssh_key_path + ".pub") as f:
    pubkey = f.read()

pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
ip, port = pod_public_endpoint(pod, 22)
sftp = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)
sync_client = WorkerClient(pod["id"], token)

item = json.loads(work_item.stringAttribValue("rpfarm_item"))
compress = bool(work_item.intAttribValue("compress"))


def progress_cb(done, total, speed):
    work_item.setStringAttrib("progress", "{:.0f}/{:.0f} MB".format(done / 2**20, total / 2**20))


# Same auto-grow check the out-of-process path runs (rpfarm/package_runner.py):
# per item, against the volume's real used-space. This debug path must not be
# the one that silently fills the volume.
autogrow_note = rppkg.maybe_grow_volume(api, cfg, sync_client, item.get("bytes") or 0, log=print)
if autogrow_note != "ok":
    work_item.setStringAttrib("volume_autogrow", autogrow_note)

t0 = time.time()
stats = rppkg.run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb)
elapsed = time.time() - t0

work_item.setFloatAttrib("seconds", elapsed)
work_item.setFloatAttrib("mbps", stats["bytes"] / 2**20 / max(1e-3, elapsed))
work_item.setIntAttrib("bytes", stats["bytes"])
work_item.setIntAttrib("files", stats["files"])
'''

HELP_TEXT = '''\
#type: node
#context: top
#internal: runpodfarmupload
#icon: opdef:/Top/runpodfarmupload?IconSVG
= RunPodFarm Upload =
Upload scene dependencies or explicit files to the selected farm context.

Connect Upload through a Wait for All gate before the compute nodes. One upload item represents a package, or one file when that grouping is selected.

@parameters
Mode:
    #id: rpfarm_mode
    Project Dependencies collects this branch's scene, USD and environment references. Custom Paths uses the explicit local → remote pairs.
Farm Context:
    #id: rpfarm_context
    Effective user, project, datacenter and volume from the selected scheduler. Override Project is an explicit exception.
Review Local / Farm Files:
    #id: rpfarm_preview
    Review both trees and the package plan. Green means identical size and modification time; amber means different; unknown is not absence. Checkboxes select uploads. Highlighted rows select files for confirmed deletion. Local files go to Trash. Confirmed deletions are immediate.
    Save Selection in a manually opened review does not upload. Continue Upload in the pre-cook window continues that cook.
Work Items:
    #id: rpfarm_grouping
    Packages by size reduces process overhead. One per file exposes each file as a work item. Hover the package list in File Review to inspect its contents.
Compression:
    #id: rpfarm_compress
    Auto uses the measured uplink when available. Files already identical on the farm are skipped before compression.
Post-command:
    #id: rpfarm_postcmd
    Runs on the sync pod once, after all packages finish.

@related
- [Node:top/ropfetch]
- [Node:top/runpodfarmdownload]
'''


def main():
    obj = hou.node("/obj")
    build_net = obj.createNode("topnet", "rpfarm_upload_build")
    sn = build_net.createNode("subnet", "runpodfarmupload_build")

    pp = sn.createNode("pythonprocessor", "pythonprocessor1")
    localsched = sn.createNode("localscheduler", "localscheduler")

    pp.setInput(0, sn.indirectInputs()[0])
    out0 = sn.node("output0")
    out0.setInput(0, pp, 0)

    # A plain relative string ("localscheduler") does NOT resolve here --
    # PDG's scheduler-override lookup for a node nested inside a subnet
    # only accepts an absolute node path (verified empirically: a bare
    # relative name silently falls back to "network default" with a
    # warning, meaning items dispatch to whatever scheduler governs the
    # OUTER topnet this asset is dropped into -- runpodfarm_scheduler in
    # real usage, causing exactly the recursion Ruling forbids). A Python
    # expression re-resolves the sibling localscheduler's absolute path at
    # cook time, so the override is correct regardless of where a user
    # instances this asset.
    pp.parm("topscheduler").setExpression(
        'hou.pwd().parent().path() + "/localscheduler"', language=hou.exprLanguage.Python
    )
    pp.parm("generate").set(GENERATE_CODE)
    pp.parm("addinternaldependencies").set(ADDDEPS_CODE)
    pp.parm("cooktask").set(COOKTASK_CODE)

    pp.moveToGoodPosition()
    localsched.moveToGoodPosition()
    out0.moveToGoodPosition()

    ptg = hou.ParmTemplateGroup()

    mode_pt = hou.StringParmTemplate(
        "rpfarm_mode", "Mode", 1, default_value=("deps",),
        menu_items=("deps", "custom"), menu_labels=("Project Dependencies", "Custom Paths"),
    )
    mode_pt.setHelp("Project dependencies: hou.fileReferences() plus the hip file itself. Custom paths: exactly the local -> remote pairs below.")

    # Default: the branch. The whole scene is a superset that in the field
    # was 9x too big (794 files / 9.97 GB against 113 / 1.32 GB), and every
    # extra byte is uplink time before a rented GPU starts. When the branch
    # cannot be worked out at all, collect_refs falls back to the whole
    # scene and says so in the log -- so the safe direction is still the
    # automatic one.
    scope_pt = hou.StringParmTemplate(
        "rpfarm_scope", "Dependencies", 1, default_value=("branch",),
        menu_items=("branch", "scene"),
        menu_labels=("This cook's branch", "Whole scene"),
    )
    scope_pt.setHelp(
        "Branch: only what the ROPs this TOP network fetches actually read. "
        "Whole scene: every file reference in the hip file. Neither can see "
        "assets a USD layer references from inside itself -- those never "
        "appear in hou.fileReferences() in the first place."
    )
    scope_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    confirm_pt = hou.ToggleParmTemplate("rpfarm_confirm", "Confirm Before Upload", default_value=True)
    confirm_pt.setHelp(
        "Show the plan -- every reference with its size, heaviest first -- and "
        "upload only what stays checked. Off for batch/headless cooks, which "
        "then use the selection this node already remembers. A cook with no UI "
        "(hython) never shows the window regardless."
    )
    confirm_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    preview_pt = hou.ButtonParmTemplate("rpfarm_preview", "Review Local / Farm Files...")
    preview_pt.setHelp(
        "Open that window now, without cooking. This is how you set the "
        "selection for a batch cook that will never open it."
    )
    preview_pt.setScriptCallback("hou.phm().previewUpload(kwargs)")
    preview_pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    preview_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    clearexclude_pt = hou.ButtonParmTemplate("rpfarm_clearexclude", "Re-check Everything")
    clearexclude_pt.setHelp("Forget every reference unchecked in the window.")
    clearexclude_pt.setScriptCallback("hou.phm().clearExclusions(kwargs)")
    clearexclude_pt.setScriptCallbackLanguage(hou.scriptLanguage.Python)
    clearexclude_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")
    clearexclude_pt.hide(True)  # Reset is part of File Review; keep saved-scene compatibility.

    # Kept in sync with rpfarm.deps.ASSET_REGEX by
    # tests/test_hda_assets.py -- the builder cannot import rpfarm (it runs
    # under hython against whatever is installed), so the default is spelled
    # out here and a test holds the two together.
    assetregex_pt = hou.StringParmTemplate(
        "rpfarm_assetregex", "Varying Path Pattern", 1,
        default_value=(r"(_|\.)\d+(_|\.)|<udim>|<u>|<v>|<u2>|<v2>|<obj_name>",))
    assetregex_pt.setHelp(
        "Whatever matches this in an evaluated path is replaced by * and globbed, "
        "so tile sets and frame sequences upload whole. Houdini expands $F4 to the "
        "CURRENT frame before anything sees it, so `.0001.` is what a cache sequence "
        "looks like here -- match it or upload one frame. Default is Conductor's "
        "(ciohoudini). Wildcards only ever return files that exist, so a pattern that "
        "is too wide costs a directory listing and some visible extra rows in the "
        "window; too narrow silently loses tiles or frames."
    )
    assetregex_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    excludepattern_pt = hou.StringParmTemplate(
        "rpfarm_excludepattern", "Exclude Paths Matching", 1, default_value=("",))
    excludepattern_pt.setHelp(
        "Comma-separated glob patterns dropped from the plan before the window opens, "
        "e.g. */backup/*, *.abc. Matched against the whole path. Empty means nothing "
        "is excluded."
    )
    excludepattern_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    # On by default: a layer file names things the composed stage never
    # shows (an unloaded payload, an unselected variant), and a missing
    # texture is a rendered-nothing GPU hour. Measured cost on the field
    # scene: +71 files / 1.6 GB of source textures the render did not read,
    # all of them visible and uncheckable in the USD window.
    usddeep_pt = hou.ToggleParmTemplate("rpfarm_usddeep", "Scan USD Layer Files Too", default_value=True)
    usddeep_pt.setHelp(
        "On: also run UsdUtils.ComputeAllDependencies on every USD layer on disk, "
        "which finds what the layer file references even when this session never "
        "composed it -- and expands <UDIM> itself. Off: only what the live stage "
        "reads. 0.02s per layer on a 1.6 GB layer, but reported slow on very large "
        "scenes; the cook logs the time it took."
    )
    usddeep_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_mode != deps }")

    # Hidden, but a real parameter and not user data: it has to travel with
    # the scene, survive save/load, and be diffable when someone asks why a
    # file did not upload. ONE parameter holds the whole answer -- what was
    # unchecked and which outputs were re-checked -- written by one window
    # in one moment, because two stores for one question is how they end up
    # disagreeing.
    exclude_pt = hou.StringParmTemplate("rpfarm_exclude", "Window Answer", 1, default_value=("",))
    exclude_pt.setHelp(
        'JSON {"off": [...], "on": [...]} -- what the artist unchecked, and which '
        "output references they re-checked. Cleared by Re-check Everything.")
    exclude_pt.hide(True)
    # Matches runpodfarm_scheduler's own rpfarm_project parm: empty string,
    # with the "basename of $JOB" fallback implemented in onGenerate
    # (Python) rather than as a live default expression here.
    # Task 17: the field shows the folder this upload will really go to
    # (the $JOB basename) instead of sitting empty; typing over it wins, as
    # a literal always beats a default expression. onGenerate's own
    # `evalParm(...) or basename($JOB)` is unchanged and still correct.
    project_pt = hou.StringParmTemplate(
        "rpfarm_project", "Project", 1, default_value=("",),
        default_expression=("hou.phm().project_default()",),
        default_expression_language=(hou.scriptLanguage.Python,),
    )
    project_pt.setHelp(
        "Project folder on the network volume: /workspace/projects/<user>/<project>. "
        "Shows the name of the $JOB directory until you type your own."
    )
    project_override_pt = hou.ToggleParmTemplate('rpfarm_projectoverride', 'Override Project', default_value=False)
    project_pt.setConditional(hou.parmCondType.HideWhen, '{ rpfarm_projectoverride == 0 }')
    context_pt = hou.StringParmTemplate('rpfarm_context', 'Farm Context', 1,
        default_expression=('hou.phm().effectiveContext()',),
        default_expression_language=(hou.scriptLanguage.Python,))
    context_pt.setConditional(hou.parmCondType.DisableWhen, '{ rpfarm_inprocess >= 0 }')
    packagegb_pt = hou.FloatParmTemplate(
        "rpfarm_packagegb", "Package Size (GB)", 1, default_value=(1.5,), min=0.1, max=16, max_is_strict=False
    )
    packagegb_pt.setHelp("Files are grouped into work items no larger than this (a single bigger file still gets its own item).")
    grouping_pt = hou.StringParmTemplate(
        "rpfarm_grouping", "Work Items", 1, default_value=("packages",),
        menu_items=("packages", "files"), menu_labels=("Packages by size", "One per file"))
    grouping_pt.setHelp("Packages reduce process and connection overhead. One per file shows each file as a PDG item. Review Files shows the exact plan before upload.")
    packagegb_pt.setConditional(hou.parmCondType.DisableWhen, "{ rpfarm_grouping == files }")
    compress_pt = hou.StringParmTemplate(
        "rpfarm_compress", "Compression", 1, default_value=("auto",),
        menu_items=("auto", "on", "off"), menu_labels=("Auto", "On", "Off"),
    )
    compress_pt.setHelp(
        "Auto uses the measured uplink when available. Identical farm files are skipped before compression."
    )

    local_pt = hou.StringParmTemplate(
        "rpfarm_local#", "Local", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
    )
    remote_pt = hou.StringParmTemplate("rpfarm_remote#", "Remote", 1, default_value=("",))
    custom_pt = hou.FolderParmTemplate(
        "rpfarm_custom", "Custom Paths", parm_templates=(local_pt, remote_pt),
        folder_type=hou.folderType.MultiparmBlock, default_value=0,
    )
    custom_pt.setHelp("Used in Custom mode (and filled in automatically by the Install Houdini preset). Local may be a file or a directory.")

    postcmd_pt = hou.StringParmTemplate("rpfarm_postcmd", "Post-command", 1, default_value=("",))
    postcmd_pt.setHelp("Shell command run on the sync pod once, after every package in this cook has uploaded. Ignored when empty.")

    preset_pt = hou.StringParmTemplate(
        "rpfarm_preset", "Preset", 1, default_value=("none",),
        menu_items=("none", "install_houdini"), menu_labels=("None", "Install Houdini from tarball"),
    )
    preset_pt.setHelp("Install Houdini from tarball computes Custom Paths + Post-command from the two fields below at generate time.")
    houtar_pt = hou.StringParmTemplate(
        "rpfarm_houtar", "Houdini Tarball", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
    )
    houtar_pt.setHelp("Local path to houdini-<version>-linux_x86_64_gcc14.2.tar.gz.")
    houtar_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_preset != install_houdini }")
    # Task 17: was a hardcoded "22.0.393" that quietly disagreed with
    # houdini_version in config.toml -- the version every pod actually runs.
    houver_pt = hou.StringParmTemplate(
        "rpfarm_houver", "Houdini Version", 1, default_value=("",),
        default_expression=('hou.phm().cfg_default("houdini_version")',),
        default_expression_language=(hou.scriptLanguage.Python,),
    )
    houver_pt.setHelp(
        "Shows houdini_version from ~/.rpfarm/config.toml -- the version the farm "
        "pods run. Type your own to install a different one. Empty means there is "
        "no config yet: run `rpfarm setup`."
    )
    houver_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_preset != install_houdini }")

    # Ruling R22: out of process (this off) is the default -- uploads must
    # not block Houdini's UI. Kept as a toggle rather than removed because
    # it costs nothing and is a straightforward way to debug a package's
    # upload logic directly in Houdini's own process (see cooktask/Help).
    inprocess_pt = hou.ToggleParmTemplate("rpfarm_inprocess", "Cook In Process (debug)", default_value=False)
    inprocess_pt.setHelp(
        "Off (default): packages upload out of process, in parallel, without blocking Houdini. "
        "On: cook in this Houdini session instead -- blocks the UI, one package at a time, useful for debugging."
    )

    for pt in (
        mode_pt, scope_pt, context_pt, project_override_pt, project_pt, grouping_pt, packagegb_pt, compress_pt,
        confirm_pt, usddeep_pt, assetregex_pt, excludepattern_pt,
        preview_pt, clearexclude_pt, exclude_pt,
        custom_pt, postcmd_pt, preset_pt, houtar_pt, houver_pt, inprocess_pt,
    ):
        ptg.append(pt)

    sn.setParmTemplateGroup(ptg)

    # Baked into the generated CreateScript as an "opuserdata" line, so
    # every instance is the right shape from the moment it is created.
    sn.setUserData("nodeshape", NODE_SHAPE)

    if os.path.exists(OUT_HDA):
        os.remove(OUT_HDA)

    new_type = sn.createDigitalAsset(
        name="runpodfarmupload",
        hda_file_name=OUT_HDA,
        description="RunPodFarm Upload",
        min_num_inputs=0,
        max_num_inputs=1,
        ignore_external_references=True,
    )

    definition = new_type.type().definition()
    definition.addSection("Help", HELP_TEXT)
    definition.addSection("PythonModule", PYTHON_MODULE)
    # The icon rides inside the asset rather than as a file on disk:
    # nothing to install, nothing to lose, and it follows the .hda
    # wherever it is copied.
    definition.addSection("IconSVG", ICON_SVG)
    definition.setIcon("opdef:.?IconSVG")
    # setParmTemplateGroup on the node (above) only affects this live
    # instance -- new instances created from the saved asset get their
    # interface from the DEFINITION's own template group, which has to be
    # set explicitly here too, or every new instance is created with zero
    # parms (verified empirically: skipping this line produces a
    # DialogScript with no parm{} blocks at all).
    definition.setParmTemplateGroup(ptg)

    # NOTHING about the internal scheduler here. The expression is baked
    # into the internal pythonprocessor's channel at build time (see
    # pp.parm("topscheduler") above) and is verifiably present in the
    # shipped asset, so re-asserting it on creation bought nothing -- and
    # cost everything, because reaching inside a locked asset needs
    # allowEditingOfContents(), which unlocks the instance PERMANENTLY. An
    # unlocked instance reads as modified, stops following its definition,
    # and saves its whole internal network into the .hip. Ruling R53.
    definition.addSection(
        "OnCreated",
        'node = kwargs["node"]\n'
        + FAMILY_ONCREATED,
    )

    # Put this node in the TAB menu, in the one submenu all four assets and
    # the setup tool share. Without this section a node is reachable only by
    # typing its exact internal name -- which is why three of the four were
    # missing from TAB while the fourth had a section of its own.
    definition.addSection("Tools.shelf", _hl.asset_tools_shelf())
    definition.setExtraFileOption("OnCreated/IsPython", True)
    definition.setExtraFileOption("OnCreated/IsScript", True)

    definition.save(OUT_HDA, template_node=new_type)

    print("OK built", OUT_HDA, "type", new_type.type().name())

    build_net.destroy()


if __name__ == "__main__":
    main()
