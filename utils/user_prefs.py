"""
utils/user_prefs.py
Lightweight JSON-backed per-user preferences.
"""

import json
import logging
import os
from typing import Any

log = logging.getLogger("qtsdex.prefs")

_FILE   = "data/user_prefs.json"
_cache: dict[str, dict] = {}
_loaded = False

VALID_STAT_STYLES = {"numbers", "bar"}

DEFAULTS: dict[str, Any] = {
    "stat_style":        "numbers",   # "numbers" | "bar"
    "last_pokemon_mode": "info",      # "info"    | "battle"
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
    try:
        os.makedirs("data", exist_ok=True)
        with open(_FILE, "w") as f:
            json.dump(_cache, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save user prefs: {e}")


def get(user_id: int, key: str) -> Any:
    _load()
    val = _cache.get(str(user_id), {}).get(key, DEFAULTS.get(key))
    # Sanitise stat_style — old "tiers" value gets reset to "numbers"
    if key == "stat_style" and val not in VALID_STAT_STYLES:
        return "numbers"
    return val


def set_pref(user_id: int, key: str, value: Any):
    _load()
    uid = str(user_id)
    _cache.setdefault(uid, {})[key] = value
    _save()
