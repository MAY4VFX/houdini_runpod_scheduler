"""``rpfarm`` command-line interface: setup, doctor, houdini, storage, farm, costs, smoke.

Thin wrappers over the same ``rpfarm/`` modules the HDAs use (``config``,
``runpod_api``, ``pods``, ``worker_client``, ``packages``, ``ledger``,
``houdini_local``) -- this module adds no new farm logic of its own, only
argument parsing, output formatting, and glue between calls that already
exist. Run as ``python3 -m rpfarm <command> ...`` (see ``rpfarm/__main__.py``).

Stdlib only, plain system ``python3`` (not ``hython``) -- same reasoning as
``rpfarm.package_runner``: every module this imports is stdlib-only by
design, so there's no reason to pay Houdini's startup cost for a CLI call.

Every network/subprocess-touching dependency is swapped at the *module*
level for tests -- ``_transport`` (RunPod HTTP), ``rpcfg.rclone_bin``,
``houdini_local.find_houdini_installations`` -- rather than threaded
through every function's parameters, mirroring the rest of ``rpfarm``'s
injectable-callable style while keeping this file's signatures plain
``cmd_x(args)``. Tests monkeypatch the module attribute the same call site
reads, e.g. ``monkeypatch.setattr(cli, "_transport", fake)``.

Exit codes: ``0`` success; ``1`` a general/API/config error; ``2`` a
command refused to proceed on purpose (e.g. ``storage rm`` on a project
with pending downloads, without ``--force``).
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import posixpath
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time

from . import config as rpcfg
from . import houdini_local
from . import ledger as rpledger
from . import packages as rppkg
from . import pods as rppods
from . import sync as rpsync
from .runpod_api import RunPodAPI, RunPodError, pod_public_endpoint
from .worker_client import WorkerClient

# None -> RunPodAPI's own default (a real urllib transport). Tests set this
# to a fake transport for the whole call; nothing here holds a long-lived
# RunPodAPI instance across commands, so there's no staleness to worry
# about.
_transport = None

MIN_BALANCE_WARN = 5.0
HOUDINI_INSTALL_DIR = "/workspace/houdini"


def _make_api(api_key):
    if _transport is None:
        return RunPodAPI(api_key)
    return RunPodAPI(api_key, transport=_transport)


# -- output helpers -----------------------------------------------------------


def _fmt_bytes(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


def _fmt_duration(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_table(rows, headers):
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("-" * w for w in widths))
    if not rows:
        lines.append("(none)")
    for r in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(lines)


def _parse_date(s, end_of_day=False):
    """``YYYY-MM-DD`` -> epoch seconds (UTC)."""
    d = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d.replace(tzinfo=datetime.timezone.utc).timestamp()


def _date_to_iso(s, end_of_day=False):
    d = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_iso(days):
    d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_pod_timestamp(value):
    """RunPod's pod ``createdAt`` as epoch seconds, or ``None``.

    A raw epoch number is accepted as-is. Otherwise this tries RunPod
    REST's own actual format first -- confirmed live 2026-09-03 against a
    real ``GET /pods`` response: ``"2026-09-03 21:19:39.775 +0000 UTC"``,
    not ISO8601 despite ``openapi.json`` documenting the field as a plain
    string with no format -- then falls back to ISO8601 (``...Z`` or
    ``+00:00``) in case a future API revision changes it. Neither format
    matching is worth failing ``farm status`` over: an unparseable
    timestamp just means uptime/est-cost show as ``"?"`` for that pod.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f %z UTC").timestamp()
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# -- shared plumbing -----------------------------------------------------------


def _connect_sync_pod(api, cfg, token, log=print):
    """Ensure the user's sync pod is up and return ``(pod, WorkerClient)``.

    Never terminates anything -- the sync pod is a shared, reused resource
    (see ``rpfarm.pods.ensure_sync_pod``'s own docstring), not a
    per-command scratch pod. ``rpfarm farm kill --sync`` is the deliberate
    way to tear it down.
    """
    with open(cfg.ssh_key_path + ".pub") as f:
        pubkey = f.read()
    pod = rppods.ensure_sync_pod(api, cfg, token, pubkey, log=log)
    client = WorkerClient(pod["id"], token)
    return pod, client


def _find_running_sync_pod(api, cfg):
    """The user's sync pod if one is already RUNNING, else ``None`` --
    never creates one (doctor/farm status must not spin up a pod just to
    look at it)."""
    name = rppods.sync_pod_name(cfg.user)
    for p in api.list_pods(name):
        if p.get("name") == name and p.get("desiredStatus") == "RUNNING":
            return p
    return None


def _housekeeping_exec(client, args_str, timeout_s=120):
    """Run ``python3 /opt/rpfarm/housekeeping.py <args_str>`` and parse its
    JSON stdout. Returns ``(exit_code, data_or_None, raw_result)``."""
    result = client.exec(f"python3 /opt/rpfarm/housekeeping.py {args_str}", timeout_s=timeout_s)
    exit_code = result.get("exit_code")
    if exit_code != 0:
        return exit_code, None, result
    try:
        return exit_code, json.loads(result.get("stdout") or "{}"), result
    except json.JSONDecodeError:
        return exit_code, None, result


def _report_housekeeping_failure(result, stream=sys.stderr):
    print((result.get("stderr") or result.get("stdout") or "housekeeping command failed").strip(), file=stream)


# -- setup ----------------------------------------------------------------


