"""One-shot builder for hda/runpodfarm_download.hda.

Task 10 -- the mirror image of ``scripts/build_runpodfarm_upload_hda.py``
(read that file's own docstring first; this one only documents where
download differs). Builds a Top/subnet node containing a pythonprocessor1
(work item generation/cook) and a localscheduler (forces this node's items
to cook on PDG's local scheduler regardless of the parent topnet's own
scheduler -- same reasoning and the same belt-and-suspenders OnCreated
event as the upload node, see its Help), wires them up, sets the
``rpfarm_*`` parameter interface, converts the subnet to a digital asset,
and saves it as a single packed .hda file at the path given on the command
line. Items dispatch out of process through ``rpfarm.package_runner``
(Ruling R22, same runner module the upload node uses -- see Task 10's
addendum for why a shared runner was chosen over a second file) by
default.

Regenerate the checked-in asset with, e.g.::

    HYTHON=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython
    OUT=/tmp/runpodfarm_download.hda
    "$HYTHON" scripts/build_runpodfarm_download_hda.py "$OUT"
    rm -rf hda/runpodfarm_download.hda
    hotl -t hda/runpodfarm_download.hda "$OUT"          # git-tracked expanded form
    hotl -l hda/runpodfarm_download.hda \\
        ~/Library/Preferences/houdini/22.0/otls/runpodfarm_download.hda  # install

Only ``hotl -l`` (the counterpart of ``-t``) round-trips correctly for this
directory layout -- ``-c``/``-C`` are for the other (``-x``/``-X``) expanded
format and silently produce an interface-less asset here (verified on the
upload node; see its own docstring).
"""
import os
import pathlib
import sys

import hou

_REPO = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from rpfarm import houdini_local as _hl  # noqa: E402
import pdg

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_download.hda"

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
ICON_SVG = (REPO_ROOT / "hda" / "icons" / "runpodfarm_download.svg").read_text()

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
import ast
import hashlib
import os
import pathlib
import sys
root = pathlib.Path(os.environ.get('RPFARM_ROOT', pathlib.Path.home() / '.rpfarm' / 'pkg'))
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
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
    import rpfarm
    return _stale_module_message(_MIN_RPFARM_VERSION, rpfarm.VERSION,
        _ASSET_BUILT_AGAINST_VERSION, root, _asset_mismatch(rpfarm, _ASSET_FINGERPRINT))

def jobMenu():
    from rpfarm import jobs
    return jobs.menu(farm_only=True)
def jobStatus():
    import hou
    from rpfarm import jobs
    return jobs.selected_description(hou.pwd().evalParm('rpfarm_job'))
'''

RESUME_CODE = '''\
stale = self.topNode().parent().hdaModule().farmCodeMessage()
if stale:
    raise RuntimeError(stale)
import os, pathlib, sys
root = pathlib.Path(os.environ.get('RPFARM_ROOT', pathlib.Path.home() / '.rpfarm' / 'pkg'))
if str(root) not in sys.path:
    sys.path.insert(0, str(root))
import json
from rpfarm import context as rpcontext, jobs
from rpfarm.runpod_api import RunPodAPI
node = self.topNode().parent()
job_id = node.evalParm('rpfarm_job')
if not job_id:
    raise RuntimeError('No submitted job selected')
cfg = rpcontext.resolve(node).cfg
job, state_path, manifest = jobs.fetch_results(
    job_id, cfg, RunPodAPI(cfg.api_key), cancel=lambda: self.context.canceling,
    log=lambda text: self.addWarning(text))
self.context.deserializeWorkItems(state_path)
if manifest.get('target') != job.get('target'):
    raise RuntimeError('The result manifest names a different target')
if job.get('state') != 'complete':
    self.addWarning('Job {}: {}. Collecting available outputs.'.format(job_id, job.get('state')))
for record in manifest.get('items', []):
    imported = item_holder.addWorkItem(name='result_' + str(record['id']))
    imported.setStringAttrib('rpfarm_pathmap', record['pathmap'])
    imported.setStringAttrib('rpfarm_delivery_key', record['delivery_key'])
    for path, tag in record['outputs']:
        imported.addOutputFile(path, tag or 'file')
