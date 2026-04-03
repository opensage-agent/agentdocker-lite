"""Backward-compat shim — moved to nitrobox.image.{store,layers} + nitrobox.storage.whiteout."""

# ruff: noqa: F401
from nitrobox.image.store import (
    ImageConfig,
    get_image_config,
    _parse_docker_env,
    _parse_docker_ports,
    _docker_inspect_to_config,
    _safe_cache_key,
    _get_image_diff_ids,
    _read_config_from_manifest_cache,
    _default_rootfs_cache_dir,
    _image_store_get,
    _image_store_populate,
    _get_image_digest,
    _get_manifest_diff_ids,
    _write_manifest,
)
from nitrobox.image.layers import (
    prepare_rootfs_layers_from_docker,
    prepare_rootfs_from_docker,
    prepare_btrfs_rootfs_from_docker,
    _extract_layers_from_registry,
    _extract_layers_from_save_tar,
    _extract_single_layer_locked,
    _extract_tar_in_userns,
    _rmtree_mapped,
    _pull_or_check_local,
)
from nitrobox.storage.whiteout import (
    _detect_whiteout_strategy,
    _kernel_version,
    _convert_whiteouts_in_layer,
    _convert_whiteouts_in_userns,
)
