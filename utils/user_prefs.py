"""
utils/user_prefs.py
Lightweight JSON-backed per-user preferences with atomic writes.
"""

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("qtsdex.prefs")

_FILE   = "data/user_prefs.json"
_cache: dict[str, dict] = {}
_loaded = False
_lock   = asyncio.Lock()

DEFAULTS: dict[str, Any] = {
    "last_pokemon_mode": "info",      # "info" | "battle"
    "pokemon_meta_shown": False,      # sticky: /pokemon opens with Meta panel if last toggled on
}


def _load():
    global _cache, _loaded
    if _loaded:
        return
    _loaded = True
    if os.path.exists(_FILE):
        try:
            with open(_FILE) as f:
                _cache = json.load(f)
        except Exception as e:
            log.error(f"Failed to load user prefs: {e}")
            _cache = {}


def _save():
    """Atomic write: write to temp file first, then rename."""
    try:
        os.makedirs("data", exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir="data", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(_cache, f, indent=2)
            # On Windows, can't rename over existing file — remove first
            if os.path.exists(_FILE):
                os.replace(tmp_path, _FILE)
            else:
                os.rename(tmp_path, _FILE)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        log.error(f"Failed to save user prefs: {e}")


def get(user_id: int, key: str) -> Any:
    _load()
    return _cache.get(str(user_id), {}).get(key, DEFAULTS.get(key))


def set_pref(user_id: int, key: str, value: Any):
    _load()
    uid = str(user_id)
    _cache.setdefault(uid, {})[key] = value
    _save()