'''

GENERATE_CODE = '''\
# Called when this node should generate new work items from upstream items.
#
# self             -   A reference to the current pdg.Node instance
# item_holder      -   A pdg.WorkItemHolder for constructing and adding work items
# upstream_items   -   The list of work items in the node above, or empty list if there are no inputs
# generation_type  -   The type of generation, e.g. pdg.generationType.Static, Dynamic, or Regenerate
#
# See this node's Help for the design: modes, and why items dispatch out of
# process by default through rpfarm.package_runner (Ruling R22).

import json
import os
import pathlib
import posixpath
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

from rpfarm import config as rpcfg
from rpfarm import houdini_local as rphou
from rpfarm import packages as rppkg
from rpfarm import pods as rppods
from rpfarm.runpod_api import RunPodAPI, RunPodError, pod_public_endpoint
from rpfarm.worker_client import WorkerClient

node = self.topNode().parent()


def _warn(message):
    """Report a problem the way PDG allows from inside generate().

    NOT hou.Node.addWarning: Houdini refuses to badge a node other than the
    one being cooked and raises hou.OperationFailed("Cannot set error badges
    on other nodes"), which turned a warning into a hard generate() failure
    and left this node with ZERO work items -- observed live when one of eight
    rendered frames was missing from the volume. `self` here is the pdg.Node,
    which owns the TOP-side badge and is legal to touch from generation.
    Falls back to stdout, and never raises: a diagnostic that can kill the
    cook is worse than no diagnostic.
    """
    try:
        self.addWarning(message)
    except Exception:
        pass
    print("[rpfarm-download] WARNING: {}".format(message), flush=True)

mode = node.evalParm("rpfarm_mode")
stale = node.hdaModule().farmCodeMessage()
if stale:
    raise RuntimeError(stale)
package_gb = node.evalParm("rpfarm_packagegb")
overwrite = node.evalParm("rpfarm_overwrite")

from rpfarm import context as rpcontext
cfg = rpcontext.resolve(node).cfg
context_path = rpcontext.snapshot(cfg)
api = RunPodAPI(cfg.api_key)
token = rpcfg.session_token()
with open(cfg.ssh_key_path + ".pub") as f:
    pubkey = f.read()

# Both modes need the sync pod: "outputs" to stat remote file sizes for
# packaging, "custom" to list each remote directory's files in the first
# place (there is no local filesystem to walk -- the files only exist on
# the farm volume).
client_holder = []
def _get_sync_client(_cfg=cfg, _api=api, _token=token, _pubkey=pubkey, _holder=client_holder):
    if not _holder:
        pod = rppods.ensure_sync_pod(_api, _cfg, _token, _pubkey)
        _holder.append(WorkerClient(pod['id'], _token))
    return _holder[0]



def _stat_sizes(remotes):
    """One exec() for every distinct remote file in this generate -- not one
    per file (addendum: "одним вызовом на пакет, не по файлу").

    The exit code is deliberately not consulted: `stat` returns non-zero if
    ANY path is missing while still printing every path it found, so trusting
    it discarded good sizes over one absent file. See
    rpfarm.packages.parse_stat_sizes.
    """
    if not remotes:
        return {}
    cmd = "stat -c '%s %n' " + " ".join(shlex.quote(r) for r in remotes)
    result = _get_sync_client().exec(cmd, timeout_s=rppkg._scaled_timeout(0))
    sizes, missing = rppkg.parse_stat_sizes(result.get("stdout"), remotes)
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        _warn(
            "{} of {} expected output file(s) are not on the farm volume, so "
            "they pack as 0 bytes and will report their own error when copied; "
            "the other {} still download. Missing: {}{}".format(
                len(missing), len(remotes), len(remotes) - len(missing), shown,
                ((" (stat: " + (result.get("stderr") or "").strip() + ")")
                 if result.get("stderr") else "")))
    return sizes


# (item dict, parent upstream pdg.WorkItem or None) -- "outputs" mode's
# items each need an explicit parent (see below); "custom" mode's don't
# (this node takes no upstream input in that mode).
planned = []

