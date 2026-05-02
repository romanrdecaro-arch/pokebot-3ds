# pokebot-3ds

Automation tool for the Generation 6 and Generation 7 mainline Pokémon
games (X, Y, Omega Ruby, Alpha Sapphire, Sun, Moon, Ultra Sun, Ultra
Moon), running on the [Azahar](https://github.com/azahar-emu/azahar)
3DS emulator (the active fork of Citra).

It reads game memory directly over Azahar's UDP RPC, decrypts and
parses PK6/PK7 Pokémon records (shininess, IVs, nature, ability,
moves), and drives the game with simulated keyboard input.

This is a 3DS-era counterpart to
[wyanido/pokebot-nds](https://github.com/wyanido/pokebot-nds), built
from the same architectural ideas (parser → memory access → dashboard)
but adapted to Azahar's UDP RPC instead of an in-emulator Lua console.

> **Disclaimer.** This is a fan project not affiliated with or endorsed
> by Nintendo, Game Freak, or The Pokémon Company. Use it only with
> games and emulator copies you legally own. No ROMs, save files, or
> game assets are distributed with this repository.

## Features

- **Three bot modes:**
  - `observe` — passive read-only; mirrors party + foe to dashboard
  - `encounter` — walks in grass, evaluates each foe vs. target, flees on miss
  - `soft_reset` — for starters / legendaries / gifts (mash A, evaluate, L+R+Start, repeat)
- **Target system** — filter by shininess, IVs, nature, gender, species, or ability; combine rules with AND/OR
- **GUI launcher** ([launcher.py](launcher.py)) — auto-installs deps, picks game/mode, runs offset finder, opens dashboard
- **Live dashboard** ([dashboard/dashboard.html](dashboard/dashboard.html)) — streams encounters over WebSocket; species names, IV color-coding, runtime stats
- **Offset finder** — scans FCRAM for valid PK7 records and reports party + foe addresses
- **PK6/PK7 parser** — full block decryption + shuffle, checksum verification

## Quick start

1. **Install Azahar** and load your Gen 6/7 game. Make sure
   *Emulation → Configure → General → Enable scripting* is on.

2. **Launch the GUI:**
   ```
   python launcher.py
   ```
   (Python 3.10+ recommended. The launcher auto-installs `PyYAML` and
   `pynput` if missing.)

3. **Find your offsets** — with the game running and party loaded,
   click *Find Offsets* in the launcher. Paste the reported addresses
   into [config.yaml](config.yaml) under the `offsets:` section.

4. **Pick a mode**, click *Start Bot*, and click *Open Dashboard* to
   watch encounters live.

## Manual / CLI usage

If you'd rather skip the GUI:

```
pip install -r requirements.txt
python -m pokebot.find_offsets         # one-time per game
# edit config.yaml: paste offsets, set mode + target
python run.py
# open dashboard/dashboard.html in a browser
```

## Architecture

```
                     ┌────────────────┐
                     │   Azahar       │
                     │  (3DS game)    │
                     └────┬───────────┘
                          │ UDP :45987 (read/write memory)
                          ▼
┌──────────────────────────────────────────┐
│              pokebot.bot                 │
│  ┌─────────┐  ┌──────────┐  ┌─────────┐  │
│  │ citra_  │  │ parser   │  │ modes/  │  │
│  │ rpc     │─▶│ (PK6/7)  │─▶│ observe │  │
│  └─────────┘  └──────────┘  │ encntr  │  │
│       ▲                     │ s_reset │  │
│       │ keystrokes          └────┬────┘  │
│  ┌────┴────┐                     │       │
│  │ input_  │◀────────────────────┘       │
│  │ driver  │                             │
│  └─────────┘    ┌──────────────────────┐ │
│                 │   dashboard_server   │─┼──▶ ws://127.0.0.1:8765
│                 └──────────────────────┘ │     ▲
└──────────────────────────────────────────┘     │
                                       dashboard.html in browser
```

## Modes

| Mode         | What it does                                    | Needs offsets             |
|--------------|-------------------------------------------------|---------------------------|
| `observe`    | Passive read-only; reports party + foe changes | `party_base`, `foe_base`  |
| `encounter`  | Walks in grass, evaluates each foe vs. target  | `foe_base`, `in_battle_flag` |
| `soft_reset` | Starters / legendaries / gifts                 | `party_base`              |

## Targets

Build a target from any combination of these rules in `config.yaml`:

- `shiny: true / false`
- `nature: [Adamant, Jolly, ...]`
- `gender: [M, F, G]`
- `species: [25, 133, ...]`        (national-dex IDs)
- `iv_min: {Atk: 31, Spe: 31}`
- `iv_exact: {HP: 31}`
- `iv_sum_min: 150`
- `perfect_iv_count_min: 5`
- `ability_num: [1, 2, 4]`         (4 = hidden)

Combine with `mode: all` (AND) or `mode: any` (OR).

## Filling in offsets

The hardest part of any cross-game memory-reading bot is that
addresses change between games, regions, and patches.

### Easy way: the offset finder

```
python -m pokebot.find_offsets
```

(Or click *Find Offsets* in the launcher.) This scans the heap and
reports every region that decrypts to a valid PK7 record. Six
consecutive valid records spaced by ~484 bytes is your party. A
standalone hit during a battle is the foe.

Paste the reported addresses into `config.yaml`:

```yaml
offsets:
  party_base:     0x330D8B58
  foe_base:       0x330D93D8
  in_battle_flag: 0x330D9438
```

### Manual way

Azahar's *Tools → Memory Viewer* and the
[PKHeX-Plugins LiveHeX](https://github.com/architdate/PKHeX-Plugins)
project both expose the same address space; LiveHeX offset tables work
directly.

For `in_battle_flag`: once `foe_base` is known, watch the bytes around
it during a battle transition; the byte that flips 0→1 entering and
1→0 exiting is your flag.

## Roadmap

- **More modes:** fishing, hatching, SOS chains (Gen 7), Wormhole (USUM), Friend Safari (XY), Hidden Grottos (XY/ORAS)
- **Per-version offset tables** for EUR, JPN, and earlier patch revisions
- **Box reads** — parser already handles 232-byte box records; needs box base address
- **Per-game menu navigation** — encounter mode's "press B to flee" is generic; some games need specific sequences

## Layout

```
pokebot-3ds/
├── README.md
├── LICENSE
├── requirements.txt
├── run.py                    ← CLI entry point
├── launcher.py               ← GUI entry point
├── config.yaml               ← user config
├── pokebot/
│   ├── parser.py             ← PK6/PK7 decrypt + parse
│   ├── citra_rpc.py          ← Azahar UDP client
│   ├── games.py              ← per-game offset registry
│   ├── targets.py            ← filter logic
│   ├── input_driver.py       ← pynput-based keyboard driver
│   ├── dashboard_server.py   ← embedded WebSocket server
│   ├── find_offsets.py       ← memory scanner utility
│   ├── bot.py                ← orchestrator
│   └── modes/
│       ├── observe.py
│       ├── encounter.py
│       └── soft_reset.py
└── dashboard/
    └── dashboard.html        ← single-file UI
```

## Credits & references

- [pokebot-nds](https://github.com/wyanido/pokebot-nds) by wyanido — the architectural template this project follows.
- [pokebot-gen3](https://github.com/40Cakes/pokebot-gen3) by 40Cakes — the inspiration for this project.
- [Azahar](https://github.com/azahar-emu/azahar) — the emulator and the bundled `dist/scripting/citra.py` that this project's RPC client is modeled on.
- [PKHeX](https://github.com/kwsch/PKHeX) — canonical reference for any Pokémon-data-structure question.
- [Project Pokémon](https://projectpokemon.org/) for the Gen 6/7 PKM structure documentation and Gen 7 RAM map.

## License

MIT — see [LICENSE](LICENSE).
