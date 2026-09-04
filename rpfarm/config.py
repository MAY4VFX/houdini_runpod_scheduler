"""``~/.rpfarm`` config store: config.toml, the rclone binary, the SSH key,
and the shared worker session token.

Stdlib only (runs inside Houdini's bundled Python and as a plain CLI).
``tomllib`` (stdlib, read-only) parses config.toml; since the stdlib has no
TOML writer, :func:`save` writes it by hand.

Every path helper reads the ``RPFARM_HOME`` env var at call time (not at
import time), so tests can point it at a tmp dir with ``monkeypatch.setenv``.
"""

from __future__ import annotations

import io
import json
import os
import platform
import secrets
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ImportError as exc:  # pragma: no cover - depends on the interpreter
    # A bare "No module named 'tomllib'" is what an artist saw when a work
    # item was launched with Xcode's python3.9 off a Dock-launched Houdini's
    # minimal PATH. The module name alone says nothing about which of the
    # several pythons on a Mac is running or what it should have been, so
    # say both.
    raise ImportError(
        "rpfarm needs Python 3.11 or newer (tomllib), but this is "
        "{}.{}.{} at {}. If this came from a PDG work item, the item's "
        "command was given the wrong interpreter -- see "
        "rpfarm.houdini_local.resolve_package_python, which is supposed to "
        "hand it the plain python bundled inside Houdini.".format(
            sys.version_info[0], sys.version_info[1], sys.version_info[2],
            sys.executable)
    ) from exc

from .runpod_api import DEFAULT_DATACENTER

CONFIG_FILENAME = "config.toml"
TOKEN_FILENAME = "token"


class ConfigError(Exception):
    pass


def home() -> Path:
    """``$RPFARM_HOME`` if set, else ``~/.rpfarm``."""
    override = os.environ.get("RPFARM_HOME")
    return Path(override) if override else Path.home() / ".rpfarm"


def _rclone_name() -> str:
    return "rclone.exe" if platform.system() == "Windows" else "rclone"


def _default_rclone_path() -> str:
    return str(home() / "bin" / _rclone_name())


def _default_ssh_key_path() -> str:
    return str(home() / "id_ed25519")


@dataclass
class Config:
    api_key: str
    user: str
    volume_id: str
    template_id: str
    datacenter: str = DEFAULT_DATACENTER
    houdini_version: str = "22.0.393"
    sesinetd_host: str = "lic.ai-vfx.com"
    sesinetd_port: int = 1715
    sync_idle_min: int = 15
    gpu_priority: list[str] = field(default_factory=list)
    rclone_path: str = field(default_factory=_default_rclone_path)
    ssh_key_path: str = field(default_factory=_default_ssh_key_path)
    # Last uplink measurement from `rpfarm doctor` (Mbps, against the sync
    # pod) -- threaded through here so the upload HDA's "auto" compression
    # mode (rpfarm.packages.resolve_compress_flag) has a real number to
    # compare against AUTO_COMPRESS_THRESHOLD_MBPS instead of always
    # falling back to "compress" for want of one. None until doctor has
    # run at least once with a sync pod up.
    measured_mbps: float | None = None


# -- config.toml ----------------------------------------------------------


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    # json.dumps' string escaping (\", \\, \n, ...) is valid TOML basic-string
    # escaping too — stdlib has no TOML writer, so this is the simplest
    # correct option without a third-party dependency.
    return json.dumps(str(v))


def save(cfg: Config) -> None:
    """Write ``cfg`` to ``$RPFARM_HOME/config.toml``, chmod 600, atomically.

    A field whose value is ``None`` (currently only ``measured_mbps``
    before ``rpfarm doctor`` has ever run with a sync pod up) is omitted
    entirely rather than written as some TOML stand-in for null — TOML has
    no null literal, and ``load()`` already falls back to the dataclass
    default for any key missing from the file.
    """
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    path = h / CONFIG_FILENAME

    lines = [
        f"{f.name} = {_toml_value(getattr(cfg, f.name))}"
        for f in fields(cfg)
        if getattr(cfg, f.name) is not None
    ]
    text = "\n".join(lines) + "\n"

    fd, tmp_path = tempfile.mkstemp(dir=str(h), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    os.chmod(path, 0o600)


def load() -> Config:
    """Read ``$RPFARM_HOME/config.toml``. Raises ConfigError if missing."""
    path = home() / CONFIG_FILENAME
    if not path.exists():
        raise ConfigError(f"no config at {path}; run `rpfarm setup` first")
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return Config(**kwargs)


# -- cached read, for UI code -----------------------------------------------
#
# Task 17: the HDAs' farm-identity parms (Template ID, Network Volume ID,
# Datacenter, GPU Priority, Project, Houdini Version) now DISPLAY what they
# will actually use, as a default expression that reads this config -- an
# empty field used to read as "not configured" while everything worked,
# because the code behind it is `parm or cfg`. A parm default expression is
# re-evaluated on every UI refresh, so it must not stat+parse config.toml
# each time, and it must never raise inside the parameter dialog.

_CACHE = {"path": None, "stamp": None, "config": None}


def load_cached() -> "Config | None":
    """:func:`load`, re-reading config.toml only when it has changed.

    Returns ``None`` instead of raising when there is no config yet (or it
    is unreadable): the caller is a UI expression, and a node that throws
    while drawing its own parameters is worse than a blank field.

    The cache key is the file's path plus (mtime_ns, size), so a rewritten
    config is picked up on the next evaluation, and ``$RPFARM_HOME``
    pointing somewhere else (as tests do) is never served a stale value.
    """
    path = home() / CONFIG_FILENAME
    try:
        st = os.stat(path)
    except OSError:
        _CACHE.update(path=str(path), stamp=None, config=None)
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    if _CACHE["path"] == str(path) and _CACHE["stamp"] == stamp:
        return _CACHE["config"]
    try:
        cfg = load()
    except (ConfigError, OSError, ValueError, TypeError):
        cfg = None
    _CACHE.update(path=str(path), stamp=stamp, config=cfg)
    return cfg


def config_value(name: str, default: str = "") -> str:
    """One config field as the string a parm should show.

    Never raises and never returns ``None``: an unset field, a missing
    config or an unparseable one all come back as ``default`` (empty), which
    is exactly what the parms meant before this existed. A list field
    (``gpu_priority``) comes back comma-separated -- the format its parm
    documents and :meth:`onStartCook` already splits on.
    """
    cfg = load_cached()
    if cfg is None:
        return default
    value = getattr(cfg, name, None)
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)):
        joined = ", ".join(str(v) for v in value if str(v))
        return joined or default
    return str(value)