if mode == "outputs":
    # Each upstream item was tagged with this cook's {local prefix: farm
    # prefix} map by the scheduler's _tagPathMap (hda/runpodfarm_scheduler.hda/
    # .../PythonModule, onSchedule) before it was ever serialized -- reading
    # it off the item, rather than needing a handle on the scheduler node
    # itself, is exactly why that attribute exists (see its own comment).
    # Its farm-side outputs are in resultData; rppkg.localize_via_pathmap
    # turns each one back into the local path the ROP was aimed at by a
    # plain longest-prefix replacement (the same operation localizePath
    # does through PDG's global path map, just entirely self-contained here
    # so this node never needs the scheduler's own Python object).
    #
    # Planned per upstream item, not pooled across all of them: PDG_Generate
    # When is set to AllUpstreamCooked below (this node's own OnCreated/
    # builder), which for a connected input makes onGenerate a DYNAMIC
    # generation pass -- and pdg.WorkItemHolder.addWorkItem then requires an
    # explicit `parent` for every item it creates (verified live: omitting
    # it raises "Dynamic work items must have an explicitly specified
    # parent" -- see the Task 10 report). Keeping one upstream item's
    # outputs as their own build_download_items() call, rather than
    # flattening every upstream item's pairs into one list first, is what
    # makes a single, correct parent available for each resulting item.
    # One line, every generate pass. An upstream item with no output files is
    # the single most common reason this node plans nothing, and without this
    # the symptom is a silent zero -- which is how three rendered frames went
    # undownloaded for three live runs.
    _missing = [u.name for u in upstream_items if not list(u.resultData)]
    print("[rpfarm-download] generate: {} upstream item(s), {} without output "
          "files{}".format(len(upstream_items), len(_missing),
                           " ({})".format(", ".join(_missing[:5])) if _missing else ""),
          flush=True)
    for up in upstream_items:
        raw_map = up.stringAttribValue("rpfarm_pathmap") or ""
        try:
            path_map = json.loads(raw_map) if raw_map else {}
        except ValueError:
            path_map = {}
        item_pairs = []
        result_data = list(up.resultData)
        for rd in result_data:
            pair = rppkg.map_output_pair(rppkg.result_data_path(rd), path_map)
            if pair:
                item_pairs.append(pair)
        if not item_pairs:
            if result_data:
                # Reported outputs that map to nothing local is a real fault
                # (a missing or wrong rpfarm_pathmap), not "nothing to do" --
                # say so instead of quietly planning zero work items.
                _warn(
                    "upstream item {} reported {} output(s), none of which mapped "
                    "back to a local path (rpfarm_pathmap has {} entry(ies))".format(
                        up.name, len(result_data), len(path_map)))
            continue
        from rpfarm import delivery
        delivery_key = up.stringAttribValue('rpfarm_delivery_key') or ''
        already_delivered = bool(delivery_key) and all(
            delivery.valid({'delivery_key': delivery_key}, cfg, [local, remote, 0])
            for remote, local in item_pairs)
        sizes = ({remote: os.path.getsize(local) for remote, local in item_pairs}
                 if already_delivered else _stat_sizes(sorted({r for r, _l in item_pairs})))
        for it in rppkg.build_download_items(mode, item_pairs, package_gb, sizes):
            it['delivery_key'] = delivery_key
            it['already_delivered'] = already_delivered
            it['job_id'] = node.evalParm('rpfarm_job') if node.parm('rpfarm_job') else ''
            planned.append((it, up))

    if not upstream_items:
        _warn("Outputs mode with no upstream input: nothing to download")

elif mode == "custom":
    pairs = []
    sizes = {}
    for i in range(1, node.evalParm("rpfarm_custom") + 1):
        remote_dir = node.evalParm("rpfarm_remote{}".format(i))
        local_dir = node.evalParm("rpfarm_local{}".format(i))
        if not remote_dir or not local_dir:
            continue
        find_cmd = "find {} -type f -printf '%s %p\\\\n'".format(shlex.quote(remote_dir))
        result = _get_sync_client().exec(find_cmd, timeout_s=rppkg._scaled_timeout(0))
        if result.get("exit_code") != 0:
            _warn(
                "listing remote dir {} failed: {}".format(remote_dir, (result.get("stderr") or "").strip())
            )
            continue
        for line in result.get("stdout", "").splitlines():
            line = line.strip()
            if not line:
                continue
            size_str, _sep, remote_path = line.partition(" ")
            try:
                size = int(size_str)
            except ValueError:
                continue
            rel = posixpath.relpath(remote_path, remote_dir)
            if rel == ".":
                local_path = os.path.join(local_dir, os.path.basename(remote_path))
            else:
                local_path = os.path.join(local_dir, rel.replace("/", os.sep))
            pairs.append((remote_path, local_path))
            sizes[remote_path] = size
    for it in rppkg.build_download_items(mode, pairs, package_gb, sizes):
        planned.append((it, upstream_items[0] if upstream_items else None))
