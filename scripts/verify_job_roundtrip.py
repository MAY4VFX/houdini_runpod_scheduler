"""Product-level three-process job round trip using fixture-only transport.

Run client, host, download as separate hython processes with a dedicated temp
directory. No RunPod API calls or paid compute. The real HDAs, host controller,
checkpoint, manifest, package transfer and delivery receipts are exercised.
"""
import argparse
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FAKE_RCLONE = r'''#!/usr/bin/env python3
import datetime,json,os,pathlib,shutil,sys
args=sys.argv[1:]
def path(value):
    value=value.removeprefix(':sftp:')
    prefix=os.environ['RPFARM_FIXTURE_REMOTE_PREFIX']
    if value.startswith(prefix):
        value=os.environ['RPFARM_FIXTURE_REMOTE_ROOT']+value[len(prefix):]
    return pathlib.Path(value)
if args[0]=='lsjson':
    root=path(next(a for a in args if a.startswith(':sftp:')))
    rows=[]
    for file in root.rglob('*') if root.exists() else []:
        if file.is_file():
            st=file.stat()
            rows.append({'Path':str(file.relative_to(root)),'Size':st.st_size,
                'ModTime':datetime.datetime.fromtimestamp(st.st_mtime,datetime.timezone.utc).isoformat()})
    print(json.dumps(rows))
elif args[0]=='copy':
    src,dst=path(args[1]),path(args[2])
    names=pathlib.Path(args[args.index('--files-from')+1]).read_text().splitlines()
    count=total=0
    for name in names:
        source,target=src/name,dst/name
        if target.exists() and target.stat().st_size==source.stat().st_size and abs(target.stat().st_mtime-source.stat().st_mtime)<.01:
            continue
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target)
        count+=1;total+=source.stat().st_size
        if target.suffix=='.exr':
            with open(os.environ['RPFARM_FIXTURE_COPIES'],'a') as f:f.write(str(target)+'\n')
    print(json.dumps({'stats':{'bytes':total,'totalBytes':total,'transfers':count,'speed':1000}}),file=sys.stderr)
else:
    raise SystemExit('unsupported fake rclone operation')
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=('client', 'host', 'download'))
    ap.add_argument('directory')
    args = ap.parse_args()
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['RPFARM_HOME'] = str(root / 'config')
    logical = '/workspace/projects/probe/scene'
    os.environ.update(RPFARM_FIXTURE_REMOTE_PREFIX=logical,
                      RPFARM_FIXTURE_REMOTE_ROOT=str(root / 'remote'),
                      RPFARM_FIXTURE_COPIES=str(root / 'copies.txt'))
    import hou
    from rpfarm import config, jobs, submission, houdini_local, host_render
    local, remote, package = root / 'local', root / 'remote', root / 'package'
    hip = local / 'scene.hip'
    job_id = 'abcdef12'
    if args.mode == 'client':
        for directory in (local, remote, package, root / 'config'):
            directory.mkdir(parents=True, exist_ok=True)
        fake = root / 'rclone'
        fake.write_text(FAKE_RCLONE)
        fake.chmod(0o755)
        key = root / 'config' / 'key'
        key.with_suffix('.pub').write_text('fixture-key')
        cfg = config.Config(api_key='fixture', user='probe', volume_id='v', template_id='t',
                            rclone_path=str(fake), ssh_key_path=str(key))
        config.save(cfg)
        install = houdini_local.HoudiniInstall(Path(hou.expandString('$HFS')))
        hda = root / 'download.hda'
        houdini_local.collapse_hda(install.hotl, houdini_local.hda_source_dir('runpodfarm_download', REPO), hda)
        hou.hda.installFile(str(hda))
        net = hou.node('/obj').createNode('topnet', 'roundtrip')
        net.node('localscheduler').parm('pdg_workingdir').set(str(root / 'pdg'))
        a = net.createNode('pythonprocessor', 'upload_fixture')
        b = net.createNode('pythonprocessor', 'render_fixture')
        a.parm('generate').set('item_holder.addWorkItem(inProcess=True)')
        a.parm('cooktask').set("with open({!r}, 'a') as f: f.write('UPLOAD\\n')".format(str(root / 'events.txt')))
        b.setInput(0, a)
        b.parm('requirescookedinputs').set(1)
        b.parm('generate').set('for up in upstream_items:\n    item_holder.addWorkItem(inProcess=True, parent=up)')
        b.parm('cooktask').set(
            "import json\nassert json.load(open({!r}))['state'] == 'running'\n".format(str(package / 'job.json')) +
            "with open({!r}, 'a') as f: f.write('RENDER\\n')\n".format(str(root / 'events.txt')) +
            "with open({!r}, 'wb') as f: f.write(b'fixture frame')\n".format(str(remote / 'image.exr')) +
            "work_item.addOutputFile({!r}, 'file/image')\n".format(logical + '/image.exr') +
            "work_item.setStringAttrib('rpfarm_pathmap', {!r})\n".format(json.dumps({str(local): logical})) +
            "work_item.setStringAttrib('rpfarm_delivery_key', 'abcdef12/render')\n")
        download = net.createNode('runpodfarmdownload', 'download')
        download.setInput(0, b)
        download.parm('rpfarm_job').set(job_id)
        download.parm('rpfarm_inprocess').set(1)
        hou.hipFile.save(str(hip))
        a.cookWorkItems(block=True)
        a.getPDGGraphContext().serializeWorkItems(str(package / 'taskgraph_in.bin'), '')
        record = {'id': job_id, 'mode': 'farm', 'state': 'submitted', 'user': 'probe',
                  'project': 'scene', 'volume_id': 'v', 'hip': str(hip), 'logical_hip': str(hip),
                  'local_root': str(local), 'target': b.path(), 'package_dir': str(package),
                  'submitted_at': __import__('time').time(), 'max_minutes': 2}
        jobs.save(record)
        jobs.write(package / 'job.json', record)
        submission.prepare(hou, str(hip), str(package / 'snapshot.hip'), str(hip))
        assert (root / 'events.txt').read_text().splitlines() == ['UPLOAD']
        print('CLIENT: uploaded once and saved immutable job')
    elif args.mode == 'host':
        rc = host_render.run(host_render.parse_args([
            '--hip', str(package / 'snapshot.hip'), '--toppath', '/obj/roundtrip/render_fixture',
            '--taskgraphin', str(package / 'taskgraph_in.bin'),
            '--taskgraphout', str(package / 'taskgraph_out.bin')]), hou)
        assert rc == 0, (package / 'job.json').read_text()
        assert (root / 'events.txt').read_text().splitlines() == ['UPLOAD', 'RENDER']
        manifest = json.loads((package / 'outputs.json').read_text())
        assert manifest['items'][0]['pathmap'] == json.dumps({str(local): logical})
        print('HOST: rendered once and published checkpoint/manifest')
    else:
        # Only the transport boundary is substituted. Native HDA generation,
        # package execution, rclone subprocess, receipts and outputs remain real.
        import shlex
        from rpfarm import runpod_api, worker_client, pods
        pod = {'id': 'fixture-sync', 'name': 'rpfarm-sync-probe', 'desiredStatus': 'RUNNING',
               'publicIp': '127.0.0.1', 'portMappings': {'22': 22}, 'networkVolumeId': 'v'}
        class API:
            def __init__(self, *a): pass
            def list_pods(self, prefix=''): return [pod]
            def get_pod(self, _id): return pod
        def actual(path):
            return Path(str(path).replace(logical, str(remote), 1)) if str(path).startswith(logical) else Path(path)
        class Client:
            def __init__(self, *a): pass
            def health(self): return {'role': 'sync'}
            def read_file(self, path):
                return actual(path).read_text() if actual(path).exists() else None
            def exec(self, command, **kwargs):
                words = shlex.split(command)
                if words and words[0] == 'stat':
                    text = '\n'.join('{} {}'.format(actual(p).stat().st_size, p) for p in words[3:] if actual(p).exists())
                    return {'exit_code': 0, 'stdout': text, 'stderr': ''}
                return {'exit_code': 0, 'stdout': '{}', 'stderr': ''}
        runpod_api.RunPodAPI = API
        worker_client.WorkerClient = pods.WorkerClient = jobs.WorkerClient = Client
        hou.hipFile.load(str(hip), suppress_save_prompt=True)
        download = hou.node('/obj/roundtrip/download')
        download.cookWorkItems(block=True)
        download.cookWorkItems(block=True)
        assert (root / 'events.txt').read_text().splitlines() == ['UPLOAD', 'RENDER']
        assert (local / 'image.exr').read_bytes() == b'fixture frame'
        assert len((root / 'copies.txt').read_text().splitlines()) == 1
        outputs = [f.path for wi in jobs.pdg_output_node(download).workItems for f in wi.outputFiles]
        assert str(local / 'image.exr') in outputs, outputs
        print('DOWNLOAD: fresh session, no render/upload replay, one copy, native local Output File')


if __name__ == '__main__':
    main()
