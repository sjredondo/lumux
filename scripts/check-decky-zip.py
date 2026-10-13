#!/usr/bin/env python3
"""Sanity-check a Decky plugin zip: store layout, metadata, no junk.

usage: check-decky-zip.py decky-lumux.zip [--lumux-version X.Y.Z]

With --lumux-version, also checks that the plugin installs that Lumux
release (py_modules/lumux_decky/_version.py).
"""

import argparse
import json
import re
import sys
import zipfile

REQUIRED = [
    "plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js",
    "py_modules/lumux_decky/__init__.py", "py_modules/lumux_decky/client.py",
    "py_modules/lumux_decky/supervisor.py", "py_modules/lumux_decky/installer.py",
    "py_modules/lumux_decky/_version.py",
]
# The plugin downloads the Flatpak; it must never ship it or other binaries
BINARY_SUFFIXES = (".flatpak", ".so", ".exe", ".AppImage")


def main(path: str, lumux_version: str = "") -> int:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        roots = {n.split("/", 1)[0] for n in names}
        if len(roots) != 1:
            print(f"expected one top-level folder, got {sorted(roots)}")
            return 1
        root = roots.pop()
        files = {n[len(root) + 1:] for n in names if not n.endswith("/")}
        missing = [f for f in REQUIRED if f not in files]
        if missing:
            print(f"missing files: {missing}")
            return 1
        junk = [f for f in files if "node_modules/" in f or "__pycache__" in f or f.startswith("src/")]
        if junk:
            print(f"unexpected files: {junk[:10]}")
            return 1
        binaries = [f for f in files if f.endswith(BINARY_SUFFIXES)
                    or zf.read(f"{root}/{f}")[:4] == b"\x7fELF"]
        if binaries:
            print(f"bundled binaries are not allowed: {binaries[:10]}")
            return 1
        pinned = re.search(r'^LUMUX_VERSION = "([^"]+)"',
                           zf.read(f"{root}/py_modules/lumux_decky/_version.py").decode(), re.M)
        if not pinned:
            print("_version.py has no LUMUX_VERSION")
            return 1
        if lumux_version and pinned.group(1) != lumux_version:
            print(f"plugin installs Lumux {pinned.group(1)} but this is Lumux {lumux_version}; "
                  "update decky/py_modules/lumux_decky/_version.py")
            return 1
        plugin = json.loads(zf.read(f"{root}/plugin.json"))
        package = json.loads(zf.read(f"{root}/package.json"))
        for key in ("name", "author", "flags", "publish"):
            if key not in plugin:
                print(f"plugin.json lacks {key!r}")
                return 1
        if "_root" in plugin["flags"] or "root" in plugin["flags"]:
            print("plugin.json must not request root")
            return 1
        print(f"{root}: {plugin['name']} {package['version']} by {plugin['author']} "
              f"(installs Lumux {pinned.group(1)}), {len(files)} files OK")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("zip")
    parser.add_argument("--lumux-version", default="")
    args = parser.parse_args()
    sys.exit(main(args.zip, args.lumux_version))