else:
    raise hou.NodeError("unknown rpfarm_mode: {}".format(mode))

# Ruling R22 (download side, Task 10): must not block Houdini's UI, and
# progress must be visible per package -- so items dispatch OUT of process
# by default (rpfarm_inprocess off), through the SAME rpfarm.package_runner
# module the upload node uses (see this node's Help for why a shared runner
# was chosen). "kind": "download" in the payload is what routes it to
# rpfarm.packages.run_download_item instead of run_upload_item.
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
print("[rpfarm-download] package runner interpreter: {} ({})".format(python3, python3_why))
items_dir = tempfile.mkdtemp(prefix="rpfarm_download_items_")
# See the upload node's onGenerate for why this has to be resolved from
# rppkg.__file__ (already imported into THIS process) rather than trusted
# to cwd or an inherited PYTHONPATH.
rpfarm_pkg_root = str(pathlib.Path(rppkg.__file__).resolve().parent.parent)


def _make_command(item_json_path):
    # No shell involved -- see the upload node's onGenerate for why a
    # "VAR=value cmd" shell prefix does not work here.
    return "{} -m rpfarm.package_runner {}".format(shlex.quote(python3), shlex.quote(item_json_path))


def _write_item_payload(name, it):
    path = os.path.join(items_dir, "{}.json".format(name))
    with open(path, "w") as f:
        json.dump({"kind": "download", "item": it, "overwrite": overwrite,
                   "context_path": context_path}, f)
    return path


for it, parent in planned:
    # Name from the parent, not a per-call counter: AllUpstreamCooked makes
    # this dynamic generation (see above), and PDG may invoke onGenerate
    # more than once as different upstream items finish cooking -- a plain
    # "download_{:03d}".format(n) restarting at 0 (or continuing from a
    # stale count) on every separate invocation could then collide with a
    # name an earlier invocation already used. it["index"] is only unique
    # WITHIN one upstream item's own build_download_items() call (outputs
    # mode plans each upstream item separately, see above); a given
    # upstream item is only ever a parent once (each work item cooks and
    # reports resultData exactly once), so parent.name + that per-parent
    # index is globally unique across however many onGenerate calls this
    # node ends up getting. "custom" mode has no parent and exactly one
    # build_download_items() call total, so its own it["index"] is already
    # globally unique on its own.
    name = "download_{}_{:03d}".format(parent.name, it["index"]) if parent is not None else "download_{:03d}".format(it["index"])
    kwargs = {"name": name, "inProcess": in_process and not it.get('already_delivered')}
    if parent is not None:
        kwargs["parent"] = parent
    wi = item_holder.addWorkItem(**kwargs)
    wi.setStringAttrib("rpfarm_item", json.dumps(it))
    wi.setStringAttrib("overwrite", overwrite)
    wi.setIntAttrib("bytes", it["bytes"])
    wi.setIntAttrib("files", len(it["files"]))
    wi.setStringAttrib('rpfarm_delivery_key', it.get('delivery_key', ''))
    if it.get('already_delivered'):
        for local, _remote, _size in it['files']:
            wi.addOutputFile(local, 'file')
        wi.setIntAttrib('rpfarm_delivered', len(it['files']))
        wi.setStringAttrib('phase', 'Already delivered')
        continue
    if not in_process:
        wi.setCommand(_make_command(_write_item_payload(name, it)))
        wi.addEnvironmentVar("PYTHONPATH", rpfarm_pkg_root)
'''

COOKTASK_CODE = '''\
# Called when an in process work item needs to cook. In process work items
# are created by passing the [in_process] flag when constructing the item
# in the [onGenerate] callback -- onGenerate above only does that when the
# Cook in process toggle (rpfarm_inprocess) is on; by default (Ruling R22)
# every item instead carries a shell ".command" (rpfarm.package_runner) and
# cooks out of process through this node's own localscheduler, in parallel
# across its slots, without blocking Houdini's UI -- see this node's Help.
# This callback is the FALLBACK path for the toggle: kept working (and
# still fully unit-testable via run_download_item) because it costs nothing
# to keep, and is a straightforward way to debug a package's download logic
# directly in Houdini's own process without going through package_runner's
# subprocess + pdgcmd round trip.
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
overwrite = work_item.stringAttribValue("overwrite") or "newer"


def progress_cb(done, total, speed):
    work_item.setStringAttrib("progress", "{:.0f}/{:.0f} MB".format(done / 2**20, total / 2**20))


