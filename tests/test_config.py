import io
import os
import stat
import zipfile

import pytest

from rpfarm import config


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    cfg = config.Config(api_key="k", user="may", volume_id="v", template_id="t", gpu_priority=["NVIDIA GeForce RTX 4090"])
    config.save(cfg)
    assert stat.S_IMODE(os.stat(tmp_path / "config.toml").st_mode) == 0o600
    back = config.load()
    assert back.api_key == "k" and back.gpu_priority == ["NVIDIA GeForce RTX 4090"] and back.datacenter == "EU-RO-1"


def test_load_missing_file_raises_config_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    with pytest.raises(config.ConfigError):
        config.load()


def test_save_preserves_non_default_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    cfg = config.Config(
        api_key="k",
        user="may",
        volume_id="v",
        template_id="t",
        gpu_priority=["NVIDIA GeForce RTX 4090", "NVIDIA A40"],
        datacenter="US-KS-2",
        sesinetd_port=1716,
        sync_idle_min=30,
    )
    config.save(cfg)
    back = config.load()
    assert back.datacenter == "US-KS-2"
    assert back.sesinetd_port == 1716
    assert back.sync_idle_min == 30
    assert back.gpu_priority == ["NVIDIA GeForce RTX 4090", "NVIDIA A40"]


# -- session_token -------------------------------------------------------------


def test_session_token_roundtrip_and_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    tok1 = config.session_token()
    assert len(tok1) == 32
    assert all(c in "0123456789abcdef" for c in tok1)
    assert stat.S_IMODE(os.stat(tmp_path / "token").st_mode) == 0o600
    tok2 = config.session_token()
    assert tok1 == tok2  # created once, then reused


# -- rclone_bin: platform -> asset mapping (no network) -----------------------


def test_platform_asset_mapping(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    assert config._platform_asset() == "osx-arm64"

    monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")
    assert config._platform_asset() == "osx-amd64"

    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")
    assert config._platform_asset() == "linux-amd64"

    monkeypatch.setattr(config.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config.platform, "machine", lambda: "AMD64")
    assert config._platform_asset() == "windows-amd64"


def test_platform_asset_unsupported_raises(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Plan9")
    monkeypatch.setattr(config.platform, "machine", lambda: "mips")
    with pytest.raises(config.ConfigError):
        config._platform_asset()


def test_rclone_url_uses_asset(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(config.platform, "machine", lambda: "arm64")
    assert config._rclone_url() == "https://downloads.rclone.org/rclone-current-osx-arm64.zip"


# -- rclone_bin: extract logic with a synthetic zip (no network) -------------


def _make_zip(member_path, content=b"#!/bin/sh\necho fake\n"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_path, content)
        zf.writestr(member_path.rsplit("/", 1)[0] + "/README.txt", b"docs")
    return buf.getvalue()


def test_rclone_bin_downloads_and_extracts(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    calls = []

    def fake_downloader(url):
        calls.append(url)
        return _make_zip("rclone-v1.66.0-osx-arm64/rclone")

    path = config.rclone_bin(downloader=fake_downloader)
    assert os.path.exists(path)
    assert open(path, "rb").read().startswith(b"#!/bin/sh")
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o111  # executable bit set
    assert len(calls) == 1


def test_rclone_bin_is_cached_after_first_download(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    calls = []

    def fake_downloader(url):
        calls.append(url)
        return _make_zip("rclone-v1.66.0-osx-arm64/rclone")

    path1 = config.rclone_bin(downloader=fake_downloader)
    path2 = config.rclone_bin(downloader=fake_downloader)
    assert path1 == path2
    assert len(calls) == 1  # second call must not re-download


def test_rclone_bin_missing_member_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))

    def fake_downloader(url):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("rclone-v1.66.0-osx-arm64/README.txt", b"docs")
        return buf.getvalue()

    with pytest.raises(config.ConfigError):
        config.rclone_bin(downloader=fake_downloader)


# -- cached reads and the parm-default accessors (Task 17) -------------------


def _write(tmp_path, **kw):
    fields = dict(api_key="rpa_ABCDEFGHIJKL7f3c", user="may", volume_id="vol-1",
                  template_id="tpl-1", datacenter="US-KS-2")
    fields.update(kw)
    config.save(config.Config(**fields))


def test_load_cached_reads_the_file_once_and_notices_a_rewrite(tmp_path, monkeypatch):
    """A parm default expression re-evaluates on every UI refresh. Reading
    config.toml per field per refresh would hammer the disk, but a cache
    that never expires would show a stale value after `rpfarm setup`."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write(tmp_path, volume_id="vol-first")

    reads = []
    real_load = config.load
    monkeypatch.setattr(config, "load", lambda: (reads.append(1), real_load())[1])

    assert config.load_cached().volume_id == "vol-first"
    for _ in range(20):
        config.load_cached()
    assert len(reads) == 1, "20 refreshes, one parse"

    _write(tmp_path, volume_id="vol-second")
    assert config.load_cached().volume_id == "vol-second"


def test_load_cached_is_none_before_setup_has_ever_run(tmp_path, monkeypatch):
    """Never raise in the UI: a node with no config yet shows empty fields,
    it does not throw while drawing its own parameters."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "nothing-here"))
    assert config.load_cached() is None
    assert config.config_value("volume_id") == ""
    assert config.config_value("volume_id", "fallback") == "fallback"


def test_load_cached_survives_a_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("this is not = = toml\n")
    assert config.load_cached() is None
    assert config.config_value("template_id") == ""


def test_config_value_renders_the_list_field_the_way_its_parm_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write(tmp_path, gpu_priority=["NVIDIA RTX A4500", "NVIDIA GeForce RTX 4090"])
    assert config.config_value("gpu_priority") == "NVIDIA RTX A4500, NVIDIA GeForce RTX 4090"


def test_config_value_of_an_unset_or_unknown_field_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    _write(tmp_path)
    assert config.config_value("gpu_priority") == ""       # set, but empty
    assert config.config_value("no_such_field") == ""
    assert config.config_value("measured_mbps") == ""      # None until doctor runs


def test_a_changed_rpfarm_home_is_never_served_the_previous_ones_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "a"))
    _write(tmp_path, volume_id="vol-a")
    assert config.config_value("volume_id") == "vol-a"
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path / "b"))
    _write(tmp_path, volume_id="vol-b")
    assert config.config_value("volume_id") == "vol-b"


def test_mask_secret_shows_enough_to_recognise_and_not_enough_to_use():
    assert config.mask_secret("rpa_ABCDEFGHIJKL7f3c") == "rpa_...7f3c"
    assert config.mask_secret("short") == "(set)"
    assert config.mask_secret("") == ""
    assert config.mask_secret(None) == ""


def test_api_key_status_says_where_the_key_comes_from():
    assert config.api_key_status("rpa_ABCDEFGHIJKL7f3c") == "from config (rpa_...7f3c)"
    assert "NOT CONFIGURED" in config.api_key_status("")
    assert "rpfarm setup" in config.api_key_status(None)


def test_api_key_status_warns_that_a_typed_key_goes_into_the_hip():
    """The whole reason the field is not substituted: a value in a parm is
    saved into the scene, which travels to the farm and into backups."""
    text = config.api_key_status("rpa_CONFIGUREDKEY1", "  rpa_TYPEDBYHAND9999  ")
    assert "set on this node (rpa_...9999)" in text
    assert "cleartext" in text and ".hip" in text
    assert "rpa_TYPEDBYHAND9999" not in text, "never the key itself"
