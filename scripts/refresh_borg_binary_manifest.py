#!/usr/bin/env python3
"""Print manifest entries for Borg releases, for app/api/borg_binaries.py.

The GitHub release API reports a sha256 digest per asset, so a version bump
does not require downloading ~180 MB of binaries to learn their checksums.

Usage::

    python scripts/refresh_borg_binary_manifest.py 1.4.4 2.0.0b21

The output is meant to be pasted into BORG_BINARIES after checking it against
the release page. It is deliberately not written to the module automatically:
a checksum that the machine both fetches and installs unattended would defeat
the point of pinning it.
"""

from __future__ import annotations

import json
import sys
import urllib.request

API = "https://api.github.com/repos/borgbackup/borg/releases/tags/{version}"

# Published asset name -> (uname -m arch, oldest glibc it was built against).
KNOWN_VARIANTS = {
    "borg-linux-glibc231-x86_64": ("x86_64", "2.31"),
    "borg-linux-glibc235-x86_64-gh": ("x86_64", "2.35"),
    "borg-linux-glibc235-arm64-gh": ("aarch64", "2.35"),
}


def entries_for(version: str) -> str:
    with urllib.request.urlopen(API.format(version=version), timeout=30) as response:
        release = json.load(response)

    lines = [f'    "{version}": (']
    for asset in release.get("assets", []):
        variant = KNOWN_VARIANTS.get(asset["name"])
        if variant is None:
            continue
        arch, min_glibc = variant
        digest = asset.get("digest", "")
        if not digest.startswith("sha256:"):
            raise SystemExit(f"No sha256 digest published for {asset['name']}")
        lines.extend(
            [
                "        BorgBinary(",
                f'            arch="{arch}",',
                f'            min_glibc="{min_glibc}",',
                f'            asset="{asset["name"]}",',
                f'            sha256="{digest.removeprefix("sha256:")}",',
                "        ),",
            ]
        )
    lines.append("    ),")
    return "\n".join(lines)


def main() -> int:
    versions = sys.argv[1:]
    if not versions:
        print(__doc__)
        return 2
    for version in versions:
        print(entries_for(version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
