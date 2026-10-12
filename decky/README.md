# Lumux for Decky Loader

Sync your Philips Hue lights to the Steam Deck's screen in **Game Mode**, from the
Quick Access menu. A thin [Decky Loader](https://decky.xyz) front-end for
[Lumux](https://github.com/enginkirmaci/lumux) (this build comes from the
[sjredondo/lumux](https://github.com/sjredondo/lumux) fork).

- Toggle screen sync on/off
- Choose the Hue entertainment area
- Brightness slider
- Status and errors (bridge, streaming, capture)
- Pair with the bridge from Game Mode (link button)

## Requirements

- SteamOS 3.x (Game Mode) with Decky Loader
- The **Lumux Flatpak** from the sjredondo/lumux releases, **v0.8.0 or newer** (the
  first release that contains `lumux-daemon`)
- A Hue bridge with an **entertainment area** (create it in the Hue app)

## First-time setup

1. In **Desktop Mode**, open Konsole and install or update the Lumux Flatpak:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/sjredondo/lumux/main/scripts/install-steamos.sh | bash
   ```

2. Pair the bridge. Either:
   - open Lumux in Desktop Mode, run the bridge wizard and pick your entertainment
     area (the plugin reads the same settings), or
   - in Game Mode, open the plugin, press the bridge's link button and select
     **Pair with Hue bridge** within 30 seconds.
3. Install this plugin (see below), open the Quick Access menu (`…` button) → Lumux,
   pick the entertainment area and switch **Sync lights** on.

## Installing (sideload)

1. Download `lumux-decky.zip` from the
   [latest release](https://github.com/sjredondo/lumux/releases/latest).
2. In Game Mode: Decky (plug icon) → ⚙ Settings → **Developer** → enable
   **Developer mode** if needed.
3. Use **Install Plugin from ZIP File** and pick the zip (for example from
   `~/Downloads`), or **Install Plugin from URL** with the release asset URL.

## How it works

The plugin's Python backend (`main.py`) starts `lumux-daemon` as a separate
process and talks to it over a Unix socket
(`$XDG_RUNTIME_DIR/app/io.github.enginkirmaci.lumux/daemon.sock`, JSON lines).
By default the daemon runs **inside the Lumux Flatpak**:

```
flatpak run --command=lumux-daemon io.github.enginkirmaci.lumux --backend gamescope
```

- Decky's bundled Python cannot load the system's PyGObject/GStreamer, and the
  read-only SteamOS image doesn't ship the GStreamer PipeWire plugin, so the
  daemon can't run inside Decky or on the stock host Python. The Flatpak bundles
  everything it needs and lives in `/home`, so it survives SteamOS updates.
- The plugin needs no `root` flag and ships no binaries.
- In Game Mode, gamescope publishes the composited screen as a PipeWire video
  node named `gamescope`. The daemon reads it directly with `pipewiresrc`, with no
  screen-share portal or consent dialog. GStreamer shrinks each frame to about
  160×100 before colour analysis, so CPU cost stays low.
- Pairing, the selected area and brightness are stored in the Flatpak's
  `settings.json` (`~/.var/app/io.github.enginkirmaci.lumux/config/lumux/`), shared
  with the desktop app.

Advanced options in the panel:

- **Detect capture source** lists the PipeWire video nodes, in case gamescope's
  node has a different name on your SteamOS version.
- **Restart Lumux service** restarts the daemon.

The runner can also be changed in
`~/homebrew/settings/Lumux/settings.json`: `"runner"` is `auto`, `flatpak`,
`host` (a `lumux-daemon` on `PATH`) or `custom` (with `"custom_command"`).

## Troubleshooting

- Daemon log: `~/homebrew/logs/Lumux/daemon.log`; plugin log:
  `~/homebrew/logs/Lumux/plugin.log`
- Talk to the running daemon from Desktop Mode or SSH:

  ```bash
  flatpak run --command=lumux-daemon io.github.enginkirmaci.lumux --ctl status
  flatpak run --command=lumux-daemon io.github.enginkirmaci.lumux --ctl list_nodes
  ```

- "This Lumux Flatpak has no lumux-daemon": update the Flatpak (step 1 above).
- Lights don't follow the screen but status says *Syncing*: run `--ctl list_nodes`.
  If gamescope's node isn't listed, PipeWire isn't letting the Flatpak see it;
  please open an issue with the output and `daemon.log`.

## Building

```bash
pnpm i
pnpm run build          # frontend only (dist/index.js)
decky plugin build .    # full zip, same as the Decky store CI (needs Docker)
```

## License

MIT (see [LICENSE](LICENSE)). Lumux © Engin KIRMACI; plugin scaffolding from the
Decky plugin template (BSD-3-Clause, included in LICENSE).
