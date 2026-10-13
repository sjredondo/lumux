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
- Install and update Lumux itself from Game Mode

Once Decky Loader is installed, setup is done entirely in Game Mode.

## Requirements

- SteamOS 3.x (Game Mode) with [Decky Loader](https://decky.xyz)
- An internet connection for the first setup
- A Hue bridge with an **entertainment area** (create it in the Hue app)

The plugin runs the **Lumux Flatpak** (`io.github.enginkirmaci.lumux`). If it's
missing, the plugin installs it for you (see below).

## First-time setup (all in Game Mode)

1. **Install the plugin.** Decky (plug icon) → ⚙ Settings → **Developer** → enable
   **Developer mode** if needed → **Install Plugin from URL** and enter

   ```
   https://github.com/sjredondo/lumux/releases/latest/download/decky-lumux.zip
   ```

   (**Install Plugin from ZIP File** works too, with the zip from the
   [latest release](https://github.com/sjredondo/lumux/releases/latest).)
2. **Install Lumux.** Open the Quick Access menu (`…` button) → decky-lumux →
   **Install Lumux**. The plugin downloads `lumux.flatpak` from the GitHub release that
   matches the plugin, and Flatpak pulls the GNOME runtime from Flathub. The first time
   that's a few hundred MB, so it can take several minutes. The panel shows progress.
   You can close the menu and come back; the install keeps going.
3. **Pair.** Press the link button on the Hue bridge, then select **Pair with Hue bridge**
   within 30 seconds.
4. Pick the entertainment area and switch **Sync lights** on.

If you already use Lumux in Desktop Mode, the plugin picks up that pairing and area
(both share the same `settings.json`), and step 3 is skipped.

## Updating

Each plugin build belongs to one Lumux release. When the installed Flatpak is older,
the panel shows **Update Lumux**. It reinstalls the Flatpak from the matching release
and restarts the service; if the lights were syncing, they resume afterwards.

To update the plugin itself, install `decky-lumux.zip` from the URL again.

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
- The plugin needs no `root` flag and ships no binaries. **Install Lumux** runs, as the
  Deck user:

  ```
  flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  flatpak install --user -y --noninteractive [--reinstall] lumux.flatpak
  ```

  with `lumux.flatpak` downloaded from the release tagged `v<Lumux version>` (set in
  `py_modules/lumux_decky/_version.py`). The latest release is used only if that tag
  has no bundle. The lookup uses the GitHub API without a token (60 requests/hour).
- If Lumux is also installed system-wide (for example the Flathub build from Discover),
  the plugin leaves it alone, installs its own copy for the user and runs that one
  (`flatpak run --user`).
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
`~/homebrew/settings/decky-lumux/settings.json`: `"runner"` is `auto`, `flatpak`,
`host` (a `lumux-daemon` on `PATH`) or `custom` (with `"custom_command"`).

## Troubleshooting

- Daemon log: `~/homebrew/logs/decky-lumux/daemon.log`; plugin log:
  `~/homebrew/logs/decky-lumux/plugin.log`
- Talk to the running daemon from Desktop Mode or SSH:

  ```bash
  flatpak run --command=lumux-daemon io.github.enginkirmaci.lumux --ctl status
  flatpak run --command=lumux-daemon io.github.enginkirmaci.lumux --ctl list_nodes
  ```

- "This Lumux Flatpak has no lumux-daemon": select **Update Lumux** in the panel.
- **Install Lumux** fails: the panel shows flatpak's error. Typical causes are no
  network (check Wi-Fi, then **Try again**), too little storage, or a GitHub rate limit
  (wait a few minutes). The full flatpak output is in `plugin.log`.
- Installing by hand still works (Desktop Mode or SSH), e.g. with
  `scripts/install-steamos.sh`; then select **Check again** in the panel.
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
