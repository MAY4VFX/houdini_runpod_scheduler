"""The sync pod owns one shared MQ server; cooks own separate client IDs."""


def ensure_shared_mq(connection_file, shared_file='/workspace/.rpfarm/mq_shared.txt',
                     log_file='/workspace/ledger/logs/mq_shared.log',
                     lock_file='/tmp/rpfarm-mq.lock', spawn=None, probe=None):
    import fcntl
    import pathlib
    import socket
    import subprocess
    import time
    shared = pathlib.Path(shared_file)
    shared.parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    def alive():
        try:
            for port in (4440, 4442):
                with socket.create_connection(('127.0.0.1', port), timeout=.3):
                    pass
            return True
        except OSError:
            return False
    probe = probe or alive
    spawn = spawn or subprocess.Popen
    with open(lock_file, 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not probe():
            shared.unlink(missing_ok=True)
            with open(log_file, 'a') as stream:
                spawn(['mqserver', '-p', '4440', '-n', '64', '-l', '1', '-c', str(shared),
                       '-w', '4442', '16', '/result'], stdout=stream, stderr=subprocess.STDOUT,
                      stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + 20
            while not (probe() and shared.exists()):
                if time.monotonic() > deadline:
                    raise TimeoutError('Shared MQ server did not start')
                time.sleep(.2)
        if not shared.exists():
            candidates = list(shared.parent.glob('mq_*.txt'))
            for path in candidates:
                line = path.read_text().splitlines()[0]
                if line.startswith('PDG_MQ '):
                    shared.write_text(line + '\n')
                    break
        if not shared.exists():
            raise RuntimeError('MQ ports are busy but no known connection record exists')
        data = shared.read_text()
        pathlib.Path(connection_file).write_text(data)
        return data
