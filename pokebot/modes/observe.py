"""
Manual control — bot sends NO inputs; you play Azahar normally while
the live party and wild encounter are read and shown in real time.

Detection uses the **authoritative PKMN-NTR algorithm** (the method
the 3DS Pokémon-bot community has used for a decade), NOT a "first
valid PK6" scan (that picks up the player's own battler / stale
copies — e.g. reporting your Fennekin instead of the wild Fletchling).

Party  → ``offsets.party_base`` (X/Y 0x08CE1CF8), ``party_stride``
          (484 in live RAM), up to 6 contiguous slots.

Wild   → scan a ~128 KB window at ``offsets.foe_base`` (X/Y
  WildOffset1 0x08800000) for checksum-valid PK6 records and pick the
  opponent by OT: a wild Pokémon has an EMPTY OT name (not owned yet),
  while everything the player owns carries OT = the trainer name.
  Verified live (Zigzagoon/Bunnelby = OT '' at 0x08803ecc tracked the
  real encounters; the player's Fennekin = OT 'Roman'). PKMN-NTR's
  pointer-anchor approach was tried first but Azahar relocates that
  region non-uniformly (0 anchor hits across every encounter), so the
  OT discriminator is what's used. Species/PID/IVs/shiny decode
  exactly; level is derived from EXP (the box-format record has no
  party level byte). Every valid PK6 is logged on change so the
  picture stays visible.
"""
from __future__ import annotations

import logging

from ..parser import calc_checksum, decrypt_pkm, encounter_payload, parse_pkm

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.4      # tight enough to catch every wild battle
_PARTY_SLOTS = 6
_PK6 = 260                  # bytes read/parsed per party record
_OPP_PK6 = 232              # min bytes needed to decode a record

# NOTE: PKMN-NTR's pointer-anchor approach does NOT work on Azahar —
# it relocates the battle pointer region non-uniformly, so the
# OpponentPattern (literal, -0x10, or even gap-invariant) gets 0 hits
# across every test. Instead the live data gives a reliable
# discriminator: the wild opponent is the valid PK6 with an EMPTY OT
# name; everything the player owns carries OT = the trainer name.


# ---------------------------------------------------------------------------
# Validation / parse
# ---------------------------------------------------------------------------

def _parse_valid(pt: bytes):
    """ParsedPokemon if ``pt`` (decrypted/plaintext, 232 or 260 B) is a
    sane record, else None."""
    try:
        if calc_checksum(pt) != int.from_bytes(pt[6:8], "little"):
            return None
        species = int.from_bytes(pt[8:10], "little")
        if not (0 < species <= 721):
            return None
        pkm = parse_pkm(pt)
    except Exception:
        return None
    if pkm.nature_id > 24 or pkm.ability_num not in (0, 1, 2, 4):
        return None
    lvl = pkm.party["level"] if pkm.party else None
    if lvl is not None and not (1 <= lvl <= 100):
        return None
    return pkm


def _decode(rec: bytes, party: bool = False):
    """Decode a record as encrypted ekx (decrypt) or plaintext. Cheap
    pre-filter on the unencrypted header (Sanity@0x04==0, key!=0 —
    PKHeX's own Valid gate) so the window sweep stays fast.

    ``party=False`` (foe window) decodes 232 bytes — BOX format, no
    party stats. Critical: a wild battle record is box-format, so
    byte 0xEC is NOT a level; parsing 260 made _parse_valid reject
    the encounter whenever that garbage byte fell outside 1..100
    (random per record → "misses some encounters"). ``party=True``
    keeps 260 so the real party slots show their true level.
    """
    n = 260 if party else 232
    if len(rec) < n:
        return None
    if rec[4] or rec[5]:
        return None
    if not (rec[0] or rec[1] or rec[2] or rec[3]):
        return None
    rec = rec[:n]
    for plaintext in (False, True):
        try:
            pt = rec if plaintext else decrypt_pkm(rec)
        except Exception:
            continue
        pkm = _parse_valid(pt)
        if pkm is not None:
            return pkm
    return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _read_party(ctx, base: int, stride: int) -> list:
    out = []
    for i in range(_PARTY_SLOTS):
        try:
            rec = ctx.rpc.read(base + i * stride, _PK6)
        except Exception:
            break
        pkm = _decode(rec, party=True)
        if pkm is None:
            break
        out.append(pkm)
    return out


