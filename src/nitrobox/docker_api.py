"""Backward-compat shim — moved to nitrobox.image.docker."""

# ruff: noqa: F401,F403
from nitrobox.image.docker import *
from nitrobox.image.docker import (
    DockerClient,
    DockerAPIError,
    ImageNotFoundError,
    DockerSocketError,
    get_client,
    _find_docker_socket,
    _load_registry_auth,
    _resolve_registry_domain,
    _UnixHTTPConnection,
    _API_VERSION,
    _client,
    _client_lock,
)
