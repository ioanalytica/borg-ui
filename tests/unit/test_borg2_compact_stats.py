import json

import pytest

from app.services.borg2_compact_stats import is_stats_closing_line, parse_compact_stats

# The logger Borg 2.0.0b24 emits the lines from; the parser does not depend
# on it.
COMPACT_LOGGER = "borg.archiver.compact_cmd"

# Verbatim `borg compact --stats -v` output of 2.0.0b23 / b24 on a scratch
# repository.
PLAIN = """Starting compaction / garbage collection...
Overall statistics, considering all 2 archives in this repository:
Source data size was 1 MB in 6 files.
Deduplicated size is 500 kB.
Deduplication factor is 0.50.
Repository size is 502 kB in 6 objects.
Compression factor is 1.00.
Compaction saved 0 B.
Finished compaction / garbage collection...
""".splitlines()

EXPECTED = {
    "archive_count": 2,
    "source_size": 1_000_000,
    "source_files": 6,
    "deduplicated_size": 500_000,
    "deduplication_factor": 0.5,
    "repository_size": 502_000,
    "object_count": 6,
    "compression_factor": 1.0,
    "compaction_saved": 0,
    # "0 B" is exact, "502 kB" is not: the payload is only as exact as its
    # least exact size token
    "size_precision": "rounded_to_printed_unit",
}

# The same repository under BORG_UNITS=raw (what every compact path here
# sets): exact byte counts.
RAW = """Overall statistics, considering all 2 archives in this repository:
Source data size was 1048576 B in 6 files.
Deduplicated size is 499712 B.
Deduplication factor is 0.48.
Repository size is 501913 B in 6 objects.
Compression factor is 1.00.
Compaction saved 0 B.
""".splitlines()

# BORG_UNITS=iec, inherited from a process environment: no decimals in the
# printed unit either.
IEC = """Source data size was 1 MiB in 6 files.
Deduplicated size is 488 KiB.
Repository size is 490 KiB in 6 objects.
Compaction saved 0 B.
""".splitlines()


def _log_json(message, name=COMPACT_LOGGER):
    return json.dumps(
        {"type": "log_message", "levelname": "INFO", "name": name, "message": message}
    )


@pytest.mark.unit
def test_parses_plain_lines():
    assert parse_compact_stats(PLAIN) == EXPECTED


@pytest.mark.unit
def test_parses_log_json_entries_and_ignores_progress():
    lines = [json.dumps({"type": "progress_percent", "current": 1, "total": 2})]
    lines += [_log_json(line) for line in PLAIN]
    assert parse_compact_stats(lines) == EXPECTED


@pytest.mark.unit
def test_accepts_any_logger_and_ignores_junk():
    """The lines are matched by wording; the logger name is not a contract
    (Borg has renamed loggers between betas)."""
    lines = [
        _log_json("Repository size is 9 GB in 1 objects.", name="borg.other"),
        "{not json",
        "",
    ]
    assert parse_compact_stats(lines) == {
        "repository_size": 9_000_000_000,
        "object_count": 1,
        "size_precision": "rounded_to_printed_unit",
    }
    assert parse_compact_stats(["{not json", ""]) is None


@pytest.mark.unit
def test_returns_none_without_a_repository_size_line():
    assert parse_compact_stats(["Source data size was 1 MB in 6 files."]) is None
    assert parse_compact_stats([]) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "text,expected",
    [
        ("Repository size is 2.39 TB in 1234 objects.", 2_390_000_000_000),
        ("Repository size is 0 B in 0 objects.", 0),
        ("Repository size is 12.5 GB in 7 objects.", 12_500_000_000),
    ],
)
def test_decimal_units_as_borg_prints_them(text, expected):
    stats = parse_compact_stats([text])
    assert stats["repository_size"] == expected
    assert "source_size" not in stats


@pytest.mark.unit
def test_raw_units_are_exact_integers():
    stats = parse_compact_stats(RAW)
    assert stats["source_size"] == 1_048_576
    assert stats["deduplicated_size"] == 499_712
    assert stats["repository_size"] == 501_913
    assert stats["compaction_saved"] == 0
    assert stats["deduplication_factor"] == 0.48
    assert stats["size_precision"] == "exact"
    # above 2**53, where a float detour would lose bytes
    big = parse_compact_stats(["Repository size is 9007199254740993 B in 1 objects."])
    assert big["repository_size"] == 9_007_199_254_740_993
    assert big["size_precision"] == "exact"


@pytest.mark.unit
def test_iec_units_are_accepted_and_marked_rounded():
    stats = parse_compact_stats(IEC)
    assert stats["source_size"] == 1_048_576
    assert stats["deduplicated_size"] == 488 * 1024
    assert stats["repository_size"] == 490 * 1024
    assert stats["size_precision"] == "rounded_to_printed_unit"
    assert parse_compact_stats(["Repository size is 2.5 TiB in 1 objects."])[
        "repository_size"
    ] == int(2.5 * 2**40)


@pytest.mark.unit
def test_is_stats_closing_line_starts_at_the_repository_size_line():
    closing = [is_stats_closing_line(line) for line in PLAIN]
    assert closing == [False, False, False, False, False, True, True, True, False]
    assert is_stats_closing_line(_log_json("Compaction saved 0 B."))
    assert not is_stats_closing_line(_log_json("Deduplicated size is 500 kB."))


@pytest.mark.unit
def test_deeply_nested_json_is_not_a_statistics_line():
    """Agent-supplied bytes: nesting past the interpreter's limit must not
    raise out of the completion handler."""
    assert parse_compact_stats(["{" * 100_000]) is None
    assert not is_stats_closing_line("{" * 100_000)
