"""The port of an ssh:// repository URL and a remote shell that was given.

Borg 2 hands BORG_RSH to the store as it is and adds the port only to the
ssh command it builds itself (verified against 2.0.0b25: with BORG_RSH set,
`ssh://host:2222//path` reached the remote shell without `-p 2222`). Borg UI
always sets BORG_RSH, so without the port in it every Borg 2 repository on
another port than 22 is looked for on port 22.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.borg2 import borg2
from app.utils.borg_env import env_with_repository_port, rsh_with_repository_port

URL = "ssh://borg@repo.example:2222/backups/repo"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("rsh", "repository", "expected"),
    [
        ("ssh -o BatchMode=yes", URL, "ssh -o BatchMode=yes -p 2222"),
        # a remote shell that names a port keeps it
        ("ssh -p 2200 -o BatchMode=yes", URL, "ssh -p 2200 -o BatchMode=yes"),
        ("ssh -p2200", URL, "ssh -p2200"),
        # nothing to add
        ("ssh -o BatchMode=yes", "ssh://borg@repo.example/backups/repo", None),
        ("ssh -o BatchMode=yes", "/local/repo", None),
        ("ssh -o BatchMode=yes", "sftp://borg@repo.example:23/repo", None),
        ("ssh -o BatchMode=yes", None, None),
        # not a port, not a command line: left alone rather than guessed at
        ("ssh -o BatchMode=yes", "ssh://borg@repo.example:port/repo", None),
        ("ssh -o 'unterminated", URL, None),
        ("", URL, None),
    ],
)
def test_the_port_of_the_url_goes_into_the_remote_shell(rsh, repository, expected):
    assert rsh_with_repository_port(rsh, repository) == (
        rsh if expected is None else expected
    )


@pytest.mark.unit
def test_both_remote_shell_variables_get_the_port():
    env = {"BORG_RSH": "ssh -i /k", "BORGSTORE_RSH": "ssh -i /s", "OTHER": "x"}

    assert env_with_repository_port(env, URL) == {
        "BORG_RSH": "ssh -i /k -p 2222",
        "BORGSTORE_RSH": "ssh -i /s -p 2222",
        "OTHER": "x",
    }
    assert env_with_repository_port({"OTHER": "x"}, URL) == {"OTHER": "x"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_borg2_command_runs_with_the_port_in_its_remote_shell(monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"{}", b""

    async def create_subprocess_exec(*cmd, env=None, **_):
        captured["env"] = env
        return Process()

    monkeypatch.setattr(
        "app.core.borg2.asyncio.create_subprocess_exec", create_subprocess_exec
    )

    await borg2.rinfo(URL, passphrase="secret")

    assert captured["env"]["BORG_RSH"].endswith(" -p 2222")


@pytest.mark.unit
def test_a_borg2_stream_runs_with_the_port_in_its_remote_shell(monkeypatch):
    captured = {}

    class Stream:
        def __init__(self, cmd, *, env=None, timeout=3600):
            captured["env"] = env

    monkeypatch.setattr("app.core.borg2.CommandLineStream", Stream)
    monkeypatch.setattr("app.core.borg2.CommandByteStream", Stream)

    for build in (
        lambda: borg2.diff_archives(URL, "aid:1", "aid:2"),
        lambda: borg2.list_archive_lines(URL, "aid:1"),
        lambda: borg2.export_archive_tar(URL, "aid:1", "etc"),
    ):
        captured.clear()
        build()
        assert captured["env"]["BORG_RSH"].endswith(" -p 2222")


@pytest.mark.unit
def test_the_environment_of_a_stored_repository_carries_the_port():
    from app.utils.borg_env import build_repository_borg_env, build_ssh_key_borg_env

    repository = SimpleNamespace(
        path=URL, passphrase="secret", remote_path=None, repository_connection=None
    )
    with (
        patch("app.utils.borg_env.resolve_repo_ssh_key_file", return_value=None),
        patch(
            "app.utils.borg_env.resolve_repository_ssh_connection", return_value=None
        ),
    ):
        env, _key = build_repository_borg_env(repository, None)
    assert env["BORG_RSH"].endswith(" -p 2222")

    env, _key = build_ssh_key_borg_env(path=URL, passphrase="secret")
    assert env["BORG_RSH"].endswith(" -p 2222")


@pytest.mark.unit
def test_break_lock_runs_with_the_port_in_its_remote_shell():
    from app.utils.process_utils import break_repository_lock

    repository = SimpleNamespace(
        id=2,
        borg_version=2,
        path=URL,
        passphrase="secret",
        connection_id=7,
        remote_path=None,
    )
    with (
        patch("app.core.borg2.borg2.borg_cmd", "borg2"),
        patch("app.utils.process_utils.subprocess.run") as run,
        patch(
            "app.utils.process_utils.get_standard_ssh_opts",
            return_value=["-o", "BatchMode=yes"],
        ),
    ):
        run.return_value = SimpleNamespace(returncode=0, stderr="")
        assert break_repository_lock(repository) is True

    assert run.call_args.kwargs["env"]["BORG_RSH"] == "ssh -o BatchMode=yes -p 2222"
