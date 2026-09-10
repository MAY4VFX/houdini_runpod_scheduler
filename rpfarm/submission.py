"""Prepare an immutable HIP snapshot without changing the artist's scene."""
import argparse
import json
from pathlib import Path


def identity_hook(logical_path):
    return '''
# RunPodFarm: restore logical scene identity after loading an immutable snapshot.
def _rpfarm_restore_snapshot_identity():
    import hou
    def loaded(event, **kwargs):
        if event == hou.hipFileEventType.AfterLoad:
            hou.hipFile.removeEventCallback(loaded)
            hou.hipFile.setName(%r)
    hou.hipFile.addEventCallback(loaded)
_rpfarm_restore_snapshot_identity()
del _rpfarm_restore_snapshot_identity
''' % logical_path


def redact_checkpoint(path, secrets):
    path = Path(path)
    data = path.read_text()
    for secret in secrets:
        if secret:
            data = data.replace(str(secret), '')
    path.write_text(data)


def prepare(hou, source, destination, logical_path):
    hou.hipFile.load(source, suppress_save_prompt=True, ignore_load_warnings=True)
    for node in hou.node('/').allSubChildren():
        if node.type().name() == 'runpodfarmscheduler':
            parm = node.parm('rpfarm_apikey')
            if parm:
                parm.set('')
    hou.appendSessionModuleSource(identity_hook(logical_path))
    hou.hipFile.save(destination)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--logical', required=True)
    args = parser.parse_args()
    import hou
    prepare(hou, args.source, args.output, args.logical)


if __name__ == '__main__':
    main()
