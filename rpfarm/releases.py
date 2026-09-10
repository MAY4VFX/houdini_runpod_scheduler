"""Immutable, next-session installation of a matching Python/HDA release."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from . import fingerprint
from . import houdini_local as hl


def _atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as f:
        f.write(text)
        temporary = f.name
    os.replace(temporary, path)


def source_id(root):
    digest = hashlib.sha256()
    for folder in ('rpfarm', 'hda'):
        for path in sorted((root / folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.name != '.DS_Store':
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def stage(root, home, install):
    root, home = Path(root), Path(home)
    if (root / 'release.json').is_file() and (root / 'houdini' / 'otls').is_dir():
        return root  # reinstall an already immutable installed bundle
    package_fingerprint = fingerprint(str(root / 'rpfarm'))
    for name in hl.HDA_NAMES:
        state = hl.asset_state(hl.hda_source_dir(name, root), package_fingerprint)
        if state['stale']:
            raise RuntimeError('Rebuild the HDA bundle before installation: ' + name)
    identifier = source_id(root)
    releases = home / 'releases'
    releases.mkdir(parents=True, exist_ok=True)
    final = releases / identifier
    if final.exists():
        if not (final / 'release.json').is_file() or not all(
                (final / 'houdini' / 'otls' / (name + '.hda')).is_file() for name in hl.HDA_NAMES):
            raise RuntimeError('Existing release is incomplete: ' + str(final))
        return final
    temporary = Path(tempfile.mkdtemp(prefix='staging-', dir=releases))
    try:
        shutil.copytree(root / 'rpfarm', temporary / 'rpfarm', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        otls = temporary / 'houdini' / 'otls'
        otls.mkdir(parents=True)
        for name in hl.HDA_NAMES:
            hl.collapse_hda(install.hotl, hl.hda_source_dir(name, root), otls / (name + '.hda'))
        shape = temporary / 'houdini' / 'config' / 'NodeShapes' / 'rpfarm.json'
        shape.parent.mkdir(parents=True)
        shutil.copyfile(hl.node_shape_source(root), shape)
        shelf = temporary / 'houdini' / 'toolbar' / hl.SHELF_FILENAME
        shelf.parent.mkdir(parents=True)
        shelf.write_text(hl.shelf_tool_source())
        (temporary / 'release.json').write_text(json.dumps({'id': identifier, 'fingerprint': package_fingerprint}))
        os.replace(temporary, final)
    except Exception:
        # Only our uniquely-created staging directory, never a prior release.
        shutil.rmtree(temporary)
        raise
    return final


def activate(release, install):
    """Switch startup configuration, leaving loaded code and HDA files intact."""
    release = Path(release).resolve()
    package_file = install.user_pref_dir / 'packages' / 'runpodfarm-release.json'
    env_file = install.user_pref_dir / 'houdini.env'
    original = env_file.read_text() if env_file.exists() else ''
    previous = hl._existing_rpfarm_root(original)
    # Initial migration: install an equivalent fallback BEFORE removing the
    # overriding houdini.env line. Each intermediate startup remains valid.
    if previous and not package_file.exists():
        _atomic_text(package_file, json.dumps({'env': [{'RPFARM_ROOT': previous}]}))
    lines = original.splitlines()
    filtered = [line for line in lines if not re.match(r'^\s*RPFARM_ROOT\s*=', line)
                and line.strip() != hl._RPFARM_ROOT_MARKER]
    if filtered != lines:
        backup = env_file.with_name('houdini.env.before-rpfarm-release')
        if not backup.exists():
            _atomic_text(backup, original)
        _atomic_text(env_file, '\n'.join(filtered) + '\n')
    package = {'env': [
        {'RPFARM_ROOT': str(release)},
        {'HOUDINI_OTLSCAN_PATH': {'value': str(release / 'houdini' / 'otls'), 'method': 'prepend'}}],
        'path': str(release / 'houdini')}
    _atomic_text(package_file, json.dumps(package, indent=2))
    return package_file


def install(root, home, installs):
    installs = list(installs)
    if not installs:
        raise RuntimeError('No Houdini installation found')
    release = stage(Path(root), Path(home), installs[0])
    for houdini in installs:
        activate(release, houdini)
    _atomic_text(Path(home) / 'release.json', json.dumps({'root': str(release)}))
    return release
