#!/usr/bin/env python3
"""Rewrite app/api/borg_binaries.json for the Borg versions this repo builds.

The versions are stated once, in Dockerfile.runtime-base. This script reads
them from there, asks the GitHub release API for the sha256 digest of each
published Linux binary, and writes the manifest. Adopting a new Borg version is
therefore: change the ARG, run this, commit.

    python scripts/refresh_borg_binary_manifest.py            # versions from the Dockerfile
    python scripts/refresh_borg_binary_manifest.py 1.4.5 2.0.0b22   # or state them

The digests come from the release API rather than from hashing a download, so a
version bump does not require pulling ~180 MB of binaries. Nothing here runs at
build or run time: the manifest is committed so that the installer verifies a
download against a value it did not fetch alongside the file.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile.runtime-base"
MANIFEST = REPO_ROOT / "app" / "api" / "borg_binaries.json"

API = "https://api.github.com/repos/borgbackup/borg/releases/tags/{version}"
RELEASE_URL = "https://github.com/borgbackup/borg/releases/download/{version}/{asset}"

# Published asset name -> (uname -m arch, oldest glibc it was built against).
# Assets outside this list are ignored: macOS and FreeBSD builds, and the source
# tarballs, are not something the Debian-family installer can use.
KNOWN_VARIANTS = {
    "borg-linux-glibc231-x86_64": ("x86_64", "2.31"),
    "borg-linux-glibc235-x86_64-gh": ("x86_64", "2.35"),
    "borg-linux-glibc235-arm64-gh": ("aarch64", "2.35"),
}


def versions_from_dockerfile() -> dict[str, str]:
    """The Borg versions this repo's runtime base installs."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    versions = {}
    for major in ("1", "2"):
        match = re.search(rf"^ARG BORG{major}_VERSION=(\S+)", text, re.M)
        if not match:
            raise SystemExit(f"No ARG BORG{major}_VERSION in {DOCKERFILE.name}")
        versions[major] = match.group(1)
    return versions


def binaries_for(version: str) -> list[dict]:
    with urllib.request.urlopen(API.format(version=version), timeout=30) as response:
        release = json.load(response)

    entries = []
    for asset in release.get("assets", []):
        variant = KNOWN_VARIANTS.get(asset["name"])
        if variant is None:
            continue
        digest = asset.get("digest", "")
        if not digest.startswith("sha256:"):
            raise SystemExit(f"No sha256 digest published for {asset['name']}")
        arch, min_glibc = variant
        entries.append(
            {
                "arch": arch,
                "min_glibc": min_glibc,
                "asset": asset["name"],
                "sha256": digest.removeprefix("sha256:"),
            }
        )

    if not entries:
        raise SystemExit(
            f"Borg {version} publishes no Linux binary this installer can use"
        )
    return entries


def main() -> int:
    if len(sys.argv) > 1:
        given = sys.argv[1:]
        if len(given) != 2:
            raise SystemExit("Pass both versions (Borg 1 then Borg 2), or neither")
        current = {"1": given[0], "2": given[1]}
    else:
        current = versions_from_dockerfile()
        print(f"Versions from {DOCKERFILE.name}: {current['1']}, {current['2']}")

    manifest = {
        "current": current,
        "release_url": RELEASE_URL,
        "binaries": {version: binaries_for(version) for version in current.values()},
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    for version, entries in manifest["binaries"].items():
        print(f"  {version}: {len(entries)} binaries")
    print(f"Wrote {MANIFEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
