"""
utils/pokeapi.py
Async PokeAPI client with TTL-based in-memory cache.
"""

import asyncio
import logging
import time
from typing import Any, Optional

import aiohttp

log = logging.getLogger("qtsdex.pokeapi")

BASE_URL = "https://pokeapi.co/api/v2"
TTL = 86400  # 24 hours in seconds

# cache entry: {"data": ..., "ts": float}
_cache: dict[str, dict] = {}
_session: Optional[aiohttp.ClientSession] = None
_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "QTsDex/2.0"},
            )
    return _session


def _cached(url: str) -> Optional[Any]:
    entry = _cache.get(url)
    if entry and (time.monotonic() - entry["ts"]) < TTL:
        return entry["data"]
    return None


def _store(url: str, data: Any):
    _cache[url] = {"data": data, "ts": time.monotonic()}


async def fetch(endpoint: str) -> Optional[dict]:
    """Fetch a PokeAPI endpoint (relative or absolute URL)."""
    url = endpoint if endpoint.startswith("http") else f"{BASE_URL}/{endpoint}"
    cached = _cached(url)
    if cached is not None:
        return cached

    session = await _get_session()
    try:
        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            _store(url, data)
            return data
    except asyncio.TimeoutError:
        log.warning(f"Timeout fetching {url}")
        return None
    except aiohttp.ClientError as e:
        log.error(f"HTTP error fetching {url}: {e}")
        return None


async def get_resource_list(endpoint: str, limit: int = 10000) -> list[str]:
    data = await fetch(f"{endpoint}?limit={limit}&offset=0")
    if not data:
        return []
    return [r["name"] for r in data.get("results", [])]


# Typed convenience wrappers
async def get_pokemon(name: str) -> Optional[dict]:
    return await fetch(f"pokemon/{name.lower()}")

async def get_move(name: str) -> Optional[dict]:
    return await fetch(f"move/{name.lower()}")

async def get_ability(name: str) -> Optional[dict]:
    return await fetch(f"ability/{name.lower()}")

async def get_type(name: str) -> Optional[dict]:
    return await fetch(f"type/{name.lower()}")

async def get_item(name: str) -> Optional[dict]:
    return await fetch(f"item/{name.lower()}")


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