def mask_secret(value: str | None) -> str:
    """``rpa_ABCD...7f3c`` -- enough to recognise a key, not enough to use."""
    if not value:
        return ""
    if len(value) <= 8:
        return "(set)"
    return "{}...{}".format(value[:4], value[-4:])


def api_key_status(config_key: str | None, node_key: str = "") -> str:
    """One line saying where the RunPod API key is coming from.

    The API key is the one identity field that must NOT be substituted into
    a parameter the way :func:`config_value` does for the others: a value in
    a parm is saved into the ``.hip``, and that file travels to the farm and
    into backups in cleartext. So the field stays empty and this line is
    shown beside it instead -- which answers the only question the artist
    actually has ("is a key configured, and is it the one I think it is")
    without putting the secret on screen.

    A key the artist typed in anyway still wins at cook time (that is the
    documented override), so this says so, and says what it costs.
    """
    node_key = (node_key or "").strip()
    if node_key:
        return (
            "set on this node ({}) -- WARNING: a key typed into a parameter is saved "
            "into the .hip in cleartext, and that file travels to the farm and into "
            "backups. Clear it and run `rpfarm setup` to keep the key in "
            "~/.rpfarm/config.toml (chmod 600) instead.".format(mask_secret(node_key))
        )
    if config_key:
        return "from config ({})".format(mask_secret(config_key))
    return "NOT CONFIGURED -- run `rpfarm setup`"


# -- session token ----------------------------------------------------------


def session_token() -> str:
    """Read (or create) the shared per-user worker token at
    ``$RPFARM_HOME/token``: 32 hex chars, chmod 600."""
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    path = h / TOKEN_FILENAME
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_hex(16)
    path.write_text(token)
    os.chmod(path, 0o600)
    return token


# -- rclone binary ----------------------------------------------------------


_ASSET_MAP = {
    ("Darwin", "arm64"): "osx-arm64",
    ("Darwin", "x86_64"): "osx-amd64",
    ("Linux", "x86_64"): "linux-amd64",
    ("Linux", "amd64"): "linux-amd64",
    ("Windows", "AMD64"): "windows-amd64",
    ("Windows", "x86_64"): "windows-amd64",
}


def _platform_asset() -> str:
    key = (platform.system(), platform.machine())
    if key not in _ASSET_MAP:
        raise ConfigError(f"unsupported platform for rclone: {key[0]}/{key[1]}")
    return _ASSET_MAP[key]


def _rclone_url() -> str:
    return f"https://downloads.rclone.org/rclone-current-{_platform_asset()}.zip"


def _default_downloader(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def _find_rclone_member(names: list[str]) -> str:
    target = _rclone_name()
    for n in names:
        if n.rstrip("/").rsplit("/", 1)[-1] == target:
            return n
    raise ConfigError(f"'{target}' not found in rclone archive (contents: {names})")


def rclone_bin(downloader=_default_downloader) -> str:
    """Return the path to a working ``rclone`` binary under
    ``$RPFARM_HOME/bin``, downloading and extracting the static build for
    this platform if it isn't there yet.

    ``downloader(url) -> bytes`` is injectable so tests never hit the
    network; it defaults to a plain ``urllib`` GET.
    """
    h = home()
    bin_dir = h / "bin"
    dest = bin_dir / _rclone_name()
    if dest.exists():
        return str(dest)

    bin_dir.mkdir(parents=True, exist_ok=True)
    data = downloader(_rclone_url())
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = _find_rclone_member(zf.namelist())
        with zf.open(member) as src, open(dest, "wb") as out:
            out.write(src.read())
    os.chmod(dest, 0o755)
    return str(dest)