def _ensure_ssh_key(path, run=subprocess.run, log=print):
    pub = path + ".pub"
    if os.path.exists(path) and os.path.exists(pub):
        log(f"[OK] SSH key already present ({path})")
        return True
    try:
        run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", path], check=True, capture_output=True, text=True)
        log(f"[OK] generated SSH key ({path})")
        return True
    except (subprocess.CalledProcessError, OSError, FileNotFoundError) as e:
        log(f"[WARN] ssh-keygen failed: {e}")
        return False


def _ensure_src_symlink(home, log=print):
    target = houdini_local.repo_root()
    link = home / "src"
    if link.is_symlink() and link.resolve() == target.resolve():
        log(f"[OK] {link} -> {target}")
        return
    if link.is_symlink() or link.exists():
        if link.is_symlink() or link.is_file():
            link.unlink()
        else:
            log(f"[WARN] {link} exists and is not a symlink -- leaving it alone")
            return
    link.symlink_to(target)
    log(f"[OK] {link} -> {target}")


def _resolve_volume_id(api, args, existing, user, log=print, prompt=input):
    """``--volume`` wins; else an already-configured volume is kept as-is
    (idempotency -- a rerun of ``setup`` must not go back to RunPod and
    possibly land on a *different* volume just because the account now has
    more than one); only a first run with neither discovers/creates one."""
    if args.volume:
        return args.volume
    if existing is not None and existing.volume_id:
        log(f"[OK] keeping configured volume {existing.volume_id} (pass --volume to change)")
        return existing.volume_id
    return _pick_volume(api, args, user, log=log, prompt=prompt)


def _pick_volume(api, args, user, log=print, prompt=input):
    if args.volume:
        return args.volume
    volumes = api.list_volumes()
    if len(volumes) == 1:
        v = volumes[0]
        log(f"[OK] using existing volume {v['id']} ({v.get('dataCenterId', '?')}, {v.get('size', '?')} GB)")
        return v["id"]
    if not volumes:
        vol = api.create_volume(f"rpfarm-{user}", 50, dc="EU-RO-1")
        log(f"[OK] created volume {vol['id']} (50 GB, EU-RO-1)")
        return vol["id"]
    if args.non_interactive:
        log("[FAIL] multiple volumes found; pass --volume <id>:")
        for v in volumes:
            log(f"    {v['id']}  {v.get('name', '')}  {v.get('dataCenterId', '')}")
        raise SystemExit(1)
    log("Multiple volumes found:")
    for i, v in enumerate(volumes, 1):
        log(f"  [{i}] {v['id']}  {v.get('name', '')}  {v.get('dataCenterId', '')}")
    choice = prompt("Which volume? [1] ").strip() or "1"
    return volumes[int(choice) - 1]["id"]


def _resolve_template_id(api, args, existing, cfg_stub, log=print):
    """Same idempotency rule as :func:`_resolve_volume_id`: a rerun keeps
    the configured template rather than re-discovering/recreating one."""
    if args.template:
        return args.template
    if existing is not None and existing.template_id:
        log(f"[OK] keeping configured template {existing.template_id} (pass --template to change)")
        return existing.template_id
    return _pick_template(api, args, cfg_stub, log=log)


def _pick_template(api, args, cfg_stub, log=print):
    if args.template:
        return args.template
    templates = api.list_templates()
    existing = next((t for t in templates if t.get("name") == "rpfarm-pod"), None)
    if existing:
        log(f"[OK] using existing template {existing['id']} (rpfarm-pod, image {existing.get('imageName')})")
        return existing["id"]
    env = {
        "SESINETD_HOST": cfg_stub.sesinetd_host,
        "SESINETD_PORT": str(cfg_stub.sesinetd_port),
        "HOUDINI_VERSION": cfg_stub.houdini_version,
    }
    tpl = api.save_template("rpfarm-pod", "ghcr.io/may4vfx/rpfarm-pod:latest", rppods.PORTS, env)
    log(f"[OK] created template {tpl['id']} (rpfarm-pod)")
    return tpl["id"]


