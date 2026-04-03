"""Backward-compat shim — moved to nitrobox.image.registry."""

# ruff: noqa: F401,F403
from nitrobox.image.registry import *
from nitrobox.image.registry import (
    parse_image_ref,
    get_image_metadata_from_registry,
    pull_image_layers,
    iter_image_layers,
    get_manifest,
    download_layer,
    get_image_config_from_registry,
    _get_token,
    _registry_request,
    _add_docker_hub_auth,
    _run_credential_helper,
)
