#!/usr/bin/env python3
"""Set the Lumux version everywhere it lives, in one go.

    scripts/set-version.py 0.8.7             # Lumux version only
    scripts/set-version.py 0.8.7 --decky 0.1.6

Updates pyproject.toml, src/lumux/__init__.py and the Decky plugin's pin
(decky/py_modules/lumux_decky/_version.py, which picks the lumux.flatpak the
plugin installs), plus decky/package.json with --decky. The metainfo release
notes are prose, so they're only checked: add a <release> entry first.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    ROOT / "pyproject.toml": r'^version = "[^"]*"$',
    ROOT / "src" / "lumux" / "__init__.py": r'^__version__ = "[^"]*"$',
    ROOT / "decky" / "py_modules" / "lumux_decky" / "_version.py": r'^LUMUX_VERSION = "[^"]*"$',
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="new Lumux version, e.g. 0.8.7")
    parser.add_argument("--decky", help="new decky-lumux plugin version, e.g. 0.1.6")
    args = parser.parse_args()
    for v in filter(None, (args.version, args.decky)):
        if not SEMVER.match(v):
            parser.error(f"not a x.y.z version: {v}")

    for path, pattern in FILES.items():
        text = path.read_text()
        key = pattern.split(" ")[0].lstrip("^")
        new, n = re.subn(pattern, f'{key} = "{args.version}"', text, count=1, flags=re.MULTILINE)
        if n != 1:
            print(f"error: no version line in {path.relative_to(ROOT)}", file=sys.stderr)
            return 1
        path.write_text(new)
        print(f"{path.relative_to(ROOT)}: {args.version}")

    if args.decky:
        pkg = ROOT / "decky" / "package.json"
        data = json.loads(pkg.read_text())
        data["version"] = args.decky
        pkg.write_text(json.dumps(data, indent=2) + "\n")
        print(f"decky/package.json: {args.decky}")

    metainfo = (ROOT / "data" / "io.github.enginkirmaci.lumux.metainfo.xml").read_text()
    newest = re.search(r'<release version="([^"]+)"', metainfo)
    if not newest or newest.group(1) != args.version:
        print(
            f"warning: add a <release version=\"{args.version}\"> entry to "
            "data/io.github.enginkirmaci.lumux.metainfo.xml (tests check it)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
