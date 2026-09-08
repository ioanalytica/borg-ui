"""Repository statistics from `borg compact --stats` (Borg 2).

Borg 2 reports repository-wide statistics only here, on INFO level; there
is no JSON form (borgbackup/borg#10329). The parser accepts the plain lines
or the `--log-json` `log_message` entries that wrap them.

Sizes are printed through Borg's `format_file_size`, which follows
`BORG_UNITS`: `si` (default, `502 kB`, `2.39 TB`), `iec` (`490 KiB`) or
`raw` (`502000 B`). The human forms are rounded to the digits Borg
prints, so the parsed byte count is only as exact as those digits; every
compact path of this application sets `BORG_UNITS=raw` and gets exact
integers. The result records which it got in `size_precision`: `exact`
when every size token was a raw byte count, `rounded_to_printed_unit`
otherwise. The deduplication and compression factors are printed with two
decimals in either mode.

The lines are matched by their wording alone, not by the logger that
emitted them (`borg.archiver.compact_cmd` today); a `--log-json` entry of
any logger is accepted.
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

PRECISION_EXACT = "exact"
PRECISION_ROUNDED = "rounded_to_printed_unit"

_UNITS = {
    "B": 1,
    "kB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "PB": 10**15,
    "EB": 10**18,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
    "PiB": 2**50,
    "EiB": 2**60,
}
_SIZE = r"(?P<size>\d+(?:\.\d+)?) (?P<unit>[kMGTPE]?B|[KMGTPE]iB)"
_PATTERNS = {
    "archive_count": re.compile(
        r"^Overall statistics, considering all (?P<n>\d+) archives"
    ),
    "source": re.compile(rf"^Source data size was {_SIZE} in (?P<n>\d+) files\."),
    "deduplicated_size": re.compile(rf"^Deduplicated size is {_SIZE}\."),
    "deduplication_factor": re.compile(
        r"^Deduplication factor is (?P<f>\d+(?:\.\d+)?)\."
    ),
    "repository": re.compile(rf"^Repository size is {_SIZE} in (?P<n>\d+) objects\."),
    "compression_factor": re.compile(r"^Compression factor is (?P<f>\d+(?:\.\d+)?)\."),
    "compaction_saved": re.compile(rf"^Compaction saved {_SIZE}\."),
}


def _bytes(match: re.Match) -> int:
    """Integer bytes without a float in between: `int` keeps raw counts
    exact above 2**53, Decimal scales the human forms as printed."""
    size, unit = match.group("size"), match.group("unit")
    if unit == "B" and "." not in size:
        return int(size)
    scaled = Decimal(size) * _UNITS[unit]
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def _message(line: str) -> Optional[str]:
    """The statistics line inside a `--log-json` entry, or the line itself."""
    text = line.strip()
    if not text:
        return None
    if text[0] != "{":
        return text
    try:
        entry = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        # Agent-supplied bytes: nesting past the interpreter's limit is not
        # a statistics line either.
        return None
    if entry.get("type") != "log_message":
        return None
    message = entry.get("message")
    return message.strip() if isinstance(message, str) else None


# The lines from "Repository size" on: once one of them arrived, the block
# has its required line and a transcript is worth parsing.
_CLOSING_KEYS = ("repository", "compression_factor", "compaction_saved")


def is_stats_closing_line(line: str) -> bool:
    """Whether this line is the "Repository size" line or one of the two
    Borg prints after it, so a caller can parse a transcript only when the
    block is complete enough to yield a result, instead of on every line."""
    message = _message(line)
    if message is None:
        return False
    return any(_PATTERNS[key].match(message) for key in _CLOSING_KEYS)


def parse_compact_stats(lines: Iterable[str]) -> Optional[dict]:
    """Return the statistics dict, or None when no "Repository size" line
    was seen (Borg 1, a compact without --stats, a failed run)."""
    stats: dict = {}
    exact = True

    def size(m: re.Match) -> int:
        nonlocal exact
        exact = exact and m.group("unit") == "B" and "." not in m.group("size")
        return _bytes(m)

    for line in lines:
        message = _message(line)
        if message is None:
            continue
        if m := _PATTERNS["archive_count"].match(message):
            stats["archive_count"] = int(m.group("n"))
        elif m := _PATTERNS["source"].match(message):
            stats["source_size"] = size(m)
            stats["source_files"] = int(m.group("n"))
        elif m := _PATTERNS["deduplicated_size"].match(message):
            stats["deduplicated_size"] = size(m)
        elif m := _PATTERNS["deduplication_factor"].match(message):
            stats["deduplication_factor"] = float(m.group("f"))
        elif m := _PATTERNS["repository"].match(message):
            stats["repository_size"] = size(m)
            stats["object_count"] = int(m.group("n"))
        elif m := _PATTERNS["compression_factor"].match(message):
            stats["compression_factor"] = float(m.group("f"))
        elif m := _PATTERNS["compaction_saved"].match(message):
            stats["compaction_saved"] = size(m)
    if "repository_size" not in stats:
        return None
    stats["size_precision"] = PRECISION_EXACT if exact else PRECISION_ROUNDED
    return stats
