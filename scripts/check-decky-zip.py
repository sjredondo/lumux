#!/usr/bin/env python3
"""Sanity-check a Decky plugin zip: store layout, metadata, no junk.

usage: check-decky-zip.py decky-lumux.zip
"""

import json
import sys
import zipfile

REQUIRED = [
    "plugin.json", "package.json", "main.py", "LICENSE", "README.md", "dist/index.js",
    "py_modules/lumux_decky/__init__.py", "py_modules/lumux_decky/client.py",
    "py_modules/lumux_decky/supervisor.py",
]


def main(path: str) -> int:
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
        plugin = json.loads(zf.read(f"{root}/plugin.json"))
        package = json.loads(zf.read(f"{root}/package.json"))
        for key in ("name", "author", "flags", "publish"):
            if key not in plugin:
                print(f"plugin.json lacks {key!r}")
                return 1
        if "_root" in plugin["flags"] or "root" in plugin["flags"]:
            print("plugin.json must not request root")
            return 1
        print(f"{root}: {plugin['name']} {package['version']} by {plugin['author']}, "
              f"{len(files)} files OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
