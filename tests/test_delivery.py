import threading
import time
import types
import pytest
from rpfarm import delivery, sync


def test_duplicate_callers_only_transfer_once_and_modified_local_invalidates(tmp_path, monkeypatch):
    cache = tmp_path / 'receipts'
    cache.mkdir()
    monkeypatch.setattr(delivery, 'root', lambda: cache)
    output = tmp_path / 'image.exr'
    item = {'files': [[str(output), '/workspace/image.exr', 3]], 'delivery_key': 'cook/item'}
    cfg = types.SimpleNamespace(volume_id='v')
    calls = []
    def copy(it):
        calls.append(it)
        time.sleep(.05)
        output.write_bytes(b'abc')
        st = output.stat()
        return {'files': 1, 'bytes': 3, 'seconds': .05,
                'verified': {str(output): {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}}}
    threads = [threading.Thread(target=lambda: delivery.transfer(item, cfg, copy)) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(calls) == 1
    output.write_bytes(b'changed')
    delivery.transfer(item, cfg, copy)
    assert len(calls) == 2


def test_failed_or_cancelled_download_does_not_leave_delivery_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, 'root', lambda: tmp_path)
    item = {'files': [[str(tmp_path / 'missing'), '/remote/file', 3]], 'delivery_key': 'job/item'}
    cfg = types.SimpleNamespace(volume_id='v')
    with pytest.raises(sync.SyncError, match='did not produce'):
        delivery.transfer(item, cfg, lambda _: {})
    with pytest.raises(InterruptedError):
        delivery.transfer(item, cfg, lambda _: {}, cancel=lambda: True)
    assert not list(tmp_path.glob('*.json'))


def test_queue_poll_does_not_wait_for_a_slow_transfer(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr('rpfarm.packages.run_download_item', lambda *a, **k: gate.wait(2))
    q = delivery.Queue()
    q.submit({}, None, None, None)
    start = time.monotonic()
    assert q.poll() == []
    assert time.monotonic() - start < .1
    gate.set()
    q.cancel()


def test_cancel_stops_even_a_transfer_process_that_prints_nothing():
    import sys
    start = time.monotonic()
    with pytest.raises(InterruptedError, match='cancelled'):
        sync._run_rclone(sys.executable, ['-c', 'import time; time.sleep(10)'],
                         cancel=lambda: time.monotonic() - start > .1)
    assert time.monotonic() - start < 2


def test_local_file_without_matching_remote_proof_is_not_a_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, 'root', lambda: tmp_path)
    output = tmp_path / 'newer-local.exr'
    output.write_bytes(b'local version kept by overwrite policy')
    item = {'files': [[str(output), '/remote/new.exr', 3]], 'delivery_key': 'cook/item'}
    cfg = types.SimpleNamespace(volume_id='v')
    result = delivery.transfer(item, cfg, lambda _: {'verified': {}})
    assert result['delivered'] == 0
    assert not delivery.valid(item, cfg, item['files'][0])
