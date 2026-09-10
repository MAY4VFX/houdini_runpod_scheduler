import pytest
from rpfarm import pods, status
from rpfarm.worker_client import WorkerClient, WorkerError


def test_housekeeping_update_preserves_current_and_finished_cook():
    key = 'test-node'
    status.update(key, cook='Cook abc: 3 tasks running')
    assert '3 tasks running' in status.update(key, farm='Sync pod running')
    status.update(key, cook='Cook abc: complete, 3 outputs delivered')
    result = status.update(key, farm='Sync pod stopped')
    assert '3 outputs delivered' in result and 'Sync pod stopped' in result


def test_unauthorized_health_fails_after_one_poll_without_sleep():
    class API:
        def get_pod(self, _id):
            return {'id': 'pod', 'publicIp': '127.0.0.1', 'portMappings': {'22': 22}}
    calls = []
    def transport(*a, **kw):
        calls.append(1)
        return 401, b'{"error":"unauthorized"}'
    with pytest.raises(WorkerError, match='access denied'):
        pods.wait_ready(API(), WorkerClient('pod', 'wrong', transport), 'pod',
                        sleep=lambda _: pytest.fail('must not wait after 401'))
    assert len(calls) == 1
