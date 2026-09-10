import json
import types
from rpfarm import releases


def test_activation_changes_only_startup_config_not_the_loaded_package(tmp_path):
    prefs = tmp_path / 'prefs'
    prefs.mkdir()
    old = tmp_path / 'old-package'
    old.mkdir()
    (old / 'module.py').write_text('old code')
    env = prefs / 'houdini.env'
    env.write_text('CUSTOM_SETTING = keep\nRPFARM_ROOT = "{}"\n'.format(old))
    release = tmp_path / 'release'
    release.mkdir()
    install = types.SimpleNamespace(user_pref_dir=prefs)
    package = releases.activate(release, install)
    assert (old / 'module.py').read_text() == 'old code'
    assert 'CUSTOM_SETTING = keep' in env.read_text()
    assert 'RPFARM_ROOT =' not in env.read_text()
    data = json.loads(package.read_text())
    assert data['env'][0]['RPFARM_ROOT'] == str(release)
    assert (prefs / 'houdini.env.before-rpfarm-release').exists()
    releases.activate(release, install)
    assert json.loads(package.read_text()) == data


def test_doctor_reads_managed_bundle_instead_of_old_default_otls(tmp_path, monkeypatch):
    from rpfarm import houdini_local as hl
    prefs = tmp_path / 'prefs'
    release = tmp_path / 'release'
    (release / 'houdini' / 'otls').mkdir(parents=True)
    (release / 'rpfarm').mkdir()
    (release / 'rpfarm' / '__init__.py').write_text('VERSION = "test"')
    install = types.SimpleNamespace(user_pref_dir=prefs)
    releases.activate(release, install)
    seen = []
    monkeypatch.setattr(hl, 'asset_state', lambda path, fp: seen.append(path) or {'stale': False})
    hl.installed_asset_states(install, {})
    assert all(path.parent == release / 'houdini' / 'otls' for path in seen)
