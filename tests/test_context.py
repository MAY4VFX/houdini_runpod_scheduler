import dataclasses
import json
import os
import types
import pytest
from rpfarm import config, context


class Node:
    def __init__(self, kind, parms=None, parent=None):
        self.kind, self.parms, self._parent = kind, parms or {}, parent
        self.nodes = {}
    def type(self):
        return types.SimpleNamespace(name=lambda: self.kind)
    def parm(self, name):
        return types.SimpleNamespace(eval=lambda: self.parms[name]) if name in self.parms else None
    def parent(self):
        return self._parent
    def node(self, path):
        return self.nodes.get(path)


def cfg():
    return config.Config(api_key='private-one', user='artist', volume_id='vol1', template_id='tpl1')


def test_all_consumers_get_scheduler_overrides_not_global_config():
    net = Node('topnet', {'topscheduler': 'farm'})
    farm = Node('runpodfarmscheduler', {'rpfarm_apikey': 'private-two',
        'rpfarm_networkvolumeid': 'vol2', 'rpfarm_templateid': 'tpl2',
        'rpfarm_datacenter': 'EU-X', 'rpfarm_project': 'showB'}, net)
    net.nodes['farm'] = farm
    upload = Node('runpodfarmupload', {'rpfarm_project': 'oldShow'}, net)
    for node in (farm, upload, Node('runpodfarmdownload', parent=net)):
        got = context.resolve(node, '/job', cfg())
        assert (got.cfg.api_key, got.cfg.volume_id, got.cfg.template_id, got.cfg.datacenter) == (
            'private-two', 'vol2', 'tpl2', 'EU-X')
        assert got.project == 'showB'
    upload.parms['rpfarm_projectoverride'] = True
    assert context.resolve(upload, '/job', cfg()).project == 'oldShow'
    assert cfg().volume_id == 'vol1'


def test_snapshot_does_not_put_credentials_in_payload_and_survives_config_change(tmp_path, monkeypatch):
    monkeypatch.setattr(context, '_snapshot_root', lambda: tmp_path)
    path = context.snapshot(cfg())
    payload = json.dumps({'context_path': path})
    assert 'private-one' not in payload
    monkeypatch.setattr(config, 'load', lambda: dataclasses.replace(cfg(), api_key='changed', volume_id='changed'))
    assert context.load_snapshot(path).api_key == 'private-one'
    assert context.load_snapshot(path).volume_id == 'vol1'
    if os.name != 'nt':
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_snapshot_rejects_arbitrary_file_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(context, '_snapshot_root', lambda: tmp_path)
    with pytest.raises(config.ConfigError):
        context.load_snapshot('/outside/config.toml')


def test_sync_reuse_never_silently_mounts_the_other_contexts_volume():
    from rpfarm import pods
    api = types.SimpleNamespace(list_pods=lambda _: [{
        'id': 'other', 'name': 'rpfarm-sync-artist', 'desiredStatus': 'RUNNING',
        'networkVolumeId': 'other-volume'}])
    assert pods.find_running_sync_pod(api, cfg()) is None
    with pytest.raises(config.ConfigError, match='different volume'):
        pods._find_or_create_sync_pod(api, cfg(), 'token', 'pubkey', print)
