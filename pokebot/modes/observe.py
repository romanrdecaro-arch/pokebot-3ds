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

Gen 7 (SM / USUM) → the party is searched in a small window at the
  save-block party, never swept for (the 0x30000000 heap is 256 MB),
  and the wild scan covers several windows: PKMN-NTR's WildOffset1/2
  for the opponent and WildOffset3/4 for SOS allies. The game's own
  trainer block is read once at start, which both names the trainer
  and proves the addresses line up on Azahar.
"""
from __future__ import annotations

import logging

from ..games import DEFAULT_OT_NAME, LIVEHEX_REFERENCES
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

#: Highest national-dex number each generation's games can hold. A
#: record claiming more is junk that happened to checksum, so the bound
#: is the game's own: Gen 7 adds 722-807 (Rowlet to Zeraora), and
#: letting those through on a Gen 6 title would only loosen the junk
#: filter for nothing.
_MAX_SPECIES = {6: 721, 7: 807}


def _species_max(ctx) -> int:
    gen = getattr(getattr(ctx, "game", None), "generation", 6)
    return _MAX_SPECIES.get(gen, _MAX_SPECIES[6])


def _parse_valid(pt: bytes, max_species: int = _MAX_SPECIES[6]):
    """ParsedPokemon if ``pt`` (decrypted/plaintext, 232 or 260 B) is a
    sane record, else None."""
    try:
        if calc_checksum(pt) != int.from_bytes(pt[6:8], "little"):
            return None
        species = int.from_bytes(pt[8:10], "little")
        if not (0 < species <= max_species):
            return None
        pkm = parse_pkm(pt)
    except Exception:
        return None
    if pkm.nature_id > 24 or pkm.ability_num not in (0, 1, 2, 4):
        return None
    # PID 0 is a half-built scratch record, not a Pokemon -- and it is
    # the one kind of junk that reads as SHINY. The formula is
    # (PSV ^ TSV) < 16; PID 0 makes PSV 0, and a record with blank OT
    # fields has a TSV under 16, so it "matches". A Noibat run logged
    # the same species-65 / Ice Body / all-zero-IV record 13 times
    # this way. Its near-empty body still sums to a valid checksum, so
    # nothing above catches it. A real PID of 0 is 1 in 4 billion.
    if pkm.pid == 0:
        return None
    lvl = pkm.party["level"] if pkm.party else None
    if lvl is not None and not (1 <= lvl <= 100):
        return None
    return pkm


def _decode(rec: bytes, party: bool = False,
            max_species: int = _MAX_SPECIES[6]):
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
        pkm = _parse_valid(pt, max_species)
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


# Fast (no broad-scan) party check used while the bot is in an input
# sequence — we want a near-zero-cost poll after each button press,
# not the multi-second broad scan get_party would do on a miss. Reads
# only the cached window (if get_party has already located the party)
# or a 12 KB default window near the X/Y PartyOffset; returns [] if
# nothing is there. Caller falls back to get_party for the slow case.
_PARTY_FAST_WINDOW = (0x08CE0000, 0x08CE3000)


def quick_get_party(ctx, player_ot):
    """Fast party check — scans ONLY the cached or default tight
    window. Never broad-scans. Returns [] when the party isn't there
    yet (so the caller can keep going)."""
    win = getattr(ctx, "_party_win", None) or _PARTY_FAST_WINDOW
    owned = _scan_owned(ctx, win[0], win[1], player_ot)
    return _refine_party(ctx, owned) if owned else []


# Azahar relocates the fixed PartyOffset (like every other address),
# so reading it literally returns nothing. Locate the live party by
# content instead: the player's team are checksum-valid PK6 whose OT
# is the trainer (boxes are empty early game). Scan the PartyOffset
# neighbourhood first, then wider; cache the base on ctx.
_PARTY_SCAN_RANGES = [(0x08C00000, 0x08F00000),
                      (0x08000000, 0x08C00000)]

#: How far either side of a configured party_base to look, for a game
#: whose party is outside the ranges above.
#:
#: Gen 7 keeps its save block in the 0x30000000 linear heap: 256 MB, a
#: quarter of a million 1 KB reads to sweep. So it is searched near the
#: address the community tools give and nowhere else. A window rather
#: than a single read because Azahar shifts addresses a little (X/Y's
#: trainer block reads 0x10 low); narrow enough on the high side to
#: stop short of box 1, which sits ~0x3000 above the party.
_PARTY_HINT_SPAN = 0x2000


def _party_ranges(party_base_cfg) -> list[tuple[int, int]]:
    """Where get_party looks: the Gen 6 ranges, or -- for a party_base
    outside them -- a window around that address."""
    if party_base_cfg and not any(lo <= party_base_cfg < hi
                                  for lo, hi in _PARTY_SCAN_RANGES):
        return [(party_base_cfg - _PARTY_HINT_SPAN,
                 party_base_cfg + _PARTY_HINT_SPAN)]
    return list(_PARTY_SCAN_RANGES)


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
        for a, p in _all_valid(buf, cur, _species_max(ctx)):
            if ((p.ot_name or "") == player_ot
                    and p.encryption_key not in seen):
                seen.add(p.encryption_key)
                out.append((a, p))
        cur += CH - _OPP_PK6                  # overlap so none is split
    out.sort(key=lambda ap: ap[0])
    return out


_PARTY_SLOT_STRIDE = 260
_PARTY_CONTIG_TOL = 8   # ±8 bytes per slot wiggle (alignment / padding)


def _filter_contiguous_party(owned):
    """Drop entries that aren't part of the contiguous save-block party.

    ``_scan_owned`` returns EVERY OT-matching PK6 in the scan window.
    The save-block party is six (or fewer) records at a fixed
    ``_PARTY_SLOT_STRIDE``-byte stride, but the same scan window also
    catches:

      - stale battle-buffer copies left over after the player moved a
        mon to the PC (PK6 bytes aren't zeroed; the game just stops
        referencing them);
      - day-care / box-1 records when their region overlaps the scan
        range.

    Both manifest as "extra slots" beyond the live party — the user
    sees a 6-slot strip when their party is 5. To kill the ghosts we
    anchor on the lowest-address record (always a real party slot
    when present) and keep only siblings sitting at
    ``slot0 + N*stride`` (±tolerance for occasional padding).
    """
    if not owned:
        return owned
    owned = sorted(owned, key=lambda ap: ap[0])
    base = owned[0][0]
    kept = []
    for addr, pkm in owned:
        idx = (addr - base) // _PARTY_SLOT_STRIDE
        if idx >= _PARTY_SLOTS:
            continue
        expect = base + idx * _PARTY_SLOT_STRIDE
        if abs(addr - expect) <= _PARTY_CONTIG_TOL:
            kept.append((addr, pkm))
    return kept


def _refine_party(ctx, owned, *, contiguous: bool = True):
    """Re-read each located party member as a 260-byte PARTY record so
    parse_pkm fills .party (level, current stats). Works for the
    in-battle / overworld party-copy slot (the normal hunt case);
    the soft-reset starter case is special because right after the
    receive cutscene the live slot is in a transitional state where
    byte 0xEC isn't the level yet — soft_reset overrides level to 5
    (starters are always Lv5).

    Each returned record carries ``source_address`` (the RAM address
    of its slot) so callers — notably the .pk6 exporter — can re-read
    the raw bytes to save a target hit.

    ``contiguous=True`` (the default, used for DISPLAY) drops
    off-grid records — stale battle-buffer ghosts left behind after a
    move-to-PC. ``contiguous=False`` (used for DETECTION) returns
    every OT-matching PK6 in the scan window; a freshly-received
    gift mon may live in the live battle buffer at an address that's
    off-grid from the save-block party cluster (Lucario from
    Korrina, for instance), and the contiguity filter would hide it.
    """
    if contiguous:
        owned = _filter_contiguous_party(owned)[:_PARTY_SLOTS]
    # contiguous=False: keep EVERY owned PK6 in the scan window —
    # box mons, live-party-buffer copies, the lot — so detection
    # callers can find new keys regardless of which RAM region the
    # gift mon landed in. The display path is the one that needs
    # the 6-slot cap.
    out = []
    for addr, pkm in owned:
        try:
            rec = ctx.rpc.read(addr, _PK6)
            pp = _decode(rec, party=True, max_species=_species_max(ctx))
        except Exception:
            pp = None
        result = pp if (pp is not None and pp.party) else pkm
        result.source_address = addr
        out.append(result)
    return out


def get_party(ctx, party_base_cfg, party_stride, player_ot,
              *, contiguous: bool = True):
    """The live party as a list of ParsedPokemon.

    The party is RE-DERIVED from content every call (a stride read
    off a cached base was fragile — when slot 2 wasn't exactly
    base+484 it stopped after the lead, so the strip collapsed to
    just the lead). We cache a tight WINDOW around the owned cluster
    and re-scan only that window each refresh (cheap); a broad scan
    runs once to find it (or again if it moves).

    ``contiguous=True`` (default) keeps only the save-block party
    cluster (slot-N-at-base+N*260) and drops stale battle-buffer
    ghosts. Use ``contiguous=False`` in soft-reset detection paths
    so a fresh gift mon (Lucario from Korrina) doesn't get hidden
    when the game writes it to the live buffer first."""
    win = getattr(ctx, "_party_win", None)
    if win:
        owned = _scan_owned(ctx, win[0], win[1], player_ot)
        if owned:
            return _refine_party(ctx, owned, contiguous=contiguous)
        ctx._party_win = None                 # moved → relocate below

    for lo, hi in _party_ranges(party_base_cfg):
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
            return _refine_party(ctx, owned, contiguous=contiguous)
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


def _all_valid(buf: bytes, base: int,
               max_species: int = _MAX_SPECIES[6]):
    """Every distinct (by enc_key) checksum-valid PK6 in the window —
    diagnostic so we can see what's actually there."""
    seen = set()
    out = []
    for off in range(0, len(buf) - _OPP_PK6 + 1, 4):
        if buf[off + 4] or buf[off + 5]:
            continue
        if not (buf[off] or buf[off + 1] or buf[off + 2] or buf[off + 3]):
            continue
        pkm = _decode(buf[off:off + _OPP_PK6], party=False,
                      max_species=max_species)
        if pkm is None or pkm.encryption_key in seen:
            continue
        seen.add(pkm.encryption_key)
        out.append((base + off, pkm))
    return out