def _party_slots(party):
    """Launcher 'party' slot dicts. Level from party stats if the
    record has them, else derived from EXP (box-format copies)."""
    out = []
    for i, p in enumerate(party):
        lvl = (p.party["level"] if p.party
               else _level_from_exp(p.exp))
        out.append({
            "slot": i, "species": p.species, "form": p.form,
            "nickname": p.nickname, "level": lvl, "shiny": p.shiny,
            "nature": p.nature, "gender": p.gender,
            "ivs": p.ivs, "pid": p.pid,
        })
    return out


def _party_sig(party):
    return tuple(
        (p.encryption_key,
         (p.party["level"] if p.party else None), p.shiny)
        for p in party)


def broadcast_party(ctx, party):
    """Push the party to the launcher strip ONLY when it changed
    (new mon, level-up, shiny) — so it refreshes the instant a
    battle ends / a catch happens, with no per-poll flicker. Always
    returns the party's encryption keys (used to exclude the
    player's own mons from wild detection)."""
    sig = _party_sig(party)
    if sig != getattr(ctx, "_party_sig", None):
        ctx._party_sig = sig
        if party:
            ctx.dashboard.broadcast("party",
                                    slots=_party_slots(party))
    return {p.encryption_key for p in party}


# Azahar relocates the fixed PartyOffset (like every other address),
# so reading it literally returns nothing. Locate the live party by
# content instead: the player's team are checksum-valid PK6 whose OT
# is the trainer (boxes are empty early game). Scan the PartyOffset
# neighbourhood first, then wider; cache the base on ctx.
_PARTY_SCAN_RANGES = [(0x08C00000, 0x08F00000),
                      (0x08000000, 0x08C00000)]


def _scan_owned(ctx, lo, hi, player_ot):
    """All checksum-valid PK6 in [lo,hi) whose OT == player_ot,
    deduped by key, lowest address first."""
    CH = 0x20000
    seen, out = set(), []
    cur = lo
    while cur < hi and not ctx.should_stop():
        buf = _read_window(ctx, cur, min(CH, hi - cur))
        if not buf:
            cur += CH
            continue
        for a, p in _all_valid(buf, cur):
            if ((p.ot_name or "") == player_ot
                    and p.encryption_key not in seen):
                seen.add(p.encryption_key)
                out.append((a, p))
        cur += CH - _OPP_PK6                  # overlap so none is split
    out.sort(key=lambda ap: ap[0])
    return out


def get_party(ctx, party_base_cfg, party_stride, player_ot):
    """The live party as a list of ParsedPokemon.

    The party is RE-DERIVED from content every call (a stride read
    off a cached base was fragile — when slot 2 wasn't exactly
    base+484 it stopped after the lead, so the strip collapsed to
    just the lead). We cache a tight WINDOW around the owned cluster
    and re-scan only that window each refresh (cheap); a broad scan
    runs once to find it (or again if it moves)."""
    win = getattr(ctx, "_party_win", None)
    if win:
        owned = _scan_owned(ctx, win[0], win[1], player_ot)
        if owned:
            return [p for _, p in owned[:_PARTY_SLOTS]]
        ctx._party_win = None                 # moved → relocate below

    for lo, hi in _PARTY_SCAN_RANGES:
        owned = _scan_owned(ctx, lo, hi, player_ot)
        if owned:
            a0 = owned[0][0]
            a1 = owned[-1][0]
            # Window covers all members + margin so it survives the
            # party shifting a little or gaining/losing a member.
            ctx._party_win = (max(lo, a0 - 0x400),
                              min(hi, a1 + _OPP_PK6 + 0x800))
            log.info(f"  party located @ {a0:#010x}..{a1:#010x} "
                     f"({len(owned)} owned PK6, OT {player_ot!r}); "
                     f"window {ctx._party_win[0]:#x}-"
                     f"{ctx._party_win[1]:#x}")
            return [p for _, p in owned[:_PARTY_SLOTS]]
    return []


