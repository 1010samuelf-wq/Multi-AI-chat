"""The Claude API key, entered on the website's Settings page.

It is saved in DATA_DIR/settings.json (the app's private volume on Fly), readable
only by the app, and never sent back to the browser. An ANTHROPIC_API_KEY
environment variable is used when no key has been saved."""

import json
import os
import threading

from .config import DATA_DIR

_PATH = DATA_DIR / "settings.json"
_lock = threading.Lock()


def _read() -> dict:
    try:
        return json.loads(_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def get_api_key() -> tuple[str, str]:
    """(key, source) where source is "saved", "env" or ""."""
    with _lock:
        saved = _read().get("anthropic_api_key", "")
    if saved:
        return saved, "saved"
    env = os.environ.get("ANTHROPIC_API_KEY", "")
    return (env, "env") if env else ("", "")


def save_api_key(key: str) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = _read()
        data["anthropic_api_key"] = key
        tmp = _PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _PATH)


def delete_api_key() -> None:
    with _lock:
        data = _read()
        data.pop("anthropic_api_key", None)
        if _PATH.exists():
            _PATH.write_text(json.dumps(data))


def hint(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 8 else ""
