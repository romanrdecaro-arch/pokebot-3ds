# Tutorial — Soft-resetting a starter with pokebot-3ds

This walks through hunting a shiny (or otherwise specific) starter
Pokémon from start to finish. The example uses Pokémon X / Y, but
the launcher flow is identical for every supported Gen 6/7 game.

> **Time commitment.** Shiny rates are 1/4096 in Gen 6/7 (≈1/683 with
> the Shiny Charm). Plan for a multi-hour hunt at minimum.

## What you'll need

- [Azahar](https://github.com/azahar-emu/azahar) installed and configured.
- A Gen 6/7 Pokémon ROM you legally own, loaded in Azahar.
- This repo cloned. `python launcher.py` (or double-click
  `pokebot-3ds.bat` on Windows).

## Step 1 — One-time setup in Azahar

Open Azahar and verify:

- *Emulation → Configure → General → Enable scripting* is **on**.
- *Emulation → Configure → Debug* — make sure **GDB stub is OFF**
  (`Use GDB stub` unchecked). With it on, the emulator silently waits
  for a debugger and the bot's RPC requests get ignored.

Boot your game once to the title screen so Azahar registers it.

## Step 2 — Find your offsets (one-time per ROM)

> **Recommended on first setup:** use **PKHeX-Plugins LiveHeX** instead
> of the bot's auto-discovery scan. See
> [LIVEHEX_SETUP.md](LIVEHEX_SETUP.md) for full step-by-step. Once you
> have the address from LiveHeX, paste it into `config.yaml` and skip
> the rest of this section.

### Auto-discovery (alternative, can be heavy on Azahar)

The bot needs to know where in the game's RAM to find the party.
Those addresses change per game / region / patch, so we discover them
once with the offset finder:

1. Open the launcher. The sidebar's `AZAHAR STATUS` line should turn
   green and say *"Detected: X (USA)"* (or whatever game is loaded).
2. Click **🔍 Find Offsets (scan RAM)**. With your party loaded and
   the game on the overworld, this scans memory for buffers that
   decrypt to valid PK7 records. Takes 1–3 minutes.
3. When the scan finishes, the launcher log prints something like:

   ```
   party_base   :  0x330D8B58
   foe_base     :  0x330D93D8
   in_battle_flag: 0x330D9438
   ```
4. Click **⚙ Edit config.yaml** and paste those addresses into the
   `offsets:` block, replacing the `0x0` placeholders. Save the file.

You only have to do this once per ROM.

## Step 3 — Save in the right spot

Walk your character to **one tile south of the Pokéball table** in
Aquacorde Town (the X/Y starter scene). The screenshot in the project
README shows the position: facing north, with the table directly above
you and the lampposts on either side.

**Save the game** before going any further. Every soft-reset returns
you to this exact spot.

> **Why this exact tile?** The bot's input sequence assumes you start
> here. If you save somewhere else, the "press Left → mash A" steps
> won't line up and the cursor won't end up on the right Pokéball.

For the other games supported by the bot, save in front of the
relevant Pokéball / professor:

| Game        | Save position                                              |
|-------------|------------------------------------------------------------|
| **X / Y**   | Aquacorde Town, one tile south of the table                |
| **OR / AS** | In front of Brendan/May's truck (Tierno-style scene)       |
| **S / M**   | Iki Town, in front of Hala's table                          |
| **US / UM** | Same as S/M (Iki Town, Hala's stage)                        |

> Game-specific input sequences for ORAS/SM/USUM aren't fully wired
> yet — the bot will fall back to a generic "mash A" loop for those.
> X/Y has the full sequence implemented and is the recommended hunt.

## Step 4 — Pick what to hunt in the launcher

In the launcher sidebar:

1. **GAME** — auto-filled from Azahar detection. Override only if you
   need to.
2. **METHOD** — set to **Starters**.
3. **STARTER** — pick the one you want to hunt:
   - X / Y: `chespin` / `fennekin` / `froakie`
   - OR / AS: `treecko` / `torchic` / `mudkip`
   - S / M / US / UM: `rowlet` / `litten` / `popplio`
4. **TARGET FILTER** — pick the criteria a candidate must meet to
   count as a hit:
   - `Shiny only` — most common; bot resets until shiny.
   - `Perfect IVs (6×31)` / `5+ perfect IVs` — IV breeders.
   - `Shiny + 4+ perfect IVs` — combine the two.
   - `Any (first match)` — useful for testing the loop.
   - `From config.yaml` — fall back to whatever's in the YAML target
     block.

## Step 5 — Click Start

Press **▶ Start Bot**. The status pill in the header turns green
("running").

What you'll see:

- The **Recently Seen** tab in the right pane fills with each
  candidate the bot evaluates — sprite, level, PID, Shiny Value,
  ability, nature, color-coded IVs, Hidden Power.
- The **Log** tab shows raw bot activity (resets, attempts, errors).
- A shiny hit gets a gold border around the row, a one-line
  `TARGET!` log entry, and the bot stops automatically.

The bot does this on every iteration:

1. Press **DpadLeft** once to face the table.
2. Mash **A** (~30 presses, 0.4s gap) until the starter selection
   menu opens.
3. Move the cursor to the chosen starter:
   - Chespin: 2× **DpadLeft** then 2× **A**
   - Fennekin: 2× **A** (cursor already on it)
   - Froakie: 2× **DpadRight** then 2× **A**
4. Mash **A** until the new Pokémon is in your party.
5. Read party slot 0, decrypt, evaluate against the filter.
6. If it matches: stop and let you take over. Otherwise: send
   **L+R+Start** (soft reset), wait 4 seconds, and repeat.

> **Keep Azahar focused while the bot runs.** Inputs are sent as
> OS-level keystrokes through pynput; if Azahar isn't the active
> window, the keys go nowhere.

## Step 6 — When the bot stops

A target hit looks like this in the Log tab:

```
[INFO] TARGET! attempt 1437: SHINY | Adamant | IVs 175 | 4×31
```

The bot has stopped sending inputs and you're at whatever screen the
game was on when slot 0 finished writing — usually the "What will
you nickname [STARTER]?" prompt. From here it's all manual:
nickname, walk to your house, save.

## Horde mode (Sweet Scent shiny hunting)

Horde battles put **5 wild Pokémon** on the field at once, each rolled
independently — so the effective shiny rate is **~5× a single
encounter**. The bot's `horde` method uses **Sweet Scent** to guarantee
a horde every time, then checks all 5 for a target and flees if none
match.

### What you need in-game

1. **Slot 1 must be a Sweet Scent user.** The easiest pick in X/Y is
   **Bulbasaur**, which Professor Sycamore gives you for free in
   Lumiose City after the first gym. The whole Bulbasaur line learns
   Sweet Scent natively, so no evolution / TM hunting required. Other
   options:
   - **Gloom** (Route 7, Y only) — already knows Sweet Scent in the
     wild, no leveling.
   - **Roselia** (Route 7) — common, learns Sweet Scent by level-up.
2. **Give slot 1 a Smoke Ball.** This held item guarantees escape from
   wild battles regardless of Speed. Without it, fast hordes can
   sometimes refuse the run; with it, every flee succeeds.
3. **Stand on a horde-enabled route.** Routes 1-3 have no horde tables
   — Sweet Scent there just gives a single wild. Route 5 onwards is
   fine. Route 7 (Skiddo / Pikachu hordes), Route 10, and Route 22
   tend to be the most popular shiny-horde spots in X/Y.

### Launcher setup

1. **METHOD** — set to **Horde encounters**.
2. **TARGET FILTER** — usually *Shiny only*; the bot stops on the
   FIRST shiny among the 5 in any horde.
3. Press **▶ Start Bot**.

### What it does each iteration

1. Press **X, A, A, Down, A, A** with 1.5-second intervals — opens
   the menu, picks slot 1, selects Sweet Scent.
2. Waits for the horde intro animation (`sweet_scent_settle`, default
   4s).
3. Scans the foe window — a 5-mon horde drops 5 fresh PK6 records
   with new encryption keys; the bot reports each as its own
   *Recently Seen* row.
4. **Any of the 5 a shiny / target → stop + alert** (battle left on
   screen for you to catch).
5. **None shiny → flee** (B-mash to dismiss appearance text, touch the
   *Run* button; Smoke Ball makes this a guaranteed success).
6. Loop.

### Troubleshooting

- **Sequence opens the wrong menu / picks the wrong slot.** Make sure
  you're standing still in the overworld when you start, not in a
  dialog or sub-menu. The sequence assumes a clean overworld state.
- **"0 new wild" forever.** Either you're on a no-horde route (check
  Serebii's [horde encounters list](https://www.serebii.net/xy/hordeencounters.shtml))
  or slot 1 doesn't know Sweet Scent yet. The bot will spam the menu
  sequence but no horde will spawn.

## Fishing mode

Hunts shiny / target Pokémon on water tiles by casting a fishing rod
and reading the foe window. No screen / bite-cue detection — the bot
just casts, taps A once to try the hook, and recasts on miss.
Imperfect timing means some bites are fumbled, but the loop is
patient and recasts are free.

### What you need in-game

1. **A fishing rod in your bag.** Old Rod is given by the Fisherman
   on Route 4 (Lumiose → Santalune); Good Rod on Route 16; Super Rod
   in Couriway Town. The bot doesn't care which rod is registered —
   it presses Y and uses whatever's bound.
2. **Register the rod to Y.** Open the **Bag** → **Key Items** → pick
   the rod → **Register**. Pressing Y in the overworld now casts it.
3. **Stand facing fishable water.** Any pier / beach / riverbank
   tile works (Couriway Town dock, Cyllage / Ambrette beach, Route
   8 / 16 / 22 banks, etc.).

### Launcher setup

1. **METHOD** — set to **Fishing**.
2. **TARGET FILTER** — pick the criteria for the hit (usually
   *Shiny only*).
3. Press **▶ Start Bot**.

### What it does each iteration

1. Press **Y** — casts the registered rod.
2. **Poll the foe window** every 0.2s for up to the slider's
   timeout (default **5.0s**). A fresh PK6 in the foe window
   coincides with the "!" appearing above the player's head — i.e.
   the bite cue.
3. **Bite detected → press A immediately** to hook (the press is
   guaranteed to land inside the bite window since we just detected
   the start of it). Battle starts.
4. Main loop scans, reports the wild to Recently Seen, evaluates
   against the target. Hit → stop + alert + `.pk6` saved to
   `targets/`. Miss → flee (Smoke Ball recommended) and recast.
5. **Polling timed out → no bite this cast** → 1s breather, recast.

### Troubleshooting

- **Bot casts but never hooks.** The bite-poll timeout is shorter
  than your rod's bite delay. Raise the **Bite-poll timeout** slider
  (or `random_encounters.fish_cast_settle` in `config.yaml`). Super
  Rod can take 6-8s in some spots.
- **Y opens the bag instead of casting.** The rod isn't registered.
  Bag → Key Items → rod → Register.
- **"0 new wild" forever.** You're not facing fishable water, so
  the cast animation never plays — Y just bounces. Walk to a tile
  that lets you cast (Couriway dock, any beach, river edge).

## Tips for reliable hunts

- **Set in-game text speed to FAST.** *Options → Text Speed → Fast.*
  Every press timing in the bot assumes Fast text. On Medium or Slow,
  dialogue will still be scrolling when the bot moves to the next
  step and presses can be eaten or land in the wrong context.
- **Keep Azahar visible** (not minimised). The bot uses
  `PostMessage` so focus isn't strictly required, but Qt occasionally
  drops messages to fully hidden windows. A small visible window is
  fine — you can keep doing other things on top of it.
- **Disable in-game animations you don't need.** *Options → Battle
  Effects → Off* shaves a few seconds off battles in encounter mode.
  Doesn't affect starter sequence.
- **Plug in your laptop / disable sleep.** A multi-hour shiny hunt
  shouldn't be interrupted by power management.
- **Watch the Log tab on the first iteration.** It'll show the
  per-event trace (`candidate`, `offset_scan`, `read_failure`, etc.)
  so you can see exactly when the bot is reading slot 0 and what's
  in it. If `candidate` events never appear, the bot's reads are
  failing — check the offset state.

## Troubleshooting

### "No party seen yet" after several resets

You probably didn't paste the offsets into `config.yaml`. Re-run
**Find Offsets** and double-check that the `party_base` line in the
config has a real hex value (not `0x0`).

### Cursor lands on wrong starter

Two common causes:

- **Save position is wrong.** Verify you're one tile south of the
  table, facing north.
- **Dialogue speed is set to slow.** *Options → Text Speed* should
  be *Fast*. The bot's mash count assumes Fast text.

### Azahar hangs at "Launching…"

Check `Use GDB stub` is unchecked in *Emulation → Configure → Debug*
and that `LLE Applets` is off in *Emulation → Configure → System*.
With either of those on, the emulator deadlocks before booting.

### Bot keeps fleeing instead of catching

That's encounter mode, not soft-reset mode — make sure the **METHOD**
dropdown says *Starters*, not *Wild encounter*.

## Resuming after a crash

Saved your hunt count somewhere visible? The bot doesn't persist
state — every run starts at attempt 1. The `Recently Seen` table
also clears on relaunch. If you want to keep historical data, copy
it out of the launcher's log before closing.
