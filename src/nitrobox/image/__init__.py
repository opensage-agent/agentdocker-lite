"""Image management: registry client, Docker Engine API, config cache, layer extraction."""

from nitrobox.image.store import get_image_config, ImageConfig
from nitrobox.image.layers import (
    prepare_rootfs_layers_from_docker,
    prepare_rootfs_from_docker,
    prepare_btrfs_rootfs_from_docker,
    rmtree_mapped,
)

__all__ = [
    "get_image_config",
    "ImageConfig",
    "prepare_rootfs_layers_from_docker",
    "prepare_rootfs_from_docker",
    "prepare_btrfs_rootfs_from_docker",
    "rmtree_mapped",
]
