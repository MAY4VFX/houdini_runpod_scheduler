import http.client
import json
import os
import sys
import threading
import time
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pod"))
import worker  # noqa: E402


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_TOKEN", "t")
    monkeypatch.setenv("RPFARM_ROLE", "sync")
    monkeypatch.setenv("RPFARM_SLOTS", "1")
    monkeypatch.setenv("HFS", str(tmp_path / "nohfs"))
    # make_server builds the detached-exec dir from WORKSPACE_ROOT, so this
    # has to be redirected BEFORE the server exists -- otherwise a test run
    # reaches for the real /workspace (the same class of mistake that had
    # the suite overwriting the artist's real Houdini prefs).
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    s = worker.make_server("127.0.0.1", 0, log_dir=str(tmp_path))
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()
    s.server_close()


def req(port, method, path, body=None, token="t"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-RPFarm-Token": token, "Content-Type": "application/json"}
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if r.getheader("Content-Type", "").startswith("application/json") else data.decode())


def test_auth_required(srv):
    assert req(srv, "GET", "/health", token="wrong")[0] == 401


def test_health(srv):
    st, h = req(srv, "GET", "/health")
    assert st == 200 and h["role"] == "sync" and h["slots"] == 1 and h["busy"] == 0


def test_task_lifecycle(srv):
    st, r = req(srv, "POST", "/tasks", {"task_id": "a1", "command": "echo hi; echo err 1>&2; exit 3", "env": {"X": "1"}})
    assert st == 202
    for _ in range(50):
        st, t = req(srv, "GET", "/tasks/a1")
        if t["state"] != "running":
            break
        time.sleep(0.1)
    assert t["state"] == "failed" and t["exit_code"] == 3
    assert "hi" in t["tail"] and "err" in t["tail"]
    st, log = req(srv, "GET", "/tasks/a1/log")
    assert st == 200 and "hi" in log


def test_slots_429(srv):
    assert req(srv, "POST", "/tasks", {"task_id": "s1", "command": "sleep 5"})[0] == 202
    assert req(srv, "POST", "/tasks", {"task_id": "s2", "command": "echo"})[0] == 429
    assert req(srv, "DELETE", "/tasks/s1")[0] == 200


def test_exec_sync_only(srv):
    st, r = req(srv, "POST", "/exec", {"command": "echo ok"})
    assert st == 200 and r["stdout"].strip() == "ok" and r["exit_code"] == 0


def test_duplicate_task_id_rejected(srv):
    assert req(srv, "POST", "/tasks", {"task_id": "d1", "command": "sleep 5"})[0] == 202
    st, r = req(srv, "POST", "/tasks", {"task_id": "d1", "command": "echo dup"})
    assert st == 409
    st, h = req(srv, "GET", "/health")
    assert st == 200 and h["busy"] == 1
    assert req(srv, "DELETE", "/tasks/d1")[0] == 200


def test_exec_output_capped(srv):
    payload = "0123456789" * 30000  # 300000 deterministic chars
    cmd = "python3 -c \"import sys; sys.stdout.write('%s')\"" % payload
    st, r = req(srv, "POST", "/exec", {"command": cmd})
    assert st == 200
    assert len(r["stdout"]) == 200000
    assert r["stdout"] == payload[-200000:]


