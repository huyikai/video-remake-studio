from __future__ import annotations

from types import SimpleNamespace
from typing import Any


class Cfg(SimpleNamespace):
    """YAML dict with attribute access and `.get()`, like OmegaConf DictConfig."""

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def wrap_cfg(obj: Any) -> Any:
    if isinstance(obj, dict):
        return Cfg(**{str(key): wrap_cfg(value) for key, value in obj.items()})
    if isinstance(obj, list):
        return [wrap_cfg(item) for item in obj]
    return obj
