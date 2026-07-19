"""Static Borg binaries published with each upstream release.

Borg publishes no wheels on PyPI, only sdists, so any pip-based install
compiles from source and needs a build toolchain on the target machine. The
static single-file binaries shipped with each release let a managed agent get
the exact Borg version its server runs without one.

The manifest is checked in rather than fetched at runtime so that the installer
can verify what it downloaded against a value the server did not learn from the
same place it got the file.

Regenerate after a Borg version bump with::

    python scripts/refresh_borg_binary_manifest.py 1.4.4 2.0.0b21
"""

from __future__ import annotations

from typing import NamedTuple

RELEASE_URL = "https://github.com/borgbackup/borg/releases/download/{version}/{asset}"


class BorgBinary(NamedTuple):
    """One published binary: which machine it runs on, and what it hashes to."""

    arch: str  # as reported by `uname -m`
    min_glibc: str  # oldest glibc the binary was built against
    asset: str
    sha256: str


# Only Linux variants are listed: the managed agent installer is Debian-family
# only. 32-bit ARM and musl have no published binary at all, which is why the
# installer has to fail explicitly for them rather than guess.
BORG_BINARIES: dict[str, tuple[BorgBinary, ...]] = {
    "1.4.4": (
        BorgBinary(
            arch="x86_64",
            min_glibc="2.31",
            asset="borg-linux-glibc231-x86_64",
            sha256="28d8053626bd375837ed4fdb4dda5ef29b2271dbe71a2c6a5749d8f8f0021c6d",
        ),
        BorgBinary(
            arch="x86_64",
            min_glibc="2.35",
            asset="borg-linux-glibc235-x86_64-gh",
            sha256="d48d3a31cf1f6fb781fe240945e0b1c246093d3b94b56ce8f501d46a8615f4de",
        ),
        BorgBinary(
            arch="aarch64",
            min_glibc="2.35",
            asset="borg-linux-glibc235-arm64-gh",
            sha256="7bfe55dfd4088f5c570a9e0d5513b5e01a20c3555eedc95c2d7de952c260eb0f",
        ),
    ),
    "2.0.0b21": (
        BorgBinary(
            arch="x86_64",
            min_glibc="2.35",
            asset="borg-linux-glibc235-x86_64-gh",
            sha256="1cde53b9d248c1a600df6f938eea3207f27141be0e8d7a80542fa8f901f221c5",
        ),
        BorgBinary(
            arch="aarch64",
            min_glibc="2.35",
            asset="borg-linux-glibc235-arm64-gh",
            sha256="d008c57e97d977a409e5570a93e805be022ce6a69aef42db4d53dd68f4326a8f",
        ),
    ),
}


def binary_table(versions: dict[str, str | None]) -> str:
    """Render the installer's binary lookup table.

    ``versions`` maps a Borg major ("1", "2") to the exact version the server
    runs. Each output line is ``major arch min_glibc sha256 url``; the
    installer picks the newest glibc build its machine can run. A major with an
    unknown version contributes no rows, and the installer then says so instead
    of installing something else.
    """
    lines = []
    for major, version in sorted(versions.items()):
        if not version:
            continue
        for binary in BORG_BINARIES.get(version, ()):
            url = RELEASE_URL.format(version=version, asset=binary.asset)
            lines.append(
                f"{major} {binary.arch} {binary.min_glibc} {binary.sha256} {url}"
            )
    return "\n".join(lines)