t0 = time.time()
stats = rppkg.run_download_item(item, cfg, sftp, sync_client, overwrite, progress_cb)
for local, _remote, _size in item['files']:
    if not os.path.isfile(local):
        raise RuntimeError('Local output missing after download: ' + local)
    work_item.addOutputFile(local, 'file')
work_item.setIntAttrib('rpfarm_delivered', stats.get('delivered', len(item['files'])))
work_item.setStringAttrib('rpfarm_delivery_key', item.get('delivery_key', ''))
elapsed = time.time() - t0

work_item.setFloatAttrib("seconds", elapsed)
work_item.setFloatAttrib("mbps", stats["bytes"] / 2**20 / max(1e-3, elapsed))
work_item.setIntAttrib("bytes", stats["bytes"])
work_item.setIntAttrib("files", stats["files"])
'''

HELP_TEXT = '''\
#type: node
#context: top
#internal: runpodfarmdownload
#icon: opdef:/Top/runpodfarmdownload?IconSVG
= RunPodFarm Download =
Deliver farm outputs to their original local paths. Cook this node normally.

@parameters
Results From:
    #id: rpfarm_job
    Current Graph cooks the connected input. A selected submitted job restores its checkpoint and result manifest inside this cook. The upstream upload/render branch is excluded, so retrieving a job never submits it again.
Mode:
    #id: rpfarm_mode
    Upstream Outputs uses the original render file paths and mapping. Custom Paths retrieves the specified directories without cooking the connected render branch.
Overwrite:
    #id: rpfarm_overwrite
    Newer preserves a newer local file. Always uses ordinary size/time comparison. Never preserves every existing local file. A preserved file that differs from the farm is not counted as verified delivery for automatic cleanup.

Downloaded files appear as native PDG Output Files. Existing validated delivery receipts skip repeated transfers. Cancelled or failed transfers leave farm results available for another cook.

