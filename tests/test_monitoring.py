from rpfarm import config, monitoring


def test_idle_housekeeping_does_not_stop_sync_for_an_active_host_job(monkeypatch):
    cfg = config.Config(api_key='test', user='artist', volume_id='v', template_id='t')
    class API:
        def __init__(self, _key): pass
        def list_pods(self):
            return [
                {'id': 'sync', 'name': 'rpfarm-sync-artist', 'desiredStatus': 'RUNNING'},
                {'id': 'host', 'name': 'rpfarm-artist-shot-abcdef12-host', 'desiredStatus': 'RUNNING',
                 'env': {'RPFARM_USER': 'artist', 'RPFARM_COOK': 'abcdef12'}}]
        def get_volume(self, _id): return {'size': 50}
        def stop_pod(self, _id): raise AssertionError('must not stop active job transport')
    monkeypatch.setattr(monitoring, 'RunPodAPI', API)
    monkeypatch.setattr(config, 'session_token', lambda: 'test')
    result = monitoring.snapshot(cfg, housekeeping=True)
    assert 'host' in result['farm']


def test_missing_liveness_evidence_does_not_count_as_idle(monkeypatch):
    cfg = config.Config(api_key='test', user='artist', volume_id='v', template_id='t')
    class API:
        def __init__(self, _key): pass
        def list_pods(self): return [{'id': 'sync', 'name': 'rpfarm-sync-artist', 'desiredStatus': 'RUNNING'}]
        def get_volume(self, _id): return {'size': 50}
        def stop_pod(self, _id): raise AssertionError('unknown must not mean safe')
    class Client:
        def __init__(self, *args): pass
        def health(self): return None
    monkeypatch.setattr(monitoring, 'RunPodAPI', API)
    monkeypatch.setattr(monitoring, 'WorkerClient', Client)
    monkeypatch.setattr(config, 'session_token', lambda: 'test')
    monitoring.snapshot(cfg, housekeeping=True)
