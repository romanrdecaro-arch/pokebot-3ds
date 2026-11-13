"""
Find the RAM byte that flips when a Gen 7 fishing bite (the "!") shows.

Why: Gen 7 writes the wild only once the battle starts, and an A press
before the "!" reels the line in, so the fishing mode needs a bite
signal it does not have. This records the memory most likely to hold
one and finds bytes that change at a moment that differs cast to cast
(the bite is random; the cast animation is not).

Ultra Sun / Ultra Moon only (the player route is USUM's). SAVE IN-GAME
FIRST: Gen 7 reads have crashed Azahar before. Every read here is to a
small object or a module's own data, never a heap sweep.

    python scripts/find_fish_bite.py record          # 8 casts, no hooking
    python scripts/find_fish_bite.py analyze logs/fish_probe_<stamp>.npz
    python scripts/find_fish_bite.py verify 0x3001234 5     # hook on == 5

Stand facing a fishing spot in the overworld with Azahar at 100% speed
(Ctrl+speed limit on): a sample every ~70 ms has to land inside the
bite window, which shrinks as emulation speeds up.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pokebot.azahar_config import load_active_profile_binds   # noqa: E402
from pokebot.citra_rpc import RPCError, wait_for_emulator     # noqa: E402
from pokebot.input_driver import InputDriver, KeyBinds        # noqa: E402
from pokebot.modes.gen7_encounter import (_HEAP, _PLAYER_ROUTE,  # noqa: E402
                                          _RO_HI, _RO_LO, Modules)

ROOT = Path(__file__).resolve().parents[1]
#: USUM code.bin .data + .bss (from the 2026-11-03 static analysis).
CODE_RW = (0x00667000, 0x006D3F04)
#: Bytes of module .data/.bss to record per sample: ~1.5 MB keeps a full
#: sample near 70 ms.
MODULE_BUDGET = 0x180000
MODULE_SEG_CAP = 0x40000


def connect():
    rpc = wait_for_emulator(timeout=10)
    pid, tid, name = rpc.attach_to_pokemon_game()
    print(f"attached to {name} (PID {pid}, TID {tid:#018x})")
    return rpc


def make_input() -> InputDriver:
    binds = load_active_profile_binds() or {}
    for k in ("_source", "_profile", "_unusable"):
        binds.pop(k, None)
    return InputDriver(binds=KeyBinds(**binds))


def tap(inp, button, hold=0.05):
    if inp.tap(button, hold_s=hold) in ("none", "refused"):
        raise SystemExit("the key press did not reach Azahar (is it running, "
                         "on the keyboard input profile?)")


def _heap(p):
    return _HEAP[0] <= p < _HEAP[1]


def regions(rpc, mods) -> list:
    """(label, address, size) of everything recorded."""
    out = [("code.rw", CODE_RW[0], CODE_RW[1] - CODE_RW[0])]
    root, offs, vtable = _PLAYER_ROUTE["USUM-USA-1.2"]
    gm = rpc.read_u32(root)
    if _heap(gm):
        out.append(("GameManager", gm, 0x400))
        fr = rpc.read_u32(gm + offs[0])
        if _heap(fr):
            out.append(("FieldResident", fr, 0x1740))
            mm = rpc.read_u32(fr + offs[1])
            if _heap(mm):
                out.append(("MoveModelMgr", mm, 0x200))
                pl = rpc.read_u32(mm + offs[2])
                if _heap(pl) and rpc.read_u32(pl) == vtable:
                    out.append(("Player", pl, 0x1000))
    if not any(r[0] == "Player" for r in out):
        print("WARNING: the player route did not resolve; recording the rest")
    out += module_segments(rpc, mods)
    return out


def module_segments(rpc, mods) -> list:
    """.data and .bss of the loaded CROs, Field* modules first. After
    loading, a CRO's segment table (+0xC8 offset, +0xCC count; entries
    of offset, size, id with 2 = .data, 3 = .bss) holds addresses."""
    mods.names()                         # finds the CRS
    if not mods.crs:
        return []
    found = []
    m = rpc.read_u32(mods.crs + 0x88)
    for _ in range(64):
        if not m:
            break
        name_ptr, nxt = struct.unpack("<II", rpc.read(m + 0x84, 8))
        name = rpc.read(name_ptr, 32).split(b"\0")[0].decode("ascii", "replace")
        tab, n = struct.unpack("<II", rpc.read(m + 0xC8, 8))
        tab = tab if tab >= _RO_LO else m + tab
        for i in range(min(n, 8)):
            addr, size, sid = struct.unpack("<III", rpc.read(tab + 12 * i, 12))
            ok = (_RO_LO <= addr < _RO_HI or _heap(addr)) and 0 < size
            if sid in (2, 3) and ok:
                found.append((f"{name}.{'data' if sid == 2 else 'bss'}",
                              addr, min(size, MODULE_SEG_CAP)))
        m = nxt
    found.sort(key=lambda r: (not r[0].startswith("Field"), r[2]))
    out, used = [], 0
    for r in found:
        if used + r[2] <= MODULE_BUDGET:
            out.append(r)
            used += r[2]
    skipped = [r[0] for r in found if r not in out]
    if skipped:
        print(f"  over budget, not recorded: {', '.join(skipped)}")
    return out


def snap(rpc, regs) -> np.ndarray:
    return np.frombuffer(b"".join(rpc.read(a, n) for _, a, n in regs), np.uint8)


def in_battle(mods) -> bool:
    try:
        return "Battle" in mods.names()
    except (RPCError, OSError):
        return True


def clear(inp, taps=4):
    for _ in range(taps):
        tap(inp, "B")
        time.sleep(0.4)


# -- record -------------------------------------------------------------
def record(args):
    print("SAVE IN-GAME FIRST if you have not. Ctrl+C stops.")
    rpc, inp = connect(), make_input()
    mods = Modules(rpc)
    if in_battle(mods):
        raise SystemExit("in a battle: finish it, stand facing water, re-run")
    regs = regions(rpc, mods)
    for label, a, n in regs:
        print(f"  {label:<28} {a:#010x} +{n:#x}")
    total = sum(r[2] for r in regs)
    t = time.monotonic()
    snap(rpc, regs)
    print(f"  {total / 1024:.0f} KB per sample, {1000 * (time.monotonic() - t):.0f} ms")

    casts, base = [], None
    try:
        for c in range(args.casts):
            clear(inp)
            time.sleep(1.0)
            times, rows = [], []
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.8:          # before the cast
                rows.append(snap(rpc, regs)); times.append(time.monotonic())
            tap(inp, "A")
            tc = time.monotonic()
            print(f"cast {c + 1}/{args.casts}: rod out, recording "
                  f"{args.wait:.0f}s, not pressing anything")
            battle = False
            while time.monotonic() - tc < args.wait:
                rows.append(snap(rpc, regs)); times.append(time.monotonic())
                if len(rows) % 10 == 0 and in_battle(mods):
                    battle = True                    # should not happen
                    print("  a battle started on its own; stopping. Run "
                          "from it by hand.")
                    break
            arr = np.stack(rows)
            if base is None:
                base = arr[0].copy()
            changed = np.flatnonzero((arr != arr[0]).any(axis=0))
            casts.append({"t": np.array(times) - tc, "cols": changed,
                          "vals": arr[:, changed], "first": arr[0].copy()})
            print(f"  {len(rows)} samples, {len(changed)} bytes changed")
            if battle:
                break
        clear(inp)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        inp.close()
    if not casts:
        return

    union = np.unique(np.concatenate([c["cols"] for c in casts]))
    addr = np.empty(total, np.uint32)
    label_of = np.empty(total, np.int16)
    i = 0
    for k, (_, a, n) in enumerate(regs):
        addr[i:i + n] = np.arange(a, a + n, dtype=np.uint32)
        label_of[i:i + n] = k
        i += n
    save = {"addr": addr[union], "label": label_of[union],
            "labels": np.array(json.dumps([[r[0], r[1]] for r in regs]))}
    for k, c in enumerate(casts):
        full = np.repeat(c["first"][union][None, :], len(c["t"]), axis=0)
        pos = np.searchsorted(union, c["cols"])
        full[:, pos] = c["vals"]
        save[f"t{k}"], save[f"v{k}"] = c["t"], full
    out = ROOT / "logs" / f"fish_probe_{time.strftime('%Y%m%d_%H%M%S')}.npz"
    np.savez_compressed(out, **save)
    print(f"saved {out}")
    analyze_file(out, args.top)


# -- analyze ------------------------------------------------------------
def analyze_file(path, top=25):
    d = np.load(path)
    addr, label = d["addr"], d["label"]
    labels = json.loads(str(d["labels"]))
    n_casts = sum(1 for k in d.files if k.startswith("t"))
    print(f"\n{n_casts} cast(s), {len(addr)} changing bytes")
    scored = []
    for j in range(len(addr)):
        entries, ok = {}, True               # value -> entry times per cast
        for k in range(n_casts):
            t, v = d[f"t{k}"], d[f"v{k}"][:, j]
            ch = np.flatnonzero(v[1:] != v[:-1]) + 1
            if len(ch) > 8:                  # a counter or animation
                ok = False
                break
            for i in ch:
                if t[i] > 0.3:               # after the A press landed
                    entries.setdefault(int(v[i]), {})[k] = float(t[i])
        if not ok:
            continue
        for val, per in entries.items():
            if len(per) < max(2, n_casts // 2):
                continue
            ts = np.array(list(per.values()))
            # the bite: entered in most casts, at a time that varies
            scored.append((len(per), float(ts.std()), j, val, per))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    print(f"\ntop candidates (entered value in N casts, spread of entry time):")
    for n, sd, j, val, per in scored[:top]:
        name, base = labels[label[j]]
        when = " ".join(f"c{k + 1}:{t:.2f}" for k, t in sorted(per.items()))
        print(f"  {int(addr[j]):#010x} {name}+{int(addr[j]) - base:#x} "
              f"-> {val:#04x}  in {n}/{n_casts}  sd {sd:.2f}s  [{when}]")
    if not scored:
        print("  none. Paste this output.")


def analyze(args):
    analyze_file(args.file, args.top)


# -- verify -------------------------------------------------------------
def verify(args):
    """Cast, wait for the byte to read VALUE, press A. A battle means
    that byte is the bite."""
    rpc, inp = connect(), make_input()
    mods = Modules(rpc)
    a, want = int(args.addr, 0), int(args.value, 0)
    hooked = 0
    try:
        for c in range(args.casts):
            clear(inp)
            time.sleep(1.0)
            tap(inp, "A")
            tc = time.monotonic()
            while time.monotonic() - tc < args.wait:
                if rpc.read_u8(a) == want:
                    break
            else:
                print(f"cast {c + 1}: the byte never read {want:#x}")
                continue
            tap(inp, "A")
            dt = time.monotonic() - tc
            end = time.monotonic() + 4
            while time.monotonic() < end and not in_battle(mods):
                time.sleep(0.1)
            if in_battle(mods):
                hooked += 1
                print(f"cast {c + 1}: flipped at {dt:.2f}s -> HOOKED. Run "
                      f"from the battle by hand, then re-run for more.")
                break
            print(f"cast {c + 1}: flipped at {dt:.2f}s, A pressed, no battle")
    finally:
        inp.close()
    print(f"{hooked} hook(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--casts", type=int, default=8)
    r.add_argument("--wait", type=float, default=12.0)
    r.add_argument("--top", type=int, default=25)
    r.set_defaults(fn=record)
    a = sub.add_parser("analyze")
    a.add_argument("file")
    a.add_argument("--top", type=int, default=25)
    a.set_defaults(fn=analyze)
    v = sub.add_parser("verify")
    v.add_argument("addr")
    v.add_argument("value")
    v.add_argument("--casts", type=int, default=5)
    v.add_argument("--wait", type=float, default=12.0)
    v.set_defaults(fn=verify)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