def _read_window(ctx, base: int, length: int) -> bytes:
    buf = bytearray()
    CHUNK = 0x10000
    cur = base
    while cur < base + length and not ctx.should_stop():
        n = min(CHUNK, base + length - cur)
        try:
            blk = ctx.rpc.read(cur, n)
        except Exception:
            blk = b""
        if not blk:
            break
        buf += blk
        cur += n
    return bytes(buf)


def _all_valid(buf: bytes, base: int):
    """Every distinct (by enc_key) checksum-valid PK6 in the window —
    diagnostic so we can see what's actually there."""
    seen = set()
    out = []
    for off in range(0, len(buf) - _OPP_PK6 + 1, 4):
        if buf[off + 4] or buf[off + 5]:
            continue
        if not (buf[off] or buf[off + 1] or buf[off + 2] or buf[off + 3]):
            continue
        pkm = _decode(buf[off:off + _OPP_PK6], party=False)
        if pkm is None or pkm.encryption_key in seen:
            continue
        seen.add(pkm.encryption_key)
        out.append((base + off, pkm))
    return out


def scan_nonparty(ctx, foe_base, foe_len, party_keys):
    """All checksum-valid PK6 in the foe window that are NOT party
    members, lowest address first — list of (addr, pkm), deduped by
    key. Every generated Pokémon has a unique encryption key; the
    player's own battle copy keeps its fixed key so it never looks
    new, but a freshly-generated wild ALWAYS introduces a brand-new
    key. So callers detect an encounter by "a key not seen before"
    rather than by address/OT (a stale wild can linger at a lower
    address and mask the real one — that was the missed-encounter
    bug). Single source of truth — manual mode + hunt loop.
    """
    buf = _read_window(ctx, foe_base, foe_len)
    out = [(a, p) for a, p in _all_valid(buf, foe_base)
           if p.encryption_key not in party_keys]
    out.sort(key=lambda ap: ap[0])
    return out


def pick_opponent(cands):
    """Most likely wild among candidates: an empty-OT record (not
    owned yet) wins, else the lowest address. ``cands`` = [(addr,
    pkm), …]. Returns (addr, pkm) or None."""
    if not cands:
        return None
    ordered = sorted(cands, key=lambda ap: ap[0])
    empties = [c for c in ordered if not (c[1].ot_name or "")]
    return (empties or ordered)[0]


def find_wild(ctx, foe_base, foe_len, party_keys, player_ot=None):
    """Best single wild opponent right now, or None (compat shim)."""
    return pick_opponent(
        scan_nonparty(ctx, foe_base, foe_len, party_keys))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _slot_dict(pkm, slot: int) -> dict:
    return {
        "slot": slot, "species": pkm.species, "form": pkm.form,
        "nickname": pkm.nickname,
        "level": pkm.party["level"] if pkm.party else None,
        "shiny": pkm.shiny, "nature": pkm.nature, "gender": pkm.gender,
        "ivs": pkm.ivs, "pid": pkm.pid,
    }


def _level_from_exp(exp: int) -> int:
    """Approx level from EXP. The wild record is box-format (no party
    level byte — reading it gave garbage Lv67/Lv28), so derive it.
    Medium-Fast curve (exp = n³) is exact for most early-route species
    (Zigzagoon, Bunnelby, …) and ±1 for others — fine for display /
    filtering; species+PID+shiny (the shiny-hunt essentials) are exact
    regardless."""
    if exp <= 0:
        return 1
    n = round(exp ** (1.0 / 3.0))
    while n > 1 and n ** 3 > exp:
        n -= 1
    while (n + 1) ** 3 <= exp and n < 100:
        n += 1
    return max(1, min(100, n))


def _desc(pkm, addr: int) -> str:
    return (f"@{addr:#010x} #{pkm.species} {pkm.nickname or ''} "
            f"~Lv{_level_from_exp(pkm.exp)} {pkm.gender} "
            f"{'★ ' if pkm.shiny else ''}key={pkm.encryption_key:08X} "
            f"PID={pkm.pid:08X} OT={pkm.ot_name!r} "
            f"TID={pkm.ot_tid} SID={pkm.ot_sid}")


