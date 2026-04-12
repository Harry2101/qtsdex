"""
utils/limitless.py
Async scraper for limitlessvgc.com/pokemon/<slug> usage stats.
TTL-based in-memory cache, stdlib HTMLParser (no BeautifulSoup dep).
"""

import asyncio
import logging
import time
from html.parser import HTMLParser
from typing import Optional

import aiohttp

log = logging.getLogger("qtsdex.limitless")

BASE_URL = "https://limitlessvgc.com/pokemon"
TTL = 86400  # 24 hours

# Section caps (trimmed at parse time, so cache stores final slices)
LIMITS = {
    "partners":  3,
    "items":     3,
    "moves":     6,
    "abilities": None,  # all
}

_cache: dict[str, dict] = {}
_session: Optional[aiohttp.ClientSession] = None
_lock = asyncio.Lock()
_semaphore = asyncio.Semaphore(4)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                headers={"User-Agent": "QTsDex/2.0 (Discord bot)"},
            )
    return _session


def _cached(slug: str) -> Optional[dict]:
    entry = _cache.get(slug)
    if entry and (time.monotonic() - entry["ts"]) < TTL:
        return entry["data"]
    return None


def _store(slug: str, data: dict):
    _cache[slug] = {"data": data, "ts": time.monotonic()}


# ── Parser ────────────────────────────────────────────────────────────────────

_SECTION_HEADINGS = {
    "Team Partners": "partners",
    "Items":         "items",
    "Moves":         "moves",
    "Abilities":     "abilities",
}


class _StatsParser(HTMLParser):
    """
    Walks the page, locates each `<th colspan=4>SECTION</th>` then collects
    following `<tr>` rows in the same `<table>`. Stops collecting for a section
    when its `</table>` closes.
    """

    def __init__(self):
        super().__init__()
        self.sections: dict[str, list[list[str]]] = {
            "partners": [], "items": [], "moves": [], "abilities": [],
        }
        self._in_th = False
        self._th_buf = ""
        self._current: Optional[str] = None  # key we're currently collecting for
        self._in_tbody = False
        self._row: Optional[list[str]] = None
        self._td_buf = ""
        self._in_td = False
        self._in_a = False  # track anchors inside td so we capture <a>NAME</a>

    def handle_starttag(self, tag, attrs):
        if tag == "th":
            self._in_th = True
            self._th_buf = ""
        elif tag == "tbody" and self._current:
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._row = []
        elif tag == "td" and self._row is not None:
            self._in_td = True
            self._td_buf = ""
        elif tag == "a" and self._in_td:
            self._in_a = True

    def handle_endtag(self, tag):
        if tag == "th" and self._in_th:
            heading = self._th_buf.strip()
            key = _SECTION_HEADINGS.get(heading)
            if key:
                self._current = key
            self._in_th = False
        elif tag == "td" and self._in_td:
            self._row.append(self._td_buf.strip())
            self._in_td = False
        elif tag == "a" and self._in_a:
            self._in_a = False
        elif tag == "tr" and self._row is not None:
            if self._current and self._row:
                self.sections[self._current].append(self._row)
            self._row = None
        elif tag == "tbody" and self._in_tbody:
            self._in_tbody = False
        elif tag == "table":
            # section ends with the table
            self._current = None
            self._in_tbody = False
            self._row = None

    def handle_data(self, data):
        if self._in_th:
            self._th_buf += data
        elif self._in_td:
            self._td_buf += data


def _extract_row(row: list[str], four_col: bool) -> Optional[dict]:
    """
    four_col: partners/items have [rank, img(empty), name, pct]
              moves/abilities have [rank, name, pct]
    """
    try:
        if four_col:
            if len(row) < 4:
                return None
            name, pct = row[2], row[3]
        else:
            if len(row) < 3:
                return None
            name, pct = row[1], row[2]
    except IndexError:
        return None
    name = name.strip()
    pct = pct.strip()
    if not name or not pct:
        return None
    return {"name": name, "pct": pct}


def _parse(html: str) -> dict:
    p = _StatsParser()
    p.feed(html)

    partners = [r for r in (_extract_row(row, True)  for row in p.sections["partners"])  if r]
    items    = [r for r in (_extract_row(row, True)  for row in p.sections["items"])     if r]
    moves    = [r for r in (_extract_row(row, False) for row in p.sections["moves"])     if r]
    abilits  = [r for r in (_extract_row(row, False) for row in p.sections["abilities"]) if r]

    return {
        "partners":  partners[:LIMITS["partners"]],
        "items":     items[:LIMITS["items"]],
        "moves":     moves[:LIMITS["moves"]],
        "abilities": abilits,  # all
    }


# ── Public API ────────────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "-")


async def fetch_meta(pokemon_name: str) -> Optional[dict]:
    """
    Fetch Limitless VGC usage stats for a Pokémon.
    Returns dict with keys partners/items/moves/abilities, or None if
    the page doesn't exist or the request fails.
    """
    slug = _slugify(pokemon_name)
    if not slug:
        return None

    cached = _cached(slug)
    if cached is not None:
        return cached

    url = f"{BASE_URL}/{slug}"
    session = await _get_session()
    try:
        async with _semaphore, session.get(url) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            html = await resp.text()
    except asyncio.TimeoutError:
        log.warning(f"Timeout fetching {url}")
        return None
    except aiohttp.ClientError as e:
        log.error(f"HTTP error fetching {url}: {e}")
        return None

    data = _parse(html)
    # Consider it "no data" if every section is empty (e.g. valid page but no usage yet)
    if not any(data.values()):
        return None

    _store(slug, data)
    return data


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
