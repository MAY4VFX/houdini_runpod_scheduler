"""Real Houdini controller failure/cancel checks, no farm and fixture files only."""
import json
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import hou
from rpfarm import host_render, jobs


def main():
    with tempfile.TemporaryDirectory(prefix='rpfarm-controller-outcomes-') as tmp:
        root = Path(tmp)
        for name, script, canceled in (
            ('failure', "raise RuntimeError('fixture generation failure')", False),
            ('cancel', "item_holder.addWorkItem(inProcess=True)", True),
        ):
            hou.hipFile.clear(suppress_save_prompt=True)
            package = root / name
            package.mkdir()
            net = hou.node('/obj').createNode('topnet', 'test')
            node = net.createNode('pythonprocessor', 'target')
            node.parm('generate').set(script)
            marker = package / 'must_not_run'
            node.parm('cooktask').set("open({!r}, 'w').write('bad')".format(str(marker)))
            hip = package / 'test.hip'
            hou.hipFile.save(str(hip))
            jobs.write(package / 'job.json', {'id': 'abcdef99', 'mode': 'farm',
                'state': 'submitted', 'target': node.path(), 'max_minutes': 1})
            if canceled:
                (package / 'cancel').touch()
            rc = host_render.run(host_render.parse_args([
                '--hip', str(hip), '--toppath', '/obj/test/target',
                '--taskgraphout', str(package / 'taskgraph_out.bin')]), hou)
            result = json.loads((package / 'job.json').read_text())
            assert rc == 1, result
            assert result['state'] == ('canceled' if canceled else 'failed'), result
            assert not marker.exists()
            print(name.upper() + ': correct terminal state, no work executed')


if __name__ == '__main__':
    main()
