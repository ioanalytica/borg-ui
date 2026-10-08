"""A repository bound to a managed agent never runs Borg on the server.

The agent is the machine that reaches the repository and holds its
credentials. A route that runs Borg on the server for such a repository works
on the wrong machine: it fails when the server cannot reach the repository,
and when it can (a repository server both reach), it acts behind the agent's
back. Each test here drives one entry point with an agent repository and
records every process the server would start.
"""

import asyncio
import os

import pytest

from app.core.security import get_password_hash
from app.database.models import (
    AgentMachine,
    LicensingState,
    Repository,
    SystemSettings,
)

ARCHIVE_ID = "a" * 64


@pytest.fixture
def server_processes(monkeypatch):
    """Every process the server starts during the test, refused at the door."""
    started: list[list[str]] = []

    async def _refuse(*argv, **_kwargs):
        started.append([str(part) for part in argv])
        raise OSError("server-side process refused by the test")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _refuse)
    return started


def _borg_processes(started: list[list[str]]) -> list[list[str]]:
    return [
        argv
        for argv in started
        if argv and os.path.basename(argv[0]).startswith("borg")
    ]


def _enable_borg_v2(test_db):
    state = test_db.query(LicensingState).first()
    if state is None:
        state = LicensingState(instance_id="test-instance-agent-server-borg")
        test_db.add(state)
    state.plan = "pro"
    state.status = "active"
    state.is_trial = False
    if test_db.query(SystemSettings).first() is None:
        test_db.add(SystemSettings())
    test_db.commit()


def _agent_repository(test_db, *, borg_version: int = 1, **fields) -> Repository:
    agent = AgentMachine(
        name="Repository Agent",
        agent_id=f"agt_server_borg_{borg_version}",
        token_hash=get_password_hash("borgui_agent_secret"),
        token_prefix="borgui_agent_secret"[:20],
        status="online",
        capabilities=[],
    )
    test_db.add(agent)
    test_db.commit()
    repo = Repository(
        name=f"Agent Repo {borg_version}",
        path=f"/agent/repositories/v{borg_version}",
        encryption=fields.pop("encryption", "repokey"),
        passphrase="secret",
        repository_type="local",
        borg_version=borg_version,
        executor_type="agent",
        execution_target="agent",
        agent_machine_id=agent.id,
        **fields,
    )
    test_db.add(repo)
    test_db.commit()
    test_db.refresh(repo)
    return repo


@pytest.mark.unit
class TestAgentRepositoryRoutesRunNoServerBorg:
    def test_wipe_preview(self, test_client, admin_headers, test_db, server_processes):
        repo = _agent_repository(test_db)

        response = test_client.post(
            f"/api/repositories/{repo.id}/wipe-preview",
            json={"run_compact": True},
            headers=admin_headers,
        )

        assert _borg_processes(server_processes) == []
        assert response.status_code == 409
        assert (
            response.json()["detail"]["key"]
            == "backend.errors.repo.agentRepositoryOperationUnsupported"
        )

    def test_restore_preview(
        self, test_client, admin_headers, test_db, server_processes
    ):
        repo = _agent_repository(test_db)

        response = test_client.post(
            "/api/restore/preview",
            json={
                "repository": repo.path,
                "archive": "archive-1",
                "paths": ["/etc/hosts"],
                "destination": "/tmp/restore",
                "repository_id": repo.id,
            },
            headers=admin_headers,
        )

        assert _borg_processes(server_processes) == []
        assert response.status_code == 409
        assert (
            response.json()["detail"]["key"]
            == "backend.errors.repo.agentRepositoryOperationUnsupported"
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/api/archives/list",
            "/api/archives/archive-1/info?include_files=true",
            "/api/archives/archive-1/contents",
        ],
    )
    def test_legacy_archive_routes(
        self, test_client, admin_headers, test_db, server_processes, path
    ):
        repo = _agent_repository(test_db)
        separator = "&" if "?" in path else "?"

        response = test_client.get(
            f"{path}{separator}repository={repo.path}", headers=admin_headers
        )

        assert _borg_processes(server_processes) == []
        assert response.status_code == 409
        assert (
            response.json()["detail"]["key"]
            == "backend.errors.repo.agentRepositoryOperationUnsupported"
        )

    def test_keyfile_export(
        self, test_client, admin_headers, test_db, server_processes
    ):
        repo = _agent_repository(test_db, encryption="keyfile", has_keyfile=True)

        response = test_client.get(
            f"/api/repositories/{repo.id}/keyfile", headers=admin_headers
        )

        assert _borg_processes(server_processes) == []
        assert response.status_code == 409
        assert (
            response.json()["detail"]["key"]
            == "backend.errors.repo.agentRepositoryKeyfileUnsupported"
        )

    def test_keyfile_upload_writes_nothing(
        self, test_client, admin_headers, test_db, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = _agent_repository(test_db, encryption="keyfile")

        response = test_client.post(
            f"/api/repositories/{repo.id}/keyfile",
            files={"keyfile": ("key", b"BORG_KEY 0123\n", "text/plain")},
            headers=admin_headers,
        )

        assert response.status_code == 409
        assert (
            response.json()["detail"]["key"]
            == "backend.errors.repo.agentRepositoryKeyfileUnsupported"
        )
        assert not (tmp_path / ".config" / "borg" / "keys").exists()
        test_db.refresh(repo)
        assert not repo.has_keyfile

    def test_v2_delete_resolves_no_name_on_the_server(
        self, test_client, admin_headers, test_db, server_processes
    ):
        _enable_borg_v2(test_db)
        repo = _agent_repository(test_db, borg_version=2)

        response = test_client.delete(
            f"/api/v2/archives/{ARCHIVE_ID}?repository={repo.id}",
            headers=admin_headers,
        )

        assert _borg_processes(server_processes) == []
        assert response.status_code == 200

    def test_v2_file_download(
        self, test_client, admin_headers, test_db, server_processes
    ):
        _enable_borg_v2(test_db)
        repo = _agent_repository(test_db, borg_version=2)

        test_client.get(
            "/api/v2/archives/download",
            params={
                "repository": repo.id,
                "archive": ARCHIVE_ID,
                "file_path": "/etc/hosts",
            },
            headers=admin_headers,
        )

        assert _borg_processes(server_processes) == []
