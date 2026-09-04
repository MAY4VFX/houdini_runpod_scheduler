"""Headless check that the four HDAs really carry the Task 17 look.

``tests/test_hda_assets.py`` proves the checked-in assets say the right
things; only Houdini can prove it *means* them. This script installs the
four assets from this checkout into a throwaway hython session, creates one
of each, and asserts what an artist would otherwise have to look at:

* the icon resolves to the asset's own ``IconSVG`` section,
* the node shape and the family violet are applied on creation,
* every farm-identity parm evaluates to the value in ``config.toml`` --
  including through PDG's own parameter accessor, which is what
  ``_templateId``/``_volumeId``/``_datacenterId`` actually read,
* the API Key parm stays empty and the masked indicator says where the key
  is, and
* the tab order is the one the reorganisation intended, with nothing lost.

It writes a throwaway config into ``$RPFARM_HOME``, so point that at a
scratch directory -- never at a real ``~/.rpfarm``. No pods, no network::

    HFS=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources
    RPFARM_ROOT=$PWD RPFARM_HOME=$(mktemp -d) JOB=/tmp/shotA \\
        "$HFS/bin/hython" scripts/verify_hda_look.py

Exit status is 0 only when every check passed.

``force_use_assets=True`` on every install matters: an artist's machine
already has these four in ``<prefs>/otls`` from ``rpfarm setup``, and those
would otherwise win and quietly verify the PREVIOUS build.
"""
import json, os, sys, tempfile, subprocess
import hou

ROOT = os.environ["RPFARM_ROOT"]
sys.path.insert(0, ROOT)
from rpfarm import config as rpcfg

FAIL = []
def check(label, got, want):
    ok = got == want
    print(("  OK   " if ok else "  FAIL ") + label + ": " + repr(got) + ("" if ok else " != " + repr(want)))
    if not ok:
        FAIL.append(label)

# --- a config to read from --------------------------------------------------
cfg = rpcfg.Config(api_key="rpa_ABCDEFGHIJKL7f3c", user="may", volume_id="vol-abc123",
                   template_id="tpl-xyz789", datacenter="US-KS-2",
                   houdini_version="22.0.393", gpu_priority=["NVIDIA RTX A4500", "NVIDIA GeForce RTX 4090"])
rpcfg.save(cfg)
print("config at", rpcfg.home())

# --- collapse + install the four assets -------------------------------------
hotl = os.path.join(os.environ["HFS"], "bin", "hotl")
tmp = tempfile.mkdtemp(prefix="rpfarm_verify_")
NAMES = {"runpodfarm_scheduler": "runpodfarmscheduler", "runpodfarm_upload": "runpodfarmupload",
         "runpodfarm_download": "runpodfarmdownload", "runpodfarm_stats": "runpodfarmstats"}
for name in NAMES:
    dest = os.path.join(tmp, name + ".hda")
    subprocess.run([hotl, "-l", os.path.join(ROOT, "hda", name + ".hda"), dest], check=True)
    hou.hda.installFile(dest, force_use_assets=True)
    print("installed", dest)

topnet = hou.node("/obj").createNode("topnet", "rpfarm_verify")
nodes = {}
for name, optype in NAMES.items():
    nodes[name] = topnet.createNode(optype, optype)

print("\n=== icon / shape / colour ===")
for name, n in nodes.items():
    print(name, "<-", n.type().definition().libraryFilePath())
    check("  icon", n.type().icon(), "opdef:/Top/%s?IconSVG" % NAMES[name])
    check("  shape", n.userData("nodeshape"), "rpfarm")
    check("  colour", tuple(round(c, 3) for c in n.color().rgb()), (0.549, 0.361, 0.882))
    svg = n.type().definition().sections()["IconSVG"].contents()
    check("  icon svg resolves", svg.strip().startswith("<?xml") and "8b5cf6" in svg, True)

print("\n=== the node shape Houdini will draw ===")
shape = json.load(open(os.path.join(ROOT, "hda", "nodeshapes", "rpfarm.json")))
check("shape name", shape["name"], "rpfarm")
check("shape outline is an octagon", len(shape["outline"]), 8)
check("flag regions", sorted(shape["flags"]), ["0", "1", "2", "3"])

