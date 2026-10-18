# PKHeX + LiveHeX with Azahar (optional)

**You do not need any of this to hunt.** The bot ships with the RAM
addresses for every supported game and reads them itself. This page is
only for people who also want PKHeX's box and trainer editors working
against the running game.

LiveHeX comes from **PKHeX-Plugins**, and two things about it are not
obvious:

- **It cannot talk to Azahar by itself.** LiveHeX speaks the NTR
  protocol of a real, modded 3DS. The bot includes a bridge that
  translates NTR into Azahar's RPC, and LiveHeX connects to that.
- **It only works with an old PKHeX.** PKHeX-Plugins stopped at version
  **23.09.25** (September 2023). Current PKHeX releases reject that
  plugin silently: no error, just no Plugins menu. You need PKHeX
  23.09.25 to go with it.

---

## Step 1 — Get the plugin

1. Open <https://github.com/architdate/PKHeX-Plugins/releases/tag/23.09.25>.
2. Download **`PKHeX-Plugins.zip`**.
3. Inside there is **one file, `AutoModPlugins.dll`**. That is
   expected. Older versions of this guide listed four DLLs, but the
   other libraries are built into this one now.

## Step 2 — Get PKHeX 23.09.25

PKHeX's official downloads only offer the current version, so build
23.09.25 from source. It takes a few minutes:

1. Install the **.NET 7 SDK** (`winget install Microsoft.DotNet.SDK.7`).
2. Download the source of PKHeX tag 23.09.25:
   <https://github.com/kwsch/PKHeX/archive/refs/tags/23.09.25.zip>, and
   extract it.
3. In the extracted folder, run:

   ```
   dotnet publish PKHeX.WinForms/PKHeX.WinForms.csproj -c Release -r win-x64 --self-contained false
   ```

4. Copy everything in `PKHeX.WinForms/bin/Release/net7.0-windows/win-x64/`
   to a folder of your own, e.g. `C:\PKHeX-23.09.25\`.

## Step 3 — Install the plugin

1. Create a folder named `plugins` next to `PKHeX.exe`, so you have
   `C:\PKHeX-23.09.25\plugins\`.
2. Put `AutoModPlugins.dll` in it.
3. Start `PKHeX.exe`. A **Plugins** menu should be in the menu bar. If
   it is not, the PKHeX version does not match the plugin (see
   Troubleshooting).

## Step 4 — Start the bridge

1. In Azahar, turn on *Emulation → Configure → Debug → Enable RPC
   server*, and load your game past the title screen.
2. In the pokebot launcher, pick **LiveHeX bridge (PKHeX)** as the
   method and press **Start**. The log shows:

   ```
   NTR bridge listening on 127.0.0.1:8000 ...
   ```

   Without the launcher, run `python -m pokebot.ntr_bridge`.

## Step 5 — Connect LiveHeX

1. In PKHeX, open a save file from the same game, so PKHeX knows the
   format.
2. **Plugins → Auto-Legality Mod → LiveHeX**.
3. Protocol **NTR**, IP `127.0.0.1`, port `8000`. Click **Connect**.

Your live party appears in **Box 1**, not in the Party tab (LiveHeX
never reads the party for Gen 6). [LIVEHEX_BRIDGE.md](LIVEHEX_BRIDGE.md)
explains why, and what the bridge can and cannot show.

## Troubleshooting

**No Plugins menu in PKHeX.** The plugin was built for PKHeX
23.09.25 and nothing else. Check *Help → About* (or the title bar) for
the PKHeX version, and make sure the DLL is directly in `plugins\`, not
in a subfolder.

**"I only have one DLL."** That is correct; see Step 1.

**LiveHeX won't connect.** The bridge must be running (Step 4) and
Azahar's RPC server must be on. Restart Azahar with the game loaded,
then start the bridge again.
