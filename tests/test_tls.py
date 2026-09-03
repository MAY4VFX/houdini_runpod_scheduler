import ssl

import pytest

from rpfarm import tls


def test_env_override_wins(tmp_path):
    bundle = tmp_path / "custom.pem"
    bundle.write_text("")
    other = tmp_path / "certifi.pem"
    other.write_text("")
    found = tls.find_ca_bundle(
        env={"RPFARM_CA_BUNDLE": str(bundle), "SSL_CERT_FILE": str(other)},
        candidates=(str(other),),
        certifi_where=lambda: str(other),
    )
    assert found == str(bundle)


def test_ssl_cert_file_is_next(tmp_path):
    bundle = tmp_path / "ssl_cert_file.pem"
    bundle.write_text("")
    found = tls.find_ca_bundle(
        env={"SSL_CERT_FILE": str(bundle)}, candidates=(), certifi_where=lambda: None
    )
    assert found == str(bundle)


def test_missing_env_paths_are_skipped(tmp_path):
    candidate = tmp_path / "system.pem"
    candidate.write_text("")
    found = tls.find_ca_bundle(
        env={"RPFARM_CA_BUNDLE": str(tmp_path / "nope.pem")},
        candidates=(str(candidate),),
        certifi_where=lambda: None,
    )
    assert found == str(candidate)


def test_certifi_before_system_candidates(tmp_path):
    certifi_bundle = tmp_path / "certifi.pem"
    certifi_bundle.write_text("")
    candidate = tmp_path / "system.pem"
    candidate.write_text("")
    found = tls.find_ca_bundle(
        env={}, candidates=(str(candidate),), certifi_where=lambda: str(certifi_bundle)
    )
    assert found == str(certifi_bundle)


def test_broken_certifi_is_not_fatal(tmp_path):
    candidate = tmp_path / "system.pem"
    candidate.write_text("")

    def boom():
        raise ImportError("no certifi here")

    assert tls.find_ca_bundle(env={}, candidates=(str(candidate),), certifi_where=boom) == str(
        candidate
    )


def test_nothing_found_is_none():
    assert tls.find_ca_bundle(env={}, candidates=(), certifi_where=lambda: None) is None


def test_context_verifies_and_has_cas():
    """The regression that broke the first live smoke.

    Under Houdini's hython, ssl.create_default_context() points at the SideFX
    build machine's cert path and loads zero CAs, so every https call fails
    with CERTIFICATE_VERIFY_FAILED. ssl_context() must always come back with a
    verifying context that actually has certificates in it.
    """
    ctx = tls.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    if tls.find_ca_bundle() is None and not ssl.create_default_context().get_ca_certs():
        pytest.skip("no CA bundle anywhere on this machine")
    assert ctx.get_ca_certs(), "ssl_context() loaded no CA certificates"


def test_context_is_cached():
    assert tls.ssl_context() is tls.ssl_context()