def test_files_403_outside_workspace(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    outside = tmp_path.parent / f"outside-{os.getpid()}.txt"
    outside.write_text("secret")
    try:
        st, r = req(srv, "GET", "/files?path=" + quote(str(outside)))
        assert st == 403
    finally:
        outside.unlink()


def test_files_reads_under_workspace(srv, tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKSPACE_ROOT", str(tmp_path))
    f = tmp_path / "sub" / "file.txt"
    f.parent.mkdir()
    f.write_text("hello world")
    st, body = req(srv, "GET", "/files?path=" + quote(str(f)))
    assert st == 200 and "hello world" in body


# -- Ruling R31: detached exec ------------------------------------------------


def _wait_done(port, handle, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st, body = req(port, "GET", "/exec/" + handle)
        assert st == 200, body
        if body["state"] == "done":
            return body
        time.sleep(0.1)
    raise AssertionError("detached run never finished")


def test_exec_detached_outlives_the_request_and_reports_its_exit_code(srv, tmp_path):
    """The bug this exists to prevent (Task 14): a command that runs longer
    than the HTTP request must keep running after the response is sent, and
    its exit code must still be retrievable afterwards.

    The synchronous path cannot do this -- the RunPod proxy cuts the response
    at ~100s and the pod-side timeout SIGKILLs the shell, closing the pipe a
    surviving grandchild then dies on.
    """
    st, body = req(srv, "POST", "/exec", {"command": "sleep 1; echo finished-after-response; exit 7", "detach": True})
    assert st == 202
    handle = body["handle"]

    # The response came back immediately, while the command is still running.
    st_now, running = req(srv, "GET", "/exec/" + handle)
    assert st_now == 200 and running["state"] == "running" and running["exit_code"] is None

    done = _wait_done(srv, handle)
    assert done["exit_code"] == 7

    log = open(body["log_path"]).read()
    assert "finished-after-response" in log


def test_exec_detached_leaves_no_zombie(srv):
    """A finished detached child must be reaped. As PID 1 in the container the
    worker inherits orphans and never reaps them -- Task 14 found the dead
    installer as `[houdini.install] <defunct>` with PPID 1, which is how the
    cause was identified in the first place.

    Checked by asking the OS about *this run's* child specifically, by pid:
    `waitpid(pid, WNOHANG)` raises ChildProcessError once it has been reaped,
    returns `(pid, status)` while it is a corpse, and `(0, 0)` while it is
    still alive. Only the first is acceptable. Asking about every child
    instead (`waitpid(-1, ...)`) made this test answer for whatever other
    tests in this file happened to still be running, which is what made it
    fail about one run in three.
    """
    st, body = req(srv, "POST", "/exec", {"command": "true", "detach": True})
    assert st == 202
    done = _wait_done(srv, body["handle"])
    assert done["pid"], "status did not report the wrapper's pid"

    with pytest.raises(ChildProcessError):
        os.waitpid(done["pid"], os.WNOHANG)

    # and the exit code survives being reaped
    _st, again = req(srv, "GET", "/exec/" + body["handle"])
    assert again["state"] == "done" and again["exit_code"] == 0


def test_exec_status_does_not_call_a_run_done_while_its_wrapper_still_lives(tmp_path, monkeypatch):
    """The wrapper writes .rc and only *then* runs `exit $rc`, so there is a
    window where the file says "finished" while the bash that wrote it is
    still alive -- and therefore not reaped. status() must not report `done`
    from inside that window: doing so hands the caller a result and leaves a
    zombie behind, which is the exact fingerprint Task 14 chased.

    Provoked deterministically by writing .rc by hand under a run that is
    still sleeping, rather than by racing the real wrapper.
    """
    monkeypatch.setattr(worker, "DETACHED_REAP_GRACE_S", 0.2)
    runs = worker.DetachedRuns(str(tmp_path / "exec"))
    started = runs.start("sleep 30")
    handle = started["handle"]
    try:
        assert runs.status(handle)["state"] == "running"

        # The file appears; the child has not gone anywhere.
        with open(runs.paths(handle)["rc"], "w") as f:
            f.write("0")

        st = runs.status(handle)
        assert st["state"] == "running", "reported done while its child was still alive"
        assert st["exit_code"] is None
        assert runs._procs[handle].poll() is None, "the child really was still running"
    finally:
        runs._procs[handle].kill()
        runs._procs[handle].wait()


def test_exec_status_reaps_the_child_before_reporting_done(tmp_path):
    """The positive half: once status() does say `done`, the child must
    already be reaped -- asserted on the Popen itself rather than by racing
    waitpid() from the outside."""
    runs = worker.DetachedRuns(str(tmp_path / "exec"))
    started = runs.start("exit 5")
    handle = started["handle"]

    deadline = time.time() + 20
    while time.time() < deadline:
        st = runs.status(handle)
        if st["state"] == "done":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["exit_code"] == 5
    assert runs._procs[handle].returncode is not None, "said done without reaping"


def test_exec_detached_writes_its_output_to_a_file_not_the_http_pipe(srv):
    st, body = req(srv, "POST", "/exec", {"command": "echo to-a-file; echo to-stderr 1>&2", "detach": True})
    assert st == 202
    _wait_done(srv, body["handle"])
    log = open(body["log_path"]).read()
    assert "to-a-file" in log and "to-stderr" in log  # stderr is merged into the same file


def test_exec_status_unknown_handle_is_404(srv):
    assert req(srv, "GET", "/exec/exec-doesnotexist")[0] == 404


def test_exec_sync_timeout_is_clamped_to_the_proxy_ceiling(srv, monkeypatch):
    """A caller may ask for 829s (the size-derived figure that Task 14's
    install used); the transport cannot deliver a response that long, so the
    handler must not pretend it can."""
    seen = {}
    real_popen = worker.subprocess.Popen

    class SpyPopen(real_popen):
        def communicate(self, *a, **kw):
            seen["timeout"] = kw.get("timeout", a[1] if len(a) > 1 else None)
            return super().communicate(*a, **kw)

    monkeypatch.setattr(worker.subprocess, "Popen", SpyPopen)
    st, body = req(srv, "POST", "/exec", {"command": "true", "timeout_s": 829})
    assert st == 200 and body["exit_code"] == 0
    assert seen["timeout"] == worker.EXEC_SYNC_CEILING_S


def test_exec_sync_timeout_response_points_at_the_detached_path(srv):
    st, body = req(srv, "POST", "/exec", {"command": "sleep 5", "timeout_s": 1})
    assert st == 504
    assert "detach" in body["hint"]


def test_exec_sync_timeout_kills_grandchildren_too(srv, tmp_path):
    """A timed-out /exec must take its whole process group with it.

    `subprocess.run`'s timeout kills only the shell it started; a grandchild
    (an installer, a tar) survives and keeps writing to a pipe nobody is
    reading -- the exact shape of Task 14's corrupted Houdini install, and
    confirmed live afterwards as a leftover zombie on the pod.
    """
    marker = tmp_path / "grandchild-still-alive"
    # bash spawns a *grandchild* that would touch the marker after the
    # timeout has already fired, then waits on it.
    cmd = "bash -c 'sleep 3; touch {}' & wait".format(marker)

    st, body = req(srv, "POST", "/exec", {"command": cmd, "timeout_s": 1})
    assert st == 504 and body["error"] == "timeout"

    time.sleep(5)  # well past when the grandchild would have fired
    assert not marker.exists(), "grandchild outlived the killed request"


def test_exec_sync_timeout_leaves_no_unreaped_child(srv):
    st, _body = req(srv, "POST", "/exec", {"command": "sleep 30", "timeout_s": 1})
    assert st == 504
    with pytest.raises(ChildProcessError):
        os.waitpid(-1, os.WNOHANG)


# ---------------------------------------------------------------------------
# XpuSupport -- so nobody has to learn this from eight crashed tasks again
# ---------------------------------------------------------------------------


def _husk(stdout, returncode=0, stderr=""):
    """subprocess.run stand-in; the probe shells out via build_shell_command."""

    class R:
        pass

    r = R()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode

    def run(cmd, **_kw):
        run.calls.append(cmd)
        return r

    run.calls = []
    return run


def test_xpu_supported_when_husk_lists_the_delegate_plainly(monkeypatch, tmp_path):
    monkeypatch.setenv("HFS", str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "husk").write_text("")
    run = _husk(" - BRAY_HdKarma (Karma CPU)\n - BRAY_HdKarmaXPU (Karma XPU)\n")

    answer = worker.XpuSupport(run=run).supported()

    assert answer["supported"] is True
    assert "KarmaXPU" in answer["detail"]


def test_xpu_unsupported_is_read_from_husks_own_wording(monkeypatch, tmp_path):
    monkeypatch.setenv("HFS", str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "husk").write_text("")
    run = _husk(" - BRAY_HdKarmaXPU (Karma XPU) - unsupported\n")

    answer = worker.XpuSupport(run=run).supported()

    assert answer["supported"] is False
    assert "unsupported" in answer["detail"]


def test_xpu_answer_is_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("HFS", str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "husk").write_text("")
    run = _husk(" - BRAY_HdKarmaXPU (Karma XPU)\n")
    probe = worker.XpuSupport(run=run)

    probe.supported()
    probe.supported()

    assert len(run.calls) == 1


def test_xpu_is_unknown_rather_than_false_when_it_cannot_ask(monkeypatch, tmp_path):
    """Unknown must not read as 'no': the preflight refuses cooks on a no."""
    monkeypatch.setenv("HFS", str(tmp_path))          # no bin/husk under it

    assert worker.XpuSupport(run=_husk("")).supported()["supported"] is None

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "husk").write_text("")
    assert worker.XpuSupport(run=_husk("nothing relevant\n")).supported()["supported"] is None


def test_a_husk_that_explodes_does_not_take_health_down(monkeypatch, tmp_path):
    monkeypatch.setenv("HFS", str(tmp_path))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "husk").write_text("")

    def boom(cmd, **_kw):
        raise OSError("no husk for you")

    answer = worker.XpuSupport(run=boom).supported()

    assert answer["supported"] is None
    assert "husk failed" in answer["detail"]


# ---------------------------------------------------------------------------
# Liveness: work that never touches this worker
#
# /health only ever counted HTTP requests, so a pod driven over SSH reported
# busy 0 with idle_s equal to its whole uptime while rendering 15 frames, and
# the kill guard cleared it. The production case is worse: rclone moves files
# to the sync pod over SFTP, so a pod taking a 4GB tarball -- the heaviest
# thing this farm does -- looked perfectly idle.
# ---------------------------------------------------------------------------


# Real /proc/net/tcp shape. Column 1 is local_address as HEX_IP:HEX_PORT,
# column 3 is the state; 0016 is port 22 and 01 is ESTABLISHED.
_NET_TCP_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr "
                   "tm->when retrnsmt   uid  timeout inode\n")