print("\n=== config-backed parameter defaults ===")
s = nodes["runpodfarm_scheduler"]
check("scheduler template id", s.parm("rpfarm_templateid").eval(), "tpl-xyz789")
check("scheduler volume id", s.parm("rpfarm_networkvolumeid").eval(), "vol-abc123")
check("scheduler datacenter", s.parm("rpfarm_datacenter").eval(), "US-KS-2")
check("scheduler gpu list", s.parm("rpfarm_gpulist").eval(), "NVIDIA RTX A4500, NVIDIA GeForce RTX 4090")
check("scheduler api key STAYS EMPTY", s.parm("rpfarm_apikey").eval(), "")
check("scheduler key status masked", s.parm("rpfarm_apikeystatus").eval(), "from config (rpa_...7f3c)")
check("scheduler project", s.parm("rpfarm_project").eval(),
      os.path.basename(os.path.normpath(hou.getenv("JOB") or hou.getenv("HIP") or "")))
u = nodes["runpodfarm_upload"]
check("upload houdini version", u.parm("rpfarm_houver").eval(), "22.0.393")
check("upload project", u.parm("rpfarm_project").eval(), s.parm("rpfarm_project").eval())

print("\n=== a typed value overrides the config ===")
s.parm("rpfarm_templateid").set("tpl-typed-by-hand")
check("override wins", s.parm("rpfarm_templateid").eval(), "tpl-typed-by-hand")
s.parm("rpfarm_apikey").set("rpa_TYPEDBYHANDXXXX")
check("status warns about a typed key",
      "WARNING" in s.parm("rpfarm_apikeystatus").eval() and "rpa_...XXXX" in s.parm("rpfarm_apikeystatus").eval(), True)
s.parm("rpfarm_apikey").set("")
s.parm("rpfarm_templateid").revertToDefaults()
check("revert restores the config value", s.parm("rpfarm_templateid").eval(), "tpl-xyz789")

print("\n=== the tabs ===")
# Houdini renames the folders of a tab set to <first>_N, so go by label.
folders = [pt.label() for pt in s.parmTemplateGroup().entries() if pt.type() == hou.parmTemplateType.Folder]
print("  tabs:", folders)
check("tab order", folders,
      ["Cook", "Farm", "Status", "Volume", "Paths", "Advanced", "Submit As Job", "Message Queue", "RPC Server"])
for parm_name in ("rpfarm_pretaskcmd", "rpfarm_posttaskcmd", "rpfarm_envmulti", "rpfarm_envunset",
                  "rpfarm_houdinimaxthreads", "rpfarm_remoteworkingdir", "rpfarm_verbose",
                  "rpfarm_minpods", "rpfarm_maxcost", "rpfarm_downloadoutputs", "pdg_workingdir",
                  "mqusage", "pdg_rpcbatch", "submitjob", "rpfarm_volume_grow", "rpfarm_status_text"):
    if s.parm(parm_name) is None:
        FAIL.append("missing parm " + parm_name)
        print("  FAIL  missing parm", parm_name)
print("  OK    every pre-existing parm still resolves")

print("\n=== what PDG's own parameter accessor sees ===")
# _templateId()/_volumeId()/_datacenterId() read self["name"].evaluateString(),
# which is PDG's parameter store, not the hou parm -- an expression default is
# only safe if THAT evaluates it too.
try:
    topnet.parm("topscheduler").set(s.path())
    topnet.createNode("genericgenerator", "gen")
    live = s.getPDGNode()          # the real RunPodFarmScheduler instance
    print("  live scheduler:", live)
    live._cfg = cfg
    check("  _templateId()", live._templateId(), "tpl-xyz789")
    check("  _volumeId()", live._volumeId(), "vol-abc123")
    check("  _datacenterId()", live._datacenterId(), "US-KS-2")
    check("  api key parm through PDG stays empty", live["rpfarm_apikey"].evaluateString(), "")
    live._checkDatacenter()        # matching config: must not raise
    print("  OK    _checkDatacenter passes when the parm agrees with the config")
    s.parm("rpfarm_datacenter").set("EU-RO-1")
    try:
        live._checkDatacenter()
        FAIL.append("stale datacenter not caught")
        print("  FAIL  _checkDatacenter let a stale EU-RO-1 through")
    except Exception as e:
        print("  OK    _checkDatacenter refuses a stale EU-RO-1:", str(e).splitlines()[0])
    s.parm("rpfarm_datacenter").revertToDefaults()
except Exception as e:
    import traceback; traceback.print_exc()
    print("  PDG accessor probe failed:", type(e).__name__, e)
    FAIL.append("pdg accessor probe")

print()
print("FAILURES:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
