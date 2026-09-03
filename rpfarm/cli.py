"""``rpfarm`` command-line interface: setup, doctor, houdini, storage, farm, costs.

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

    Accepts an ISO8601 string (``...Z`` or ``+00:00``) or a raw epoch
    number -- the REST API has been observed to return either depending on
    endpoint/version, and neither is worth failing ``farm status`` over.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
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
    home = rpcfg.home()
    home.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key
    if not api_key:
        if args.non_interactive:
            print("error: --non-interactive requires --api-key", file=sys.stderr)
            return 1
        api_key = prompt("RunPod API key: ").strip()
    if not api_key:
        print("error: no API key given", file=sys.stderr)
        return 1

    default_user = getpass.getuser()
    user = args.user
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
        volume_id = _pick_volume(api, args, user)
    except SystemExit as e:
        return e.code or 1

    datacenter = "EU-RO-1"
    try:
        vinfo = api.get_volume(volume_id)
        if vinfo:
            datacenter = vinfo.get("dataCenterId", datacenter)
    except RunPodError:
        pass

    cfg = rpcfg.Config(api_key=api_key, user=user, volume_id=volume_id, template_id="", datacenter=datacenter)
    cfg.template_id = _pick_template(api, args, cfg)

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


def _measure_uplink(cfg, pod, rclone_bin, size_mb=20, copy_fn=rpsync.rclone_copy):
    ip, port = pod_public_endpoint(pod, 22)
    target = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)
    remote_dir = "/workspace/.rpfarm"
    remote_name = "rpfarm_doctor_uplink.bin"
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, remote_name)
        with open(local, "wb") as f:
            f.write(os.urandom(size_mb * 2**20))
        entry = rpsync.FileEntry(local=local, remote=posixpath.join(remote_dir, remote_name), size=os.path.getsize(local))
        stats = copy_fn([entry], target, "up", rclone_bin, tmp, remote_dir)
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
            mbps = _measure_uplink(cfg, sync_pod, cfg.rclone_path)
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


def _stage_tar_from_sftp_url(url, rclone_bin, tmp_dir, run=subprocess.run):
    """Stage a tarball that lives on an external SFTP host (not the rpfarm
    sync pod) into a local temp file via rclone's on-the-fly ``:sftp,...:``
    remote syntax, so ``cmd_houdini_install`` only ever has to deal with a
    local path from here on. Auth is whatever rclone's sftp backend picks
    up by default (ssh-agent / the caller's own ``~/.ssh`` keys) -- the
    same as a manual ``scp`` from that host would use; this repo's own
    ``rpfarm`` SSH key is for the farm's pods, not arbitrary hosts.
    """
    m = re.match(r"^sftp://(?:([^@/]+)@)?([^/]+)(/.+)$", url)
    if not m:
        raise ValueError(f"invalid sftp url: {url!r} (expected sftp://[user@]host/path)")
    user, host, path = m.group(1) or "root", m.group(2), m.group(3)
    local = os.path.join(tmp_dir, os.path.basename(path))
    args = [rclone_bin, "copyto", f":sftp,host={host},user={user}:{path}", local]
    proc = run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rclone copyto from {url} failed: {proc.stderr}")
    return local


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
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"houdini rm {shlex.quote(args.version)}"
    if args.dry_run:
        cmd_str += " --dry-run"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=120)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    if not data.get("ok"):
        print(data.get("error", "failed"), file=sys.stderr)
        return 2
    verb = "would free" if args.dry_run else "freed"
    print(f"{verb} {_fmt_bytes(data.get('bytes_freed'))}: {data.get('path')}")
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
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"rm {shlex.quote(args.user_project)}"
    if args.force:
        cmd_str += " --force"
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
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)
    token = rpcfg.session_token()
    _pod, client = _connect_sync_pod(api, cfg, token)
    cmd_str = f"prune --older-days {args.older_days}"
    if args.dry_run:
        cmd_str += " --dry-run"
    exit_code, data, result = _housekeeping_exec(client, cmd_str, timeout_s=180)
    if exit_code != 0 or data is None:
        _report_housekeeping_failure(result)
        return 1
    candidates = data.get("candidates", [])
    rows = [(c["user"], c["project"], _fmt_bytes(c["bytes"]), f"{c['age_days']:.0f}d") for c in candidates]
    print(_fmt_table(rows, ["user", "project", "size", "age"]))
    verb = "deleted" if data.get("deleted") else "would delete"
    print(f"{verb} {len(candidates)} project(s)")
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
    cfg = rpcfg.load()
    api = _make_api(cfg.api_key)

    if args.pod:
        api.terminate_pod(args.pod)
        print(f"[OK] terminated {args.pod}")
        return 0

    if args.sync:
        name = rppods.sync_pod_name(cfg.user)
        pods = [p for p in api.list_pods(name) if p.get("name") == name]
    elif args.all:
        pods = api.list_pods("rpfarm-")
    else:
        print("error: pass --all, --pod <id>, or --sync", file=sys.stderr)
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

    p_setup = sub.add_parser("setup", help="set up ~/.rpfarm, find/create volume+template, install HDAs")
    p_setup.add_argument("--api-key")
    p_setup.add_argument("--user")
    p_setup.add_argument("--volume")
    p_setup.add_argument("--template")
    p_setup.add_argument("--non-interactive", action="store_true")

    sub.add_parser("doctor", help="check the whole setup end to end")

    p_houdini = sub.add_parser("houdini", help="manage Houdini installs on the farm volume")
    houdini_sub = p_houdini.add_subparsers(dest="houdini_command", required=True)
    p_hi = houdini_sub.add_parser("install")
    p_hi.add_argument("--tar", required=True, help="local path, or sftp://[user@]host/path")
    p_hi.add_argument("--version", default=None, help="defaults to config.toml's houdini_version")
    houdini_sub.add_parser("ls")
    p_hrm = houdini_sub.add_parser("rm")
    p_hrm.add_argument("version")
    p_hrm.add_argument("--dry-run", action="store_true")

    p_storage = sub.add_parser("storage", help="inspect/manage the farm volume")
    storage_sub = p_storage.add_subparsers(dest="storage_command", required=True)
    storage_sub.add_parser("ls")
    p_du = storage_sub.add_parser("du")
    p_du.add_argument("path")
    p_rm = storage_sub.add_parser("rm")
    p_rm.add_argument("user_project", metavar="USER/PROJECT")
    p_rm.add_argument("--force", action="store_true")
    p_prune = storage_sub.add_parser("prune")
    p_prune.add_argument("--older-days", type=float, default=30)
    p_prune.add_argument("--dry-run", action="store_true")
    p_grow = storage_sub.add_parser("grow")
    p_grow.add_argument("amount", metavar="+N", help="grow by N GB, e.g. +20")
    p_recreate = storage_sub.add_parser("recreate")
    p_recreate.add_argument("--size", type=int, required=True)
    p_recreate.add_argument("--tar", default=None)
    p_recreate.add_argument("--version", default=None)

    p_farm = sub.add_parser("farm", help="see/kill running rpfarm pods")
    farm_sub = p_farm.add_subparsers(dest="farm_command", required=True)
    farm_sub.add_parser("status")
    p_kill = farm_sub.add_parser("kill")
    p_kill.add_argument("--all", action="store_true")
    p_kill.add_argument("--pod")
    p_kill.add_argument("--sync", action="store_true")

    p_costs = sub.add_parser("costs", help="ledger + billing cost summary")
    p_costs.add_argument("--by", choices=["project", "user", "cook"], default="project")
    p_costs.add_argument("--since", metavar="YYYY-MM-DD")
    p_costs.add_argument("--until", metavar="YYYY-MM-DD")
    p_costs.add_argument("--billing", action="store_true", help="merge in RunPod's actual billed cost")

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
    except rpcfg.ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except RunPodError as e:
        print(f"error: RunPod API: {e}", file=sys.stderr)
        return 1

    print(f"unknown command: {args.command}", file=sys.stderr)  # pragma: no cover - argparse enforces choices
    return 2