def cmd_setup(args, prompt=input):
    """Idempotent by design: a rerun (the checklist's own "install Houdini
    locally, then rerun `rpfarm setup`") must never reset hand-tuned or
    doctor-written fields -- `gpu_priority`, `houdini_version`,
    `sync_idle_min`, `measured_mbps`, `sesinetd_host/port`, `rclone_path`,
    `ssh_key_path` -- back to their dataclass defaults. When a
    `config.toml` already exists, this loads and mutates it in place
    (only `api_key`/`user`/`volume_id`/`template_id`/`datacenter` are ever
    touched by `setup` itself); a first run with no existing config still
    builds a fresh `Config` from scratch.
    """
    home = rpcfg.home()
    home.mkdir(parents=True, exist_ok=True)

    try:
        existing = rpcfg.load()
    except rpcfg.ConfigError:
        existing = None

    api_key = args.api_key or (existing.api_key if existing else None)
    if not api_key:
        if args.non_interactive:
            print("error: --non-interactive requires --api-key (no existing config.toml to reuse one from)", file=sys.stderr)
            return 1
        api_key = prompt("RunPod API key: ").strip()
    if not api_key:
        print("error: no API key given", file=sys.stderr)
        return 1

    default_user = (existing.user if existing else None) or getpass.getuser()
    user = args.user or (existing.user if existing else None)
    if not user:
        user = default_user if args.non_interactive else (prompt(f"User name [{default_user}]: ").strip() or default_user)

    api = _make_api(api_key)
    try:
        api.list_pods()
        balance = api.balance()
    except RunPodError as e:
        print(f"[FAIL] RunPod API key invalid or unreachable: {e}", file=sys.stderr)
        return 1
    print(f"[OK] RunPod API key valid (balance ${balance:.2f})")

    try:
        volume_id = _resolve_volume_id(api, args, existing, user)
    except SystemExit as e:
        return e.code or 1

    datacenter = (existing.datacenter if existing else None) or "EU-RO-1"
    try:
        vinfo = api.get_volume(volume_id)
        if vinfo:
            datacenter = vinfo.get("dataCenterId", datacenter)
    except RunPodError:
        pass

    if existing is not None:
        cfg = existing
        cfg.api_key = api_key
        cfg.user = user
        cfg.volume_id = volume_id
        cfg.datacenter = datacenter
    else:
        cfg = rpcfg.Config(api_key=api_key, user=user, volume_id=volume_id, template_id="", datacenter=datacenter)

    cfg.template_id = _resolve_template_id(api, args, existing, cfg)

    rpcfg.save(cfg)
    print(f"[OK] wrote {home / rpcfg.CONFIG_FILENAME}")

    rpcfg.session_token()
    print(f"[OK] worker token ready ({home / rpcfg.TOKEN_FILENAME})")

    _ensure_ssh_key(cfg.ssh_key_path)

    rclone_path = rpcfg.rclone_bin()
    print(f"[OK] rclone ready ({rclone_path})")

    _ensure_src_symlink(home)

    installs = houdini_local.find_houdini_installations()
    if not installs:
        print("[WARN] no local Houdini installation found -- HDAs not installed")
    for inst in installs:
        results = houdini_local.build_and_install_hdas(inst, home / "otls")
        ok_count = sum(1 for r in results if r["ok"])
        level = "OK" if ok_count == len(results) else "WARN"
        print(f"[{level}] Houdini {inst.version}: {ok_count}/{len(results)} HDA(s) installed")
        for r in results:
            if not r["ok"]:
                print(f"    [WARN] {r['name']}: {r['error']}")
        houdini_local.write_rpfarm_root_env(inst)

    print()
    print("Setup checklist:")
    print(f"  [x] config.toml, token, ssh key    -> {home}")
    print(f"  [x] rclone                         -> {rclone_path}")
    print(f"  [x] repo symlink                   -> {home / 'src'}")
    if installs:
        print(f"  [x] HDAs installed for {len(installs)} local Houdini installation(s)")
    else:
        print("  [ ] HDAs -- install Houdini locally, then rerun `rpfarm setup`")
    print("  [ ] Houdini on the farm volume      -- `rpfarm houdini install --tar <path> --version <ver>`")
    print()
    print("Run `rpfarm doctor` to verify everything end to end.")
    return 0


# -- doctor ----------------------------------------------------------------


def _measure_uplink(cfg, pod, client, rclone_bin, size_mb=20, copy_fn=rpsync.rclone_copy):
    """Upload ``size_mb`` of random bytes to the sync pod and return the
    measured Mbps. Removes the probe file from the volume afterward
    (best-effort, via ``client``) so repeated `doctor` runs don't leave
    junk behind on shared paid storage -- failure to clean up is not
    itself a measurement failure and is not surfaced to the caller.
    """
    ip, port = pod_public_endpoint(pod, 22)
    target = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)
    remote_dir = "/workspace/.rpfarm"
    remote_name = "rpfarm_doctor_uplink.bin"
    remote_path = posixpath.join(remote_dir, remote_name)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, remote_name)
            with open(local, "wb") as f:
                f.write(os.urandom(size_mb * 2**20))
            entry = rpsync.FileEntry(local=local, remote=remote_path, size=os.path.getsize(local))
            stats = copy_fn([entry], target, "up", rclone_bin, tmp, remote_dir)
    finally:
        client.exec(f"rm -f {shlex.quote(remote_path)}", timeout_s=15)
    return (stats.bytes * 8) / 1e6 / max(stats.seconds, 1e-3)


