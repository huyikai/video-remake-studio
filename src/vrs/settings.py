from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import os
import yaml


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "config").is_dir():
            return parent
    return Path.cwd()


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} 必须是 YAML 映射")
    return data


def resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()
    return path


class Settings:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or repo_root()
        self.default = load_yaml(self.root / "config" / "default.yaml")
        self.h3 = load_yaml(self.root / "config" / "h3_constraints.yaml")
        self.providers = load_yaml(self.root / "config" / "providers.yaml")
        self.paths = load_yaml(self.root / "config" / "paths.yaml")
        smtp_file = self.root / "config" / "smtp.local.yaml"
        self.smtp = load_yaml(smtp_file) if smtp_file.is_file() else load_yaml(
            self.root / "config" / "smtp.example.yaml"
        )
        local = load_yaml(self.root / "config" / "local.yaml")
        if local:
            if "paths" in local:
                self.paths = deep_merge(self.paths, local["paths"])
            if "providers" in local:
                self.providers = deep_merge(self.providers, local["providers"])
            if "smtp" in local:
                self.smtp = deep_merge(self.smtp, local["smtp"])
            if "h3" in local:
                self.h3 = deep_merge(self.h3, local["h3"])
            rest = {
                k: v
                for k, v in local.items()
                if k not in {"paths", "providers", "smtp", "h3"}
            }
            if rest:
                self.default = deep_merge(self.default, rest)
        self._apply_runtime_env()
        self._apply_smtp_env()
        self._apply_douyin_env()

    def _apply_runtime_env(self) -> None:
        runtime_path = self.root / "data" / "runtime.yaml"
        runtime = load_yaml(runtime_path)
        mode = os.environ.get("VRS_MODE") or runtime.get("mode") or self.default.get("mode") or "mock"
        self.default["mode"] = str(mode).lower() if str(mode).lower() in {"mock", "real"} else "mock"
        speed = runtime.get("mock_speed") or self.default.get("mock_speed") or "1x"
        self.default["mock_speed"] = str(speed) if str(speed) in {"0.25x", "1x", "4x"} else "1x"
        self.default["mock_faults"] = runtime.get("mock_faults") or self.default.get("mock_faults") or {}
        cookie_status = str(runtime.get("douyin_cookie_status") or "ok").lower()
        self.default["douyin_cookie_status"] = cookie_status if cookie_status in {"ok", "expired"} else "ok"

    def _apply_smtp_env(self) -> None:

        mapping = {
            "VRS_SMTP_HOST": "host",
            "VRS_SMTP_PORT": "port",
            "VRS_SMTP_USER": "user",
            "VRS_SMTP_PASSWORD": "password",
            "VRS_SMTP_FROM": "from_addr",
            "VRS_SMTP_SECURITY": "security",
        }
        for env_name, key in mapping.items():
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                continue
            if key == "port":
                self.smtp[key] = int(raw)
            else:
                self.smtp[key] = raw
        enabled = os.environ.get("VRS_SMTP_ENABLED")
        if enabled is not None and enabled != "":
            self.smtp["enabled"] = enabled.lower() in {"1", "true", "yes", "on"}
        to_raw = os.environ.get("VRS_SMTP_TO")
        if to_raw:
            self.smtp["to"] = [item.strip() for item in to_raw.split(",") if item.strip()]

    def _apply_douyin_env(self) -> None:
        import os

        cookie = os.environ.get("VRS_DOUYIN_COOKIE")
        if cookie is None or cookie == "":
            return
        douyin = dict(self.default.get("douyin") or {})
        douyin["cookie"] = cookie
        self.default["douyin"] = douyin

    def path(self, key: str) -> Path:
        value = self.paths[key]
        return resolve_path(self.root, str(value))

    def bind_host(self) -> str:
        return str(self.default.get("bind_host", "127.0.0.1"))

    def bind_port(self) -> int:
        return int(self.default.get("bind_port", 8787))

    def mode(self) -> str:
        return str(self.default.get("mode") or "mock").lower()

    def mock_speed(self) -> str:
        return str(self.default.get("mock_speed") or "1x")

    def reload(self) -> None:
        self.__init__(self.root)
