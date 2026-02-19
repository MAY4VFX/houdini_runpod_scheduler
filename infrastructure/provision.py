#!/usr/bin/env python3
"""
RunPodFarm Infrastructure Provisioner

Automates the entire RunPodFarm setup. The user provides credentials,
and this script provisions everything else.

Usage:
    python3 infrastructure/provision.py

Steps:
  1. Install JuiceFS CLI (if not installed)
  2. Format JuiceFS volume (Redis + B2)
  3. Create RunPod Network Volume via API
  4. Upload Houdini to Network Volume (via a temporary pod)
  5. Create RunPod Template (Docker image)
  6. Deploy Auth API to Cloudflare Workers
  7. Deploy Dashboard to Cloudflare Pages
  8. Create first admin account and project in Auth API
  9. Generate .env file with all configuration
 10. Test connectivity end-to-end

Requirements: Python 3.10+, no pip dependencies (stdlib only).
External tools used via subprocess: curl, openssl, npm/npx, juicefs, docker.
"""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
STATE_FILE = SCRIPT_DIR / ".provision-state.json"
ENV_FILE = PROJECT_ROOT / ".env"
RSA_KEY_FILE = SCRIPT_DIR / ".juicefs-rsa.pem"
RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"
DOCKER_IMAGE = "runpodfarm/worker:latest"

# ---------------------------------------------------------------------------
# ANSI colour helpers (disabled when not a tty)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def green(t: str) -> str:
    return _c("32", t)


def red(t: str) -> str:
    return _c("31", t)


def yellow(t: str) -> str:
    return _c("33", t)


def cyan(t: str) -> str:
    return _c("36", t)


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def banner(text: str) -> None:
    width = 60
    print()
    print(bold("=" * width))
    print(bold(f"  {text}"))
    print(bold("=" * width))


def info(msg: str) -> None:
    print(f"  {cyan('[*]')} {msg}")


def ok(msg: str) -> None:
    print(f"  {green('[OK]')} {msg}")


def warn(msg: str) -> None:
    print(f"  {yellow('[!]')} {msg}")


def fail(msg: str) -> None:
    print(f"  {red('[FAIL]')} {msg}")


def step_done(msg: str) -> None:
    print(f"\n  {green('>>>')} {bold(msg)}")


