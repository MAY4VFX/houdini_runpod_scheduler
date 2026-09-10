"""Resolve one farm configuration for schedulers, transfers and UI callbacks.

Transfer payloads carry only a reference to an owner-only LOCAL snapshot.
Credentials must never become PDG attributes/environment serialized to a farm.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import tempfile
import tomllib

from . import config

OVERRIDES = {
    'api_key': 'rpfarm_apikey', 'template_id': 'rpfarm_templateid',
    'volume_id': 'rpfarm_networkvolumeid', 'datacenter': 'rpfarm_datacenter',
    'cloud_type': 'rpfarm_cloudtype', 'capacity_wait_min': 'rpfarm_capacitywait',
    'sync_idle_min': 'rpfarm_syncidle', 'sync_delete_min': 'rpfarm_syncdelete',
}


def value(node, name, default=None):
    try:
        parm = node.parm(name) if node is not None else None
        return parm.eval() if parm is not None else default
    except (AttributeError, KeyError):
        return default


def scheduler_node(node):
    current = node
    while current is not None:
        try:
            kind = current.type().name()
        except AttributeError:
            return None
        if kind == 'runpodfarmscheduler':
            return current
        if kind in ('topnet', 'topnetmgr'):
            path = value(current, 'topscheduler', '')
            found = current.node(path) if path else None
            return found if found and found.type().name() == 'runpodfarmscheduler' else None
        current = current.parent()
    return None


def config_for_scheduler(node, cfg=None):
    cfg = copy.copy(cfg if cfg is not None else config.load())
    for field, parm in OVERRIDES.items():
        setting = value(node, parm)
        if setting is not None and setting != '':
            setattr(cfg, field, setting)
    return cfg


@dataclasses.dataclass
class FarmContext:
    cfg: config.Config
    project: str
    local_root: str

    @property
    def remote_root(self):
        return '/workspace/projects/{}/{}'.format(self.cfg.user, self.project)

    def description(self):
        return '{} / {} · {} · volume {}\n{} → {}'.format(
            self.cfg.user, self.project, self.cfg.datacenter, self.cfg.volume_id,
            self.local_root, self.remote_root)


def resolve(node, local_root=None, cfg=None):
    scheduler = scheduler_node(node)
    cfg = config_for_scheduler(scheduler, cfg)
    if local_root is None:
        try:
            import hou
            local_root = hou.getenv('JOB') or hou.getenv('HIP') or os.getcwd()
        except (ImportError, AttributeError):
            local_root = os.getcwd()
    inherited = value(scheduler, 'rpfarm_project', '') or os.path.basename(os.path.normpath(local_root))
    override = value(node, 'rpfarm_projectoverride', False)
    project = value(node, 'rpfarm_project', '') if override or scheduler is None else inherited
    return FarmContext(cfg, str(project or inherited or 'default'), os.path.normpath(local_root))


def _snapshot_root():
    # Never $RPFARM_HOME: on a host pod that is the package ON THE SHARED VOLUME.
    owner = str(os.getuid()) if hasattr(os, 'getuid') else hashlib.sha256(
        str(Path.home()).encode()).hexdigest()[:12]
    root = Path(tempfile.gettempdir()) / ('rpfarm-contexts-' + owner)
    if root.is_symlink():
        raise config.ConfigError('Transfer context directory must not be a symlink')
    root.mkdir(mode=0o700, exist_ok=True)
    if os.name != 'nt' and (root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077):
        raise config.ConfigError('Transfer context directory is not private to this user')
    return root


def snapshot(cfg):
    data = dataclasses.asdict(cfg)
    name = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() + '.toml'
    path = _snapshot_root() / name
    config.save_to(cfg, path)
    return str(path)


def load_snapshot(path):
    if not path:
        return config.load()  # compatibility with already generated work items
    candidate = Path(path)
    if candidate.is_symlink() or candidate.resolve().parent != _snapshot_root().resolve():
        raise config.ConfigError('Invalid local transfer context reference')
    if os.name != 'nt' and (candidate.stat().st_uid != os.getuid() or candidate.stat().st_mode & 0o077):
        raise config.ConfigError('Transfer context file is not private to this user')
    with candidate.open('rb') as f:
        return config.Config(**tomllib.load(f))
