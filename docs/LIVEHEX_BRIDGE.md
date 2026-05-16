# LiveHeX bridge — PKHeX over Azahar

X/Y's live party/foe data is a deep, multi-level C++ object graph in
RAM that can't be parsed in bounded effort. The community-standard
tool, **PKHeX-Plugins LiveHeX**, sidesteps this entirely by reading
the save block over the **NTR debugger protocol** — but NTR is for
real 3DS hardware, and Azahar speaks its own UDP RPC instead.

This bridge makes Azahar look like a 3DS running NTR:

```
PKHeX + PKHeX-Plugins ──NTR / TCP 8000──▶ pokebot bridge ──UDP 45987──▶ Azahar
```

You then get PKHeX's full, battle-tested GUI (box editor, trainer
data, legality, etc.) working against the running Azahar game.

## Use the matched 23.09.25 build (NOT the old PKHeX folder)

PKHeX-Plugins is abandoned (last release Sept 2023, v23.09.25) and
the current PKHeX (26.x) silently rejects it — that's why
`D:\PKHeX (26.05.05)\` shows no Plugins menu (PKHeX 26.5 vs plugin
24.5).

We built a **guaranteed-matched pair** from source:

```
D:\PKHeX-23.09.25\
  PKHeX.exe               (WinForms, built from kwsch/PKHeX tag 23.09.25)
  PKHeX.Core.dll          (23.9.25.0)
  plugins\AutoModPlugins.dll  (built against NuGet PKHeX.Core 23.9.25)
```

PKHeX.Core in the exe and the version the plugin links are the
**same** (23.9.25.0), so the Plugins menu loads. Needs the .NET 7
Desktop runtime (installed). **Launch `D:\PKHeX-23.09.25\PKHeX.exe`**,
not the old 26.05.05 one.

(Rebuild recipe, if ever needed: `dotnet build` PKHeX-Plugins'
`AutoLegalityMod/AutoModPlugins.csproj -c Release`; `dotnet publish`
PKHeX `PKHeX.WinForms.csproj -c Release -r win-x64 --self-contained
false`; copy the `bin/Release/net7.0-windows/win-x64/` output to a
folder and drop `AutoModPlugins.dll` in its `plugins\`.)

## Steps

1. Open Azahar, load Pokémon X/Y, get past the title screen.
2. In the pokebot launcher, pick **LiveHeX bridge (PKHeX)** from the
   method dropdown and press **Start**. The log shows:

   ```
   NTR bridge listening on 127.0.0.1:8000 (emulating process 'kujira-2' …)
   ```

   Or run it standalone: `python -m pokebot.ntr_bridge`

3. Open `D:\PKHeX-23.09.25\PKHeX.exe`, load any X/Y save (so PKHeX
   knows the format). Confirm a **Plugins** menu is present.
4. **Plugins → Auto-Legality Mod → LiveHeX**.
5. Protocol: **NTR**. IP: `127.0.0.1`. Port: `8000`. Click **Connect**.
6. PKHeX reads the live game through the bridge → Azahar.

## Where to find the party: **Box 1**

PKHeX-Plugins LiveHeX **never reads the party for X/Y** — on connect it
only reads box 1 and the trainer block (verified in PKHeX-Plugins
`LiveHexUI.cs` / `LiveHexController.cs`; the party tab stays whatever
your dummy save had). So PKHeX's *Party* tab will **not** reflect the
game.

Instead, the bridge captures the live party from the save block and
**serves it as Box 1**. Open the **box editor** and look at Box 1:
slots 1–6 are your live party (Fennekin, Bunnelby, …) as full PK6 —
sprites, legality, summary, the works. The bridge anchors the party at
`trainer_block + 0x200` (the decrypted X/Y save keeps `PokePartySave`
right after `MyStatus`), with a save-window scan as fallback.

The bridge log shows it on startup, e.g.:

```
Party anchored @ 0x08c79e3c (trainer+0x200); 2 member(s): species [653,659] → Box 1
```

## What works / caveats

- **Works:** trainer block (Trainer Data editor — real TID/SID/name),
  the **live party in Box 1**, and box viewing/editing at LiveHeX's
  published offsets.
- **Caveat:** the save block reflects in-game *save* state plus
  area-transition writes; it is not frame-by-frame. Party in Box 1
  refreshes when you re-Connect (or hit *Read Current Box*) after the
  game writes the save block (on save / area transition).
- **Box 1 is overlaid by the party** while connected — your real PC
  Box 1 isn't shown there (early-game it's empty anyway). Don't *write*
  Box 1 back to the game expecting it to land in the PC.
- **Not supported:** real-time wild-encounter capture — a wild
  Pokémon is never written to the save block (you can't save
  mid-battle). Inherent to the save-block approach, not the bridge.

## Ports

`rpc.livehex_host` / `rpc.livehex_port` in `config.yaml` override the
bridge bind address (default `127.0.0.1:8000` — the NTR default
PKHeX-Plugins expects).
