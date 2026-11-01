"""Key loading from a gitignored .env file or the environment."""

import os
from pathlib import Path


class MissingKeyError(Exception):
    """Raised when a required key is in neither the environment nor the .env file."""

    def __init__(self, name: str) -> None:
        super().__init__(f"missing {name}: set it in .env or the environment")
        self.name = name


def load_env(path: Path = Path(".env")) -> dict[str, str]:
    """Parse KEY=VALUE lines. A missing file gives an empty dict."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def require_key(name: str, env_file: Path = Path(".env")) -> str:
    """Return the key from the environment, else the .env file, else raise."""
    value = os.environ.get(name)
    if value:
        return value
    value = load_env(env_file).get(name)
    if value:
        return value
    raise MissingKeyError(name)