def run(
    cmd: list[str] | str,
    *,
    check: bool = True,
    capture: bool = False,
    shell: bool = False,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Thin wrapper around subprocess.run with sensible defaults."""
    merged_env = {**os.environ, **(env or {})}
    kwargs: dict[str, Any] = dict(
        check=check,
        text=True,
        cwd=cwd,
        env=merged_env,
    )
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if shell:
        kwargs["shell"] = True
    if input is not None:
        kwargs["input"] = input
    return subprocess.run(cmd, **kwargs)


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: dict | None = None,
    timeout: int = 30,
) -> dict:
    """Minimal HTTP JSON client using only urllib (stdlib)."""
    body = None
    if data is not None:
        body = json.dumps(data).encode()

    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            err_body = json.loads(raw)
        except Exception:
            err_body = {"raw": raw}
        raise RuntimeError(
            f"HTTP {exc.code} from {method} {url}: {json.dumps(err_body, indent=2)}"
        ) from exc


def runpod_gql(api_key: str, query: str, variables: dict | None = None) -> dict:
    """Execute a RunPod GraphQL query."""
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = http_request(
        RUNPOD_GRAPHQL,
        method="POST",
        headers={"Authorization": f"Bearer {api_key}"},
        data=payload,
    )
    if "errors" in resp:
        raise RuntimeError(
            f"RunPod GraphQL errors: {json.dumps(resp['errors'], indent=2)}"
        )
    return resp.get("data", resp)


# ---------------------------------------------------------------------------
# State persistence (resume capability)
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def mark_done(state: dict, step_key: str, result: Any = True) -> None:
    state[step_key] = result
    save_state(state)


# ---------------------------------------------------------------------------
# .env file helpers
# ---------------------------------------------------------------------------


def load_env_file() -> dict[str, str]:
    """Parse an existing .env file into a dict. Ignores comments and blanks."""
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        # Strip surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        values[key] = val
    return values


def save_env_file(config: dict[str, str]) -> None:
    """Write config dict as a .env file, grouped by section."""
    sections = {
        "RunPod": [
            "RUNPOD_API_KEY",
            "RUNPOD_TEMPLATE_ID",
            "RUNPOD_NETWORK_VOLUME_ID",
            "RUNPOD_DATACENTER",
            "GPU_TYPE",
        ],
        "Redis (Upstash)": [
            "REDIS_URL",
        ],
        "Backblaze B2": [
            "B2_ENDPOINT",
            "B2_KEY_ID",
            "B2_APP_KEY",
            "B2_BUCKET",
        ],
        "JuiceFS": [
            "JUICEFS_VOLUME_NAME",
        ],
        "Houdini Licensing": [
            "SIDEFX_CLIENT_ID",
            "SIDEFX_CLIENT_SECRET",
        ],
        "Auth API": [
            "AUTH_API_URL",
            "ADMIN_EMAIL",
            "ADMIN_PASSWORD",
        ],
        "Project": [
            "PROJECT_NAME",
            "PROJECT_ID",
            "ARTIST_API_KEY",
        ],
        "Docker": [
            "DOCKER_IMAGE",
        ],
    }

    lines: list[str] = [
        "# RunPodFarm Configuration",
        "# Generated by infrastructure/provision.py",
        f"# Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    written_keys: set[str] = set()
    for section_name, keys in sections.items():
        lines.append(f"# === {section_name} ===")
        for key in keys:
            val = config.get(key, "")
            lines.append(f"{key}={val}")
            written_keys.add(key)
        lines.append("")

    # Write any remaining keys not covered by sections
    remaining = {k: v for k, v in config.items() if k not in written_keys}
    if remaining:
        lines.append("# === Other ===")
        for k, v in sorted(remaining.items()):
            lines.append(f"{k}={v}")
        lines.append("")

    ENV_FILE.write_text("\n".join(lines) + "\n")
    ok(f".env written to {ENV_FILE}")


# ---------------------------------------------------------------------------
# Credential collection
# ---------------------------------------------------------------------------

CREDENTIAL_FIELDS = [
    # (key, prompt, required, default, validator_fn, is_secret)
    ("RUNPOD_API_KEY", "RunPod API Key", True, "", None, True),
    ("REDIS_URL", "Upstash Redis URL (rediss://...)", True, "", lambda v: v.startswith("rediss://"), False),
    ("B2_KEY_ID", "Backblaze B2 Key ID", False, "", None, False),
    ("B2_APP_KEY", "Backblaze B2 Application Key", False, "", None, True),
    ("B2_BUCKET", "Backblaze B2 Bucket Name", False, "", None, False),
    ("B2_ENDPOINT", "B2 S3 Endpoint URL (e.g. https://s3.eu-central-003.backblazeb2.com)", False, "", lambda v: v.startswith("https://"), False),
    ("SIDEFX_CLIENT_ID", "SideFX API Client ID (online licensing)", False, "", None, False),
    ("SIDEFX_CLIENT_SECRET", "SideFX API Client Secret", False, "", None, True),
    ("ADMIN_EMAIL", "Admin Email (for Auth API)", True, "", lambda v: "@" in v, False),
    ("ADMIN_PASSWORD", "Admin Password (min 8 chars)", True, "", lambda v: len(v) >= 8, True),
    ("PROJECT_NAME", "Project Name", False, "default", None, False),
    ("RUNPOD_DATACENTER", "RunPod Datacenter ID", False, "EU-RO-1", None, False),
    ("GPU_TYPE", "GPU Type", False, "NVIDIA GeForce RTX 4090", None, False),
]


def collect_credentials() -> dict[str, str]:
    """Interactively collect credentials, pre-filling from .env if available."""
    existing = load_env_file()
    config = dict(existing)  # start with existing values

    banner("Credential Collection")
    print()
    info("Enter your credentials below. Press Enter to keep existing values.")
    info("Leave optional fields blank to skip the related step.\n")

    for key, prompt, required, default, validator, is_secret in CREDENTIAL_FIELDS:
        current = config.get(key, "") or default

        # Build the prompt string
        label = f"  {bold(prompt)}"
        if current and not is_secret:
            label += f" [{dim(current)}]"
        elif current and is_secret:
            masked = current[:4] + "*" * (len(current) - 4) if len(current) > 4 else "****"
            label += f" [{dim(masked)}]"
        if not required:
            label += f" {dim('(optional)')}"
        label += ": "

        while True:
            if is_secret:
                value = getpass.getpass(label)
            else:
                value = input(label)

            value = value.strip()

            # Use current/default if empty
            if not value:
                value = current

            # Validate
            if required and not value:
                warn("This field is required.")
                continue

            if value and validator and not validator(value):
                if key == "REDIS_URL":
                    warn("Must start with rediss://")
                elif key == "B2_ENDPOINT":
                    warn("Must start with https://")
                elif key == "ADMIN_EMAIL":
                    warn("Must be a valid email address.")
                elif key == "ADMIN_PASSWORD":
                    warn("Must be at least 8 characters.")
                else:
                    warn("Validation failed.")
                continue

            break

        if value:
            config[key] = value

    # Save immediately so values persist
    save_env_file(config)
    ok("Credentials saved to .env")
    return config


# ---------------------------------------------------------------------------
# Step 1: Install JuiceFS CLI
# ---------------------------------------------------------------------------


def install_juicefs(config: dict[str, str], state: dict) -> None:
    if state.get("juicefs_installed"):
        ok("JuiceFS already marked as installed (previous run)")
        return

    if shutil.which("juicefs"):
        ok("JuiceFS CLI already installed")
        mark_done(state, "juicefs_installed")
        return

    info("Downloading JuiceFS CLI...")
    system = platform.system().lower()

    if system == "darwin":
        # Try Homebrew first
        if shutil.which("brew"):
            info("Installing via Homebrew...")
            run(["brew", "install", "juicefs"])
        else:
            info("Installing via official script...")
            run(["bash", "-c", "curl -sSL https://d.juicefs.com/install | sh -"])
    elif system == "linux":
        run(["bash", "-c", "curl -sSL https://d.juicefs.com/install | sh -"])
    else:
        raise RuntimeError(f"Unsupported platform: {system}. Install JuiceFS manually.")

    if not shutil.which("juicefs"):
        raise RuntimeError(
            "JuiceFS installation completed but binary not found in PATH. "
            "You may need to restart your shell or add it to PATH."
        )

    ok("JuiceFS CLI installed")
    mark_done(state, "juicefs_installed")


# ---------------------------------------------------------------------------
# Step 2: Format JuiceFS volume
# ---------------------------------------------------------------------------


def format_juicefs(config: dict[str, str], state: dict) -> None:
    if state.get("juicefs_formatted"):
        ok("JuiceFS volume already formatted (previous run)")
        return

    redis_url = config.get("REDIS_URL", "")
    b2_endpoint = config.get("B2_ENDPOINT", "")
    b2_key = config.get("B2_KEY_ID", "")
    b2_secret = config.get("B2_APP_KEY", "")
    b2_bucket = config.get("B2_BUCKET", "")

    if not all([redis_url, b2_endpoint, b2_key, b2_secret, b2_bucket]):
        warn("Missing B2 or Redis credentials -- skipping JuiceFS format.")
        warn("You can run this step later after providing all credentials.")
        return

    volume_name = config.get("JUICEFS_VOLUME_NAME", "runpodfarm")
    config["JUICEFS_VOLUME_NAME"] = volume_name

    # Generate RSA key for encryption if not exists
    if not RSA_KEY_FILE.exists():
        info("Generating RSA key for JuiceFS encryption...")
        run(["openssl", "genrsa", "-out", str(RSA_KEY_FILE), "2048"])
        os.chmod(RSA_KEY_FILE, 0o600)
        ok(f"RSA key saved to {RSA_KEY_FILE}")
    else:
        ok(f"RSA key already exists at {RSA_KEY_FILE}")

    info(f"Formatting JuiceFS volume: {volume_name}")
    info(f"  Redis: {redis_url.split('@')[0]}@***")
    info(f"  S3: {b2_endpoint}/{b2_bucket}")

    bucket_url = f"{b2_endpoint}/{b2_bucket}"

    run(
        [
            "juicefs",
            "format",
            "--storage", "s3",
            "--bucket", bucket_url,
            "--access-key", b2_key,
            "--secret-key", b2_secret,
            "--encrypt-rsa-key", str(RSA_KEY_FILE),
            "--compress", "lz4",
            "--trash-days", "7",
            redis_url,
            volume_name,
        ]
    )

    ok("JuiceFS volume formatted successfully")
    mark_done(state, "juicefs_formatted")


# ---------------------------------------------------------------------------
# Step 3: Create RunPod Network Volume
# ---------------------------------------------------------------------------


def create_network_volume(config: dict[str, str], state: dict) -> None:
    existing_id = state.get("network_volume_id") or config.get("RUNPOD_NETWORK_VOLUME_ID", "")
    if existing_id:
        ok(f"Network Volume already exists: {existing_id}")
        config["RUNPOD_NETWORK_VOLUME_ID"] = existing_id
        return

    api_key = config["RUNPOD_API_KEY"]
    datacenter = config.get("RUNPOD_DATACENTER", "EU-RO-1")

    info("Querying available datacenters...")
    dc_query = """
    query {
        myself {
            id
        }
    }
    """
    # Verify the API key works
    try:
        runpod_gql(api_key, dc_query)
        ok("RunPod API key is valid")
    except Exception as exc:
        raise RuntimeError(f"RunPod API key validation failed: {exc}") from exc

    size_gb = 50
    info(f"Creating Network Volume: runpodfarm-houdini ({size_gb} GB) in {datacenter}...")

    mutation = """
    mutation createNetworkVolume($input: CreateNetworkVolumeInput!) {
        createNetworkVolume(input: $input) {
            id
            name
            size
            dataCenterId
        }
    }
    """
    variables = {
        "input": {
            "name": "runpodfarm-houdini",
            "dataCenterId": datacenter,
            "size": size_gb,
        }
    }

    result = runpod_gql(api_key, mutation, variables)
    volume = result.get("createNetworkVolume", {})
    volume_id = volume.get("id")

    if not volume_id:
        raise RuntimeError(f"Failed to create Network Volume. Response: {json.dumps(result, indent=2)}")

    ok(f"Network Volume created: {volume_id}")
    info(f"  Name: {volume.get('name')}")
    info(f"  Size: {volume.get('size')} GB")
    info(f"  Datacenter: {volume.get('dataCenterId')}")

    config["RUNPOD_NETWORK_VOLUME_ID"] = volume_id
    save_env_file(config)
    mark_done(state, "network_volume_id", volume_id)


# ---------------------------------------------------------------------------
# Step 4: Upload Houdini to Network Volume
# ---------------------------------------------------------------------------


def setup_houdini(config: dict[str, str], state: dict) -> None:
    if state.get("houdini_uploaded"):
        ok("Houdini already uploaded to Network Volume (previous run)")
        return

    volume_id = config.get("RUNPOD_NETWORK_VOLUME_ID", "")
    if not volume_id:
        warn("No Network Volume ID -- skipping Houdini upload.")
        return

    api_key = config["RUNPOD_API_KEY"]
    datacenter = config.get("RUNPOD_DATACENTER", "EU-RO-1")

    banner_text = "Upload Houdini to Network Volume"
    print()
    info("This step requires creating a temporary pod to install Houdini")
    info("on the Network Volume. You have two options:\n")
    print(f"  {bold('Option A')}: Automatic (creates a temporary CPU pod)")
    print(f"  {bold('Option B')}: Manual (follow instructions, mark as done)\n")

    choice = input(f"  Choose [{bold('A')}/b]: ").strip().lower() or "a"

    if choice == "b":
        _houdini_manual_instructions(config)
        confirm = input("\n  Have you completed the Houdini installation? [y/N]: ").strip().lower()
        if confirm == "y":
            mark_done(state, "houdini_uploaded")
            ok("Marked Houdini as installed")
        else:
            warn("Houdini installation not confirmed. You can re-run this step later.")
        return

    # Option A: Create a temporary pod
    info("Creating temporary pod with Network Volume...")

    mutation = """
    mutation createPod($input: PodFindAndDeployOnDemandInput!) {
        podFindAndDeployOnDemand(input: $input) {
            id
            name
            desiredStatus
            imageName
            machine {
                podHostId
            }
        }
    }
    """
    variables = {
        "input": {
            "name": "runpodfarm-houdini-setup",
            "imageName": "ubuntu:22.04",
            "gpuTypeId": "NVIDIA GeForce RTX 4090",
            "cloudType": "SECURE",
            "volumeInGb": 0,
            "containerDiskInGb": 20,
            "networkVolumeId": volume_id,
            "dataCenterId": datacenter,
            "startSsh": True,
            "dockerArgs": "sleep infinity",
            "gpuCount": 1,
        }
    }

    try:
        result = runpod_gql(api_key, mutation, variables)
        pod = result.get("podFindAndDeployOnDemand", {})
        pod_id = pod.get("id")
    except Exception as exc:
        warn(f"Failed to create temporary pod: {exc}")
        warn("Falling back to manual instructions.")
        _houdini_manual_instructions(config)
        return

    if not pod_id:
        warn("Failed to create temporary pod. Falling back to manual instructions.")
        _houdini_manual_instructions(config)
        return

    ok(f"Temporary pod created: {pod_id}")
    info("Waiting for pod to be ready...")

    # Poll for pod status
    for attempt in range(60):
        time.sleep(5)
        query = """
        query pod($input: PodFilter!) {
            pod(input: $input) {
                id
                desiredStatus
                runtime {
                    uptimeInSeconds
                    ports {
                        ip
                        isIpPublic
                        privatePort
                        publicPort
                        type
                    }
                }
            }
        }
        """
        try:
            pod_data = runpod_gql(api_key, query, {"input": {"podId": pod_id}})
            pod_info = pod_data.get("pod", {})
            runtime = pod_info.get("runtime")
            if runtime and runtime.get("uptimeInSeconds", 0) > 0:
                ok("Pod is running")
                # Find SSH port
                ports = runtime.get("ports", [])
                ssh_port = None
                ssh_ip = None
                for p in ports:
                    if p.get("privatePort") == 22:
                        ssh_ip = p.get("ip")
                        ssh_port = p.get("publicPort")
                        break

                if ssh_ip and ssh_port:
                    info(f"SSH available at: {ssh_ip}:{ssh_port}")
                break
        except Exception:
            pass

        if attempt % 6 == 5:
            info(f"Still waiting... ({(attempt + 1) * 5}s)")
    else:
        warn("Pod startup timed out after 5 minutes")

    print()
    info("The temporary pod is now running with your Network Volume mounted")
    info("at /workspace. You need to:\n")
    print(f"    1. Download the Houdini Linux installer from sidefx.com")
    print(f"    2. Upload it to the pod using runpodctl or SCP")
    print(f"    3. Install Houdini to /workspace/houdini\n")
    print(f"    Example commands:")
    print(f"      runpodctl send houdini-20.5.xxx-linux_x86_64_gcc11.2.tar.gz")
    print(f"      # On the pod:")
    print(f"      tar xf houdini*.tar.gz")
    print(f"      cd houdini*")
    print(f"      ./houdini.install --install-houdini \\")
    print(f"          --no-install-license --accept-EULA 2024-01-01 \\")
    print(f"          --install-dir /workspace/houdini")
    print()

    input(f"  Press {bold('Enter')} when Houdini installation is complete...")

    # Terminate the temp pod
    info("Terminating temporary pod...")
    terminate_mutation = """
    mutation terminatePod($input: PodTerminateInput!) {
        podTerminate(input: $input)
    }
    """
    try:
        runpod_gql(api_key, terminate_mutation, {"input": {"podId": pod_id}})
        ok("Temporary pod terminated")
    except Exception as exc:
        warn(f"Failed to terminate pod {pod_id}: {exc}")
        warn("Please terminate it manually in the RunPod dashboard.")

    mark_done(state, "houdini_uploaded")


def _houdini_manual_instructions(config: dict[str, str]) -> None:
    """Print manual instructions for Houdini upload."""
    volume_id = config.get("RUNPOD_NETWORK_VOLUME_ID", "unknown")
    print(textwrap.dedent(f"""
    {bold('Manual Houdini Installation Instructions')}
    {'-' * 45}

    1. Go to https://www.runpod.io/console/pods
    2. Create a new pod:
       - GPU: Any cheap GPU (e.g. RTX 3070)
       - Image: ubuntu:22.04
       - Network Volume: {volume_id}
       - Docker command: sleep infinity
    3. Connect via SSH or Web Terminal
    4. Download Houdini Linux installer to the pod
    5. Install:
       tar xf houdini-*.tar.gz
       cd houdini-*
       ./houdini.install --install-houdini \\
           --no-install-license --accept-EULA 2024-01-01 \\
           --install-dir /workspace/houdini
    6. Verify: ls /workspace/houdini/bin/hython
    7. Terminate the temporary pod
    """))


# ---------------------------------------------------------------------------
# Step 5: Build and push Docker image
# ---------------------------------------------------------------------------


def build_docker(config: dict[str, str], state: dict) -> None:
    if state.get("docker_built"):
        ok("Docker image already built (previous run)")
        return

    if not shutil.which("docker"):
        warn("Docker not found in PATH. Skipping Docker build.")
        warn("Install Docker Desktop and re-run, or build manually:")
        print(f"    docker build -f {PROJECT_ROOT}/docker/Dockerfile \\")
        print(f"        -t {DOCKER_IMAGE} {PROJECT_ROOT}")
        return

    dockerfile = PROJECT_ROOT / "docker" / "Dockerfile"
    if not dockerfile.exists():
        warn(f"Dockerfile not found at {dockerfile}. Skipping.")
        return

    image = config.get("DOCKER_IMAGE", DOCKER_IMAGE)
    config["DOCKER_IMAGE"] = image

    info(f"Building Docker image: {image}")
    run(
        ["docker", "build", "-f", str(dockerfile), "-t", image, str(PROJECT_ROOT)],
    )
    ok(f"Docker image built: {image}")

    # Ask about push
    push = input(f"\n  Push image to Docker Hub? [y/N]: ").strip().lower()
    if push == "y":
        info(f"Pushing {image}...")
        run(["docker", "push", image])
        ok(f"Image pushed: {image}")

    save_env_file(config)
    mark_done(state, "docker_built")


# ---------------------------------------------------------------------------
# Step 6: Create RunPod Template
# ---------------------------------------------------------------------------


def create_template(config: dict[str, str], state: dict) -> None:
    existing_id = state.get("template_id") or config.get("RUNPOD_TEMPLATE_ID", "")
    if existing_id:
        ok(f"RunPod Template already exists: {existing_id}")
        config["RUNPOD_TEMPLATE_ID"] = existing_id
        return

    api_key = config["RUNPOD_API_KEY"]
    image = config.get("DOCKER_IMAGE", DOCKER_IMAGE)
    volume_id = config.get("RUNPOD_NETWORK_VOLUME_ID", "")

    info(f"Creating RunPod Template: runpodfarm-worker")
    info(f"  Image: {image}")

    # Build environment variables for the template
    env_vars: dict[str, str] = {}

    redis_url = config.get("REDIS_URL", "")
    if redis_url:
        env_vars["JUICEFS_REDIS_URL"] = redis_url

    b2_endpoint = config.get("B2_ENDPOINT", "")
    b2_bucket = config.get("B2_BUCKET", "")
    if b2_endpoint and b2_bucket:
        env_vars["JUICEFS_BUCKET_URL"] = f"{b2_endpoint}/{b2_bucket}"

    sidefx_id = config.get("SIDEFX_CLIENT_ID", "")
    if sidefx_id:
        env_vars["SIDEFX_CLIENT_ID"] = sidefx_id

    sidefx_secret = config.get("SIDEFX_CLIENT_SECRET", "")
    if sidefx_secret:
        env_vars["SIDEFX_CLIENT_SECRET"] = sidefx_secret

    # Read RSA key if it exists
    rsa_key_content = ""
    if RSA_KEY_FILE.exists():
        rsa_key_content = RSA_KEY_FILE.read_text().strip()
        env_vars["JUICEFS_RSA_KEY"] = rsa_key_content

    # Build env list for GraphQL
    env_list = [{"key": k, "value": v} for k, v in env_vars.items()]

    mutation = """
    mutation saveTemplate($input: SaveTemplateInput!) {
        saveTemplate(input: $input) {
            id
            name
            imageName
        }
    }
    """
    variables = {
        "input": {
            "name": "runpodfarm-worker",
            "imageName": image,
            "dockerArgs": "",
            "containerDiskInGb": 10,
            "volumeInGb": 0,
            "env": env_list,
            "isServerless": False,
            "startSsh": True,
            "readme": "RunPodFarm Worker - distributed Houdini rendering on RunPod",
        }
    }

    if volume_id:
        variables["input"]["networkVolumeId"] = volume_id

    result = runpod_gql(api_key, mutation, variables)
    template = result.get("saveTemplate", {})
    template_id = template.get("id")

    if not template_id:
        raise RuntimeError(f"Failed to create template. Response: {json.dumps(result, indent=2)}")

    ok(f"RunPod Template created: {template_id}")
    info(f"  Name: {template.get('name')}")
    info(f"  Image: {template.get('imageName')}")

    config["RUNPOD_TEMPLATE_ID"] = template_id
    save_env_file(config)
    mark_done(state, "template_id", template_id)


# ---------------------------------------------------------------------------
# Step 7: Deploy Auth API to Cloudflare Workers
# ---------------------------------------------------------------------------


def deploy_auth_api(config: dict[str, str], state: dict) -> None:
    if state.get("auth_api_url"):
        ok(f"Auth API already deployed: {state['auth_api_url']}")
        config["AUTH_API_URL"] = state["auth_api_url"]
        return

    auth_api_dir = PROJECT_ROOT / "auth-api"
    if not auth_api_dir.exists():
        warn("auth-api/ directory not found. Skipping.")
        return

    if not shutil.which("npm"):
        warn("npm not found in PATH. Install Node.js 18+ and retry.")
        return

    # Check if wrangler is available
    info("Checking Cloudflare Wrangler...")
    npx = shutil.which("npx")
    if not npx:
        warn("npx not found. Install Node.js 18+ and retry.")
        return

    # Check wrangler login status
    info("Verifying Cloudflare authentication...")
    whoami_result = run(
        [npx, "wrangler", "whoami"],
        cwd=str(auth_api_dir),
        capture=True,
        check=False,
    )

    if whoami_result.returncode != 0 or "not authenticated" in (whoami_result.stdout + whoami_result.stderr).lower():
        warn("You need to log in to Cloudflare first.")
        info("Running: npx wrangler login")
        run([npx, "wrangler", "login"], cwd=str(auth_api_dir))

    # Install dependencies
    info("Installing auth-api dependencies...")
    run(["npm", "install"], cwd=str(auth_api_dir))

    # Create KV namespace if needed
    info("Creating KV namespace for Auth API...")
    kv_result = run(
        [npx, "wrangler", "kv", "namespace", "create", "KV"],
        cwd=str(auth_api_dir),
        capture=True,
        check=False,
    )
    kv_output = kv_result.stdout + kv_result.stderr

    # Parse KV namespace ID from output
    kv_match = re.search(r'id\s*=\s*"([a-f0-9]+)"', kv_output)
    if kv_match:
        kv_id = kv_match.group(1)
        info(f"KV namespace ID: {kv_id}")

        # Update wrangler.toml with the real KV ID
        wrangler_toml = auth_api_dir / "wrangler.toml"
        if wrangler_toml.exists():
            content = wrangler_toml.read_text()
            content = re.sub(
                r'id\s*=\s*"placeholder_kv_id"',
                f'id = "{kv_id}"',
                content,
            )
            wrangler_toml.write_text(content)
            ok("Updated wrangler.toml with KV namespace ID")
    elif "already exists" in kv_output.lower():
        info("KV namespace already exists")
    else:
        warn("Could not parse KV namespace ID from output. You may need to update wrangler.toml manually.")
        print(f"    Output: {kv_output[:500]}")

    # Deploy
    info("Deploying Auth API to Cloudflare Workers...")
    deploy_result = run(
        [npx, "wrangler", "deploy"],
        cwd=str(auth_api_dir),
        capture=True,
        check=False,
    )

    deploy_output = deploy_result.stdout + deploy_result.stderr

    if deploy_result.returncode != 0:
        fail("Auth API deployment failed")
        print(f"\n{deploy_output}\n")
        raise RuntimeError("Wrangler deploy failed")

    # Parse deployed URL from output
    url_match = re.search(r'(https://[a-zA-Z0-9._-]+\.workers\.dev)', deploy_output)
    if url_match:
        api_url = url_match.group(1)
    else:
        api_url = "https://runpodfarm-auth.workers.dev"
        warn(f"Could not parse URL from deploy output. Using default: {api_url}")

    ok(f"Auth API deployed: {api_url}")

    # Set JWT_SECRET as a Cloudflare secret
    jwt_secret = _secrets.token_hex(32)
    info("Setting JWT_SECRET as Cloudflare Workers secret...")
    secret_proc = run(
        [npx, "wrangler", "secret", "put", "JWT_SECRET"],
        cwd=str(auth_api_dir),
        input=jwt_secret,
        check=False,
        capture=True,
    )
    if secret_proc.returncode == 0:
        ok("JWT_SECRET configured")
    else:
        warn("Failed to set JWT_SECRET. Set it manually with: npx wrangler secret put JWT_SECRET")

    config["AUTH_API_URL"] = api_url
    save_env_file(config)
    mark_done(state, "auth_api_url", api_url)


# ---------------------------------------------------------------------------
# Step 8: Deploy Dashboard to Cloudflare Pages
# ---------------------------------------------------------------------------


def deploy_dashboard(config: dict[str, str], state: dict) -> None:
    if state.get("dashboard_url"):
        ok(f"Dashboard already deployed: {state['dashboard_url']}")
        return

    dashboard_dir = PROJECT_ROOT / "dashboard"
    if not dashboard_dir.exists():
        warn("dashboard/ directory not found. Skipping.")
        return

    if not shutil.which("npm"):
        warn("npm not found in PATH. Install Node.js 18+ and retry.")
        return

    npx = shutil.which("npx")
    if not npx:
        warn("npx not found. Install Node.js 18+ and retry.")
        return

    api_url = config.get("AUTH_API_URL", "https://runpodfarm-auth.workers.dev")

    # Install dependencies
    info("Installing dashboard dependencies...")
    run(["npm", "install"], cwd=str(dashboard_dir))

    # Write .env for Vite build
    dashboard_env = dashboard_dir / ".env"
    dashboard_env.write_text(f"VITE_API_URL={api_url}\n")
    info(f"Dashboard .env written (VITE_API_URL={api_url})")

    # Build
    info("Building dashboard...")
    run(["npm", "run", "build"], cwd=str(dashboard_dir))
    ok("Dashboard built successfully")

    dist_dir = dashboard_dir / "dist"
    if not dist_dir.exists():
        raise RuntimeError("Dashboard build did not produce dist/ directory")

    # Deploy to Cloudflare Pages
    info("Deploying dashboard to Cloudflare Pages...")
    deploy_result = run(
        [
            npx, "wrangler", "pages", "deploy", "dist",
            "--project-name=runpodfarm-dashboard",
        ],
        cwd=str(dashboard_dir),
        capture=True,
        check=False,
    )

    deploy_output = deploy_result.stdout + deploy_result.stderr

    if deploy_result.returncode != 0:
        fail("Dashboard deployment failed")
        print(f"\n{deploy_output}\n")
        raise RuntimeError("Wrangler pages deploy failed")

    # Parse deployed URL
    url_match = re.search(r'(https://[a-zA-Z0-9._-]+\.pages\.dev)', deploy_output)
    if url_match:
        dashboard_url = url_match.group(1)
    else:
        dashboard_url = "https://runpodfarm-dashboard.pages.dev"
        warn(f"Could not parse URL from deploy output. Using default: {dashboard_url}")

    ok(f"Dashboard deployed: {dashboard_url}")
    mark_done(state, "dashboard_url", dashboard_url)


# ---------------------------------------------------------------------------
# Step 9: Setup admin account and project in Auth API
# ---------------------------------------------------------------------------


def setup_auth(config: dict[str, str], state: dict) -> None:
    if state.get("auth_setup_done"):
        ok("Auth setup already completed (previous run)")
        project_id = state.get("project_id", "")
        artist_key = state.get("artist_api_key", "")
        if project_id:
            config["PROJECT_ID"] = project_id
        if artist_key:
            config["ARTIST_API_KEY"] = artist_key
        return

    api_url = config.get("AUTH_API_URL", "")
    if not api_url:
        warn("No AUTH_API_URL configured. Skipping auth setup.")
        return

    email = config.get("ADMIN_EMAIL", "")
    password = config.get("ADMIN_PASSWORD", "")
    if not email or not password:
        warn("Missing ADMIN_EMAIL or ADMIN_PASSWORD. Skipping auth setup.")
        return

    # Step 9a: Register admin
    info("Registering admin account...")
    try:
        register_resp = http_request(
            f"{api_url}/auth/register",
            method="POST",
            data={"email": email, "password": password},
        )
        token = register_resp.get("token", "")
        admin_info = register_resp.get("admin", {})
        ok(f"Admin registered: {admin_info.get('email', email)}")
    except RuntimeError as exc:
        # If admin already exists, try login instead
        if "403" in str(exc) or "already exists" in str(exc).lower():
            info("Admin already exists, logging in...")
            login_resp = http_request(
                f"{api_url}/auth/login",
                method="POST",
                data={"email": email, "password": password},
            )
            token = login_resp.get("token", "")
            ok("Admin login successful")
        else:
            raise

    if not token:
        raise RuntimeError("Failed to obtain auth token")

    auth_headers = {"Authorization": f"Bearer {token}"}

    # Step 9b: Create project
    project_name = config.get("PROJECT_NAME", "default")
    redis_url = config.get("REDIS_URL", "")
    b2_endpoint = config.get("B2_ENDPOINT", "")
    b2_key = config.get("B2_KEY_ID", "")
    b2_secret = config.get("B2_APP_KEY", "")
    b2_bucket = config.get("B2_BUCKET", "")

    # Read RSA key
    rsa_key = ""
    if RSA_KEY_FILE.exists():
        rsa_key = RSA_KEY_FILE.read_text().strip()

    # Check if a project already exists
    info("Checking existing projects...")
    try:
        projects_resp = http_request(
            f"{api_url}/projects",
            method="GET",
            headers=auth_headers,
        )
        existing_projects = projects_resp.get("projects", [])
    except Exception:
        existing_projects = []

    project_id = ""
    if existing_projects:
        # Use the first existing project
        project_id = existing_projects[0].get("id", "")
        ok(f"Using existing project: {existing_projects[0].get('name')} ({project_id})")
    else:
        info(f"Creating project: {project_name}")

        # If B2 credentials are missing, use placeholder values
        project_data = {
            "name": project_name,
            "redisUrl": redis_url or "rediss://placeholder",
            "b2Endpoint": b2_endpoint or "https://placeholder",
            "b2AccessKey": b2_key or "placeholder",
            "b2SecretKey": b2_secret or "placeholder",
            "b2Bucket": b2_bucket or "placeholder",
            "juicefsRsaKey": rsa_key or "placeholder",
        }

        project_resp = http_request(
            f"{api_url}/projects",
            method="POST",
            headers=auth_headers,
            data=project_data,
        )
        project = project_resp.get("project", {})
        project_id = project.get("id", "")
        ok(f"Project created: {project.get('name')} ({project_id})")

    if project_id:
        config["PROJECT_ID"] = project_id

    # Step 9c: Create default artist
    info("Creating default artist...")
    try:
        artist_resp = http_request(
            f"{api_url}/projects/{project_id}/artists",
            method="POST",
            headers=auth_headers,
            data={"name": "Default Artist", "email": email},
        )
        artist = artist_resp.get("artist", {})
        api_key = artist.get("apiKey", "")
        ok(f"Artist created: {artist.get('name')} ({artist.get('id')})")
        if api_key:
            ok(f"Artist API Key: {api_key}")
            config["ARTIST_API_KEY"] = api_key
    except RuntimeError as exc:
        warn(f"Artist creation failed: {exc}")
        warn("You can create artists later via the dashboard.")
        api_key = ""

    save_env_file(config)
    mark_done(state, "auth_setup_done", True)
    if project_id:
        mark_done(state, "project_id", project_id)
    if api_key:
        mark_done(state, "artist_api_key", api_key)


# ---------------------------------------------------------------------------
# Step 10: End-to-end connectivity test
# ---------------------------------------------------------------------------


def test_connectivity(config: dict[str, str], state: dict) -> None:
    print()
    results: list[tuple[str, bool, str]] = []

    # Test 1: RunPod API
    info("Testing RunPod API connectivity...")
    api_key = config.get("RUNPOD_API_KEY", "")
    if api_key:
        try:
            runpod_gql(api_key, "query { myself { id } }")
            results.append(("RunPod API", True, "Connected"))
        except Exception as exc:
            results.append(("RunPod API", False, str(exc)[:80]))
    else:
        results.append(("RunPod API", False, "No API key configured"))

    # Test 2: Redis (via Python subprocess - no pip dependency needed)
    info("Testing Redis connectivity...")
    redis_url = config.get("REDIS_URL", "")
    if redis_url:
        redis_test = run(
            [
                sys.executable, "-c",
                f"import socket, ssl, urllib.parse; "
                f"u=urllib.parse.urlparse('{redis_url}'); "
                f"s=socket.create_connection((u.hostname, u.port or 6379), timeout=5); "
                f"ctx=ssl.create_default_context(); "
                f"ss=ctx.wrap_socket(s, server_hostname=u.hostname); "
                f"ss.send(b'PING\\r\\n'); "
                f"r=ss.recv(64); "
                f"ss.close(); "
                f"assert b'PONG' in r, f'Unexpected: {{r}}'; "
                f"print('PONG received')",
            ],
            capture=True,
            check=False,
        )
        if redis_test.returncode == 0:
            results.append(("Redis (Upstash)", True, "PONG received"))
        else:
            err = (redis_test.stderr or redis_test.stdout or "unknown error").strip()
            results.append(("Redis (Upstash)", False, err[:80]))
    else:
        results.append(("Redis (Upstash)", False, "No URL configured"))

    # Test 3: Auth API health
    info("Testing Auth API health...")
    auth_url = config.get("AUTH_API_URL", "")
    if auth_url:
        try:
            health = http_request(f"{auth_url}/health")
            status = health.get("status", "unknown")
            if status == "ok":
                results.append(("Auth API", True, f"Healthy ({auth_url})"))
            else:
                results.append(("Auth API", False, f"Status: {status}"))
        except Exception as exc:
            results.append(("Auth API", False, str(exc)[:80]))
    else:
        results.append(("Auth API", False, "No URL configured"))

    # Test 4: B2 connectivity (try a HEAD on the endpoint)
    info("Testing B2 endpoint...")
    b2_endpoint = config.get("B2_ENDPOINT", "")
    if b2_endpoint:
        try:
            req = urllib.request.Request(b2_endpoint, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                results.append(("Backblaze B2", True, f"Reachable ({resp.status})"))
        except urllib.error.HTTPError as exc:
            # 403 is expected (no auth), but it means the endpoint works
            if exc.code in (400, 403, 405):
                results.append(("Backblaze B2", True, f"Reachable (HTTP {exc.code})"))
            else:
                results.append(("Backblaze B2", False, f"HTTP {exc.code}"))
        except Exception as exc:
            results.append(("Backblaze B2", False, str(exc)[:80]))
    else:
        results.append(("Backblaze B2", False, "No endpoint configured"))

    # Test 5: JuiceFS
    info("Testing JuiceFS CLI...")
    if shutil.which("juicefs"):
        jfs_result = run(["juicefs", "version"], capture=True, check=False)
        if jfs_result.returncode == 0:
            version = jfs_result.stdout.strip().split("\n")[0]
            results.append(("JuiceFS CLI", True, version))
        else:
            results.append(("JuiceFS CLI", False, "Command failed"))
    else:
        results.append(("JuiceFS CLI", False, "Not installed"))

    # Print summary
    print()
    print(bold("  Connectivity Test Results"))
    print(f"  {'=' * 56}")
    for name, success, detail in results:
        icon = green("[PASS]") if success else red("[FAIL]")
        print(f"  {icon} {name:<20} {dim(detail)}")
    print(f"  {'=' * 56}")

    passed = sum(1 for _, s, _ in results if s)
    total = len(results)
    if passed == total:
        ok(f"All {total} tests passed")
    else:
        warn(f"{passed}/{total} tests passed")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main() -> int:
    banner("RunPodFarm Infrastructure Provisioner")
    print()
    info(f"Project root:  {PROJECT_ROOT}")
    info(f"State file:    {STATE_FILE}")
    info(f"Env file:      {ENV_FILE}")
    info(f"Platform:      {platform.system()} {platform.machine()}")
    print()

    # Check Python version
    if sys.version_info < (3, 10):
        fail(f"Python 3.10+ required (found {sys.version})")
        return 1

    # Load previous state
    state = load_state()
    if state:
        info(f"Found state from previous run ({len(state)} entries)")
        reset = input(f"  Reset state and start fresh? [y/N]: ").strip().lower()
        if reset == "y":
            state = {}
            save_state(state)
            ok("State reset")

    # Collect credentials
    config = collect_credentials()

    # Define all steps
    steps: list[tuple[str, str, Any]] = [
        ("juicefs_install", "Install JuiceFS CLI", install_juicefs),
        ("juicefs_format", "Format JuiceFS volume (Redis + B2)", format_juicefs),
        ("network_volume", "Create RunPod Network Volume", create_network_volume),
        ("houdini_upload", "Upload Houdini to Network Volume", setup_houdini),
        ("docker_build", "Build & push Docker image", build_docker),
        ("template_create", "Create RunPod Pod Template", create_template),
        ("auth_deploy", "Deploy Auth API (Cloudflare Workers)", deploy_auth_api),
        ("dashboard_deploy", "Deploy Dashboard (Cloudflare Pages)", deploy_dashboard),
        ("auth_setup", "Setup admin account & project", setup_auth),
        ("connectivity_test", "Test connectivity end-to-end", test_connectivity),
    ]

    total = len(steps)
    for i, (step_key, name, func) in enumerate(steps, 1):
        print()
        print(bold(f"  --- Step {i}/{total}: {name} ---"))

        # Show skip hint if step was already done
        if state.get(step_key + "_done") or (
            step_key == "juicefs_install" and state.get("juicefs_installed")
        ):
            print(f"  {dim('(completed in a previous run)')}")

        choice = input(f"  Run this step? [{bold('Y')}/n/skip] ").strip().lower()
        if choice == "skip":
            info(f"Skipped: {name}")
            continue
        if choice == "n":
            info("Stopping provisioner.")
            break

        try:
            func(config, state)
            step_done(f"{name} -- done")
        except KeyboardInterrupt:
            print()
            warn("Interrupted by user")
            break
        except Exception as exc:
            fail(f"{name} failed: {exc}")
            import traceback
            traceback.print_exc()
            cont = input(f"\n  Continue to next step? [y/N]: ").strip().lower()
            if cont != "y":
                break

    # Final summary
    banner("Provisioning Summary")
    print()

    summary_fields = [
        ("RunPod API Key", _mask(config.get("RUNPOD_API_KEY", ""))),
        ("RunPod Datacenter", config.get("RUNPOD_DATACENTER", "")),
        ("Network Volume ID", config.get("RUNPOD_NETWORK_VOLUME_ID", "")),
        ("Template ID", config.get("RUNPOD_TEMPLATE_ID", "")),
        ("Redis URL", _mask_url(config.get("REDIS_URL", ""))),
        ("B2 Bucket", config.get("B2_BUCKET", "")),
        ("Auth API URL", config.get("AUTH_API_URL", "")),
        ("Project ID", config.get("PROJECT_ID", "")),
        ("Artist API Key", config.get("ARTIST_API_KEY", "")),
        ("Docker Image", config.get("DOCKER_IMAGE", "")),
    ]

    for label, value in summary_fields:
        if value:
            print(f"  {label:<24} {green(value)}")
        else:
            print(f"  {label:<24} {dim('(not set)')}")

    print()
    info(f"Configuration saved to: {ENV_FILE}")
    info(f"State saved to: {STATE_FILE}")
    print()

    if config.get("ARTIST_API_KEY"):
        print(bold("  Next steps:"))
        print(f"    1. Open Houdini and add the RunPodFarm Scheduler HDA")
        print(f"    2. Enter your Artist API Key in the scheduler parameters")
        print(f"    3. Configure a TOP network and cook!")
        print()
    else:
        print(bold("  Next steps:"))
        print(f"    1. Complete any skipped steps by re-running this script")
        print(f"    2. Create artist accounts via the dashboard or Auth API")
        print(f"    3. Distribute API keys to artists")
        print()

    return 0


def _mask(value: str) -> str:
    """Mask a sensitive value, showing only first/last 4 chars."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _mask_url(url: str) -> str:
    """Mask credentials inside a URL."""
    if not url:
        return ""
    # rediss://default:SECRETHERE@host:port
    match = re.match(r'(rediss?://[^:]+:)([^@]+)(@.+)', url)
    if match:
        return match.group(1) + "****" + match.group(3)
    return url


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n\n  {yellow('[!]')} Interrupted. State has been saved -- re-run to resume.")
        sys.exit(130)