def read_pk6_at(ctx, addr: int):
    """Decode the single record at ``addr``, or None.

    232 bytes is one RPC round trip, against the 128 a full foe-window
    scan costs. That difference is the whole reason a caller can poll
    fast enough to react inside a bite window.
    """
    try:
        buf = ctx.rpc.read(addr, _OPP_PK6)
    except Exception:
        return None
    if not buf or len(buf) < _OPP_PK6:
        return None
    return _decode(bytes(buf), party=False, max_species=_species_max(ctx))


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
    out = [(a, p) for a, p in _all_valid(buf, foe_base, _species_max(ctx))
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


def _report_encounter(ctx, pkm, addr: int, count: int, via: str):
    """Log/broadcast an encounter, saving a .pk6 for a hit.

    Returns the saved path (or None). For a WILD hit that path is the
    pre-capture record, which PKHeX will reject — a wild Pokémon has
    no OT, ball, version or met data until it is caught. Callers that
    go on to catch it should re-export with
    ``pk6_export.save_caught_pk6`` and pass this path as
    ``supersedes``.
    """
    log.info(f"WILD ({via}) {_desc(pkm, addr)} "
             f"PSV={pkm.psv} TSV={pkm.tsv}"
             f"{'  <<< SHINY >>>' if pkm.shiny else ''}")
    payload = encounter_payload(pkm)
    payload["level"] = _level_from_exp(pkm.exp)   # box record → from EXP
    ctx.dashboard.broadcast(
        "encounter", source="wild", address=f"{addr:#010x}",
        count=count, **payload)
    is_target_hit = bool(ctx.target and ctx.target.matches(pkm))
    saved = None
    if pkm.shiny or is_target_hit:
        from ..pk6_export import save_target_pk6
        saved = save_target_pk6(ctx, addr, pkm,
                                "shiny" if pkm.shiny else "wild")
    if is_target_hit:
        log.info(f"*** TARGET HIT *** {ctx.target.describe(pkm)}")
        ctx.dashboard.broadcast(
            "target_hit", count=count,
            reason=ctx.target.describe(pkm), species=pkm.species,
            shiny=pkm.shiny, nature=pkm.nature, ivs=pkm.ivs)
    return saved


# ---------------------------------------------------------------------------
# Windows, and Gen 7's trainer block
# ---------------------------------------------------------------------------

def foe_windows(o) -> list[tuple[int, int]]:
    """Every (base, length) the wild scan covers.

    The main window plus any extras the game declares. Gen 7 keeps its
    SOS ally in a different part of memory from the opponent, so one
    window would see one or the other, never both.
    """
    out = []
    if o.foe_base:
        out.append((o.foe_base, getattr(o, "foe_scan_len", 0) or 0x20000))
    for base, length in getattr(o, "foe_extra", ()) or ():
        out.append((int(base), int(length)))
    return out


def scan_windows(ctx, windows, party_keys):
    """scan_nonparty across several windows: (addr, pkm), lowest
    address first, one entry per encryption key."""
    keys, out = set(), []
    for base, length in windows:
        for a, p in scan_nonparty(ctx, base, length, party_keys):
            if p.encryption_key not in keys:
                keys.add(p.encryption_key)
                out.append((a, p))
    out.sort(key=lambda ap: ap[0])
    return out


#: MyStatus7, the Gen 7 trainer block (PKHeX MyStatus7.cs): TID16 at
#: 0x00, SID16 at 0x02, the OT name at 0x38 as up to 13 UTF-16 chars.
_MYSTATUS7_LEN = 0xC0
_MYSTATUS7_OT = slice(0x38, 0x38 + 0x1A)


def gen7_trainer_ot(ctx) -> str | None:
    """The OT name in a Gen 7 game's own trainer block, or None.

    One 0xC0-byte read at the address PKHeX-Plugins' LiveHeX uses. It
    is the quickest proof that the Gen 7 addresses -- real-hardware
    ones -- line up on Azahar, and it names the trainer the party scan
    needs whatever config.yaml says.
    """
    game = getattr(ctx, "game", None)
    if getattr(game, "generation", 0) != 7:
        return None
    ref = LIVEHEX_REFERENCES.get(getattr(game, "key", "")) or {}
    tb = ref.get("trainer_block")
    if not tb:
        return None
    try:
        raw = bytes(ctx.rpc.read(tb, _MYSTATUS7_LEN))
    except Exception as exc:
        log.warning(f"  Gen 7 trainer block @{tb:#010x} unreadable: {exc}")
        return None
    ot = raw[_MYSTATUS7_OT].decode("utf-16-le", errors="replace")
    ot = ot.split("\x00", 1)[0]
    if not ot.strip() or "\ufffd" in ot:
        log.warning(f"  Gen 7 trainer block @{tb:#010x} holds no readable "
                    f"name. The Gen 7 addresses come from real hardware; "
                    f"if Azahar keeps this game's memory elsewhere, the "
                    f"party and wilds will not be found either. Paste "
                    f"this log.")
        return None
    tid, sid = (int.from_bytes(raw[i:i + 2], "little") for i in (0, 2))
    # What the trainer card shows: the low six digits of the 32-bit ID.
    card_id = ((sid << 16) | tid) % 1_000_000
    log.info(f"  trainer block @{tb:#010x}: OT {ot!r}, "
             f"ID No. {card_id:06d}")
    return ot


def _first_party(ctx, party_base, party_stride, player_ot):
    """The party at startup, and the OT it was found under.

    A Gen 7 game can name its trainer itself: when config.yaml's name
    finds nothing and the trainer block holds a different one, that is
    tried before giving up. Either way the search is retried every poll.
    """
    game_ot = gen7_trainer_ot(ctx)
    party = get_party(ctx, party_base, party_stride, player_ot)
    if not party and game_ot and game_ot != player_ot:
        log.warning(f"  no party under OT {player_ot!r}; the game's own "
                    f"trainer block says {game_ot!r}, so trying that. Set "
                    f"soft_reset.trainer_name to it to skip this step.")
        party = get_party(ctx, party_base, party_stride, game_ot)
        if party:
            player_ot = game_ot
    if not party:
        where = ", ".join(f"{lo:#010x}-{hi:#010x}"
                          for lo, hi in _party_ranges(party_base))
        log.warning(f"  no party found yet (OT {player_ot!r}; searched "
                    f"{where}). Retried every poll; wild detection runs "
                    f"either way.")
    return party, player_ot


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(ctx) -> None:
    o = ctx.game.offsets
    party_base = o.party_base
    party_stride = o.party_stride or 484
    windows = foe_windows(o)
    player_ot = (ctx.config.get("soft_reset", {}) or {}).get(
        "trainer_name", DEFAULT_OT_NAME)

    from ..pk6_export import ensure_targets_dir
    ensure_targets_dir()                    # targets/ shows up now
    log.info("Mode: manual control (live wild detection — bot sends "
             f"no inputs; player OT {player_ot!r})")
    log.info(f"  party_base={party_base:#010x} stride={party_stride}  "
             f"foe window(s) " + ", ".join(
                 f"[{b:#010x},{b + n:#010x})" for b, n in windows))
    if not party_base and not windows:
        log.error("No party_base/foe_base configured (X/Y: party_base "
                  "0x08CE1CF8, foe_base 0x08800000).")
        return

    party, player_ot = _first_party(ctx, party_base, party_stride,
                                    player_ot)
    party_keys: set[int] = broadcast_party(ctx, party)

    def wilds():
        """Non-party records the player does not own. A wild has no OT
        until it is caught, so a record carrying the player's is their
        own battle copy -- whether or not the party read found it."""
        return [(a, p) for a, p in scan_windows(ctx, windows, party_keys)
                if (p.ot_name or "") != player_ot]

    # Baseline: ignore every non-party PK6 already in the foe window
    # (a wild left over from before the bot started + the player's
    # battle copy). Detection is by NEW encryption key after this —
    # robust to a stale wild lingering at a low address.
    seen: set[int] = set()
    if windows:
        for _, p in wilds():
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

        if not windows:
            ctx._stop_evt.wait(_POLL_INTERVAL_S)
            continue

        cands = wilds()
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
            # Horde-aware: a horde battle puts 5 unseen non-party PK6
            # in the foe window at once. Report EVERY one (5 rows in
            # Recently Seen) instead of picking just the lead — gives
            # the full picture and lets the launcher's target_hit
            # broadcast fire on ANY shiny in the horde.
            for a, p in sorted(new, key=lambda ap: ap[0]):
                seen.add(p.encryption_key)
                enc_count += 1
                _report_encounter(ctx, p, a, enc_count, "new-key")

        # Bound memory without re-reporting current stale records. Only
        # rebuild from the live window when it's non-empty — rebuilding
        # from an empty `cands` would wipe `seen` entirely and then
        # re-report a wild still lingering in the foe buffer as if it
        # were brand new.
        if len(seen) > 512 and cands:
            seen = {p.encryption_key for _, p in cands}

        ctx._stop_evt.wait(_POLL_INTERVAL_S)

    log.info("Manual control stopped.")
