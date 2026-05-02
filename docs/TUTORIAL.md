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

## Optional: find the dialog flag

The default soft-reset uses a fixed A-press count to clear Tierno's
dialogue. That's fine on Fast text speed but unreliable on Medium /
Slow. For surgical timing, find the in-memory **dialog flag** — a
byte that flips to non-zero whenever any text box / menu / cutscene
is on screen — and the bot can wait for it to clear instead of
guessing.

How to discover the address:

```
python -m pokebot.find_dialog_flag --save-config config.yaml
```

It walks you through 4 snapshots: stand on the overworld, then talk
to an NPC, then dismiss it, then talk to another. The script diffs
the snapshots and reports a ranked list of candidate addresses. The
top pick is auto-saved to `config.yaml` under `offsets.dialog_flag`.

Once the address is set, the bot uses it for adaptive exit detection
on the receive phase (waits for the flag to drop back to 0 instead
of mashing a fixed number of B presses).

## Resuming after a crash

Saved your hunt count somewhere visible? The bot doesn't persist
state — every run starts at attempt 1. The `Recently Seen` table
also clears on relaunch. If you want to keep historical data, copy
it out of the launcher's log before closing.