_LISTENING_SSH = "   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1\n"
_ESTABLISHED_SSH = "   1: 0100007F:0016 0100007F:C001 01 00000000:00000000 00:00000000 00000000     0        0 2\n"
_ESTABLISHED_HTTP = "   2: 0100007F:1F40 0100007F:C002 01 00000000:00000000 00:00000000 00000000     0        0 3\n"


def _fake_proc(net_tcp="", pids=None):
    """pids: {pid: comm}. All alive -- zombies have their own fixture below,
    because a process with no readable stat is treated as already gone."""
    pids = pids or {}
    files = {"/proc/net/tcp": _NET_TCP_HEADER + net_tcp}
    for pid, comm in pids.items():
        files["/proc/%s/comm" % pid] = comm + "\n"
        files["/proc/%s/stat" % pid] = "%s (%s) S 1 1 0\n" % (pid, comm)

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    def listdir(path):
        return list(pids) + ["net", "self", "uptime"]

    return read, listdir


def test_an_established_ssh_connection_is_seen():
    read, listdir = _fake_proc(net_tcp=_ESTABLISHED_SSH)

    assert worker.Liveness(read=read, listdir=listdir).ssh_sessions() == 1


def test_a_listening_socket_is_not_a_session():
    """sshd always listens; that is not somebody being there."""
    read, listdir = _fake_proc(net_tcp=_LISTENING_SSH)

    assert worker.Liveness(read=read, listdir=listdir).ssh_sessions() == 0


