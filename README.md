# Lumux — Philips Hue Sync for Wayland

[![Flathub](https://flathub.org/api/badge?locale=en)](https://flathub.org/apps/io.github.enginkirmaci.lumux)

Real-time ambient lighting synchronization for Philips Hue lights on Linux (Wayland) using a GTK4/Adwaita interface.

**Status:** Active

> **About this fork:** [sjredondo/lumux](https://github.com/sjredondo/lumux) is a fork of
> [enginkirmaci/lumux](https://github.com/enginkirmaci/lumux) with fixes for SteamOS / Arch
> and for streaming reliability. See [What this fork changes](#what-this-fork-changes).

## Features

- Real-time screen capture and color synchronization
- Ambilight-style edge zone layout
- GTK4 + libadwaita native interface
- Configurable resolution scale and FPS for performance tuning
- Support for multiple Hue zones and lights
- RGB ↔ XY color conversion for Hue color accuracy
- Smooth color transitions with configurable smoothing
- Multi-monitor support (subject to compositor portal support)
- System tray integration when available

## Install

### SteamOS / Steam Deck (this fork)

In Desktop Mode, open Konsole and run:

```bash
curl -fsSL https://raw.githubusercontent.com/sjredondo/lumux/main/scripts/install-steamos.sh | bash
```

This installs the latest release's `lumux.flatpak` as a user Flatpak (the GNOME runtime
comes from Flathub). To install a downloaded bundle by hand instead:

```bash
flatpak install --user lumux.flatpak
```

If you previously installed Lumux from Flathub, uninstall it first
(`flatpak uninstall io.github.enginkirmaci.lumux`). Your pairing and settings live in
`~/.var/app/io.github.enginkirmaci.lumux/` and are kept.

SteamOS Desktop Mode runs an X11 session, which has no screen-cast portal; Lumux detects
this and captures the screen through X11 instead (Settings → Capture → Capture Method).
Sync runs in Desktop Mode only; Game Mode (gamescope) isn't supported.

### Flathub (upstream)

```bash
flatpak install flathub io.github.enginkirmaci.lumux
```

Or install from [Flathub](https://flathub.org/apps/io.github.enginkirmaci.lumux)

### From Source

1. Clone the repository:
```bash
git clone https://github.com/enginkirmaci/lumux.git
cd lumux
```

2. Install in editable mode:
```bash
pip install -e .
```

3. Run:
```bash
python -m lumux
# or, if installed:
lumux
```

### Editable Install
```bash
pip install -e .
```

## Requirements

**System:**
- Linux with a Wayland compositor that supports screen-casting portals (xdg-desktop-portal)

**Common packages (example on Fedora):**
- gtk4, libadwaita, xdg-desktop-portal (install via your distro package manager)

**Arch Linux:**
```bash
sudo pacman -S --needed python-gobject python-cairo gtk4 libadwaita gstreamer \
  gst-plugins-base gst-plugins-good gst-plugin-pipewire python-numpy python-pillow \
  python-requests python-zeroconf xdg-desktop-portal
```

**Python:**
- Python 3.10+ recommended
- Dependencies are managed in `pyproject.toml` and installed automatically with `pip install -e .`:
  - PyGObject>=3.42.0
  - numpy>=1.24.0
  - Pillow>=10.0.0
  - requests>=2.28.0
  - urllib3>=1.26.0
  - pydbus>=0.6.0
  - zeroconf>=0.60.0

## Usage

### First Time Setup

1. Start the application:
```bash
python -m lumux
# or
lumux
```

2. Open **Settings**

3. **Bridge Connection**
   - Discover Bridges or enter IP manually
   - Click **Authenticate** and press the link button on your Hue bridge to obtain the app key

4. **Zone Configuration**
   - Configure ambilight zones for edge-based color capture

5. **Sync Settings**
   - Adjust transition time, brightness scale, smoothing, resolution scale and FPS
   - Save settings

### Starting Sync

1. Click **Start Sync**
2. Zone preview shows real-time screen colors
3. Hue lights will follow the captured colors
4. Click **Stop Sync** to stop

## Configuration

- Settings are saved to `~/.config/lumux/settings.json`
- Autostart can be enabled via settings (if supported by the system)

## Performance Tips

- **Resolution scale:** reduce (e.g. 0.125 or 0.0625) to lower CPU usage
- **FPS:** 15–30 is often sufficient
- **Transition time and smoothing:** increase for smoother output

## Troubleshooting

### Wayland Screen Capture Issues
- Ensure xdg-desktop-portal is installed and properly configured on your distro
- Some compositors (or portal backends) may require additional user permissions or settings

### Bridge Connection Issues
- Ensure your device is on the same local network as the Hue bridge
- Verify the bridge is reachable via the bridge web interface
- Temporarily test with firewall disabled if necessary

### High CPU Usage
- Lower FPS and resolution scale in settings

## What this fork changes

- **Streaming stays connected.** The Hue bridge ends an entertainment session after about
  10 seconds without packets. Lumux used to send only when a screen frame arrived, so a
  slow screen-share dialog, a static screen, or no capture at all left the lights dead.
  A sender thread now streams at 30 Hz regardless, and reconnects with backoff if the
  bridge drops the session.
- **Native DTLS.** Streaming uses the system libssl directly (ctypes) instead of piping
  frames through an `openssl s_client` subprocess. The handshake is actually verified,
  errors are reported in the UI, and each frame is exactly one datagram.
- **Works right after pairing.** Pairing or picking an area in Settings previously didn't
  reach the mode manager, so Start failed until Lumux was restarted; re-pairing kept using
  the old keys.
- **Only your entertainment area.** Reading mode (which also runs automatically when sync
  stops) never falls back to every light on the bridge.
- **X11 capture** for X11 sessions such as SteamOS Desktop Mode.
- **Lower resource use.** Frames are pulled at the sync rate instead of a Python callback
  per compositor frame, unchanged frames aren't re-analysed, the UI no longer freezes while
  connecting, and portal setup no longer iterates GTK's main loop from a worker thread.
- **Fixed zone mapping.** The lower half of the left/right screen edges was never mapped to a
  light, and zones sharing a light were averaged unevenly.
- **Tests.** `tests/` runs against a fake Hue bridge (HTTPS + DTLS) and real X11 capture.

### Running the tests

The end-to-end tests need root (ports 443/2100), `openssl` and an X server, so run them in
a container, e.g. Arch:

```bash
docker run --rm -v "$PWD":/src:ro archlinux bash -c '
  pacman -Syu --noconfirm --needed python-gobject python-cairo gtk4 libadwaita gstreamer \
    gst-plugins-base gst-plugins-good python-numpy python-pillow python-requests \
    python-urllib3 python-zeroconf python-pytest python-pip openssl xorg-server-xvfb &&
  pip install --break-system-packages pydbus && cp -r /src /w && cd /w &&
  PYTHONPATH=src:tests xvfb-run -a -s "-screen 0 1280x800x24" python -m pytest -v tests'
```

## Contributing

Contributions welcome. Please open issues and pull requests. If you plan large changes, open an issue first to discuss.

## License

MIT License

## Acknowledgements / Notes

- Works with Hue v2 API library (python-hue-v2)
- Screen capture on Wayland depends on the compositor and the portal implementation
