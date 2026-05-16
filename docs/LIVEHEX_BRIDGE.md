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

## One-time: PKHeX-Plugins installed?

You already have it — `D:\PKHeX (26.05.05)\plugins\AutoModPlugins.dll`
(installed earlier via `setup_bleedingedge.ps1`). Launch
`PKHeX.exe`; you should see a **Plugins** / **Auto-Legality Mod**
menu.

## Steps

1. Open Azahar, load Pokémon X/Y, get past the title screen.
2. In the pokebot launcher, pick **LiveHeX bridge (PKHeX)** from the
   method dropdown and press **Start**. The log shows:

   ```
   NTR bridge listening on 127.0.0.1:8000 (emulating process 'kujira-2' …)
   ```

   Or run it standalone: `python -m pokebot.ntr_bridge`

3. Open `PKHeX.exe`, load any X/Y save (so PKHeX knows the format).
4. **Plugins → Auto-Legality Mod → LiveHeX**.
5. Protocol: **NTR**. IP: `127.0.0.1`. Port: `8000`. Click **Connect**.
6. PKHeX reads the live game through the bridge → Azahar.

## What works / caveats

- **Works:** box viewing/editing, trainer block, anything LiveHeX
  reads at its published RAM offsets — the same data path thousands
  of LiveHeX users rely on.
- **Caveat:** the X/Y save block reflects in-game *save* state plus
  area-transition writes; it is not frame-by-frame live. Save (or
  cross an area boundary) in-game, then re-read in PKHeX.
- **Not supported:** real-time wild-encounter capture — a wild
  Pokémon is never written to the save block (you can't save
  mid-battle). That limitation is inherent to the save-block
  approach, not the bridge.

## Ports

`rpc.livehex_host` / `rpc.livehex_port` in `config.yaml` override the
bridge bind address (default `127.0.0.1:8000` — the NTR default
PKHeX-Plugins expects).
