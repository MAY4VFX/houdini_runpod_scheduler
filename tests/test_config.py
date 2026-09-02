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
