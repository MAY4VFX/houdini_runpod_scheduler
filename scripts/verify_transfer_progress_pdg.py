"""Local PDG cook proving native percent/custom-state callbacks. No farm."""
import os
import pathlib
import shlex
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    import hou
    import pdg
    from rpfarm.houdini_local import resolve_package_python
    python, _ = resolve_package_python(hfs=hou.getenv('HFS'))
    with tempfile.TemporaryDirectory(prefix='rpfarm-progress-pdg-') as tmp:
        net = hou.node('/obj').createNode('topnet', 'verify_progress')
        local = net.node('localscheduler')
        local.parm('pdg_workingdir').set(tmp)
        pp = net.createNode('pythonprocessor', 'transfer_fixture')
        worker = (
            "import sys,os,time;sys.path.insert(0,os.environ['PDG_SCRIPTDIR']);"
            "import pdgcmd;from rpfarm.progress import TransferProgress;"
            "p=TransferProgress(pdgcmd);"
            "p._native=lambda method,value:getattr(pdgcmd,method)(value,to_stdout=False);"
            "p.phase('Checking farm');p.plan(1000,2);"
            "p.begin_transfer();p(500,1000,100);time.sleep(1);"
            "p(1000,1000,100);p.phase('Unpacking on farm');time.sleep(1);p.finish()"
        )
        command = shlex.join([python, '-c', worker])
        pp.parm('generate').set(
            'wi = item_holder.addWorkItem(name="upload_fixture")\n'
            'wi.setCommand({!r})\n'
            'wi.addEnvironmentVar("PYTHONPATH", {!r})\n'.format(command, str(REPO)))
        pp.cookWorkItems(block=True, tops_only=True)
        observed = []
        pp.cookWorkItems(block=False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for wi in pp.getPDGNode().workItems:
                observed.append((wi.cookPercent, wi.customState))
            if pp.getPDGNode().isCooked:
                break
            time.sleep(0.02)
        pp.getPDGGraphContext().waitAllEvents()
        items = pp.getPDGNode().workItems
        assert len(items) == 1
        item = items[0]
        assert item.state == pdg.workItemState.CookedSuccess, item.state
        assert item.cookPercent == 100.0, item.cookPercent
        # Houdini clears customState on CookedSuccess. Verify it WHILE cooking.
        assert (50.0, 'Transferring') in observed, (set(observed), item.stringAttribValue('phase'))
        assert item.stringAttribValue('phase') == 'Complete'
        assert item.intAttribValue('bytes_done') == 1000
        print('Native PDG verified: live (50%, Transferring), final 100%, bytes_done=1000')
        net.destroy()


if __name__ == '__main__':
    main()