def cmd_doctor(args):
    try:
        cfg = rpcfg.load()
    except rpcfg.ConfigError as e:
        print(f"[FAIL] {e}")
        return 1

    failures = 0

    def ok(msg):
        print(f"[OK]   {msg}")

    def warn(msg):
        print(f"[WARN] {msg}")

    def fail(msg):
        nonlocal failures
        failures += 1
        print(f"[FAIL] {msg}")

    api = _make_api(cfg.api_key)

    try:
        api.list_pods()
        balance = api.balance()
        if balance < MIN_BALANCE_WARN:
            warn(f"RunPod balance ${balance:.2f} < ${MIN_BALANCE_WARN:.0f} -- top up your account")
        else:
            ok(f"RunPod API key valid (balance ${balance:.2f})")
    except RunPodError as e:
        fail(f"RunPod API key invalid or unreachable: {e}")

    try:
        vol = api.get_volume(cfg.volume_id)
        if vol:
            ok(f"volume {cfg.volume_id} exists ({vol.get('size', '?')} GB, {vol.get('dataCenterId', '?')})")
        else:
            fail(f"volume {cfg.volume_id} not found -- `rpfarm storage recreate` or fix config.toml")
    except RunPodError as e:
        fail(f"could not look up volume {cfg.volume_id}: {e}")

    try:
        templates = api.list_templates()
        tpl = next((t for t in templates if t.get("id") == cfg.template_id), None)
        if tpl:
            ok(f"template {cfg.template_id} exists (image {tpl.get('imageName', '?')})")
        else:
            fail(f"template {cfg.template_id} not found -- rerun `rpfarm setup`")
    except RunPodError as e:
        fail(f"could not list templates: {e}")

    try:
        with socket.create_connection((cfg.sesinetd_host, cfg.sesinetd_port), timeout=5):
            ok(f"{cfg.sesinetd_host}:{cfg.sesinetd_port} reachable (license server)")
    except OSError as e:
        fail(f"{cfg.sesinetd_host}:{cfg.sesinetd_port} unreachable ({e}) -- check network/VPN")

    try:
        proc = subprocess.run([cfg.rclone_path, "--version"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            ok(f"rclone: {proc.stdout.splitlines()[0] if proc.stdout else 'ok'}")
        else:
            fail(f"`rclone --version` failed: {proc.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as e:
        fail(f"rclone not runnable at {cfg.rclone_path} ({e}) -- rerun `rpfarm setup`")

    if os.path.exists(cfg.ssh_key_path) and os.path.exists(cfg.ssh_key_path + ".pub"):
        ok(f"SSH key present ({cfg.ssh_key_path})")
    else:
        fail(f"SSH key missing at {cfg.ssh_key_path} -- rerun `rpfarm setup`")

    installs = houdini_local.find_houdini_installations()
    if not installs:
        warn("no local Houdini installation found -- HDAs not checked")
    for inst in installs:
        otls = inst.user_pref_dir / "otls"
        missing = [n for n in houdini_local.HDA_NAMES if not (otls / f"{n}.hda").exists()]
        if missing:
            warn(f"Houdini {inst.version}: missing HDA(s) {', '.join(missing)} -- rerun `rpfarm setup`")
        else:
            newest = max((otls / f"{n}.hda").stat().st_mtime for n in houdini_local.HDA_NAMES)
            ok(f"Houdini {inst.version}: all 4 HDAs installed (last updated {time.ctime(newest)})")

    sync_pod = _find_running_sync_pod(api, cfg)
    if sync_pod is None:
        warn("Houdini on the farm volume: not checked, requires a running pod (`rpfarm storage ls` starts one)")
        warn("uplink speed: not checked, requires a running sync pod")
    else:
        token = rpcfg.session_token()
        client = WorkerClient(sync_pod["id"], token)
        exit_code, data, result = _housekeeping_exec(client, "houdini ls", timeout_s=60)
        if exit_code == 0 and data is not None:
            versions = [v["version"] for v in data.get("versions", [])]
            if versions:
                ok(f"Houdini on volume: {', '.join(versions)}")
            else:
                warn("no Houdini installed on the farm volume -- `rpfarm houdini install`")
        else:
            warn(f"could not check Houdini on volume: {(result.get('stderr') or result.get('stdout') or '').strip()}")

        try:
            mbps = _measure_uplink(cfg, sync_pod, client, cfg.rclone_path)
            ok(f"uplink: {mbps:.1f} Mbps (measured against the sync pod)")
            cfg.measured_mbps = mbps
            rpcfg.save(cfg)
        except Exception as e:  # noqa: BLE001 - a failed measurement is a WARN, not fatal to doctor
            warn(f"could not measure uplink: {e}")

    try:
        types = api.gpu_types(dc=cfg.datacenter)
        by_id = {t["id"]: t for t in types}
        if not cfg.gpu_priority:
            warn("gpu_priority is empty in config.toml -- no GPU pod could ever be created")
        else:
            any_stock = False
            for gid in cfg.gpu_priority:
                status = ((by_id.get(gid) or {}).get("lowestPrice") or {}).get("stockStatus")
                if status:
                    any_stock = True
                    ok(f"GPU stock for {gid} in {cfg.datacenter}: {status}")
                else:
                    warn(f"GPU stock for {gid} in {cfg.datacenter}: none right now")
            if not any_stock:
                warn(f"no stock at all for gpu_priority in {cfg.datacenter} -- a cook may fail to get a GPU pod")
    except RunPodError as e:
        warn(f"could not query GPU stock: {e}")

    print()
    print(f"{'FAIL' if failures else 'OK'}: {failures} failing check(s)")
    return 1 if failures else 0


# -- houdini ----------------------------------------------------------------


# Identity files an ``ssh``/``scp`` to the same host would try on its own,
# in ssh's own preference order. rclone's sftp backend does *not* read
# these (or ``~/.ssh/config``): with no ``key_file`` it offers only what an
# ssh-agent holds, so on a Mac with an empty agent -- the normal state --
# staging died with "unable to authenticate, attempted methods [none
# publickey]" even though plain ``scp`` from that host worked. Task 14, the
# first live run of this path, hit exactly that.
_SSH_DEFAULT_IDENTITIES = ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa")


def _default_ssh_key_files(home=None):
    """Existing stock ssh identities, in ssh's own preference order."""
    home = home or os.path.expanduser("~")
    paths = [os.path.join(home, ".ssh", name) for name in _SSH_DEFAULT_IDENTITIES]
    return [p for p in paths if os.path.isfile(p)]


def _sftp_remotes_to_try(host, user, key_file=None):
    """rclone ``:sftp,...:`` remote strings to attempt, in order.

    An explicit ``key_file`` is the only candidate. Otherwise: the bare
    remote first (an ssh-agent identity, if the agent holds one), then one
    candidate per stock identity file. rclone takes a single ``key_file``
    and gives up if it is rejected, whereas ``ssh`` walks its identities
    until one is accepted -- so the walking has to happen here, or the
    host's key simply has to be the first one alphabetically. Every
    candidate fails (or succeeds) at connection setup, before any bytes
    move, so retrying costs a handshake and never a partial transfer.
    """
    base = f":sftp,host={host},user={user}"
    if key_file:
        return [f"{base},key_file={key_file}"]
    return [base] + [f"{base},key_file={p}" for p in _default_ssh_key_files()]


def _is_sftp_auth_failure(stderr):
    return "unable to authenticate" in (stderr or "") or "couldn't connect SSH" in (stderr or "")


def _stage_tar_from_sftp_url(url, rclone_bin, tmp_dir, run=subprocess.run, key_file=None):
    """Stage a tarball that lives on an external SFTP host (not the rpfarm
    sync pod) into a local temp file via rclone's on-the-fly ``:sftp,...:``
    remote syntax, so ``cmd_houdini_install`` only ever has to deal with a
    local path from here on. Auth is meant to match what a manual ``scp``
    from that host would use -- an ssh-agent identity, else whichever of
    the caller's stock ``~/.ssh`` keys the host accepts
    (:func:`_sftp_remotes_to_try`, since rclone looks at neither on its
    own). This repo's own ``rpfarm`` SSH key is for the farm's pods, not
    arbitrary hosts.
    """
    m = re.match(r"^sftp://(?:([^@/]+)@)?([^/]+)(/.+)$", url)
    if not m:
        raise ValueError(f"invalid sftp url: {url!r} (expected sftp://[user@]host/path)")
    user, host, path = m.group(1) or "root", m.group(2), m.group(3)
    local = os.path.join(tmp_dir, os.path.basename(path))
    last_err = ""
    for remote in _sftp_remotes_to_try(host, user, key_file):
        args = [rclone_bin, "copyto", f"{remote}:{path}", local]
        proc = run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            return local
        last_err = proc.stderr
        if not _is_sftp_auth_failure(last_err):
            break
    raise RuntimeError(f"rclone copyto from {url} failed: {last_err}")


def cmd_houdini_install(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    version = args.version or cfg.houdini_version

    with tempfile.TemporaryDirectory() as tmp:
        tar_local = args.tar
        if tar_local.startswith("sftp://"):
            print(f"staging {tar_local} locally ...")
            tar_local = _stage_tar_from_sftp_url(tar_local, cfg.rclone_path, tmp)
        if not os.path.isfile(tar_local):
            print(f"error: tarball not found: {tar_local}", file=sys.stderr)
            return 1

        print(f"connecting to sync pod for {cfg.user} ...")
        pod, client = _connect_sync_pod(api, cfg, token)
        ip, port = pod_public_endpoint(pod, 22)
        sftp = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)

        pairs, post_command = rppkg.houdini_install_preset(tar_local, version)
        local, remote_dir = pairs[0]
        size = os.path.getsize(local)

        note = rppkg.maybe_grow_volume(api, cfg, client, size, log=print)
        if note != "ok":
            print(f"[volume] {note}")

        item = {
            "files": [(local, posixpath.join(remote_dir, os.path.basename(local)), size)],
            "local_root": os.path.dirname(local),
            "remote_root": remote_dir.rstrip("/"),
            "post_command": post_command,
            "bytes": size,
        }

        def progress(done, total, speed):
            print(f"\ruploading: {done / 2**20:.0f}/{total / 2**20:.0f} MB", end="", flush=True)

        stats = rppkg.run_upload_item(item, cfg, sftp, client, compress=False, progress_cb=progress)
        print()
        print(f"[OK] uploaded {_fmt_bytes(stats['bytes'])} in {stats['seconds']:.1f}s; installer ran on the sync pod")
        print(f"[OK] Houdini {version} installed to {HOUDINI_INSTALL_DIR}/{version}")
    return 0


def cmd_houdini_ls(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    exit_code, data, result = _housekeeping_exec(client, "houdini ls", timeout_s=60)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    rows = [(v["version"], _fmt_bytes(v["bytes"])) for v in data.get("versions", [])]
    print(_fmt_table(rows, ["version", "size"]))
    if data.get("partial"):
        print("(partial: some sizes are cached/stale)")
    return 0


def cmd_houdini_rm(args):
    """Delete one installed Houdini version -- potentially tens of GB.
    Same confirmation gate as ``storage prune`` (Ruling R30): defaults to
    a dry run and requires ``--yes``/``--force`` to actually delete;
    ``--dry-run`` is kept as an explicit, inert alias for the default.
    Unlike ``storage rm``, ``pod/housekeeping.py``'s ``houdini rm`` already
    has a native ``--dry-run`` mode, so the gate is just which of that or
    a real delete gets sent -- no separate client-side-only refusal needed.
    """
    dry_run = not args.yes
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"houdini rm {shlex.quote(args.version)}"
    if dry_run:
        cmd_str += " --dry-run"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=120)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    if not data.get("ok"):
        print(data.get("error", "failed"), file=sys.stderr)
        return 2
    if dry_run:
        print(f"would free {_fmt_bytes(data.get('bytes_freed'))}: {data.get('path')} -- pass --yes to actually delete")
    else:
        print(f"freed {_fmt_bytes(data.get('bytes_freed'))}: {data.get('path')}")
    return 0


# -- storage ----------------------------------------------------------------


def cmd_storage_ls(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)

    size_gb = rppkg.get_volume_size_gb(api, cfg)
    cmd_str = "ls"
    if size_gb:
        cmd_str += f" --volume-size-gb {size_gb}"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=180)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1

    vol = data.get("volume") or {}
    if vol.get("total"):
        print(f"volume: {_fmt_bytes(vol.get('used'))} / {_fmt_bytes(vol.get('total'))} ({vol.get('used_pct', 0):.1f}%)")
    else:
        print(f"volume: {_fmt_bytes(vol.get('used'))} used (total unknown -- pass a volume size)")
    print()
    print("zones:")
    for zone, nbytes in (data.get("zones") or {}).items():
        print(f"  {zone:10s} {_fmt_bytes(nbytes)}")
    print()
    rows = [
        (p["user"], p["project"], _fmt_bytes(p["bytes"]), "yes" if p.get("outputs_pending") else "")
        for p in data.get("projects", [])
    ]
    print(_fmt_table(rows, ["user", "project", "size", "outputs pending"]))
    if data.get("partial"):
        print("(partial: some sizes are cached/stale -- rerun for a full remeasure)")
    return 0


def cmd_storage_du(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    exit_code, data, result = _housekeeping_exec(client, f"du {shlex.quote(args.path)}", timeout_s=60)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    rows = [(e["path"], _fmt_bytes(e["bytes"])) for e in data]
    print(_fmt_table(rows, ["path", "size"]))
    return 0


def cmd_storage_rm(args):
    """Delete one project directory. ``--force`` is required for ANY
    deletion, not just to override housekeeping's own "outputs pending"
    guard: ``pod/housekeeping.py``'s ``cmd_rm`` deletes immediately with
    no confirmation at all when nothing is pending, so without a
    CLI-level gate a project with no pending downloads was one typo away
    from silent, permanent, irreversible loss on shared paid storage.
    Without ``--force`` this refuses client-side and never even connects
    to a sync pod -- review with ``storage ls``/``storage du`` first.
    """
    if not args.force:
        print(
            f"refusing to delete {args.user_project} without --force "
            "-- this permanently deletes files from the shared farm volume. "
            "Review with `rpfarm storage ls`/`storage du` first, then rerun with --force.",
            file=sys.stderr,
        )
        return 2

    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"rm {shlex.quote(args.user_project)} --force"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=120)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    if not data.get("ok"):
        print(data.get("error", "failed"), file=sys.stderr)
        return 2
    print(f"freed {_fmt_bytes(data.get('bytes_freed'))}: {data.get('path')}")
    return 0


def cmd_storage_prune(args):
    """List (and, only with ``--yes``, delete) projects unused for
    ``--older-days``. Defaults to a dry run: this is bulk, unattended-
    looking deletion across every project on shared paid storage, so
    "show what would happen" is the safe default and ``--dry-run`` is
    kept only as an explicit alias for it (scripts that already pass it
    keep working unchanged). Actually deleting requires ``--yes``.
    """
    dry_run = not args.yes
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"prune --older-days {args.older_days}"
    if dry_run:
        cmd_str += " --dry-run"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=180)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    candidates = data.get("candidates", [])
    rows = [(c["user"], c["project"], _fmt_bytes(c["bytes"]), f"{c['age_days']:.0f}d") for c in candidates]
    print(_fmt_table(rows, ["user", "project", "size", "age"]))
    if dry_run:
        print(f"would delete {len(candidates)} project(s) -- pass --yes to actually delete")
    else:
        print(f"deleted {len(candidates)} project(s)")
    if data.get("boot_logs_rotated"):
        print(f"rotated {len(data['boot_logs_rotated'])} old boot log(s)")
    return 0


_GROW_RE = re.compile(r"^\+(\d+)$")


def cmd_storage_grow(args):
    m = _GROW_RE.match(args.amount)
    if not m:
        print("error: amount must look like +N (grow by N GB), e.g. +20", file=sys.stderr)
        return 1
    delta_gb = int(m.group(1))

    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    current = api.get_volume(cfg.volume_id)
    if not current:
        print(f"error: volume {cfg.volume_id} not found", file=sys.stderr)
        return 1
    new_size = int(current["size"]) + delta_gb
    api.resize_volume(cfg.volume_id, new_size)
    rppkg.set_volume_size_gb(cfg.volume_id, new_size)
    print(f"[OK] volume {cfg.volume_id} grown {current['size']} GB -> {new_size} GB")
    return 0


def cmd_storage_recreate(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    old_id = cfg.volume_id

    new_vol = api.create_volume(f"rpfarm-{cfg.user}", args.size, dc=cfg.datacenter)
    new_id = new_vol["id"]
    print(f"[OK] created new volume {new_id} ({args.size} GB, {cfg.datacenter})")

    cfg.volume_id = new_id
    rpcfg.save(cfg)
    print(f"[OK] config now points at {new_id}")

    if args.tar and args.version:
        rc = cmd_houdini_install(argparse.Namespace(tar=args.tar, version=args.version))
        if rc != 0:
            print("[WARN] Houdini install on the new volume failed -- run `rpfarm houdini install` yourself.")
    else:
        print("Next: `rpfarm houdini install --tar <path> --version <ver>` to put Houdini on the new volume.")

    print("Projects re-sync from local copies on their next cook -- local is the source of truth.")
    print("When you're done migrating, delete the OLD volume yourself (never automatic):")
    print(f'  curl -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/networkvolumes/{old_id}')
    return 0


# -- farm ----------------------------------------------------------------


def cmd_farm_status(args):
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    pods = api.list_pods("rpfarm-")
    if not pods:
        print("no rpfarm pods running")
        return 0

    now = time.time()
    rows = []
    for p in pods:
        created = _parse_pod_timestamp(p.get("createdAt"))
        uptime_s = (now - created) if created is not None else None
        cost_per_hr = float(p.get("costPerHr") or 0.0)
        est_cost = uptime_s / 3600.0 * cost_per_hr if uptime_s is not None else None
        rows.append((
            p.get("name", "?"),
            p.get("id", "?"),
            p.get("desiredStatus", "?"),
            f"${cost_per_hr:.3f}/h",
            _fmt_duration(uptime_s),
            f"${est_cost:.2f}" if est_cost is not None else "?",
        ))
    print(_fmt_table(rows, ["name", "id", "status", "rate", "uptime", "est cost"]))

    sync_name = rppods.sync_pod_name(cfg.user)
    sync_pod = next((p for p in pods if p.get("name") == sync_name and p.get("desiredStatus") == "RUNNING"), None)
    if sync_pod:
        token = rpcfg.session_token()
        client = WorkerClient(sync_pod["id"], token)
        exit_code, data, _result = _housekeeping_exec(client, "sync-idle", timeout_s=15)
        if exit_code == 0 and data is not None and data.get("idle_seconds") is not None:
            print(f"\nsync pod idle for {data['idle_seconds']:.0f}s (kill threshold: {cfg.sync_idle_min * 60}s)")
    return 0


def cmd_farm_kill(args):
    """Terminate rpfarm pod(s). Scope is opt-in and explicit, because the
    account-wide ``rpfarm-`` prefix used by ``farm status`` covers every
    user's pods, not just the caller's: ``--pod ID`` one specific pod;
    ``--sync`` this user's own sync pod only (``rpfarm-sync-<user>``);
    ``--all`` this user's own cook pods only (``rpfarm-<user>-*`` --
    never the sync pod, never another user's pods); ``--everyone`` is the
    one flag that reaches outside ``cfg.user`` at all, killing every
    ``rpfarm-*`` pod on the whole shared account.
    """
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)

    if args.pod:
        api.terminate_pod(args.pod)
        print(f"[OK] terminated {args.pod}")
        return 0

    if args.sync:
        name = rppods.sync_pod_name(cfg.user)
        pods = [p for p in api.list_pods(name) if p.get("name") == name]
    elif args.everyone:
        pods = api.list_pods("rpfarm-")
    elif args.all:
        pods = api.list_pods(f"rpfarm-{cfg.user}-")
    else:
        print("error: pass --all (your own pods), --everyone (DANGER: every user's pods), --pod <id>, or --sync", file=sys.stderr)
        return 1

    if not pods:
        print("nothing to terminate")
        return 0
    for p in pods:
        api.terminate_pod(p["id"])
        print(f"[OK] terminated {p.get('name', '?')} ({p['id']})")
    return 0


# -- costs ----------------------------------------------------------------


def cmd_costs(args):
    cfg = rpcfg.load()
    records = rpledger.load_all(rpcfg.home() / "ledger")

    since_epoch = _parse_date(args.since) if args.since else None
    until_epoch = _parse_date(args.until, end_of_day=True) if args.until else None

    if args.billing:
        api = _make_api(cfg.api_key)
        since_iso = _date_to_iso(args.since) if args.since else _days_ago_iso(90)
        until_iso = _date_to_iso(args.until, end_of_day=True) if args.until else _now_iso()
        try:
            billing = api.billing_pods(since_iso, until_iso)
            records = rpledger.merge_billing(records, billing)
        except RunPodError as e:
            print(f"[WARN] could not fetch billing, showing estimates only: {e}", file=sys.stderr)

    summary = rpledger.summarize(records, by=args.by, since=since_epoch, until=until_epoch)
    rows = [
        (g["key"], f"${g['cost']:.2f}", f"{g['gpu_hours']:.2f}", g["tasks"], f"${g['cost_per_task']:.3f}")
        for g in summary
    ]
    print(_fmt_table(rows, [args.by, "cost", "gpu hours", "tasks", "$/task"]))
    print(f"\ntotal: ${sum(g['cost'] for g in summary):.2f}")
    return 0


# -- argument parsing / dispatch --------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(prog="rpfarm")
    sub = p.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="set up ~/.rpfarm, find/create volume+template, install HDAs (safe to rerun)")
    p_setup.add_argument("--api-key", help="RunPod API key (default: prompt, or the key already in config.toml)")
    p_setup.add_argument("--user", help="your farm username (default: config.toml's, else the OS login name)")
    p_setup.add_argument("--volume", help="use this network volume id instead of discovering/keeping one")
    p_setup.add_argument("--template", help="use this pod template id instead of discovering/keeping one")
    p_setup.add_argument("--non-interactive", action="store_true", help="never prompt; fail instead of asking")

    sub.add_parser("doctor", help="check the whole setup end to end (key, volume, template, HDAs, GPU stock, ...)")

    p_houdini = sub.add_parser("houdini", help="manage Houdini installs on the shared farm volume")
    houdini_sub = p_houdini.add_subparsers(dest="houdini_command", required=True)
    p_hi = houdini_sub.add_parser("install", help="upload a Houdini tarball to the volume and run its installer")
    p_hi.add_argument("--tar", required=True, help="local path, or sftp://[user@]host/path")
    p_hi.add_argument("--version", default=None, help="defaults to config.toml's houdini_version")
    houdini_sub.add_parser("ls", help="list Houdini versions installed on the volume, with size")
    p_hrm = houdini_sub.add_parser("rm", help="delete one installed Houdini version from the volume (potentially tens of GB)")
    p_hrm.add_argument("version", help="e.g. 22.0.393, or \"legacy\" for a v1 flat install")
    p_hrm.add_argument("--dry-run", action="store_true", help="(default behaviour) show what would be freed; delete nothing")
    p_hrm.add_argument("--yes", "--force", dest="yes", action="store_true", help="actually delete the version")

    p_storage = sub.add_parser("storage", help="inspect/manage the shared farm volume")
    storage_sub = p_storage.add_subparsers(dest="storage_command", required=True)
    storage_sub.add_parser("ls", help="zones, per-project sizes, and volume totals")
    p_du = storage_sub.add_parser("du", help="sizes of the immediate children of a path on the volume")
    p_du.add_argument("path", help="an absolute path on the volume, e.g. /workspace/projects/may")
    p_rm = storage_sub.add_parser("rm", help="permanently delete one project directory (requires --force)")
    p_rm.add_argument("user_project", metavar="USER/PROJECT", help="e.g. may/shotA -- the project directory to delete")
    p_rm.add_argument("--force", action="store_true", help="required for any deletion -- this is irreversible, shared, paid storage")
    p_prune = storage_sub.add_parser("prune", help="find (and, with --yes, delete) projects unused for --older-days")
    p_prune.add_argument("--older-days", type=float, default=30, help="candidate threshold in days (default 30)")
    p_prune.add_argument("--dry-run", action="store_true", help="(default behaviour) show candidates only, delete nothing")
    p_prune.add_argument("--yes", "--force", dest="yes", action="store_true", help="actually delete the listed candidates")
    p_grow = storage_sub.add_parser("grow", help="grow the volume (RunPod volumes only ever grow, never shrink)")
    p_grow.add_argument("amount", metavar="+N", help="grow by N GB, e.g. +20")
    p_recreate = storage_sub.add_parser(
        "recreate", help="create a brand-new volume, point config.toml at it, print the old volume's delete command"
    )
    p_recreate.add_argument("--size", type=int, required=True, help="size of the new volume, in GB")
    p_recreate.add_argument("--tar", default=None, help="also install Houdini on the new volume (with --version)")
    p_recreate.add_argument("--version", default=None, help="Houdini version to install on the new volume (with --tar)")

    p_farm = sub.add_parser("farm", help="see/kill running rpfarm pods")
    farm_sub = p_farm.add_subparsers(dest="farm_command", required=True)
    farm_sub.add_parser("status", help="list running rpfarm-* pods (all users) with rate/uptime/est cost")
    p_kill = farm_sub.add_parser("kill", help="terminate pod(s) -- pick exactly one of the flags below")
    p_kill.add_argument("--all", action="store_true", help="kill all of YOUR OWN cook pods (rpfarm-<user>-*) -- not the sync pod, not other users' pods")
    p_kill.add_argument("--everyone", action="store_true", help="DANGER: kill every rpfarm-* pod on the whole account, including other users' pods and every sync pod")
    p_kill.add_argument("--pod", metavar="ID", help="kill one specific pod by id")
    p_kill.add_argument("--sync", action="store_true", help="kill YOUR OWN sync pod (rpfarm-sync-<user>) only")

    p_costs = sub.add_parser("costs", help="ledger + billing cost summary")
    p_costs.add_argument("--by", choices=["project", "user", "cook"], default="project", help="group totals by (default: project)")
    p_costs.add_argument("--since", metavar="YYYY-MM-DD", help="only records started on/after this date")
    p_costs.add_argument("--until", metavar="YYYY-MM-DD", help="only records started on/before this date")
    p_costs.add_argument("--billing", action="store_true", help="merge in RunPod's actual billed cost (GET /billing/pods)")

    # Imported here, not at module scope: rpfarm.smoke reuses this module's
    # formatting helpers, so a top-level import either way round would be
    # circular. By the time build_parser() runs, this module is fully loaded.
    from . import smoke as rpsmoke

    rpsmoke.build_smoke_parser(sub)

    return p


_HOUDINI_HANDLERS = {"install": cmd_houdini_install, "ls": cmd_houdini_ls, "rm": cmd_houdini_rm}
_STORAGE_HANDLERS = {
    "ls": cmd_storage_ls,
    "du": cmd_storage_du,
    "rm": cmd_storage_rm,
    "prune": cmd_storage_prune,
    "grow": cmd_storage_grow,
    "recreate": cmd_storage_recreate,
}
_FARM_HANDLERS = {"status": cmd_farm_status, "kill": cmd_farm_kill}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "setup":
            return cmd_setup(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "houdini":
            return _HOUDINI_HANDLERS[args.houdini_command](args)
        if args.command == "storage":
            return _STORAGE_HANDLERS[args.storage_command](args)
        if args.command == "farm":
            return _FARM_HANDLERS[args.farm_command](args)
        if args.command == "costs":
            return cmd_costs(args)
        if args.command == "smoke":
            from . import smoke as rpsmoke

            return rpsmoke.cmd_smoke(args)
    except rpcfg.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RunPodError as e:
        print(f"error: RunPod API: {e}", file=sys.stderr)
        return 1

    print(f"unknown command: {args.command}", file=sys.stderr)  # pragma: no cover - argparse enforces choices
    return 2