def test_connections_on_other_ports_are_not_ssh_sessions():
    """The worker's own HTTP traffic must not read as an ssh session."""
    read, listdir = _fake_proc(net_tcp=_LISTENING_SSH + _ESTABLISHED_HTTP)

    assert worker.Liveness(read=read, listdir=listdir).ssh_sessions() == 0


def test_several_sessions_are_counted():
    read, listdir = _fake_proc(net_tcp=_ESTABLISHED_SSH + _ESTABLISHED_SSH)

    assert worker.Liveness(read=read, listdir=listdir).ssh_sessions() == 2


def test_an_unreadable_proc_reports_unknown_not_zero():
    """Zero would read as 'nobody here' and clear the pod for termination."""

    def read(path):
        raise PermissionError(path)

    assert worker.Liveness(read=read, listdir=lambda p: []).ssh_sessions() is None
    assert worker.Liveness(read=read, listdir=_boom).transfers() is None


def _boom(path):
    raise PermissionError(path)


def test_transfer_processes_are_counted():
    read, listdir = _fake_proc(pids={"11": "rclone", "12": "sftp-server",
                                     "13": "bash", "14": "hython-bin"})

    assert worker.Liveness(read=read, listdir=listdir).transfers() == 2


def test_no_transfer_processes_is_zero_not_unknown():
    read, listdir = _fake_proc(pids={"11": "bash", "12": "sshd"})

    assert worker.Liveness(read=read, listdir=listdir).transfers() == 0


