"""
Manual control — bot sends NO inputs; you play Azahar normally while
the live party and wild encounters are read and shown in real time.

Detection uses the **authoritative PKMN-NTR address map** (the offset
table the 3DS Pokémon-bot community has used for a decade), NOT the
save block and NOT an object-graph scan:

  - Party  → ``offsets.party_base`` (X/Y 0x08CE1CF8), ``party_stride``
             (484 in live RAM), encrypted ekx, up to 6 contiguous slots.
  - Wild   → a ~128 KB window at ``offsets.foe_base`` (X/Y WildOffset1
             0x08800000). The encounter is NOT at a fixed sub-offset,
             so we scan the window for a checksum-valid PK6 — exactly
             what PKMN-NTR's ReadOpponent does (read 0x1FFFF bytes,
             decrypt, pattern-match).

A cheap unencrypted-header pre-filter (PK6 ``Sanity@0x04 == 0`` and
key ≠ 0 — PKHeX's own ``Valid`` gate) means the 128 KB sweep is fast
enough to run every poll. Records are decoded both as encrypted ekx
and as plaintext, so it works whether live RAM holds the data
encrypted or decrypted.

Shiny is computed by the parser from the record's embedded OT (this is
identical to PKHeX's ``IsShiny`` — the value PKMN-NTR / PCalc use for
the opponent). The human log line also prints OT-TID/SID/PID so a
live test can confirm it's correct for wild mons.
"""
from __future__ import annotations

import logging

from ..parser import calc_checksum, decrypt_pkm, encounter_payload, parse_pkm

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.8
_PARTY_SLOTS = 6
_PK6 = 260                  # bytes we read/parse per record (party PK6)


# ---------------------------------------------------------------------------
# Validation / parse
# ---------------------------------------------------------------------------

def _parse_valid(pt: bytes):
    """Return a ParsedPokemon if ``pt`` (decrypted/plaintext 260 B) is a
    sane record, else None. Checksum + species/level/nature/ability."""
    try:
        if calc_checksum(pt) != int.from_bytes(pt[6:8], "little"):
            return None
        species = int.from_bytes(pt[8:10], "little")
        if not (0 < species <= 721):
            return None
        pkm = parse_pkm(pt)
    except Exception:
        return None
    lvl = pkm.party["level"] if pkm.party else None
    if lvl is None or not (1 <= lvl <= 100):
        return None
    if pkm.nature_id > 24 or pkm.ability_num not in (0, 1, 2, 4):
        return None
    return pkm


def _decode(rec: bytes):
    """Cheap-filter then decode one ``_PK6``-byte candidate as either
    encrypted ekx (decrypt) or already-plaintext. Returns ParsedPokemon
    or None. The pre-filter uses ONLY the unencrypted PK6 header
    (key @0x00, Sanity @0x04 — PKHeX's `Valid => Sanity==0` gate) so
    ~all of RAM is rejected without the costly decrypt."""
    if len(rec) < _PK6:
        return None
    if rec[4] or rec[5]:                       # Sanity != 0 → not a mon
        return None
    if not (rec[0] or rec[1] or rec[2] or rec[3]):
        return None                            # key 0 → empty slot
    for cand in (rec, None):
        try:
            pt = rec if cand is None else decrypt_pkm(rec)
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
    """Up to 6 contiguous party slots. Stops at the first empty/invalid
    slot (the party is contiguous)."""
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


