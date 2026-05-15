"""
Gen 6 Pokémon sprite cache.

Downloads Gen 6 (X/Y) front sprites from the public PokeAPI sprites
mirror on first request and caches them under the user home so they
load instantly afterwards.

Used by the launcher's "Recently Seen" panel.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# PokeAPI sprite mirror, Gen VI X/Y front sprites.
_SPRITE_URL_TMPL = (
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
    "pokemon/versions/generation-vi/x-y/{sid}.png"
)
# Fallback to default Gen 5 art if the Gen 6 mirror doesn't have the
# species (notably forms / late additions).
_FALLBACK_URL_TMPL = (
    "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
    "pokemon/{sid}.png"
)

_CACHE_DIR = Path.home() / ".pokebot-3ds-sprites"


def cache_dir() -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def get_sprite_path(species_id: int, shiny: bool = False) -> Optional[Path]:
    """Return a local PNG path for a species, downloading if missing.

    Returns ``None`` if the species can't be downloaded (offline, 404,
    etc.) so callers can fall back to a placeholder.
    """
    if not species_id or species_id < 1:
        return None
    suffix = "shiny" if shiny else "normal"
    fname = f"{species_id}_{suffix}.png"
    target = cache_dir() / fname
    if target.exists() and target.stat().st_size > 0:
        return target
    # Download synchronously. Caller decides whether to invoke us in a
    # background thread.
    urls = [_SPRITE_URL_TMPL.format(sid=species_id)]
    if shiny:
        urls = [u.replace("/x-y/", "/x-y/shiny/") for u in urls]
    urls.append(_FALLBACK_URL_TMPL.format(sid=species_id))
    for url in urls:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "pokebot-3ds/0.1"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status != 200:
                    continue
                data = resp.read()
            if not data:
                continue
            target.write_bytes(data)
            return target
        except Exception as e:
            log.debug(f"sprite download failed for #{species_id} ({url}): {e}")
            continue
    return None


# ---------------------------------------------------------------------------
# Animated sprites (Pokémon Showdown). Showdown's "ani" set covers every
# species incl. Gen 6, but is keyed by name, not dex id — so we resolve
# id → Showdown name via PokeAPI once and cache it.
# ---------------------------------------------------------------------------

_SHOWDOWN_TMPL = "https://play.pokemonshowdown.com/sprites/{folder}/{name}.gif"
_NAME_CACHE = _CACHE_DIR / "_names.json"


def _load_name_cache() -> dict:
    try:
        if _NAME_CACHE.exists():
            return json.loads(_NAME_CACHE.read_text())
    except Exception:
        pass
    return {}


def _save_name_cache(cache: dict) -> None:
    try:
        cache_dir()
        _NAME_CACHE.write_text(json.dumps(cache))
    except Exception:
        pass


def _showdown_name(species_id: int) -> Optional[str]:
    """dex id → Showdown sprite slug (lowercase, alphanumerics only).

    Cached to ~/.pokebot-3ds-sprites/_names.json so we hit PokeAPI at
    most once per species, ever.
    """
    cache = _load_name_cache()
    key = str(species_id)
    if key in cache:
        return cache[key] or None
    url = f"https://pokeapi.co/api/v2/pokemon/{species_id}/"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "pokebot-3ds/0.1"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read())
        raw = data.get("name", "")
    except Exception as e:
        log.debug(f"name lookup failed for #{species_id}: {e}")
        return None
    slug = "".join(c for c in raw.lower() if c.isalnum())
    cache[key] = slug
    _save_name_cache(cache)
    return slug or None


def get_animated_sprite_path(species_id: int,
                             shiny: bool = False) -> Optional[Path]:
    """Return a local animated-GIF path for a species, downloading if
    missing. ``None`` when unavailable (offline / no Showdown sprite),
    so callers fall back to ``get_sprite_path`` (static PNG).
    """
    if not species_id or species_id < 1:
        return None
    fname = f"{species_id}_{'shiny' if shiny else 'normal'}_ani.gif"
    target = cache_dir() / fname
    if target.exists() and target.stat().st_size > 0:
        return target
    name = _showdown_name(species_id)
    if not name:
        return None
    folder = "ani-shiny" if shiny else "ani"
    url = _SHOWDOWN_TMPL.format(folder=folder, name=name)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "pokebot-3ds/0.1"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
        if not data:
            return None
        target.write_bytes(data)
        return target
    except Exception as e:
        log.debug(f"animated sprite download failed #{species_id}: {e}")
        return None


def hidden_power(ivs: dict) -> tuple[str, int]:
    """Compute Hidden Power type + base power from IVs (Gen 2+ formula)."""
    order = ("HP", "Atk", "Def", "Spe", "SpA", "SpD")
    bits_lo = sum(((ivs.get(s, 0) & 1) << i) for i, s in enumerate(order))
    bits_hi = sum((((ivs.get(s, 0) >> 1) & 1) << i) for i, s in enumerate(order))
    type_idx = bits_lo * 15 // 63
    power = bits_hi * 40 // 63 + 30
    types = ("Fighting", "Flying", "Poison", "Ground", "Rock", "Bug",
             "Ghost", "Steel", "Fire", "Water", "Grass", "Electric",
             "Psychic", "Ice", "Dragon", "Dark")
    return types[type_idx], power
