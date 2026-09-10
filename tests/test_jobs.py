import json
import pytest
from rpfarm import jobs


def test_job_survives_a_new_reader_and_contains_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'directory', lambda: tmp_path)
    record = {'id': 'abcdef12', 'state': 'submitted', 'mode': 'farm',
              'project': 'shot', 'api_key': 'secret', 'token': 'private'}
    jobs.save(record)
    assert jobs.load('abcdef12')['state'] == 'submitted'
    assert 'secret' not in (tmp_path / 'abcdef12.json').read_text()
    assert 'api_key' not in jobs.list_jobs('shot')[0]
    with pytest.raises(ValueError):
        jobs.load('../other')


def test_cancel_background_is_a_cooperative_job_request_not_a_pid_kill(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'directory', lambda: tmp_path)
    job = jobs.save({'id': 'abcdef12', 'mode': 'background', 'state': 'running', 'pid': 999999})
    jobs.cancel(job)
    assert jobs.load('abcdef12')['state'] == 'cancel_requested'


def test_local_delivery_is_remembered_separately_from_remote_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'directory', lambda: tmp_path)
    jobs.save({'id': 'abcdef12', 'mode': 'farm', 'state': 'complete'})
    f = tmp_path / 'image.exr'
    f.write_bytes(b'img')
    jobs.downloaded('abcdef12', [str(f)])
    got = jobs.load('abcdef12')
    assert got['state'] == 'complete'
    assert '1 local output' in jobs.describe(got)
