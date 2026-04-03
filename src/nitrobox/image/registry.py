"""Pure-Python OCI registry client — zero external dependencies.

Downloads Docker/OCI images directly from container registries
(Docker Hub, ghcr.io, etc.) without requiring Docker or Podman.

Uses only stdlib: urllib, json, hashlib, tarfile.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Default registry and endpoints
_DOCKER_HUB = "registry-1.docker.io"
_DOCKER_AUTH = "https://auth.docker.io/token"

# Accept headers for manifest (includes schema1 for very old images)
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",
    "application/vnd.docker.distribution.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
])

# Token cache: (registry, repo) -> (token, expiry_timestamp)
_token_cache: dict[str, tuple[str, float]] = {}


def parse_image_ref(image: str) -> tuple[str, str, str]:
    """Parse image reference into (registry, repository, reference).

    The third element is a tag (e.g. ``"22.04"``) or a digest
    (e.g. ``"sha256:abc..."``).

    Examples::

        "ubuntu:22.04"              → ("registry-1.docker.io", "library/ubuntu", "22.04")
        "python:3.11-slim"          → ("registry-1.docker.io", "library/python", "3.11-slim")
        "ghcr.io/org/repo:v1"       → ("ghcr.io", "org/repo", "v1")
        "myregistry:5000/img"       → ("myregistry:5000", "img", "latest")
        "ubuntu@sha256:abc..."      → ("registry-1.docker.io", "library/ubuntu", "sha256:abc...")
        "img:v1@sha256:abc..."      → ("registry-1.docker.io", "library/img", "sha256:abc...")
    """
    # Handle digest references: image@sha256:...
    digest = None
    if "@" in image:
        image, digest = image.rsplit("@", 1)

    tag = "latest"
    # Split tag
    if ":" in image and not image.rsplit(":", 1)[-1].startswith("/"):
        # Could be tag or port. If after last ":" looks like a tag (no "/"), it's a tag
        parts = image.rsplit(":", 1)
        if "/" not in parts[1]:
            image, tag = parts

    # Digest takes precedence over tag
    ref = digest if digest else tag

    # Split registry from repository
    # A registry contains "." or ":" or is "localhost"
    if "/" in image:
        first, rest = image.split("/", 1)
        if "." in first or ":" in first or first == "localhost":
            return first, rest, ref
        # No registry prefix — Docker Hub
        return _DOCKER_HUB, image, ref

    # Bare name like "ubuntu" → Docker Hub library image
    return _DOCKER_HUB, f"library/{image}", ref


def _get_token(registry: str, repo: str) -> str | None:
    """Get bearer token for registry authentication.

    Caches tokens until expiry (matching containers-image's approach).
    For Docker Hub, reads credentials from ``~/.docker/config.json``
    and sends them via HTTP Basic Auth to get an authenticated token
    (avoids anonymous rate limits).
    """
    # Check cache first
    cache_key = f"{registry}/{repo}"
    cached = _token_cache.get(cache_key)
    if cached:
        token, expiry = cached
        if time.time() < expiry:
            return token

    if registry == _DOCKER_HUB:
        url = f"{_DOCKER_AUTH}?service=registry.docker.io&scope=repository:{repo}:pull"
    else:
        # Try token endpoint from WWW-Authenticate header
        try:
            urllib.request.urlopen(f"https://{registry}/v2/", timeout=5)
            return None  # No auth needed
        except urllib.error.HTTPError as e:
            if e.code != 401:
                return None
            auth_header = e.headers.get("WWW-Authenticate", "")
            # Parse: Bearer realm="...",service="...",scope="..."
            realm = re.search(r'realm="([^"]+)"', auth_header)
            service = re.search(r'service="([^"]+)"', auth_header)
            if not realm:
                return None
            url = f"{realm.group(1)}?service={service.group(1) if service else ''}&scope=repository:{repo}:pull"
        except (OSError, urllib.error.URLError):
            return None

    try:
        req = urllib.request.Request(url)
        # Add Basic auth from ~/.docker/config.json to avoid
        # Docker Hub anonymous rate limits.
        _add_docker_hub_auth(req, registry)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            token = data.get("token") or data.get("access_token")
            if token:
                # Cache with expiry (default 300s, minimum 60s like containers-image)
                expires_in = data.get("expires_in", 300)
                _token_cache[cache_key] = (
                    token,
                    time.time() + max(int(expires_in) - 30, 60),
                )
            return token
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as e:
        logger.debug("Token request failed: %s", e)
        return None


def _run_credential_helper(helper: str, server_url: str) -> str | None:
    """Run ``docker-credential-<helper> get`` and return base64 auth.

    Matches containers-image ``pkg/docker/config/config.go`` credential
    helper protocol.
    """
    import base64

    try:
        result = subprocess.run(
            [f"docker-credential-{helper}", "get"],
            input=server_url.encode(),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        cred = json.loads(result.stdout)
        user = cred.get("Username", "")
        secret = cred.get("Secret", "")
        if user and secret:
            return base64.b64encode(f"{user}:{secret}".encode()).decode()
    except Exception:
        pass
    return None


def _add_docker_hub_auth(req: urllib.request.Request, registry: str) -> None:
    """Add HTTP Basic Auth to a token request from ~/.docker/config.json.

    Supports three credential sources (matching containers-image):
    1. Per-registry ``credHelpers`` (e.g. ``docker-credential-osxkeychain``)
    2. Default ``credsStore`` (e.g. ``docker-credential-pass``)
    3. Inline base64 ``auths`` entries
    """
    docker_hub_keys = ["https://index.docker.io/v1/", "docker.io", registry]
    config_path = Path.home() / ".docker" / "config.json"
    try:
        if not config_path.exists():
            return
        data = json.loads(config_path.read_text())

        # 1. Per-registry credential helpers (credHelpers)
        cred_helpers = data.get("credHelpers", {})
        for key in docker_hub_keys:
            helper = cred_helpers.get(key)
            if helper:
                cred = _run_credential_helper(helper, key)
                if cred:
                    req.add_header("Authorization", f"Basic {cred}")
                    return

        # 2. Default credential store (credsStore)
        creds_store = data.get("credsStore")
        if creds_store:
            for key in docker_hub_keys:
                cred = _run_credential_helper(creds_store, key)
                if cred:
                    req.add_header("Authorization", f"Basic {cred}")
                    return

        # 3. Inline base64 auth entries
        auths = data.get("auths", {})
        for key in docker_hub_keys:
            entry = auths.get(key)
            if entry and "auth" in entry:
                # config.json stores base64(user:pass)
                req.add_header("Authorization", f"Basic {entry['auth']}")
                return
    except Exception:
        pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow redirects — handle them manually to drop auth headers."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _registry_request(
    registry: str, path: str, token: str | None,
    accept: str | None = None,
    *,
    max_retries: int = 3,
) -> bytes:
    """Make authenticated request to registry API.

    Handles Docker Hub redirects: blob requests return 307 to CDN,
    and the CDN rejects Authorization headers.  We follow redirects
    manually without auth (matching containers-image).

    Retries transient failures with exponential backoff.
    """
    url = f"https://{registry}{path}"

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if accept:
            req.add_header("Accept", accept)

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                # Follow redirect WITHOUT auth header (CDN rejects it)
                redirect_url = e.headers.get("Location")
                if redirect_url:
                    with urllib.request.urlopen(redirect_url, timeout=120) as resp:
                        return resp.read()
            if e.code >= 500 and attempt < max_retries:
                _retry_sleep(attempt)
                continue
            raise
        except (OSError, urllib.error.URLError):
            if attempt < max_retries:
                _retry_sleep(attempt)
                continue
            raise


def _retry_sleep(attempt: int) -> None:
    """Exponential backoff with jitter (matching containers-image body_reader)."""
    wait = min(2 ** attempt, 30) + random.random() * 0.1
    time.sleep(wait)


def _get_arm_variant() -> str:
    """Detect ARM variant from /proc/cpuinfo (v6, v7, v8).

    Matches containers-image platform matching logic.
    """
    import platform

    machine = platform.machine()
    if machine == "aarch64":
        return "v8"
    if machine.startswith("armv"):
        return machine[3:]  # "armv7l" → "v7l" → typically "v7"
    return ""


def get_manifest(
    registry: str, repo: str, tag: str, token: str | None,
) -> dict[str, Any]:
    """Get image manifest, resolving manifest lists to the current platform.

    Handles platform matching with ``architecture``, ``os``, and
    ``variant`` fields (matching containers-image's full platform
    comparison).
    """
    data = _registry_request(
        registry, f"/v2/{repo}/manifests/{tag}", token,
        accept=_MANIFEST_ACCEPT,
    )
    manifest = json.loads(data)

    # If it's a manifest list / OCI index, resolve to current platform
    media_type = manifest.get("mediaType", "")
    if "list" in media_type or "index" in media_type:
        import platform

        arch = platform.machine()
        arch_map = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}
        target_arch = arch_map.get(arch, arch)
        target_variant = _get_arm_variant()

        # Two-pass: first try exact match (arch + variant), then arch-only
        best: dict | None = None
        for m in manifest.get("manifests", []):
            p = m.get("platform", {})
            if p.get("os") != "linux" or p.get("architecture") != target_arch:
                continue
            manifest_variant = p.get("variant", "")
            if target_variant and manifest_variant == target_variant:
                best = m
                break  # exact match
            if best is None:
                best = m  # first arch match as fallback

        if best:
            digest = best["digest"]
            data = _registry_request(
                registry, f"/v2/{repo}/manifests/{digest}", token,
                accept=_MANIFEST_ACCEPT,
            )
            return json.loads(data)

        raise RuntimeError(
            f"No linux/{target_arch} manifest found for {repo}:{tag}"
        )

    return manifest


def get_image_config_from_registry(
    registry: str, repo: str, manifest: dict, token: str | None,
) -> dict[str, Any]:
    """Download and parse the image config blob."""
    config_digest = manifest["config"]["digest"]
    data = _registry_request(
        registry, f"/v2/{repo}/blobs/{config_digest}", token,
    )
    return json.loads(data)


def get_image_metadata_from_registry(image: str) -> dict:
    """Get diff_ids + container config from registry in a single call.

    Returns an :class:`~nitrobox.rootfs.ImageConfig`-compatible dict.
    Raises on failure — callers decide whether to fall back or propagate.
    """
    from nitrobox.image.store import _parse_docker_env, _parse_docker_ports

    registry, repo, tag = parse_image_ref(image)
    token = _get_token(registry, repo)
    manifest = get_manifest(registry, repo, tag, token)
    config = get_image_config_from_registry(registry, repo, manifest, token)

    container_config = config.get("config", {})
    return {
        "diff_ids": config.get("rootfs", {}).get("diff_ids"),
        "cmd": container_config.get("Cmd"),
        "entrypoint": container_config.get("Entrypoint"),
        "env": _parse_docker_env(container_config.get("Env")),
        "working_dir": container_config.get("WorkingDir") or None,
        "exposed_ports": _parse_docker_ports(container_config.get("ExposedPorts")),
    }


def _download_blob_streaming(
    registry: str,
    repo: str,
    digest: str,
    token: str | None,
    dest: Path,
    *,
    max_retries: int = 3,
) -> None:
    """Stream a blob to *dest* with retry + HTTP Range resume.

    Matches containers-image's ``bodyReader`` reconnection logic:
    on failure, resumes from the byte offset already written.
    """
    url = f"https://{registry}/v2/{repo}/blobs/{digest}"
    offset = 0

    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if offset > 0:
            req.add_header("Range", f"bytes={offset}-")

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            try:
                resp = opener.open(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308):
                    redirect_url = e.headers.get("Location")
                    if redirect_url:
                        redir_req = urllib.request.Request(redirect_url)
                        if offset > 0:
                            redir_req.add_header("Range", f"bytes={offset}-")
                        resp = urllib.request.urlopen(redir_req, timeout=120)
                    else:
                        raise
                elif e.code >= 500 and attempt < max_retries:
                    _retry_sleep(attempt)
                    continue
                else:
                    raise

            mode = "ab" if offset > 0 else "wb"
            with open(dest, mode) as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    offset += len(chunk)
            resp.close()
            return
        except (OSError, urllib.error.URLError):
            if attempt < max_retries:
                _retry_sleep(attempt)
                continue
            raise


def download_layer(
    registry: str, repo: str, digest: str, token: str | None,
) -> bytes:
    """Download a single layer blob from the registry.

    For backward compatibility returns bytes.  Prefer
    :func:`_download_blob_streaming` for large blobs.
    """
    return _registry_request(
        registry, f"/v2/{repo}/blobs/{digest}", token,
    )


def iter_image_layers(
    image: str,
    needed_diff_ids: set[str],
) -> Iterator[tuple[str, Path]]:
    """Yield ``(diff_id, temp_file_path)`` one layer at a time.

    Each blob is streamed to a temporary file, verified against
    its digest, and yielded.  The caller should delete the temp
    file after processing.  This avoids loading all layers into
    memory simultaneously (matching containers-image's streaming
    approach).
    """
    registry, repo, tag = parse_image_ref(image)
    token = _get_token(registry, repo)
    manifest = get_manifest(registry, repo, tag, token)
    config = get_image_config_from_registry(registry, repo, manifest, token)

    diff_ids = config.get("rootfs", {}).get("diff_ids", [])
    layers = manifest.get("layers", [])

    if len(diff_ids) != len(layers):
        raise RuntimeError(
            f"Manifest/config layer count mismatch: {len(layers)} vs {len(diff_ids)}"
        )

    for diff_id, layer_desc in zip(diff_ids, layers):
        if diff_id not in needed_diff_ids:
            continue
        layer_digest = layer_desc["digest"]
        size_mb = layer_desc.get("size", 0) / 1024 / 1024
        logger.info("Downloading layer %.1fMB: %s", size_mb, diff_id[:20])

        tmp = Path(tempfile.mktemp(suffix=".blob"))
        try:
            _download_blob_streaming(registry, repo, layer_digest, token, tmp)

            # Verify digest
            h = hashlib.sha256()
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    h.update(chunk)
            actual = "sha256:" + h.hexdigest()
            if actual != layer_digest:
                raise RuntimeError(
                    f"Layer digest mismatch: expected {layer_digest}, got {actual}"
                )
            yield diff_id, tmp
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def pull_image_layers(
    image: str,
    needed_diff_ids: set[str],
) -> dict[str, bytes]:
    """Download layer blobs from registry for the given diff_ids.

    Returns a dict mapping diff_id → raw layer tarball bytes.
    Only downloads layers whose diff_id is in ``needed_diff_ids``.

    .. note:: Prefer :func:`iter_image_layers` for large images to
       avoid loading all layers into memory at once.
    """
    result: dict[str, bytes] = {}
    for diff_id, tmp_path in iter_image_layers(image, needed_diff_ids):
        try:
            result[diff_id] = tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)
    return result


