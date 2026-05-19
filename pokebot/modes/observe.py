"""
Manual control — bot sends NO inputs; you play Azahar normally while
the live party and wild encounter are read and shown in real time.

Detection uses the **authoritative PKMN-NTR algorithm** (the method
the 3DS Pokémon-bot community has used for a decade), NOT a "first
valid PK6" scan (that picks up the player's own battler / stale
copies — e.g. reporting your Fennekin instead of the wild Fletchling).

Party  → ``offsets.party_base`` (X/Y 0x08CE1CF8), ``party_stride``
          (484 in live RAM), up to 6 contiguous slots.

Wild   → PKMN-NTR ``HandleOpponentData`` for Gen 6:
  1. Read a ~128 KB window at ``offsets.foe_base`` (X/Y WildOffset1
     0x08800000).
  2. Find the 12-byte ``OpponentPattern`` — three fixed LE pointers
     the battle struct always lays down just before the opponent.
  3. The opponent's encrypted PK6 is exactly ``OpponentOffset`` (637)
     bytes after each match → decrypt 232 bytes.

Azahar relocates the 0x08C7xxxx pointer neighbourhood by ~-0x10, so
the pattern is matched at delta 0 AND -0x10. Every valid PK6 in the
window is also logged (address / species / OT) as a diagnostic so a
single live encounter pins down exactly what's where.
"""
from __future__ import annotations

import logging
import struct

from ..parser import calc_checksum, decrypt_pkm, encounter_payload, parse_pkm

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.8
_PARTY_SLOTS = 6
_PK6 = 260                  # bytes read/parsed per party record
_OPP_PK6 = 232              # PKMN-NTR takes 232 (POKEBYTES) for the opponent

# PKMN-NTR LookupTable (X/Y). OpponentPattern is three fixed LE
# pointers the battle struct always lays down; the opponent's ekx is
# OpponentOffset (637) bytes past the match. The ABSOLUTE values are
# version/relocation-dependent (Azahar shifts these regions by an
# unknown, non-uniform delta — matching literal bytes finds nothing),
# but the GAPS between the three pointers are invariant. So we match
# on the structure: three consecutive heap pointers spaced exactly
# d12 / d23 apart. Relocation-proof and ~zero false positives.
_XY_OPP_P1 = 0x08C67560
_XY_OPP_P2 = 0x08C7A8DC
_XY_OPP_P3 = 0x08C7B6D0
_XY_OPP_D12 = (_XY_OPP_P2 - _XY_OPP_P1) & 0xFFFFFFFF   # 0x1337C
_XY_OPP_D23 = (_XY_OPP_P3 - _XY_OPP_P2) & 0xFFFFFFFF   # 0x00DF4
_XY_OPP_OFFSET = 637
_HEAP_LO, _HEAP_HI = 0x08000000, 0x10000000


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


def _find_opponent(buf: bytes, base: int):
    """PKMN-NTR opponent locate, relocation-proof: find three
    consecutive LE heap pointers spaced d12/d23 apart (the
    OpponentPattern's invariant structure), opponent ekx is +637.
    Returns list of (abs_addr, pkm, ptr_delta)."""
    hits = []
    n = len(buf)
    for i in range(0, n - 12 + 1, 4):
        p1, p2, p3 = struct.unpack_from("<III", buf, i)
        if not (_HEAP_LO <= p1 < _HEAP_HI):
            continue
        if ((p2 - p1) & 0xFFFFFFFF) != _XY_OPP_D12:
            continue
        if ((p3 - p2) & 0xFFFFFFFF) != _XY_OPP_D23:
            continue
        o = i + _XY_OPP_OFFSET
        pkm = _decode(buf[o:o + _OPP_PK6])
        if pkm is not None:
            hits.append((base + o, pkm,
                         (p1 - _XY_OPP_P1) & 0xFFFFFFFF))
    return hits


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


def _desc(pkm, addr: int) -> str:
    lvl = pkm.party["level"] if pkm.party else "?"
    return (f"@{addr:#010x} #{pkm.species} {pkm.nickname or ''} "
            f"Lv{lvl} {pkm.gender} {'★ ' if pkm.shiny else ''}"
            f"PID={pkm.pid:08X} OT={pkm.ot_name!r} "
            f"TID={pkm.ot_tid} SID={pkm.ot_sid}")


def _report_encounter(ctx, pkm, addr: int, count: int, via: str) -> None:
    log.info(f"WILD ({via}) {_desc(pkm, addr)} "
             f"PSV={pkm.psv} TSV={pkm.tsv}")
    ctx.dashboard.broadcast(
        "encounter", source="wild", address=f"{addr:#010x}",
        count=count, **encounter_payload(pkm))
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

    log.info("Mode: manual control (PKMN-NTR opponent locate — bot "
             "sends no inputs)")
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
        opp_hits = _find_opponent(buf, foe_base)
        valids = _all_valid(buf, foe_base)

        # Diagnostic dump — only when the window's contents change
        # (new battle / new mon), so the log stays readable.
        sig = (frozenset(p.encryption_key for _, p in valids),
               frozenset(p.encryption_key for _, p, _ in opp_hits))
        if sig != last_window_sig and (valids or opp_hits):
            last_window_sig = sig
            log.info(f"  foe window: {len(valids)} valid PK6, "
                     f"{len(opp_hits)} pattern-located "
                     f"(anchor Δ d12={_XY_OPP_D12:#x} "
                     f"d23={_XY_OPP_D23:#x}):")
            for a, p in valids:
                tag = ("PARTY" if p.encryption_key in party_keys
                       else "stale/other")
                log.info(f"    [{tag}] {_desc(p, a)}")
            for a, p, d in opp_hits:
                log.info(f"    [OPPONENT ptrΔ={d:#x}] {_desc(p, a)}")
            if not opp_hits:
                log.info("    [no opponent anchor in window — either "
                         "not in a wild battle right now, or the "
                         "anchor lays out differently here; NOT "
                         "reporting a guess]")

        # Report ONLY the PKMN-NTR pattern-located opponent. No
        # "first valid PK6" fallback — that grabbed stale/own mons
        # (the wrong-Pokémon bug). If the anchor isn't found we stay
        # silent rather than emit a guess.
        chosen, via = None, ""
        for a, p, d in opp_hits:
            if p.encryption_key in party_keys:
                continue
            chosen, via = (a, p), f"anchor ptrΔ={d:#x}"
            break

        if chosen is not None:
            a, p = chosen
            if p.encryption_key not in seen:
                seen.add(p.encryption_key)
                enc_count += 1
                _report_encounter(ctx, p, a, enc_count, via)

        if len(seen) > 256:
            seen.clear()

        ctx._stop_evt.wait(_POLL_INTERVAL_S)

    log.info("Manual control stopped.")
