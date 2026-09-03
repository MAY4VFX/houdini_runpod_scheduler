"""A usable TLS context, including inside Houdini's bundled Python.

Houdini ships its own OpenSSL, and its compiled-in CA locations point at the
SideFX build machine::

    >>> ssl.get_default_verify_paths().openssl_cafile
    '/Users/prisms/builder-new/WeeklyDevToolsHEAD/dev_tools/local/ssl/cert.pem'

That path exists on no user's machine, so ``ssl.create_default_context()``
under ``hython`` comes back with **zero** CA certificates and every https
request -- the RunPod REST API, the GraphQL balance query, every worker call
through the RunPod proxy -- fails with::

    ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: unable to get local issuer certificate

This module finds a real CA bundle instead and builds a verifying context
from it. Verification is never disabled: the pods' HTTP proxy carries a
per-cook worker token, and RunPod's API carries the account's API key, so an
unverified connection would hand both to whoever answers.

Stdlib only, like the rest of ``rpfarm`` (``certifi`` is *used* when it
happens to be importable, but never required).
"""

from __future__ import annotations

import os
import ssl

# Where a system CA bundle lives, per platform. Checked in order, after the
# environment and certifi.
CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",  # macOS (and LibreSSL-based systems)
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew, Apple silicon
    "/usr/local/etc/ca-certificates/cert.pem",  # Homebrew, Intel
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL/Fedora
    "/etc/ssl/ca-bundle.pem",  # SUSE
)

# Environment variables that name a bundle, most specific first. SSL_CERT_FILE
# is OpenSSL's own override and is already honoured by a default context when
# the interpreter has a working OpenSSL -- it is repeated here because
# Houdini's does not.
CA_BUNDLE_ENV_VARS = ("RPFARM_CA_BUNDLE", "SSL_CERT_FILE")

_context = None


def _certifi_where():
    import certifi

    return certifi.where()


def find_ca_bundle(env=None, candidates=CA_BUNDLE_CANDIDATES, certifi_where=_certifi_where):
    """Path to a CA bundle file, or None if this machine has none.

    None does not mean "give up": it means the interpreter's own default
    context is the best available answer, which is the normal case for a
    system Python.
    """
    env = os.environ if env is None else env

    for var in CA_BUNDLE_ENV_VARS:
        path = env.get(var)
        if path and os.path.exists(path):
            return path

    try:
        path = certifi_where()
    except Exception:
        # certifi is optional; a missing or broken one is not an error here.
        path = None
    if path and os.path.exists(path):
        return path

    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def ssl_context():
    """A verifying :class:`ssl.SSLContext` with CA certificates in it.

    Built once and reused: loading a bundle costs a few milliseconds and the
    scheduler makes an https call every tick.
    """
    global _context
    if _context is not None:
        return _context

    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs():
        bundle = find_ca_bundle()
        if bundle:
            try:
                ctx.load_verify_locations(cafile=bundle)
            except (ssl.SSLError, OSError):
                # A bundle we cannot parse is no better than none; leave the
                # default context alone so the caller sees the real TLS error.
                pass
    _context = ctx
    return ctx