def test_a_process_that_exits_mid_scan_does_not_break_the_count():
    """/proc entries vanish under you; that must not make the pod unknown."""
    read, listdir = _fake_proc(pids={"11": "rclone"})

    def flaky(path):
        if path.endswith("/99/comm"):
            raise FileNotFoundError(path)
        return read(path)

    live = worker.Liveness(read=flaky, listdir=lambda p: ["11", "99"])
    assert live.transfers() == 1


# ---------------------------------------------------------------------------
# zombies are not work, and one sample is not evidence
#
# Read off a live sync pod: eleven processes named sftp-server, every one
# <defunct> and reparented to PID 1 -- an upload that had already finished.
# Counting them reported "transfers: 11" and looked like a successful field
# validation. It was the opposite: a pod pinned at busy forever, which
# `farm kill` could never clear. /proc/net/tcp on the same pod held only a
# LISTEN row for :0016, because rclone opens and closes a connection per file.
# ---------------------------------------------------------------------------


def _proc_with(pids):
    """pids: {pid: (comm, state)} -- state 'Z' is a zombie."""
    files = {"/proc/net/tcp": _NET_TCP_HEADER}
    for pid, (comm, state) in pids.items():
        files["/proc/%s/comm" % pid] = comm + "\n"
        files["/proc/%s/stat" % pid] = "%s (%s) %s 1 1 0 0 -1 0 0 0\n" % (pid, comm, state)

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return read, lambda p: list(pids)


def test_a_defunct_transfer_is_not_counted():
    """The exact live reading: 11 sftp-server zombies, upload long finished."""
    read, listdir = _proc_with({str(p): ("sftp-server", "Z") for p in range(11)})

    assert worker.Liveness(read=read, listdir=listdir).transfers() == 0


def test_live_and_dead_transfers_are_told_apart():
    read, listdir = _proc_with({"1": ("rclone", "R"), "2": ("sftp-server", "Z"),
                                "3": ("sftp-server", "S"), "4": ("bash", "S")})

    assert worker.Liveness(read=read, listdir=listdir).transfers() == 2


def test_a_process_name_with_spaces_does_not_break_zombie_detection():
    """/proc/<pid>/stat is "pid (comm) state" and comm can contain anything."""
    files = {"/proc/7/comm": "rclone\n",
             "/proc/7/stat": "7 (rclone (odd) name) Z 1 1 0\n"}

    def read(path):
        if path in files:
            return files[path]
        raise FileNotFoundError(path)

    assert worker.Liveness(read=read, listdir=lambda p: ["7"]).transfers() == 0


def test_the_sampler_remembers_when_work_last_happened():
    """One sample is not evidence: mid-upload there are instants with no
    socket and no byte moving, and calling that idle cuts a transfer in half."""
    clock = [1000.0]
    moved = {"bytes": 500}

    def read(path):
        if path == "/proc/net/tcp":
            return _NET_TCP_HEADER
        if path.endswith("/comm"):
            return "rclone\n"
        if path.endswith("/stat"):
            return "9 (rclone) R 1 1 0\n"
        if path.endswith("/io"):
            return "read_bytes: 0\nwrite_bytes: %d\n" % moved["bytes"]
        raise FileNotFoundError(path)

    live = worker.Liveness(read=read, listdir=lambda p: ["9"], clock=lambda: clock[0])

    assert live.busy_idle_s() is None          # nothing seen yet
    live.sample()                              # first sample: no delta yet
    moved["bytes"] += 4096
    clock[0] += 2
    live.sample()                              # bytes moved -> work
    assert live.busy_idle_s() == 0.0

    # the transfer finishes; the age grows from the last time work happened
    clock[0] += 30
    live.sample()
    assert live.busy_idle_s() == 30.0


