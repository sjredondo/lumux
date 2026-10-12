#!/usr/bin/env bash
# Install (or update) Lumux from the latest GitHub release as a user Flatpak.
# Works on SteamOS Desktop Mode and any other distro with Flatpak.
set -euo pipefail

REPO="${LUMUX_REPO:-sjredondo/lumux}"
APP_ID="io.github.enginkirmaci.lumux"

url=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
  | grep -o '"browser_download_url": *"[^"]*\.flatpak"' | head -1 | cut -d'"' -f4)
if [ -z "$url" ]; then
  echo "No .flatpak asset found in the latest release of ${REPO}" >&2
  exit 1
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
echo "Downloading ${url}"
curl -fL "$url" -o "$tmp/lumux.flatpak"

# The GNOME runtime comes from Flathub
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo

if flatpak info --system "$APP_ID" >/dev/null 2>&1; then
  echo "Note: the Flathub build of Lumux is also installed system-wide."
  echo "      Remove it to avoid launching the wrong one:"
  echo "      flatpak uninstall --system $APP_ID"
fi

flatpak install --user -y --reinstall "$tmp/lumux.flatpak"
echo "Installed. Launch Lumux from the application menu, or: flatpak run $APP_ID"
