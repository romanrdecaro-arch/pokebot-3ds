# Setting up PKHeX + LiveHeX with Azahar

The bot needs to know the in-memory address of your party block
(`party_base`). The most reliable way to find it is **PKHeX-Plugins'
LiveHeX feature** — a community-maintained extension to PKHeX that
attaches to a running Citra/Azahar session and reads/writes your
live party Pokémon data directly. Once LiveHeX works, the address
it uses is the address the bot needs.

This is a one-time setup. After you find the address and paste it
into `config.yaml`, the bot reads from that address directly and
never needs to scan again.

---

## Step 1 — Install PKHeX

If you already opened your save file in PKHeX earlier, you have it
installed. If not:

1. Download the latest release of PKHeX from
   <https://github.com/kwsch/PKHeX/releases>
2. Extract `PKHeX.exe` somewhere stable (e.g. `C:\PKHeX\`).
3. Launch it once to confirm it starts.

## Step 2 — Install PKHeX-Plugins

PKHeX-Plugins adds the LiveHeX feature on top of vanilla PKHeX.

1. Go to <https://github.com/architdate/PKHeX-Plugins/releases>
2. Download the latest **`PKHeX-Plugins.zip`** (matched to your
   PKHeX version — usually the very latest works fine).
3. Inside the zip you'll find:
   - `AutoModPlugins.dll`
   - `PKHeX.Core.AutoMod.dll`
   - `PKHeX.Core.Injection.dll`
   - `PKHeX.Core.Enhancements.dll`
4. **Create a folder named `plugins`** in the same directory as
   `PKHeX.exe` (so you have `C:\PKHeX\plugins\`).
5. Drop all four DLLs into that `plugins\` folder.
6. Re-launch PKHeX. A new menu item — **Tools → "Auto-Legality Mod"**
   (or similar) — should appear, confirming the plugins loaded.

## Step 3 — Configure Azahar for live RPC

Make sure scripting is enabled (this is what both LiveHeX and the
bot use):

1. In Azahar, **Emulation → Configure → General**.
2. Tick **Enable scripting** if it isn't already.
3. Click OK and load your Pokémon X/Y save.
4. Walk to the overworld with at least one Pokémon in slot 0
   (e.g. Fennekin if you've already picked the starter).

## Step 4 — Connect LiveHeX

1. In PKHeX, go to **Tools → "Open Live HeX"** (the menu name varies
   slightly between plugin versions).
2. A connection dialog appears. Pick **Citra / Azahar** as the
   connection type.
3. Default host `127.0.0.1` and port `45987` — leave as is.
4. Click **Connect**.
5. PKHeX should populate its party / box display with your live
   game state. Walk around in-game; the displayed party updates
   live. **You're done with PKHeX setup.**

## Step 5 — Find the address LiveHeX is using

LiveHeX hardcodes the per-game party address. To extract it:

### Easy path — read the plugin source

PKHeX-Plugins is open-source. The address table for Pokémon X/Y
lives in:

<https://github.com/architdate/PKHeX-Plugins/blob/master/PKHeX.Core.Injection/BotController/Helpers>

Look for a file named `LPBasic.cs`, `LPPointer.cs`, or
`LiveHeXVersion.cs`. There will be entries like:

```csharp
case LiveHeXVersion.X_v144:
    pkmoffset = 0x8C861E8;   // example — actual value will differ
    boxoffset = 0x...
```

(The exact values are version-specific. Open the file in your
browser and search for "X" / "Y" / "GenVI".)

Whatever address is listed for your X/Y version is the one to
paste into the bot's `config.yaml`.

### Verify it with the bot

From a terminal in the project folder, with Azahar still running
your Y save:

```
python run.py --mode debug --verify-address 0x<address>
```

Examples:

```
python run.py --mode debug --verify-address 0x8C861E8
```

If the address is correct you'll see:

```
✓ Valid PK6: species=#653 Lv5 shiny=False OT='Roman'
Saved party_base = 0x08C861E8 to config.yaml.
```

If wrong, the read either fails or doesn't validate as a PK6.
Try the next candidate from the source file.

## Step 6 — Run the bot normally

With `party_base` written to `config.yaml`, all subsequent bot
runs (Starters / Manual / etc.) read directly from that address.
No discovery scans, no Azahar log spam.

If Azahar restarts and ASLR moves things, the saved address may
become stale. Run `--verify-address` again with the new value
from the next LiveHeX session.

## Troubleshooting

**LiveHeX connects but party is empty / garbage**

The plugin's hardcoded address is for a different X/Y version
(USA vs JPN, v1.4 vs v1.5, etc.). Check the plugin's version
selector and pick "X (USA, latest)" or whichever matches your
ROM.

**LiveHeX won't connect**

Make sure Azahar is foregrounded with the game running. The
scripting RPC binds on game start. If still failing, restart
Azahar with the game loaded fresh.

**PKHeX plugins don't load**

Confirm the DLLs are in `<PKHeX-folder>\plugins\` (not in some
sub-folder). Confirm the plugin version matches your PKHeX
version — mismatches usually cause silent loading failures.