def _report_encounter(ctx, pkm, addr: int, count: int, via: str) -> None:
    log.info(f"WILD ({via}) {_desc(pkm, addr)} "
             f"PSV={pkm.psv} TSV={pkm.tsv}"
             f"{'  <<< SHINY >>>' if pkm.shiny else ''}")
    payload = encounter_payload(pkm)
    payload["level"] = _level_from_exp(pkm.exp)   # box record → from EXP
    ctx.dashboard.broadcast(
        "encounter", source="wild", address=f"{addr:#010x}",
        count=count, **payload)
    if ctx.target and ctx.target.matches(pkm):
        log.info(f"*** TARGET HIT *** {ctx.target.describe(pkm)}")
        ctx.dashboard.broadcast(
            "target_hit", count=count,
            reason=ctx.target.describe(pkm), species=pkm.species,
            shiny=pkm.shiny, nature=pkm.nature, ivs=pkm.ivs)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx) -> None:
    o = ctx.game.offsets
    party_base = o.party_base
    party_stride = o.party_stride or 484
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000
    player_ot = (ctx.config.get("soft_reset", {}) or {}).get(
        "trainer_name", "Roman")

    log.info("Mode: manual control (live wild detection — bot sends "
             f"no inputs; player OT {player_ot!r})")
    log.info(f"  party_base={party_base:#010x} stride={party_stride}  "
             f"foe window=[{foe_base:#010x},{foe_base + foe_len:#010x})")
    if not party_base and not foe_base:
        log.error("No party_base/foe_base configured (X/Y: party_base "
                  "0x08CE1CF8, foe_base 0x08800000).")
        return

    party = get_party(ctx, party_base, party_stride, player_ot)
    party_keys: set[int] = broadcast_party(ctx, party)

    # Baseline: ignore every non-party PK6 already in the foe window
    # (a wild left over from before the bot started + the player's
    # battle copy). Detection is by NEW encryption key after this —
    # robust to a stale wild lingering at a low address.
    seen: set[int] = set()
    if foe_base:
        for _, p in scan_nonparty(ctx, foe_base, foe_len, party_keys):
            seen.add(p.encryption_key)
        log.info(f"  baseline: {len(seen)} pre-existing non-party "
                 f"PK6 ignored. Watching for new keys…")

    last_window_sig = None
    enc_count = 0
    loop_n = 0

    while not ctx.should_stop():
        loop_n += 1

        # Re-read the party EVERY poll (cheap once the window is
        # cached) so a catch / level-up / faint shows immediately
        # when the battle ends; broadcast only on actual change.
        party = get_party(ctx, party_base, party_stride, player_ot)
        keys = broadcast_party(ctx, party)
        if keys:
            party_keys = keys

        if not foe_base:
            ctx._stop_evt.wait(_POLL_INTERVAL_S)
            continue

        cands = scan_nonparty(ctx, foe_base, foe_len, party_keys)
        new = [(a, p) for a, p in cands
               if p.encryption_key not in seen]

        # Diagnostic dump — only when window contents change.
        sig = frozenset(p.encryption_key for _, p in cands)
        if sig != last_window_sig and cands:
            last_window_sig = sig
            log.info(f"  foe window: {len(cands)} non-party PK6, "
                     f"{len(new)} new:")
            for a, p in cands:
                tag = ("NEW" if p.encryption_key not in seen
                       else "seen/stale")
                log.info(f"    [{tag}] {_desc(p, a)}")

        if new:
            a, p = pick_opponent(new)
            enc_count += 1
            _report_encounter(ctx, p, a, enc_count, "new-key")
            for _, np in new:
                seen.add(np.encryption_key)

        # Bound memory without re-reporting current stale records.
        if len(seen) > 512:
            seen = {p.encryption_key for _, p in cands}

        ctx._stop_evt.wait(_POLL_INTERVAL_S)

    log.info("Manual control stopped.")
