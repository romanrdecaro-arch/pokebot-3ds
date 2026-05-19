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


def _decode(rec: bytes):
    """Decode a record as encrypted ekx (decrypt) or plaintext. Cheap
    pre-filter on the unencrypted header (Sanity@0x04==0, key!=0 —
    PKHeX's own Valid gate) so the window sweep stays fast."""
    if len(rec) < _OPP_PK6:
        return None
    if rec[4] or rec[5]:
        return None
    if not (rec[0] or rec[1] or rec[2] or rec[3]):
        return None
    for plaintext in (False, True):
        try:
            pt = rec if plaintext else decrypt_pkm(
                rec if len(rec) in (232, 260) else rec[:232])
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
        pkm = _decode(rec)
        if pkm is None:
            break
        out.append(pkm)
    return out


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
        pkm = _decode(buf[off:off + _PK6] if off + _PK6 <= len(buf)
                       else buf[off:off + _OPP_PK6])
        if pkm is None or pkm.encryption_key in seen:
            continue
        seen.add(pkm.encryption_key)
        out.append((base + off, pkm))
    return out


def select_wild(valids, party_keys, player_ot):
    """From ``valids`` [(addr, pkm), …] return the wild candidates,
    lowest address first. A WILD opponent has an EMPTY OT name (not
    owned yet); everything the player owns carries OT = player_ot.
    Verified live (Zigzagoon/Bunnelby OT='' vs Fennekin OT='Roman').
    Single source of truth — used by manual mode and the hunt loop.
    """
    out = []
    for a, p in valids:
        if p.encryption_key in party_keys:
            continue
        ot = p.ot_name or ""
        if ot == "" or ot != player_ot:
            out.append((a, p))
    out.sort(key=lambda ap: ap[0])
    return out


def find_wild(ctx, foe_base, foe_len, party_keys, player_ot):
    """Scan the foe window once and return the live wild opponent as
    ``(addr, pkm)`` or None. The hunt loop's detection entry point."""
    buf = _read_window(ctx, foe_base, foe_len)
    wilds = select_wild(_all_valid(buf, foe_base), party_keys, player_ot)
    return wilds[0] if wilds else None


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
            f"{'★ ' if pkm.shiny else ''}PID={pkm.pid:08X} "
            f"OT={pkm.ot_name!r} TID={pkm.ot_tid} SID={pkm.ot_sid}")


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
    foe_len = getattr(o, "foe_scan_len", 0) or 0x8000
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

    seen: set[int] = set()        # wild enc_keys already reported
    party_keys: set[int] = set()
    last_window_sig = None
    enc_count = 0
    loop_n = 0

    while not ctx.should_stop():
        loop_n += 1

        if party_base:
            party = _read_party(ctx, party_base, party_stride)
            party_keys = {p.encryption_key for p in party}
            if party:
                ctx.dashboard.broadcast(
                    "party",
                    slots=[_slot_dict(p, i) for i, p in enumerate(party)])
                if loop_n == 1 or loop_n % 15 == 0:
                    log.info("  party: " + ", ".join(
                        f"#{p.species}{'★' if p.shiny else ''}"
                        f"Lv{p.party['level'] if p.party else '?'}"
                        for p in party))

        if not foe_base:
            ctx._stop_evt.wait(_POLL_INTERVAL_S)
            continue

        buf = _read_window(ctx, foe_base, foe_len)
        valids = _all_valid(buf, foe_base)

        # Wild = the empty-OT non-party record (see select_wild —
        # shared with the hunt loop). The PKMN-NTR pointer anchor does
        # not exist in Azahar; OT is the reliable, session-proof
        # discriminator.
        wilds = select_wild(valids, party_keys, player_ot)

        # Diagnostic dump — only when window contents change.
        sig = frozenset(p.encryption_key for _, p in valids)
        if sig != last_window_sig and valids:
            last_window_sig = sig
            log.info(f"  foe window: {len(valids)} valid PK6, "
                     f"{len(wilds)} wild candidate(s):")
            for a, p in valids:
                if p.encryption_key in party_keys:
                    tag = "PARTY"
                elif (p.ot_name or "") == "" or p.ot_name != player_ot:
                    tag = "WILD?"
                else:
                    tag = "owned/stale"
                log.info(f"    [{tag}] {_desc(p, a)}")
            if not wilds:
                log.info("    [no wild candidate — not in a wild "
                         "battle right now]")

        # Report the wild opponent (lowest-address empty-OT mon).
        if wilds:
            a, p = wilds[0]
            if p.encryption_key not in seen:
                seen.add(p.encryption_key)
                enc_count += 1
                _report_encounter(ctx, p, a, enc_count, "OT-empty")

        if len(seen) > 256:
            seen.clear()

        ctx._stop_evt.wait(_POLL_INTERVAL_S)

    log.info("Manual control stopped.")
