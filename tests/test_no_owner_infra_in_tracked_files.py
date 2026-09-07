"""One guard against the class Ruling R49 named: an address, machine or
account identifier that belongs to whoever runs this farm, checked into a
PUBLIC repository.

The rule is deliberately narrow. A guard that cries wolf is worse than no
guard -- it gets muted, and then it catches nothing. So each pattern below
has to be something with no legitimate use in tracked files at all, and
each carries the sentence a person needs in order to fix it. Real values
live in ``~/.rpfarm/config.toml`` and in the hub's
``departments/infra/map.md``; documentation uses placeholders.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SELF = Path(__file__).name

# Files that are allowed to contain anything: this test (it spells the
# patterns out) and anything not actually text.
SKIP_NAMES = {SELF}


def _tracked_text_files():
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-z"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:  # pragma: no cover
        pytest.skip(f"git not usable here ({e})")
    if out.returncode != 0:  # pragma: no cover - a tarball, not a checkout
        pytest.skip("not a git checkout")
    for rel in out.stdout.split("\0"):
        if not rel or Path(rel).name in SKIP_NAMES:
            continue
        path = REPO / rel
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue  # binary, or gone
        yield rel, text


# -- the patterns -----------------------------------------------------------
#
# Each is (name, compiled regex, what to do about it). The regex runs per
# line, so the report can name the line.

# The owner's own domain. Nothing in this repository has a reason to name
# it: the license server, the relay and every service behind it are the
# deployer's, not the project's.
OWNER_DOMAIN = re.compile(r"\bexample\.invalid\b")

# Personal machines by name. Meaningless to anyone else who clones this,
# and in tests they read as noise rather than as an example.
PERSONAL_HOSTS = re.compile(r"\b(?:workstation01|hypervisor01|oracl)\b")

# The owner's home directory. Examples use /Users/artist.
PERSONAL_HOME = re.compile(r"(?:/Users/artist|/home/may)(?:/|\b)")

# A public IPv4 given as a LOGIN TARGET -- "user@1.2.3.4" on a line that
# also runs ssh/scp/rsync/sftp. That is a machine plus the command to get
# into it. The "@" is what keeps this narrow: an IP as a flag value
# (``--sftp-host=1.2.3.4``, which tests are full of) is not a way in and
# does not match. Private ranges, loopback and link-local describe a LAN
# rather than an entrance, so they are fine.
SSH_COMMAND = re.compile(r"\b(?:ssh|scp|rsync|sftp)\b")
LOGIN_AT_IP = re.compile(r"[\w.\-]+@(?P<ip>(?:\d{1,3}\.){3}\d{1,3})\b")
PRIVATE_IP = re.compile(
    r"^(?:10\.|127\.|169\.254\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|0\.)"
)

# A RunPod resource id written out as a literal. Real ones are exactly ten
# lowercase alphanumerics mixing letters and digits ("2ze7qdwkt3"); the
# stubs tests use ("vol123", "tpl123", "oldvol") are shorter and never
# match, and neither do placeholders like "<volume id>".
RUNPOD_ID_FIELD = re.compile(
    # The field name may itself be quoted -- it is a JSON key as often as
    # it is a Python keyword argument.
    r"""(networkVolumeId|templateId|volume_id|template_id)["']?\s*[:=]\s*["']([^"']+)["']"""
)
RUNPOD_ID_SHAPE = re.compile(r"^(?=[a-z0-9]{10}$)(?=.*[a-z])(?=.*\d)")


def _findings():
    for rel, text in _tracked_text_files():
        for n, line in enumerate(text.splitlines(), 1):
            where = f"{rel}:{n}"
            if OWNER_DOMAIN.search(line):
                yield (where, line.strip(),
                       "the deployer's own domain -- use example.com, and keep the "
                       "real name in the hub's departments/infra/map.md")
            if PERSONAL_HOSTS.search(line):
                yield (where, line.strip(),
                       "a personal machine by name -- use a neutral stub "
                       "(buildhost, and so on)")
            if PERSONAL_HOME.search(line):
                yield (where, line.strip(),
                       "somebody's home directory -- examples use /Users/artist")
            m = LOGIN_AT_IP.search(line) if SSH_COMMAND.search(line) else None
            if m and not PRIVATE_IP.match(m.group("ip")):
                yield (where, line.strip(),
                       "a login command against a public IP -- name the host in the "
                       "hub's infra map instead, not here")
            for field, value in RUNPOD_ID_FIELD.findall(line):
                if RUNPOD_ID_SHAPE.match(value):
                    yield (where, line.strip(),
                           f"what looks like a real RunPod id in {field} -- read it "
                           f"from ~/.rpfarm/config.toml, or write a placeholder")


def test_no_owner_infrastructure_in_tracked_files():
    """Ruling R49. The license server's address sat in this public repo as a
    default value for months; the fix that lasts is the one that fails a
    test rather than the one that depends on remembering."""
    found = list(_findings())
    assert not found, "owner infrastructure in tracked files:\n" + "\n".join(
        f"  {where}: {why}\n      {line}" for where, line, why in found
    )


# -- the guard's own teeth --------------------------------------------------


@pytest.mark.parametrize("line,expected", [
    ('sesinetd_host: str = "lic.example.invalid"', True),
    ('sesinetd_host: str = "lic.example.com"', False),
    ("- ssh opc@89.168.89.3: kill the socat duplicates", True),
    ("ssh -o StrictHostKeyChecking=no root@10.0.0.10", False),
    ("scp builder@buildhost:/srv/houdini/x.tar.gz .", False),
    ('"networkVolumeId": "2ze7qdwkt3",', True),
    ('"networkVolumeId": "<volume id>",', False),
    ('volume_id="vol123", template_id="tpl123"', False),
    ("tar is on workstation01 under /home/may/Downloads", True),
    ("OCIO=/Users/artist/color/config.ocio", False),
])
def test_the_guard_catches_what_it_claims_to(line, expected, tmp_path, monkeypatch):
    # Patch the module object itself: pytest may import this file as
    # "tests.<name>" or as "<name>", depending on rootdir, and a string
    # target that does not resolve silently patches nothing.
    monkeypatch.setattr(sys.modules[__name__], "_tracked_text_files",
                        lambda: [("probe.txt", line)])
    assert bool(list(_findings())) is expected