def test_a_gap_between_files_stays_well_inside_the_grace_window():
    """rclone opens and closes a connection per file. The gap must read as a
    few seconds since work, not as idle -- classify_for_kill's grace covers it."""
    clock = [0.0]
    moved = {"bytes": 0}

    def read(path):
        if path == "/proc/net/tcp":
            return _NET_TCP_HEADER
        if path.endswith("/comm"):
            return "sftp-server\n"
        if path.endswith("/stat"):
            return "9 (sftp-server) S 1 1 0\n"
        if path.endswith("/io"):
            return "read_bytes: 0\nwrite_bytes: %d\n" % moved["bytes"]
        raise FileNotFoundError(path)

    live = worker.Liveness(read=read, listdir=lambda p: ["9"], clock=lambda: clock[0])
    live.sample()
    moved["bytes"] += 1 << 20        # file 1 transferring
    clock[0] += 2
    live.sample()
    clock[0] += 2                    # between files: nothing moves
    live.sample()
    moved["bytes"] += 1 << 20        # file 2 transferring
    clock[0] += 2
    live.sample()

    assert live.busy_idle_s() == 0.0


# ---------------------------------------------------------------------------
# work, not presence
#
# Excluding zombies was not enough. A live sftp-server holding an open
# connection and doing nothing is presence, not work, and counting it pins the
# pod at "busy" exactly as zombies did -- the same failure with a longer fuse.
# Observed shape: cook cancelled mid-upload, directory grew 0 bytes in 10s, no
# process holding a file open, and the old counter still said 11.
# ---------------------------------------------------------------------------


def _io_proc(pids, net_tcp=""):
    """pids: {pid: (comm, state, read_bytes, write_bytes)}."""
    files = {"/proc/net/tcp": _NET_TCP_HEADER + net_tcp}
    for pid, (comm, state, rb, wb) in pids.items():
        files["/proc/%s/comm" % pid] = comm + "\n"
        files["/proc/%s/stat" % pid] = "%s (%s) %s 1 1 0\n" % (pid, comm, state)
        files["/proc/%s/io" % pid] = (
            "rchar: 1\nwchar: 2\nread_bytes: %d\nwrite_bytes: %d\n" % (rb, wb))

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]

    return read, lambda p: list(pids), files


def test_an_idle_live_transfer_process_is_not_work():
    """The cancelled-cook shape: process alive, nothing moving."""
    clock = [0.0]
    read, listdir, files = _io_proc({"9": ("sftp-server", "S", 1000, 2000)})
    live = worker.Liveness(read=read, listdir=listdir, clock=lambda: clock[0])

    live.sample()                    # first sample: no delta available yet
    clock[0] += 2
    live.sample()                    # bytes unchanged -> not work
    clock[0] += 300
    live.sample()

    assert live.busy_idle_s() is None


def test_bytes_moving_is_work():
    clock = [0.0]
    read, listdir, files = _io_proc({"9": ("sftp-server", "S", 1000, 2000)})
    live = worker.Liveness(read=read, listdir=listdir, clock=lambda: clock[0])

    live.sample()
    files["/proc/9/io"] = "rchar: 1\nwchar: 2\nread_bytes: 1000\nwrite_bytes: 9999\n"
    clock[0] += 2
    live.sample()

    assert live.busy_idle_s() == 0.0


def test_an_open_session_counts_even_with_nothing_moving():
    """Somebody logged in is somebody present, between files or otherwise."""
    clock = [0.0]
    read, listdir, _f = _io_proc({}, net_tcp=_ESTABLISHED_SSH)
    live = worker.Liveness(read=read, listdir=listdir, clock=lambda: clock[0])

    live.sample()

    assert live.busy_idle_s() == 0.0


def test_eleven_zombies_left_by_a_cancelled_cook_are_not_work():
    """The exact live reading, now from both angles: dead AND not moving."""
    clock = [0.0]
    read, listdir, _f = _io_proc(
        {str(p): ("sftp-server", "Z", 500, 500) for p in range(11)})
    live = worker.Liveness(read=read, listdir=listdir, clock=lambda: clock[0])

    live.sample()
    clock[0] += 2
    live.sample()

    assert live.transfers() == 0
    assert live.busy_idle_s() is None


def test_a_kernel_without_proc_pid_io_does_not_report_false_activity():
    read, listdir, files = _io_proc({"9": ("rclone", "R", 0, 0)})
    del files["/proc/9/io"]
    live = worker.Liveness(read=read, listdir=listdir, clock=lambda: 0.0)

    live.sample()
    live.sample()

    assert live.moved_bytes() is None
    assert live.busy_idle_s() is None
