"""ssh:// repository URLs in the syntax of the Borg major that reads them.

Borg 2 reads `ssh://host/path` as relative to the login directory and
`ssh://host//path` as absolute (verified against 2.0.0b25: the client asks the
host for `FILE:path` and `FILE:/path`). Borg 1 reads the first as absolute.
"""

import pytest

from app.utils.repository_paths import build_ssh_repository_path

CONNECTION = {"username": "borg", "host": "repo.example", "port": 22}
PREFIXED = {**CONNECTION, "ssh_path_prefix": "/volume1"}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        # a plain path: a leading slash means absolute
        ("/srv/backups/repo", "ssh://borg@repo.example:22//srv/backups/repo"),
        ("backups/repo", "ssh://borg@repo.example:22/backups/repo"),
        # Borg 1's spelling of "in the login directory" stays relative
        ("/./backups/repo", "ssh://borg@repo.example:22/./backups/repo"),
        # a URL keeps the form it came in
        (
            "ssh://other@elsewhere:2222//srv/backups/repo",
            "ssh://borg@repo.example:22//srv/backups/repo",
        ),
        (
            "ssh://other@elsewhere:2222/backups/repo",
            "ssh://borg@repo.example:22/backups/repo",
        ),
    ],
)
def test_borg2_url_tells_absolute_from_relative(raw_path, expected):
    assert build_ssh_repository_path(raw_path, CONNECTION, borg_version=2) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_path", "connection", "expected"),
    [
        (
            "/srv/backups/repo",
            CONNECTION,
            "ssh://borg@repo.example:22/srv/backups/repo",
        ),
        ("/./backups/repo", CONNECTION, "ssh://borg@repo.example:22/./backups/repo"),
        (
            "ssh://other@elsewhere:2222/srv/backups/repo",
            CONNECTION,
            "ssh://borg@repo.example:22/srv/backups/repo",
        ),
        ("/backups/repo", PREFIXED, "ssh://borg@repo.example:22/volume1/backups/repo"),
        (
            "ssh://borg@repo.example:22/volume1/backups/repo",
            PREFIXED,
            "ssh://borg@repo.example:22/volume1/backups/repo",
        ),
    ],
)
def test_borg1_url_is_unchanged(raw_path, connection, expected):
    assert build_ssh_repository_path(raw_path, connection) == expected
    assert build_ssh_repository_path(raw_path, connection, borg_version=1) == expected
    assert (
        build_ssh_repository_path(
            raw_path, connection, stored_path="ssh://borg@repo.example:22" + raw_path
        )
        == expected
    )


@pytest.mark.unit
def test_borg2_path_under_a_command_prefix_is_absolute():
    assert (
        build_ssh_repository_path("/backups/repo", PREFIXED, borg_version=2)
        == "ssh://borg@repo.example:22//volume1/backups/repo"
    )


@pytest.mark.unit
def test_borg2_prefix_is_not_applied_twice_to_a_stored_url():
    """Rebuilding the stored absolute URL must not read its two slashes as a
    path the prefix is missing from."""
    stored = "ssh://borg@repo.example:22//volume1/backups/repo"

    assert build_ssh_repository_path(stored, PREFIXED, borg_version=2) == stored
    # what the repository form sends back: the tail of the stored URL
    assert (
        build_ssh_repository_path(
            "//volume1/backups/repo", PREFIXED, borg_version=2, stored_path=stored
        )
        == stored
    )


@pytest.mark.unit
def test_borg2_prefix_leaves_the_login_directory_alone():
    assert (
        build_ssh_repository_path(
            "ssh://borg@repo.example:22/backups/repo", PREFIXED, borg_version=2
        )
        == "ssh://borg@repo.example:22/backups/repo"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored",
    [
        "ssh://borg@repo.example:22/backups/repo",
        "ssh://borg@repo.example:22//srv/backups/repo",
        "ssh://borg@repo.example:22/./backups/repo",
    ],
)
def test_the_repository_form_round_trip_keeps_a_borg2_url(stored):
    """The form shows what follows the host and sends it back as the path.
    Saving a repository whose path was not touched must not move it: a
    relative repository read as absolute would point at another directory,
    and the next backup would create a repository there."""
    sent_back = stored[len("ssh://borg@repo.example:22") :]

    assert (
        build_ssh_repository_path(
            sent_back, CONNECTION, borg_version=2, stored_path=stored
        )
        == stored
    )
    assert build_ssh_repository_path(stored, CONNECTION, borg_version=2) == stored


@pytest.mark.unit
def test_a_changed_path_on_a_borg2_repository_is_a_plain_path():
    stored = "ssh://borg@repo.example:22/backups/repo"

    assert (
        build_ssh_repository_path(
            "/srv/backups/new", CONNECTION, borg_version=2, stored_path=stored
        )
        == "ssh://borg@repo.example:22//srv/backups/new"
    )


@pytest.mark.unit
def test_the_directory_on_the_host_of_an_absolute_borg2_url():
    from app.utils.repository_paths import ssh_repository_directory

    assert (
        ssh_repository_directory(
            "ssh://borg@repo.example:22//srv/backups/repo", borg_version=2
        )
        == "/srv/backups/repo"
    )
    assert (
        ssh_repository_directory("ssh://borg@repo.example:22/srv/backups/repo")
        == "/srv/backups/repo"
    )


@pytest.mark.unit
def test_a_relative_borg2_url_names_no_directory_to_mount():
    """`ssh://host/backups/repo` is `<login directory>/backups/repo` for
    Borg 2. Mounting `/backups/repo` instead would hand a cloud mirror the
    contents of another directory."""
    from types import SimpleNamespace

    from app.services.rclone_repository_service import RcloneRepositoryService
    from app.utils.repository_paths import ssh_repository_directory

    with pytest.raises(ValueError, match="ssh://host//path"):
        ssh_repository_directory(
            "ssh://borg@repo.example:22/backups/repo", borg_version=2
        )

    service = RcloneRepositoryService.__new__(RcloneRepositoryService)
    relative = SimpleNamespace(
        path="ssh://borg@repo.example:22/backups/repo", borg_version=2
    )
    absolute = SimpleNamespace(
        path="ssh://borg@repo.example:22//srv/backups/repo", borg_version=2
    )
    borg1 = SimpleNamespace(
        path="ssh://borg@repo.example:22/backups/repo", borg_version=1
    )
    with pytest.raises(ValueError, match="login"):
        service._ssh_repository_remote_path(relative)
    assert service._ssh_repository_remote_path(absolute) == "/srv/backups/repo"
    assert service._ssh_repository_remote_path(borg1) == "/backups/repo"