@related
- [Node:top/runpodfarmupload]
- [Node:top/switch]
'''


def main():
    obj = hou.node("/obj")
    build_net = obj.createNode("topnet", "rpfarm_download_build")
    sn = build_net.createNode("subnet", "runpodfarmdownload_build")

    pp = sn.createNode("pythonprocessor", "pythonprocessor1")
    localsched = sn.createNode("localscheduler", "localscheduler")

    resume = sn.createNode('pythonprocessor', 'submitted_results')
    resume.parm('generate').set(RESUME_CODE)
    custom_source = sn.createNode('pythonprocessor', 'custom_paths')
    custom_source.parm('generate').set('item_holder.addWorkItem()')
    source = sn.createNode('switch', 'source')
    source.setInput(0, sn.indirectInputs()[0])
    source.setInput(1, resume)
    source.setInput(2, custom_source)
    source.parm('input').setExpression(
        "2 if hou.pwd().parent().evalParm('rpfarm_mode') == 'custom' else (1 if hou.pwd().parent().evalParm('rpfarm_job') else 0)", language=hou.exprLanguage.Python)
    source.parm('invalidate').set(0)
    pp.setInput(0, source)
    out0 = sn.node("output0")
    out0.setInput(0, pp, 0)

    # See the upload node's builder for why this must be an absolute-path
    # Python expression, not a bare relative node name.
    pp.parm("topscheduler").setExpression(
        'hou.pwd().parent().path() + "/localscheduler"', language=hou.exprLanguage.Python
    )
    # "outputs" mode's onGenerate reads upstream_items[i].resultData -- which
    # is only populated once an upstream item has actually COOKED, not
    # merely once it has been generated (PDG's default "Generate When" is
    # Automatic, which for this graph shape resolves to AllUpstreamGenerated
    # -- i.e. generate() would fire as soon as the upstream node's work
    # items structurally exist, before they ever run, so resultData would
    # always be empty; live-verified while building this node, see the
    # Task 10 report). AllUpstreamCooked makes PDG defer (and, for dynamic
    # upstream generation, re-invoke) this node's generate() until every
    # upstream item has actually finished cooking.
    pp.parm("pdg_workitemgeneration").set(int(pdg.generateWhen.AllUpstreamCooked))
    pp.parm("generate").set(GENERATE_CODE)
    pp.parm("cooktask").set(COOKTASK_CODE)

    pp.moveToGoodPosition()
    localsched.moveToGoodPosition()
    out0.moveToGoodPosition()

    ptg = hou.ParmTemplateGroup()
    job_pt = hou.StringParmTemplate('rpfarm_job', 'Results From', 1, default_value=('',),
        item_generator_script='return hou.phm().jobMenu()',
        item_generator_script_language=hou.scriptLanguage.Python)
    job_pt.setHelp('Current graph cooks the connected input. A submitted job restores its results inside this cook without rerunning Upload or Render.')
    job_pt.setConditional(hou.parmCondType.HideWhen, '{ rpfarm_mode == custom }')
    job_status_pt = hou.StringParmTemplate('rpfarm_jobstatus', 'Job Status', 1,
        default_expression=("hou.phm().jobStatus()",),
        default_expression_language=(hou.scriptLanguage.Python,))
    job_status_pt.setConditional(hou.parmCondType.DisableWhen, '{ rpfarm_inprocess >= 0 }')
    job_status_pt.setConditional(hou.parmCondType.HideWhen, '{ rpfarm_mode == custom }')

    mode_pt = hou.StringParmTemplate(
        "rpfarm_mode", "Mode", 1, default_value=("outputs",),
        menu_items=("outputs", "custom"), menu_labels=("Upstream Outputs", "Custom Paths"),
    )
    mode_pt.setHelp("Upstream outputs: resultData of every upstream work item, localized via rpfarm_pathmap. Custom paths: exactly the remote -> local pairs below.")

    packagegb_pt = hou.FloatParmTemplate(
        "rpfarm_packagegb", "Package Size (GB)", 1, default_value=(1.5,), min=0.1, max=16, max_is_strict=False
    )
    packagegb_pt.setHelp("Files are grouped into work items no larger than this (a single bigger file still gets its own item).")

    overwrite_pt = hou.StringParmTemplate(
        "rpfarm_overwrite", "Overwrite", 1, default_value=("newer",),
        menu_items=("newer", "always", "never"), menu_labels=("If Newer", "Always", "Never"),
    )
    overwrite_pt.setHelp(
        "newer: rclone --update (skip a local file that is not older than the remote). "
        "always: no extra flag -- rclone's own default comparison, which still skips a file "
        "whose size and mtime already match exactly. never: rclone --ignore-existing."
    )

    remote_pt = hou.StringParmTemplate("rpfarm_remote#", "Remote", 1, default_value=("",))
    local_pt = hou.StringParmTemplate(
        "rpfarm_local#", "Local", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Directory,
    )
    custom_pt = hou.FolderParmTemplate(
        "rpfarm_custom", "Custom Paths", parm_templates=(remote_pt, local_pt),
        folder_type=hou.folderType.MultiparmBlock, default_value=0,
    )
    custom_pt.setHelp("Used in Custom mode: remote directory on the farm volume -> local directory to download it into.")

    # Ruling R22: out of process (this off) is the default. Kept as a
    # toggle for the same debug reason as the upload node's.
    inprocess_pt = hou.ToggleParmTemplate("rpfarm_inprocess", "Cook In Process (debug)", default_value=False)
    inprocess_pt.setHelp(
        "Off (default): packages download out of process, in parallel, without blocking Houdini. "
        "On: cook in this Houdini session instead -- blocks the UI, one package at a time, useful for debugging."
    )

    for pt in (job_pt, job_status_pt, mode_pt, packagegb_pt, overwrite_pt, custom_pt, inprocess_pt):
        ptg.append(pt)

    sn.setParmTemplateGroup(ptg)

    # Baked into the generated CreateScript as an "opuserdata" line, so
    # every instance is the right shape from the moment it is created.
    sn.setUserData("nodeshape", NODE_SHAPE)

    if os.path.exists(OUT_HDA):
        os.remove(OUT_HDA)

    new_type = sn.createDigitalAsset(
        name="runpodfarmdownload",
        hda_file_name=OUT_HDA,
        description="RunPodFarm Download",
        min_num_inputs=0,
        max_num_inputs=1,
        ignore_external_references=True,
    )

    definition = new_type.type().definition()
    definition.addSection('PythonModule', PYTHON_MODULE)
    definition.addSection("Help", HELP_TEXT)
    # The icon rides inside the asset rather than as a file on disk:
    # nothing to install, nothing to lose, and it follows the .hda
    # wherever it is copied.
    definition.addSection("IconSVG", ICON_SVG)
    definition.setIcon("opdef:.?IconSVG")
    # See the upload node's builder for why this has to be set on the
    # DEFINITION's own template group too, not just the live instance.
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
