"""Offline two-process probe. Run with hython: SCRIPT first|second TEMP_DIR.

TEMP_DIR must be a dedicated temporary directory. No farm API is imported.
"""
import json
import sys
from pathlib import Path

import hou
import pdg

MODE = sys.argv[1]
ROOT = Path(sys.argv[2]).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
MARKER = ROOT / "executions.txt"
HIP = ROOT / "probe.hip"
STATE = ROOT / "taskgraph_out.bin"
MANIFEST = ROOT / "outputs.json"
REMOTE = "/workspace/projects/probe/render.0001.exr"

if MODE == "first":
    MARKER.unlink(missing_ok=True)
    net = hou.node("/obj").createNode("topnet", "probe")
    upload = net.createNode("pythonprocessor", "A")
    render = net.createNode("pythonprocessor", "B")
    upload.parm("generate").set("item_holder.addWorkItem(inProcess=True)")
    render.parm("generate").set(
        "for upstream in upstream_items:\n"
        "    item_holder.addWorkItem(inProcess=True, parent=upstream)"
    )
    render.parm("requirescookedinputs").set(1)
    render.setInput(0, upload)
    for node in (upload, render):
        node.parm("cooktask").set(
            "with open({!r}, 'a') as f:\n    f.write({!r})".format(
                str(MARKER), node.name() + "\n"
            )
        )
    render.parm("cooktask").set(
        render.parm("cooktask").eval()
        + "\nwork_item.addOutputFile({!r}, 'file/image')".format(REMOTE)
        + "\nwork_item.setStringAttrib('rpfarm_pathmap', 'probe-map')"
        + "\nwork_item.setStringAttrib('delivery_key', 'probe-key')"
    )

    subnet = net.createNode("subnet", "Download")
    subnet.setInput(0, render)
    subnet.addSpareParmTuple(hou.StringParmTemplate("rpfarm_job", "Job", 1))
    # The product persists this when submitting/selecting a saved job.
    # Empty means normal Current Graph, with the external input selected.
    subnet.parm("rpfarm_job").set("probe-job")
    resume = subnet.createNode("pythonprocessor", "resume")
    resume.parm("generate").set(
        """import json
print('RESUME COOK SET', [n.name for n in self.context.cookSet], flush=True)
self.context.deserializeWorkItems(%r)
with open(%r) as stream:
    manifest = json.load(stream)
for record in manifest:
    imported = item_holder.addWorkItem(name='import_' + str(record['id']))
    imported.setIntAttrib('original_render_id', record['id'])
    for path, tag in record['outputs']:
        imported.addOutputFile(path, tag)
    for name, value in record['attrs'].items():
        imported.setStringAttrib(name, value)
""" % (str(STATE), str(MANIFEST))
    )
    switch = subnet.createNode("switch", "source")
    switch.setInput(0, subnet.indirectInputs()[0])
    switch.setInput(1, resume)
    switch.parm("input").setExpression(
        "1 if hou.pwd().parent().evalParm('rpfarm_job') else 0",
        language=hou.exprLanguage.Python,
    )
    switch.parm("invalidate").set(0)
    download = subnet.createNode("pythonprocessor", "download")
    download.setInput(0, switch)
    download.parm("pdg_workitemgeneration").set(
        int(pdg.generateWhen.AllUpstreamCooked)
    )
    download.parm("generate").set(
        "for upstream in upstream_items:\n"
        "    item_holder.addWorkItem(inProcess=True, parent=upstream)"
    )
    download.parm("cooktask").set(
        "assert work_item.stringAttribValue('rpfarm_pathmap') == 'probe-map'\n"
        "assert work_item.stringAttribValue('delivery_key') == 'probe-key'\n"
        "assert any(x.path == {!r} for x in work_item.inputFiles)\n".format(REMOTE)
        + "with open({!r}, 'a') as f:\n    f.write('DOWNLOAD\\n')\n".format(str(MARKER))
        + "work_item.addOutputFile({!r}, 'file/text')".format(str(MARKER))
    )
    subnet.node("output0").setInput(0, download)
    subnet.createDigitalAsset(
        name="resume_switch_probe", hda_file_name=str(ROOT / "probe.hda"),
        min_num_inputs=0, max_num_inputs=1, ignore_external_references=True,
    )
    hou.hipFile.save(str(HIP))
    render.cookWorkItems(block=True, save_prompt=False)
    render.getPDGNode().context.serializeWorkItems(str(STATE), "")
    MANIFEST.write_text(json.dumps([
        {
            "id": item.id,
            "outputs": [(output.path, output.tag) for output in item.outputFiles],
            "attrs": {name: item.stringAttribValue(name)
                      for name in ("rpfarm_pathmap", "delivery_key")},
        }
        for item in render.getPDGNode().workItems
    ]))
    assert MARKER.read_text().splitlines() == ["A", "B"]
elif MODE == "second":
    hou.hipFile.load(str(HIP))
    download = hou.node("/obj/probe/Download")
    # No manual restore, custom class registration, or upstream cook.
    download.cookWorkItems(block=True, save_prompt=False)
    download.cookWorkItems(block=True, save_prompt=False)
    lines = MARKER.read_text().splitlines()
    assert lines[:2] == ["A", "B"]
    assert lines.count("A") == lines.count("B") == 1
    assert lines.count("DOWNLOAD") >= 1
    for name in ("A", "B"):
        items = hou.node("/obj/probe/" + name).getPDGNode().workItems
        assert len(items) == 1
        assert items[0].state == pdg.workItemState.CookedSuccess
    for node in download.allSubChildren():
        assert not node.errors(), (node.path(), node.errors())
else:
    raise SystemExit("Expected first or second")
print("EXECUTIONS", MARKER.read_text().splitlines(), flush=True)
