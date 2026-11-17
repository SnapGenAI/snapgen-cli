"""Where the API key lives: SNAPGEN_API_KEY wins, else the config file written by `snapgen auth login`."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("SNAPGEN_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "snapgen"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "snapgen"


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    path = config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions before writing the key (no-op on Windows, where %APPDATA% is per-user).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return path


def resolve_api_key() -> tuple[str, str]:
    """(key, source). source is 'env', 'config' or '' when no key is set."""
    env = os.environ.get("SNAPGEN_API_KEY", "").strip()
    if env:
        return env, "env"
    stored = str(load().get("api_key") or "").strip()
    if stored:
        return stored, "config"
    return "", ""


def mask(key: str) -> str:
    return key[:4] + "..." + key[-4:] if len(key) > 12 else "..."