def _scan_foe(ctx, base: int, length: int):
    """Scan [base, base+length) for checksum-valid PK6 records. Yields
    (addr, pkm). Reads in 64 KB chunks overlapping by one record so a
    boundary-straddling encounter isn't missed."""
    CHUNK = 0x10000
    OVER = _PK6
    cur = base
    end = base + length
    while cur < end and not ctx.should_stop():
        n = min(CHUNK, end - cur)
        try:
            blk = ctx.rpc.read(cur, n)
        except Exception:
            cur += n
            continue
        if not blk:
            cur += n
            continue
        for off in range(0, len(blk) - _PK6 + 1, 4):
            # Cheap reject on the unencrypted header before decrypt.
            if blk[off + 4] or blk[off + 5]:
                continue
            if not (blk[off] or blk[off + 1]
                    or blk[off + 2] or blk[off + 3]):
                continue
            pkm = _decode(blk[off:off + _PK6])
            if pkm is not None:
                yield cur + off, pkm
        cur += (n - OVER) if n == CHUNK else n


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _slot_dict(pkm, slot: int) -> dict:
    return {
        "slot":     slot,
        "species":  pkm.species,
        "form":     pkm.form,
        "nickname": pkm.nickname,
        "level":    pkm.party["level"] if pkm.party else None,
        "shiny":    pkm.shiny,
        "nature":   pkm.nature,
        "gender":   pkm.gender,
        "ivs":      pkm.ivs,
        "pid":      pkm.pid,
    }


def _report_encounter(ctx, pkm, addr: int, count: int) -> None:
    lvl = pkm.party["level"] if pkm.party else "?"
    log.info(
        f"WILD @ {addr:#010x}: #{pkm.species} {pkm.nickname or ''} "
        f"Lv{lvl} {'★SHINY★ ' if pkm.shiny else ''}"
        f"PID={pkm.pid:08X} OT-TID={pkm.ot_tid} OT-SID={pkm.ot_sid} "
        f"PSV={pkm.psv} TSV={pkm.tsv}")
    ctx.dashboard.broadcast(
        "encounter", source="wild", address=f"{addr:#010x}",
        count=count, **encounter_payload(pkm))
    if ctx.target and ctx.target.matches(pkm):
        log.info(f"*** TARGET HIT *** {ctx.target.describe(pkm)}")
        ctx.dashboard.broadcast(
            "target_hit", count=count, reason=ctx.target.describe(pkm),
            species=pkm.species, shiny=pkm.shiny,
            nature=pkm.nature, ivs=pkm.ivs)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx) -> None:
    o = ctx.game.offsets
    party_base = o.party_base
    party_stride = o.party_stride or 484
    foe_base = o.foe_base
    foe_len = getattr(o, "foe_scan_len", 0) or 0x20000

    log.info("Mode: manual control (live party + wild detection via "
             "PKMN-NTR address map — bot sends no inputs)")
    log.info(f"  party_base={party_base:#010x} stride={party_stride}  "
             f"foe window=[{foe_base:#010x},"
             f"{foe_base + foe_len:#010x})")
    if not party_base and not foe_base:
        log.error("No party_base/foe_base configured. Set them in "
                  "config.yaml [offsets:] (X/Y: party_base 0x08CE1CF8, "
                  "foe_base 0x08800000).")
        return

    seen: set[int] = set()        # wild enc_keys already reported
    party_keys: set[int] = set()  # party enc_keys (skip in foe scan)
    enc_count = 0
    loop_n = 0

    while not ctx.should_stop():
        loop_n += 1

        # --- Party → Party tab (rebuilt every poll, never accumulates).
        if party_base:
            party = _read_party(ctx, party_base, party_stride)
            party_keys = {p.encryption_key for p in party}
            if party:
                slots = [_slot_dict(p, i) for i, p in enumerate(party)]
                ctx.dashboard.broadcast("party", slots=slots)
                if loop_n == 1 or loop_n % 12 == 0:
                    log.info("  party: " + ", ".join(
                        f"#{p.species}"
                        f"{'★' if p.shiny else ''}"
                        f"Lv{p.party['level'] if p.party else '?'}"
                        for p in party))

        # --- Wild foe → scan window, report each NEW distinct mon.
        if foe_base:
            for addr, pkm in _scan_foe(ctx, foe_base, foe_len):
                ek = pkm.encryption_key
                if ek in seen or ek in party_keys:
                    continue
                seen.add(ek)
                enc_count += 1
                _report_encounter(ctx, pkm, addr, enc_count)

        # Drop stale wild keys so a re-encounter of the same species
        # (new PID) is reported again, but keep memory bounded.
        if len(seen) > 256:
            seen.clear()

        ctx._stop_evt.wait(_POLL_INTERVAL_S)

    log.info("Manual control stopped.")
