"""Docker Compose compatibility layer."""

from agentdocker_lite.compose._parse import (
    _Service, _parse_compose, _substitute, _topo_sort,
)
from agentdocker_lite.compose._network import SharedNetwork
from agentdocker_lite.compose._project import ComposeProject

__all__ = [
    "ComposeProject",
    "SharedNetwork",
    "_Service",
    "_parse_compose",
    "_substitute",
    "_topo_sort",
]
