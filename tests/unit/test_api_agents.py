import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.agent_auth import AGENT_AUTH_HEADER, AGENT_TOKEN_PREFIX_LENGTH
from app.core.security import get_password_hash
from app.database.models import (
    AgentEnrollmentToken,
    AgentJob,
    AgentJobLog,
    AgentMachine,
    BackupJob,
    BackupPlan,
    CheckJob,
    LicensingState,
    Operation,
    Repository,
)
from app.services.operations.executors import load_default_executors


def _set_plan(test_db, plan: str) -> None:
    state = test_db.query(LicensingState).first()
    if state is None:
        state = LicensingState(instance_id="test-instance-agents")
        test_db.add(state)
    state.plan = plan
    state.status = "active"
    test_db.commit()


@pytest.fixture(autouse=True)
def _enable_paid_managed_agent_features(test_db):
    _set_plan(test_db, "pro")


def _create_enrollment_token(test_client: TestClient, admin_headers, name="agent"):
    response = test_client.post(
        "/api/managed-machines/enrollment-tokens",
        json={"name": name, "expires_in_minutes": 60},
        headers=admin_headers,
    )
    assert response.status_code == 201
    return response.json()


def _register_agent(
    test_client: TestClient,
    enrollment_token: str,
    name="laptop",
    capabilities: list[str] | None = None,
):
    response = test_client.post(
        "/api/agents/register",
        json={
            "enrollment_token": enrollment_token,
            "name": name,
            "hostname": "laptop.local",
            "os": "linux",
            "arch": "amd64",
            "agent_version": "0.1.1",
            "borg_versions": [
                {"major": 1, "version": "1.2.8", "path": "/usr/bin/borg"}
            ],
            "capabilities": capabilities or ["backup.create", "logs.stream"],
        },
    )
    assert response.status_code == 200
    return response.json()


def _agent_headers(agent_token: str) -> dict[str, str]:
    return {AGENT_AUTH_HEADER: f"Bearer {agent_token}"}


def _get_agent(test_db, agent_id: str) -> AgentMachine:
    agent = (
        test_db.query(AgentMachine).filter(AgentMachine.agent_id == agent_id).first()
    )
    assert agent is not None
    return agent


def _create_agent_job(test_db, agent: AgentMachine, status: str = "queued") -> AgentJob:
    now = datetime.now(timezone.utc)
    job = AgentJob(
        agent_machine_id=agent.id,
        job_type="backup",
        status=status,
        payload={
            "schema_version": 1,
            "job_kind": "backup.create",
            "repository": {"id": 7},
        },
        created_at=now,
        updated_at=now,
    )
    test_db.add(job)
    test_db.commit()
    test_db.refresh(job)
    return job


@pytest.mark.unit
class TestAgentEnrollmentTokens:
    def test_create_enrollment_token_returns_full_token_once(
        self, test_client: TestClient, admin_headers
    ):
        data = _create_enrollment_token(test_client, admin_headers, "macbook setup")

        assert data["name"] == "macbook setup"
        assert data["token"].startswith("borgui_enroll_")
        assert data["token_prefix"] == data["token"][:AGENT_TOKEN_PREFIX_LENGTH]
        assert data["expires_at"].endswith("+00:00")
        assert data["created_at"].endswith("+00:00")

    def test_list_enrollment_tokens_hides_full_token(
        self, test_client: TestClient, admin_headers
    ):
        _create_enrollment_token(test_client, admin_headers)

        response = test_client.get(
            "/api/managed-machines/enrollment-tokens", headers=admin_headers
        )

        assert response.status_code == 200
        tokens = response.json()
        assert len(tokens) == 1
        assert "token" not in tokens[0]
        assert tokens[0]["token_prefix"].startswith("borgui_enroll_")

    def test_create_enrollment_token_requires_admin(
        self, test_client: TestClient, auth_headers
    ):
        response = test_client.post(
            "/api/managed-machines/enrollment-tokens",
            json={"name": "viewer token"},
            headers=auth_headers,
        )

        assert response.status_code == 403

    def test_revoke_enrollment_token_blocks_registration(
        self, test_client: TestClient, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)

        revoke = test_client.post(
            f"/api/managed-machines/enrollment-tokens/{enrollment['id']}/revoke",
            headers=admin_headers,
        )
        assert revoke.status_code == 204

        response = test_client.post(
            "/api/agents/register",
            json={"enrollment_token": enrollment["token"], "name": "laptop"},
        )
        assert response.status_code == 401


@pytest.mark.unit
class TestAgentRegistrationAndHeartbeat:
    def test_register_agent_copies_enrollment_default_path_to_machine(
        self, test_client: TestClient, test_db, admin_headers
    ):
        enrollment = test_client.post(
            "/api/managed-machines/enrollment-tokens",
            json={
                "name": "odroid setup",
                "expires_in_minutes": 60,
                "default_path": " /home/karanhudia ",
            },
            headers=admin_headers,
        )
        assert enrollment.status_code == 201
        assert enrollment.json()["default_path"] == "/home/karanhudia"

        registered = _register_agent(test_client, enrollment.json()["token"])
        agent = _get_agent(test_db, registered["agent_id"])
        assert agent.default_path == "/home/karanhudia"

        response = test_client.get(
            "/api/managed-machines/agents", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()[0]["default_path"] == "/home/karanhudia"

    def test_register_agent_consumes_enrollment_token_and_lists_machine(
        self, test_client: TestClient, test_db, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)

        registered = _register_agent(test_client, enrollment["token"])

        assert registered["agent_id"].startswith("agt_")
        assert registered["agent_token"].startswith("borgui_agent_")
        assert registered["poll_interval_seconds"] == 15
        assert registered["server_time"].endswith("+00:00")

        token_row = (
            test_db.query(AgentEnrollmentToken)
            .filter(AgentEnrollmentToken.id == enrollment["id"])
            .first()
        )
        assert token_row.used_at is not None

        response = test_client.get(
            "/api/managed-machines/agents", headers=admin_headers
        )
        assert response.status_code == 200
        agents = response.json()
        assert len(agents) == 1
        assert agents[0]["agent_id"] == registered["agent_id"]
        assert agents[0]["hostname"] == "laptop.local"
        assert agents[0]["status"] == "online"
        assert agents[0]["capabilities"] == ["backup.create", "logs.stream"]

    def test_enrollment_token_is_one_time_use(
        self, test_client: TestClient, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        _register_agent(test_client, enrollment["token"], name="first")

        response = test_client.post(
            "/api/agents/register",
            json={"enrollment_token": enrollment["token"], "name": "second"},
        )

        assert response.status_code == 401

    def test_expired_enrollment_token_is_rejected(
        self, test_client: TestClient, test_db
    ):
        raw_token = "borgui_enroll_expired-token"
        now = datetime.now(timezone.utc)
        token = AgentEnrollmentToken(
            name="expired",
            token_hash=get_password_hash(raw_token),
            token_prefix=raw_token[:AGENT_TOKEN_PREFIX_LENGTH],
            expires_at=now - timedelta(minutes=1),
            created_at=now - timedelta(minutes=2),
        )
        test_db.add(token)
        test_db.commit()

        response = test_client.post(
            "/api/agents/register",
            json={"enrollment_token": raw_token, "name": "expired-agent"},
        )

        assert response.status_code == 401

    def test_never_expiring_enrollment_token_registers_agent(
        self, test_client: TestClient, admin_headers
    ):
        enrollment = test_client.post(
            "/api/managed-machines/enrollment-tokens",
            json={"name": "permanent enrollment", "expires_never": True},
            headers=admin_headers,
        )
        assert enrollment.status_code == 201

        registered = _register_agent(test_client, enrollment.json()["token"])

        assert registered["agent_id"].startswith("agt_")

    def test_heartbeat_updates_agent_status(
        self, test_client: TestClient, test_db, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        registered = _register_agent(test_client, enrollment["token"])

        response = test_client.post(
            "/api/agents/heartbeat",
            json={
                "agent_id": registered["agent_id"],
                "hostname": "renamed.local",
                "agent_version": "0.1.1",
                "borg_versions": [
                    {"major": 2, "version": "2.0.0b10", "path": "/usr/local/bin/borg2"}
                ],
                "capabilities": ["backup.create", "backup.cancel"],
                "running_job_ids": [],
            },
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        data = response.json()
        assert data["poll_interval_seconds"] == 15
        assert data["cancel_job_ids"] == []

        agent = (
            test_db.query(AgentMachine)
            .filter(AgentMachine.agent_id == registered["agent_id"])
            .first()
        )
        assert agent.hostname == "renamed.local"
        assert agent.agent_version == "0.1.1"
        assert agent.status == "online"
        assert agent.last_seen_at is not None
        assert agent.capabilities == ["backup.create", "backup.cancel"]

    def test_heartbeat_requires_agent_token(self, test_client: TestClient):
        response = test_client.post(
            "/api/agents/heartbeat",
            json={"agent_id": "agt_missing"},
        )

        assert response.status_code == 401

    def test_heartbeat_rejects_agent_id_mismatch(
        self, test_client: TestClient, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        registered = _register_agent(test_client, enrollment["token"])

        response = test_client.post(
            "/api/agents/heartbeat",
            json={"agent_id": "agt_different"},
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 403

    def test_revoked_agent_cannot_heartbeat(
        self, test_client: TestClient, test_db, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        registered = _register_agent(test_client, enrollment["token"])
        agent = (
            test_db.query(AgentMachine)
            .filter(AgentMachine.agent_id == registered["agent_id"])
            .first()
        )

        revoke = test_client.post(
            f"/api/managed-machines/agents/{agent.id}/revoke",
            headers=admin_headers,
        )
        assert revoke.status_code == 204

        response = test_client.post(
            "/api/agents/heartbeat",
            json={"agent_id": registered["agent_id"]},
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 403

    def test_unregister_revokes_agent_token(
        self, test_client: TestClient, test_db, admin_headers
    ):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        registered = _register_agent(test_client, enrollment["token"])

        response = test_client.post(
            "/api/agents/unregister",
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 204
        agent = _get_agent(test_db, registered["agent_id"])
        test_db.refresh(agent)
        assert agent.status == "revoked"

        heartbeat = test_client.post(
            "/api/agents/heartbeat",
            json={"agent_id": registered["agent_id"]},
            headers=_agent_headers(registered["agent_token"]),
        )
        assert heartbeat.status_code == 403


@pytest.mark.unit
class TestAgentJobTransport:
    def test_websocket_session_times_out_missing_hello(
        self, test_client: TestClient, admin_headers, monkeypatch
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
            capabilities=["session.commands"],
        )
        monkeypatch.setattr(
            "app.api.agents.AGENT_SESSION_HELLO_TIMEOUT_SECONDS",
            0.01,
        )

        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect(
                "/api/agents/session",
                headers=_agent_headers(registered["agent_token"]),
            ) as websocket:
                websocket.receive_json()

        assert exc_info.value.code == 1008

    def test_websocket_session_hello_marks_agent_online_until_disconnect(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
            capabilities=["session.commands", "filesystem.browse"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        agent.status = "offline"
        test_db.commit()

        with test_client.websocket_connect(
            "/api/agents/session",
            headers=_agent_headers(registered["agent_token"]),
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "agent_id": registered["agent_id"],
                    "hostname": "session-host.local",
                    "agent_version": "0.2.0",
                    "borg_versions": [
                        {
                            "major": 2,
                            "version": "2.0.0b10",
                            "path": "/usr/local/bin/borg2",
                        }
                    ],
                    "capabilities": ["session.commands", "filesystem.browse"],
                    "running_job_ids": [],
                }
            )

            assert websocket.receive_json()["type"] == "hello_ack"
            test_db.refresh(agent)
            assert agent.status == "online"
            assert agent.hostname == "session-host.local"
            assert agent.agent_version == "0.2.0"
            assert "filesystem.browse" in agent.capabilities

        test_db.refresh(agent)
        assert agent.status == "offline"

    def test_websocket_session_dispatches_durable_job_without_polling(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
            capabilities=["session.commands", "backup.create", "logs.stream"],
        )
        agent = _get_agent(test_db, registered["agent_id"])

        with test_client.websocket_connect(
            "/api/agents/session",
            headers=_agent_headers(registered["agent_token"]),
        ) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "agent_id": registered["agent_id"],
                    "hostname": "session-host.local",
                    "agent_version": "0.2.0",
                    "borg_versions": [],
                    "capabilities": [
                        "session.commands",
                        "backup.create",
                        "logs.stream",
                    ],
                    "running_job_ids": [],
                }
            )
            assert websocket.receive_json()["type"] == "hello_ack"

            queued = test_client.post(
                f"/api/managed-machines/agents/{agent.id}/backup-jobs",
                json={
                    "repository_path": "/backups/laptop",
                    "archive_name": "laptop-now",
                    "source_paths": ["/home/user/docs"],
                },
                headers=admin_headers,
            )

            assert queued.status_code == 201
            command = websocket.receive_json()
            assert command["type"] == "command"
            assert command["command"] == "backup.create"
            assert command["job_id"] == queued.json()["id"]
            assert command["payload"]["job_kind"] == "backup.create"
            websocket.send_json(
                {
                    "type": "log",
                    "command_id": command["command_id"],
                    "job_id": queued.json()["id"],
                    "sequence": "not-a-number",
                    "stream": "stderr",
                    "message": "still handled",
                }
            )

            polled = test_client.get(
                "/api/agents/jobs/poll",
                headers=_agent_headers(registered["agent_token"]),
            )
            assert polled.status_code == 200
            assert polled.json()["jobs"] == []

        log = (
            test_db.query(AgentJobLog)
            .filter(AgentJobLog.agent_job_id == queued.json()["id"])
            .one()
        )
        assert log.sequence == 0
        assert log.message == "still handled"

    def test_admin_can_queue_backup_job_and_agent_can_poll_it(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])

        queued = test_client.post(
            f"/api/managed-machines/agents/{agent.id}/backup-jobs",
            json={
                "repository_path": "/backups/laptop",
                "archive_name": "laptop-now",
                "source_paths": ["/home/user/docs"],
                "borg_version": 1,
                "compression": "zstd",
                "exclude_patterns": ["*.tmp"],
                "custom_flags": ["--one-file-system"],
                "secrets": {"BORG_PASSPHRASE": {"value": "secret"}},
            },
            headers=admin_headers,
        )

        assert queued.status_code == 201
        queued_job = queued.json()
        assert queued_job["agent_machine_id"] == agent.id
        assert queued_job["job_type"] == "backup"
        assert queued_job["status"] == "queued"
        assert queued_job["payload"] == {
            "schema_version": 1,
            "job_kind": "backup.create",
            "repository": {
                "path": "/backups/laptop",
                "borg_version": 1,
            },
            "backup": {
                "archive_name": "laptop-now",
                "source_paths": ["/home/user/docs"],
                "compression": "zstd",
                "exclude_patterns": ["*.tmp"],
                "custom_flags": ["--one-file-system"],
            },
            "secrets": {"BORG_PASSPHRASE": {"value": "secret"}},
        }

        polled = test_client.get(
            "/api/agents/jobs/poll",
            headers=_agent_headers(registered["agent_token"]),
        )

        assert polled.status_code == 200
        jobs = polled.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["id"] == queued_job["id"]
        assert jobs[0]["payload"]["job_kind"] == "backup.create"

    def test_admin_cannot_queue_backup_job_for_revoked_agent(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        agent.status = "revoked"
        test_db.commit()

        response = test_client.post(
            f"/api/managed-machines/agents/{agent.id}/backup-jobs",
            json={
                "repository_path": "/backups/laptop",
                "archive_name": "laptop-now",
                "source_paths": ["/home/user/docs"],
            },
            headers=admin_headers,
        )

        assert response.status_code == 409

    def test_deleted_agent_cannot_poll_jobs(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        _create_agent_job(test_db, agent)

        delete = test_client.delete(
            f"/api/managed-machines/agents/{agent.id}",
            headers=admin_headers,
        )
        assert delete.status_code == 204

        response = test_client.get(
            "/api/agents/jobs/poll",
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 403

    def test_poll_only_returns_queued_jobs_for_authenticated_agent(
        self, test_client: TestClient, test_db, admin_headers
    ):
        first = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers, "first")["token"],
            name="first",
        )
        second = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers, "second")["token"],
            name="second",
        )
        first_agent = _get_agent(test_db, first["agent_id"])
        second_agent = _get_agent(test_db, second["agent_id"])
        first_job = _create_agent_job(test_db, first_agent)
        _create_agent_job(test_db, second_agent)
        _create_agent_job(test_db, first_agent, status="running")

        response = test_client.get(
            "/api/agents/jobs/poll",
            headers=_agent_headers(first["agent_token"]),
        )

        assert response.status_code == 200
        jobs = response.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["id"] == first_job.id
        assert jobs[0]["type"] == "backup"
        assert jobs[0]["payload"]["job_kind"] == "backup.create"

    def test_claim_start_progress_log_and_complete_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent)
        headers = _agent_headers(registered["agent_token"])

        claim = test_client.post(f"/api/agents/jobs/{job.id}/claim", headers=headers)
        assert claim.status_code == 200
        assert claim.json()["status"] == "claimed"

        repeated_claim = test_client.post(
            f"/api/agents/jobs/{job.id}/claim", headers=headers
        )
        assert repeated_claim.status_code == 200
        assert repeated_claim.json()["status"] == "claimed"

        start = test_client.post(
            f"/api/agents/jobs/{job.id}/start",
            json={},
            headers=headers,
        )
        assert start.status_code == 200
        assert start.json()["status"] == "running"

        progress = test_client.post(
            f"/api/agents/jobs/{job.id}/progress",
            json={
                "progress_percent": 42.5,
                "current_file": "/home/user/report.pdf",
                "original_size": 1024,
                "compressed_size": 512,
                "deduplicated_size": 128,
                "nfiles": 3,
                "backup_speed": 12.5,
                "total_expected_size": 4096,
                "estimated_time_remaining": 9,
            },
            headers=headers,
        )
        assert progress.status_code == 200

        log = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stderr",
                "message": "Creating archive",
            },
            headers=headers,
        )
        assert log.status_code == 200
        assert log.json() == {"accepted": True, "duplicate": False}

        duplicate_log = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stderr",
                "message": "Creating archive",
            },
            headers=headers,
        )
        assert duplicate_log.status_code == 200
        assert duplicate_log.json() == {"accepted": True, "duplicate": True}

        listed_logs = test_client.get(
            f"/api/managed-machines/agent-jobs/{job.id}/logs",
            headers=admin_headers,
        )
        assert listed_logs.status_code == 200
        logs = listed_logs.json()
        assert len(logs) == 1
        assert logs[0]["sequence"] == 1
        assert logs[0]["stream"] == "stderr"
        assert logs[0]["message"] == "Creating archive"

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={
                "result": {
                    "archive_name": "agent-archive",
                    "return_code": 0,
                }
            },
            headers=headers,
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed"

        test_db.refresh(job)
        assert job.status == "completed"
        assert job.progress_percent == 42.5
        assert job.current_file == "/home/user/report.pdf"
        assert job.result["archive_name"] == "agent-archive"
        assert (
            test_db.query(AgentJobLog)
            .filter(AgentJobLog.agent_job_id == job.id)
            .count()
            == 1
        )

        after_complete = test_client.post(
            f"/api/agents/jobs/{job.id}/progress",
            json={"progress_percent": 99},
            headers=headers,
        )
        assert after_complete.status_code == 409

    def test_warning_return_code_completes_with_warnings(
        self, test_client: TestClient, test_db, admin_headers
    ):
        """Borg warning exit codes (legacy 1, modern 100-127) are not failures:
        the job and its linked backup job end completed_with_warnings, the
        archive name is still captured, and the warning is surfaced as the
        same i18n message server-side backups use."""
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        backup_job = BackupJob(repository="/repo", status="running")
        test_db.add(backup_job)
        test_db.commit()
        job.backup_job_id = backup_job.id
        test_db.commit()
        headers = _agent_headers(registered["agent_token"])

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={
                "result": {
                    "archive_name": "warned-archive",
                    "return_code": 1,
                }
            },
            headers=headers,
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed_with_warnings"

        test_db.refresh(job)
        test_db.refresh(backup_job)
        assert job.status == "completed_with_warnings"
        assert "jobCompletedWithWarning" in (job.error_message or "")
        assert backup_job.status == "completed_with_warnings"
        assert backup_job.archive_name == "warned-archive"
        assert backup_job.progress == 100
        assert "backupCompletedWithWarning" in (backup_job.error_message or "")

        # A repeated report must stay idempotent for the new terminal status.
        repeated = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 1}},
            headers=headers,
        )
        assert repeated.status_code == 200

    def test_completed_backup_job_enqueues_index_followups(
        self, test_client: TestClient, test_db, admin_headers
    ):
        """A completed agent backup no longer writes last_backup itself; it
        enqueues the backup follow-up chain, and archive_sync derives the
        column from the listing (#933)."""
        load_default_executors()
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        repo = Repository(name="linked", path="/repo", encryption="none")
        test_db.add(repo)
        test_db.commit()
        job = _create_agent_job(test_db, agent, status="running")
        backup_job = BackupJob(
            repository="/repo", repository_id=repo.id, status="running"
        )
        test_db.add(backup_job)
        test_db.commit()
        job.backup_job_id = backup_job.id
        test_db.commit()

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"archive_name": "a1", "return_code": 0}},
            headers=_agent_headers(registered["agent_token"]),
        )
        assert complete.status_code == 200

        test_db.refresh(repo)
        assert repo.last_backup is None
        ops = (
            test_db.query(Operation)
            .filter(Operation.repository_id == repo.id)
            .order_by(Operation.id)
            .all()
        )
        assert [o.kind for o in ops][:1] == ["archive_sync"]
        assert {o.trigger for o in ops} == {"followup"}

    def test_failed_followup_enqueue_never_fails_the_backup(
        self, test_client: TestClient, test_db, admin_headers, monkeypatch
    ):
        """A flush failure inside the follow-up enqueue (here a colliding
        Operation id, the shape enqueue() produces) is undone in its own
        savepoint: the completion still succeeds and the terminal state of
        the agent job and the backup job is committed, with no chain rows."""
        from sqlalchemy.exc import IntegrityError

        load_default_executors()
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        repo = Repository(name="linked", path="/repo", encryption="none")
        test_db.add(repo)
        test_db.commit()
        from app.services.operations.enqueue import enqueue

        existing = enqueue(test_db, "stats", repository_id=repo.id)
        job = _create_agent_job(test_db, agent, status="running")
        backup_job = BackupJob(
            repository="/repo", repository_id=repo.id, status="running"
        )
        test_db.add(backup_job)
        test_db.commit()
        job.backup_job_id = backup_job.id
        test_db.commit()
        seen = {}

        def colliding_enqueue(db, repository_id, **kwargs):
            db.add(
                Operation(
                    id=existing.id,
                    run_id=existing.run_id,
                    kind="archive_sync",
                    category="index",
                    status="queued",
                    trigger="followup",
                    repository_id=repository_id,
                )
            )
            try:
                db.flush()
            except IntegrityError as exc:
                seen["error"] = exc
                raise
            raise AssertionError("the colliding flush did not fail")

        monkeypatch.setattr(
            "app.api.agents.enqueue_backup_followups", colliding_enqueue
        )
        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"archive_name": "a1", "return_code": 0}},
            headers=_agent_headers(registered["agent_token"]),
        )
        assert complete.status_code == 200, complete.text
        assert "error" in seen
        test_db.expire_all()
        assert test_db.get(AgentJob, job.id).status == "completed"
        assert test_db.get(BackupJob, backup_job.id).status == "completed"
        assert test_db.get(BackupJob, backup_job.id).archive_name == "a1"
        assert (
            test_db.query(Operation).filter(Operation.repository_id == repo.id).count()
            == 1
        )

    def test_modern_warning_range_counts_as_warning(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        headers = _agent_headers(registered["agent_token"])

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 104}},
            headers=headers,
        )
        assert complete.status_code == 200
        test_db.refresh(job)
        assert job.status == "completed_with_warnings"

    def test_error_return_code_in_completion_report_fails_the_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        """A completion report carrying an explicit borg error code (2-99) is
        a failure - the server classifies, not the transport."""
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        headers = _agent_headers(registered["agent_token"])

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 2}},
            headers=headers,
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "failed"

        test_db.refresh(job)
        assert job.status == "failed"
        assert "exited with code 2" in (job.error_message or "")

    @pytest.mark.parametrize("malformed", ["2", 2.0, True, [1]])
    def test_malformed_return_code_in_completion_report_fails_closed(
        self, test_client: TestClient, test_db, admin_headers, malformed
    ):
        """A completion report is only trusted with an int return code;
        anything else fails the job (and its linked backup job) instead of
        slipping past the classification as a success."""
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        backup_job = BackupJob(repository="/repo", status="running")
        test_db.add(backup_job)
        test_db.commit()
        job.backup_job_id = backup_job.id
        test_db.commit()
        headers = _agent_headers(registered["agent_token"])

        complete = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": malformed}},
            headers=headers,
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "failed"

        test_db.refresh(job)
        test_db.refresh(backup_job)
        assert job.status == "failed"
        assert "malformed return code" in (job.error_message or "")
        assert backup_job.status == "failed"

    def test_repeated_complete_report_is_idempotent(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        headers = _agent_headers(registered["agent_token"])

        first = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"archive_name": "first-archive", "return_code": 0}},
            headers=headers,
        )
        second = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"archive_name": "retry-archive", "return_code": 0}},
            headers=headers,
        )

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["status"] == "completed"
        test_db.refresh(job)
        assert job.result == {"archive_name": "first-archive", "return_code": 0}

    def test_heartbeat_requeues_stale_claimed_job_not_running_on_agent(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job = _create_agent_job(test_db, agent, status="claimed")
        job.claimed_at = stale_at
        job.updated_at = stale_at
        test_db.commit()

        response = test_client.post(
            "/api/agents/heartbeat",
            json={
                "agent_id": registered["agent_id"],
                "agent_version": "0.1.1",
                "borg_versions": [],
                "capabilities": ["backup.create"],
                "running_job_ids": [],
            },
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        test_db.refresh(job)
        assert job.status == "queued"
        assert job.claimed_at is None

    def _create_repository_job(
        self, test_db, agent, *, job_kind, operation, stale_at
    ) -> AgentJob:
        job = AgentJob(
            agent_machine_id=agent.id,
            job_type="repository",
            status="running",
            payload={
                "schema_version": 1,
                "job_kind": job_kind,
                "repository": {"id": 7},
                "operation": operation,
            },
            created_at=stale_at,
            started_at=stale_at,
            updated_at=stale_at,
        )
        test_db.add(job)
        test_db.commit()
        test_db.refresh(job)
        return job

    def test_heartbeat_fails_stale_request_scoped_repository_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job = self._create_repository_job(
            test_db,
            agent,
            job_kind="repository.extract_archive_file",
            operation={"archive": "a", "file_path": "f"},
            stale_at=stale_at,
        )

        response = test_client.post(
            "/api/agents/heartbeat",
            json={
                "agent_id": registered["agent_id"],
                "agent_version": "0.1.1",
                "borg_versions": [],
                "capabilities": ["backup.create"],
                "running_job_ids": [],
            },
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        test_db.refresh(job)
        # No receiver for the timed-out download request -> fail, don't restart.
        assert job.status == "failed"
        assert "no client is waiting" in (job.error_message or "")

    def test_heartbeat_requeues_stale_durable_repository_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job = self._create_repository_job(
            test_db,
            agent,
            job_kind="repository.check",
            operation={"maintenance_job": {"kind": "check", "id": 1}},
            stale_at=stale_at,
        )

        response = test_client.post(
            "/api/agents/heartbeat",
            json={
                "agent_id": registered["agent_id"],
                "agent_version": "0.1.1",
                "borg_versions": [],
                "capabilities": ["backup.create"],
                "running_job_ids": [],
            },
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        test_db.refresh(job)
        # Durable maintenance op keeps its retry-on-reconnect behaviour.
        assert job.status == "queued"

    def test_heartbeat_requeues_stale_rclone_sync_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        # rclone_sync owns a durable RcloneSyncJob record and also runs after
        # backups, so it must be retried on reconnect, not failed.
        job = self._create_repository_job(
            test_db,
            agent,
            job_kind="repository.rclone_sync",
            operation={"rclone": {"remote_name": "r", "remote_path": "p"}},
            stale_at=stale_at,
        )

        response = test_client.post(
            "/api/agents/heartbeat",
            json={
                "agent_id": registered["agent_id"],
                "agent_version": "0.1.1",
                "borg_versions": [],
                "capabilities": ["backup.create"],
                "running_job_ids": [],
            },
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        test_db.refresh(job)
        assert job.status == "queued"

    def test_upload_job_artifact_drops_body_without_consumer(
        self, test_client: TestClient, test_db, admin_headers
    ):
        # No download is waiting (relay channel not registered) -> the endpoint
        # drains the body and reports it was dropped, so the agent never blocks.
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/artifact",
            content=b"orphaned-bytes",
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        assert response.json() == {"accepted": False, "size": 0}

    def test_agent_cannot_mutate_another_agents_job(
        self, test_client: TestClient, test_db, admin_headers
    ):
        first = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers, "first")["token"],
            name="first",
        )
        second = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers, "second")["token"],
            name="second",
        )
        second_agent = _get_agent(test_db, second["agent_id"])
        second_job = _create_agent_job(test_db, second_agent)

        response = test_client.post(
            f"/api/agents/jobs/{second_job.id}/claim",
            headers=_agent_headers(first["agent_token"]),
        )

        assert response.status_code == 404

    def test_fail_job_records_error_and_return_code(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/fail",
            json={"error_message": "borg exited with code 2", "return_code": 2},
            headers=_agent_headers(registered["agent_token"]),
        )

        assert response.status_code == 200
        test_db.refresh(job)
        assert job.status == "failed"
        assert job.error_message == "borg exited with code 2"
        assert job.result == {"return_code": 2}

    def test_admin_cancel_request_reaches_running_agent_via_heartbeat(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")

        cancel = test_client.post(
            f"/api/managed-machines/agent-jobs/{job.id}/cancel",
            headers=admin_headers,
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "cancel_requested"

        heartbeat = test_client.post(
            "/api/agents/heartbeat",
            json={"agent_id": registered["agent_id"], "running_job_ids": [job.id]},
            headers=_agent_headers(registered["agent_token"]),
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["cancel_job_ids"] == [job.id]

        canceled = test_client.post(
            f"/api/agents/jobs/{job.id}/cancel",
            json={},
            headers=_agent_headers(registered["agent_token"]),
        )
        assert canceled.status_code == 200
        assert canceled.json()["status"] == "canceled"

    def test_admin_can_list_agent_jobs(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent)

        response = test_client.get(
            "/api/managed-machines/agent-jobs", headers=admin_headers
        )

        assert response.status_code == 200
        jobs = response.json()
        assert len(jobs) == 1
        assert jobs[0]["id"] == job.id


class TestAgentJobReaper:
    def _stale_running_job(self, test_db, test_client, admin_headers, status="running"):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status=status)
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job.started_at = stale_at
        job.updated_at = stale_at
        test_db.commit()
        return job

    def test_reaps_stale_in_flight_job(self, test_client, test_db, admin_headers):
        from app.services.agent_job_reaper import reap_stale_agent_jobs

        job = self._stale_running_job(test_db, test_client, admin_headers)

        reaped = reap_stale_agent_jobs(test_db)

        assert reaped == 1
        test_db.refresh(job)
        assert job.status == "failed"
        assert job.completed_at is not None
        assert "orphaned" in (job.error_message or "")

    def test_spares_recently_active_job(self, test_client, test_db, admin_headers):
        from app.services.agent_job_reaper import reap_stale_agent_jobs

        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")  # updated_at = now

        reaped = reap_stale_agent_jobs(test_db)

        assert reaped == 0
        test_db.refresh(job)
        assert job.status == "running"

    def test_ignores_terminal_jobs(self, test_client, test_db, admin_headers):
        from app.services.agent_job_reaper import reap_stale_agent_jobs

        job = self._stale_running_job(
            test_db, test_client, admin_headers, status="completed"
        )

        reaped = reap_stale_agent_jobs(test_db)

        assert reaped == 0
        test_db.refresh(job)
        assert job.status == "completed"

    def test_fails_linked_backup_job(self, test_client, test_db, admin_headers):
        from app.services.agent_job_reaper import reap_stale_agent_jobs
        from app.database.models import BackupJob

        backup_job = BackupJob(
            repository="/repo",
            status="running",
        )
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)

        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        # Link and stale-ify in a single commit; updated_at has an onupdate, so a
        # later separate commit would refresh the timestamp and un-stale the job.
        job.backup_job_id = backup_job.id
        job.started_at = stale_at
        job.updated_at = stale_at
        test_db.commit()

        reaped = reap_stale_agent_jobs(test_db)

        assert reaped == 1
        test_db.refresh(backup_job)
        assert backup_job.status == "failed"

    def test_does_not_overwrite_terminal_backup_job(
        self, test_client, test_db, admin_headers
    ):
        from app.services.agent_job_reaper import reap_stale_agent_jobs
        from app.database.models import BackupJob

        # An already-finished backup must not be flipped back to failed when its
        # AgentJob gets stale-reaped after the fact.
        backup_job = BackupJob(repository="/repo", status="completed")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)

        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        job = _create_agent_job(test_db, agent, status="running")
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job.backup_job_id = backup_job.id
        job.started_at = stale_at
        job.updated_at = stale_at
        test_db.commit()

        reap_stale_agent_jobs(test_db)

        test_db.refresh(backup_job)
        assert backup_job.status == "completed"


NOTIFIER_PATCH_TARGET = "app.services.agent_job_notifications.notification_service"


@pytest.mark.unit
class TestAgentJobNotifications:
    """Agent-executed jobs must fire the same user-facing notifications the
    server-side execution paths send from inside their services."""

    def _register(self, test_client, test_db, admin_headers):
        registered = _register_agent(
            test_client,
            _create_enrollment_token(test_client, admin_headers)["token"],
        )
        agent = _get_agent(test_db, registered["agent_id"])
        return agent, _agent_headers(registered["agent_token"])

    def _linked_backup_job(self, test_db, agent, *, agent_status="running"):
        backup_job = BackupJob(repository="/repo", status="running")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)
        job = _create_agent_job(test_db, agent, status=agent_status)
        job.backup_job_id = backup_job.id
        test_db.commit()
        return job, backup_job

    def test_completed_backup_job_sends_success_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/complete",
                json={"result": {"archive_name": "agent-archive", "return_code": 0}},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_backup_success.assert_awaited_once()
        args = notifier.send_backup_success.await_args.args
        assert args[1] == "/repo"
        assert args[2] == "agent-archive"
        notifier.send_backup_warning.assert_not_awaited()
        notifier.send_backup_failure.assert_not_awaited()

    def test_warning_completion_sends_warning_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/complete",
                json={"result": {"archive_name": "agent-archive", "return_code": 1}},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_backup_warning.assert_awaited_once()
        notifier.send_backup_success.assert_not_awaited()

    def test_error_return_code_sends_failure_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/complete",
                json={"result": {"return_code": 2}},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_backup_failure.assert_awaited_once()
        args = notifier.send_backup_failure.await_args.args
        assert args[1] == "/repo"
        assert "borg exited with code 2" in args[2]
        assert args[3] == backup_job.id
        notifier.send_backup_success.assert_not_awaited()

    def test_failed_job_sends_failure_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/fail",
                json={"error_message": "disk full", "return_code": 2},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_backup_failure.assert_awaited_once()
        args = notifier.send_backup_failure.await_args.args
        assert args[2] == "disk full"

    def test_repeated_completion_does_not_duplicate_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            for _ in range(2):
                response = test_client.post(
                    f"/api/agents/jobs/{job.id}/complete",
                    json={
                        "result": {"archive_name": "agent-archive", "return_code": 0}
                    },
                    headers=headers,
                )
                assert response.status_code == 200

        notifier.send_backup_success.assert_awaited_once()

    def test_first_start_report_sends_backup_start_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(
            test_db, agent, agent_status="claimed"
        )

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            for _ in range(2):
                response = test_client.post(
                    f"/api/agents/jobs/{job.id}/start",
                    json={},
                    headers=headers,
                )
                assert response.status_code == 200

        notifier.send_backup_start.assert_awaited_once()
        args = notifier.send_backup_start.await_args.args
        assert args[1] == "/repo"

    def _agent_check_job(self, test_db, agent, *, name="agent-check-repo"):
        repository = Repository(name=name, path=f"/{name}")
        test_db.add(repository)
        test_db.commit()
        test_db.refresh(repository)
        check_job = CheckJob(repository_id=repository.id, status="running")
        test_db.add(check_job)
        test_db.commit()
        test_db.refresh(check_job)
        now = datetime.now(timezone.utc)
        job = AgentJob(
            agent_machine_id=agent.id,
            job_type="repository",
            status="running",
            payload={
                "schema_version": 1,
                "job_kind": "repository.check",
                "repository": {"id": repository.id},
                "operation": {"maintenance_job": {"kind": "check", "id": check_job.id}},
            },
            created_at=now,
            updated_at=now,
        )
        test_db.add(job)
        test_db.commit()
        test_db.refresh(job)
        return job, check_job, repository

    def _running_compact_operation(self, test_db, repository):
        operation = Operation(
            repository_id=repository.id,
            kind="compact",
            category="maintenance",
            status="running",
            trigger="manual",
            priority=10,
            run_id="run-compact",
        )
        test_db.add(operation)
        test_db.commit()
        return operation

    def _compact_agent_job(self, test_db, agent, repository, operation):
        now = datetime.now(timezone.utc)
        job = AgentJob(
            agent_machine_id=agent.id,
            job_type="repository",
            status="running",
            payload={
                "schema_version": 1,
                "job_kind": "repository.compact",
                "repository": {"id": repository.id},
                "operation": {
                    "maintenance_job": {"kind": "compact", "id": operation.id}
                },
            },
            created_at=now,
            updated_at=now,
        )
        test_db.add(job)
        test_db.commit()
        return job

    def test_agent_compact_completion_persists_stats_from_job_log(
        self, test_client, test_db, admin_headers
    ):
        """An agent compact that ran with --stats --info streams the INFO
        statistics lines into the job log; completion parses them onto the
        operation's result and refreshes total_size (#931)."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(name="agent-compact-repo", path="/agent-compact")
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)
        for sequence, message in enumerate(
            (
                "Repository size is 502000 B in 6 objects.",
                "Compaction saved 0 B.",
            ),
            start=1,
        ):
            log = test_client.post(
                f"/api/agents/jobs/{job.id}/logs",
                json={
                    "sequence": sequence,
                    "stream": "stderr",
                    "message": json.dumps(
                        {
                            "type": "log_message",
                            "levelname": "INFO",
                            "name": "borg.archiver.compact_cmd",
                            "message": message,
                        }
                    ),
                },
                headers=headers,
            )
            assert log.status_code == 200, log.text

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(operation)
        test_db.refresh(repository)
        assert operation.status == "completed"
        assert operation.result["stats"]["repository_size"] == 502_000
        assert operation.result["stats"]["compaction_saved"] == 0
        assert repository.total_size == "490.23 KB"
        assert repository.total_size_source == "compact_stats"

        status = test_client.get(
            f"/api/repositories/compact-jobs/{operation.id}",
            headers=admin_headers,
        )
        assert status.status_code == 200, status.text
        assert status.json()["stats"]["repository_size"] == 502_000

    def test_agent_compact_statistics_arriving_after_completion_are_absorbed(
        self, test_client, test_db, admin_headers
    ):
        """Seen live: the agent's last log lines and its completion are
        handled on different paths and the completion won the race by
        150 ms, so the parse at completion saw no statistics. A later log
        line for a completed compact re-collects and parses them, and the
        operation's log file grows with it (the facade's `logs` setter is a
        no-op once the file exists)."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(
            name="agent-compact-late", path="/agent-compact-late", total_size="keep"
        )
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)

        def post_log(sequence, message):
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/logs",
                json={
                    "sequence": sequence,
                    "stream": "stdout",
                    "message": json.dumps(
                        {
                            "type": "log_message",
                            "levelname": "INFO",
                            "name": "borg.archiver.compact_cmd",
                            "message": message,
                        }
                    ),
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text

        post_log(1, "Starting compaction / garbage collection...")
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(operation)
        assert operation.status == "completed"
        assert (operation.result or {}).get("stats") is None
        assert Path(operation.log_file_path).read_text().count("log_message") == 1

        # the statistics lines arrive after the completion
        post_log(2, "Repository size is 1836754184 B in 2796 objects.")
        post_log(3, "Compaction saved 0 B.")
        test_db.refresh(operation)
        test_db.refresh(repository)
        stats = operation.result["stats"]
        assert stats["repository_size"] == 1_836_754_184
        assert stats["size_precision"] == "exact"
        assert stats["compaction_saved"] == 0
        assert "Compaction saved" in Path(operation.log_file_path).read_text()
        assert repository.total_size == "1.71 GB"
        assert repository.total_size_source == "compact_stats"

        # a further line is kept in the log; the statistics stay
        post_log(4, "Finished compaction / garbage collection...")
        test_db.refresh(operation)
        assert operation.result["stats"]["repository_size"] == 1_836_754_184
        assert Path(operation.log_file_path).read_text().count("log_message") == 4

    def test_late_compact_statistics_keep_a_size_measured_since(
        self, test_client, test_db, admin_headers
    ):
        """The `stats` follow-up is enqueued the moment the compact completes
        and can finish before the agent's last frames drain. Its measurement
        is newer and stands; the statistics still land on the operation."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(name="agent-compact-newer", path="/agent-newer")
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(operation)
        followup = Operation(
            repository_id=repository.id,
            kind="stats",
            category="index",
            status="completed",
            trigger="followup",
            priority=10,
            run_id="run-compact",
            depends_on_id=operation.id,
            completed_at=operation.completed_at + timedelta(seconds=1),
            result={"bytes": 7_000_000_000, "size_refreshed": True},
        )
        repository.total_size = "7.00 GB"
        repository.total_size_source = "borg2_index"
        test_db.add(followup)
        test_db.commit()

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stdout",
                "message": "Repository size is 5 B in 1 objects.",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        test_db.refresh(operation)
        test_db.refresh(repository)
        assert operation.result["stats"]["repository_size"] == 5
        assert repository.total_size == "7.00 GB"
        assert repository.total_size_source == "borg2_index"

    def test_late_compact_statistics_replace_a_size_the_followup_could_not_measure(
        self, test_client, test_db, admin_headers
    ):
        """A `stats` follow-up that completed without writing a size (nothing
        can measure this repository) leaves the compact's figure as the one
        to use, however old the stored size is."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(
            name="agent-compact-unmeasurable",
            path="/agent-unmeasurable",
            total_size="7.00 GB",
            total_size_source="storage_used",
        )
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(operation)
        test_db.add(
            Operation(
                repository_id=repository.id,
                kind="stats",
                category="index",
                status="completed",
                trigger="followup",
                priority=10,
                run_id="run-compact",
                depends_on_id=operation.id,
                completed_at=operation.completed_at + timedelta(seconds=1),
                result={"total_size": "7.00 GB", "size_refreshed": False},
            )
        )
        test_db.commit()

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stdout",
                "message": "Repository size is 1836754184 B in 2796 objects.",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        test_db.refresh(repository)
        assert repository.total_size == "1.71 GB"
        assert repository.total_size_source == "compact_stats"

    def test_late_compact_statistics_are_ignored_long_after_completion(
        self, test_client, test_db, admin_headers
    ):
        """The absorption exists for lines the outbox delivers seconds after
        the completion. A line for a job that completed long ago is stored
        as a log row and changes nothing else."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(
            name="agent-compact-stale", path="/agent-stale", total_size="keep"
        )
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(job)
        job.updated_at = job.updated_at - timedelta(hours=1)
        test_db.commit()

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stdout",
                "message": "Repository size is 0 B in 0 objects.",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        test_db.refresh(operation)
        test_db.refresh(repository)
        assert (operation.result or {}).get("stats") is None
        assert repository.total_size == "keep"
        assert "Repository size" not in Path(operation.log_file_path).read_text()
        assert (
            test_db.query(AgentJobLog)
            .filter(AgentJobLog.agent_job_id == job.id)
            .count()
            == 1
        )

    def test_late_compact_statistics_on_a_legacy_row(
        self, test_client, test_db, admin_headers
    ):
        """A compact queued before phase 5 lives in `compact_jobs`. Its late
        statistics still refresh the repository size and its `logs` column
        grows; the statistics themselves have no column there and are not
        kept."""
        from sqlalchemy.orm import Session as SASession

        from app.database.models import CompactJob

        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(name="agent-compact-legacy", path="/agent-legacy")
        test_db.add(repository)
        test_db.commit()
        compact_job = CompactJob(repository_id=repository.id, status="running")
        test_db.add(compact_job)
        test_db.commit()
        now = datetime.now(timezone.utc)
        job = AgentJob(
            agent_machine_id=agent.id,
            job_type="repository",
            status="running",
            payload={
                "schema_version": 1,
                "job_kind": "repository.compact",
                "repository": {"id": repository.id},
                "operation": {
                    "maintenance_job": {"kind": "compact", "id": compact_job.id}
                },
            },
            created_at=now,
            updated_at=now,
        )
        test_db.add(job)
        test_db.commit()
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}},
            headers=headers,
        )
        assert response.status_code == 200

        response = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stdout",
                "message": "Repository size is 502000 B in 6 objects.",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        test_db.expire_all()
        refreshed = test_db.get(CompactJob, compact_job.id)
        test_db.refresh(repository)
        assert refreshed.status == "completed"
        assert refreshed.logs.endswith("Repository size is 502000 B in 6 objects.")
        assert refreshed.has_logs is True
        assert "stats" not in CompactJob.__table__.columns
        with SASession(bind=test_db.get_bind()) as fresh:
            assert getattr(fresh.get(CompactJob, compact_job.id), "stats", None) is None
        assert repository.total_size == "490.23 KB"
        assert repository.total_size_source == "compact_stats"

    def test_compact_completion_keeps_a_size_measured_while_it_was_unreported(
        self, test_client, test_db, admin_headers
    ):
        """The agent finished, lost its connection, and reports ten minutes
        later; a `stats` follow-up measured the repository in between. The
        completion parse must not overwrite that with the older figure."""
        agent, headers = self._register(test_client, test_db, admin_headers)
        repository = Repository(
            name="agent-compact-reported-late",
            path="/agent-reported-late",
            total_size="7.00 GB",
            total_size_source="borg2_index",
        )
        test_db.add(repository)
        test_db.commit()
        operation = self._running_compact_operation(test_db, repository)
        job = self._compact_agent_job(test_db, agent, repository, operation)
        finished = datetime.now(timezone.utc) - timedelta(minutes=10)
        test_db.add(
            Operation(
                repository_id=repository.id,
                kind="stats",
                category="index",
                status="completed",
                trigger="schedule",
                priority=10,
                run_id="run-between",
                completed_at=(finished + timedelta(minutes=5)).replace(tzinfo=None),
                result={"bytes": 7_000_000_000, "size_refreshed": True},
            )
        )
        test_db.commit()
        log = test_client.post(
            f"/api/agents/jobs/{job.id}/logs",
            json={
                "sequence": 1,
                "stream": "stderr",
                "message": "Repository size is 502000 B in 6 objects.",
            },
            headers=headers,
        )
        assert log.status_code == 200, log.text
        response = test_client.post(
            f"/api/agents/jobs/{job.id}/complete",
            json={"result": {"return_code": 0}, "completed_at": finished.isoformat()},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        test_db.refresh(operation)
        test_db.refresh(repository)
        assert operation.result["stats"]["repository_size"] == 502_000
        assert repository.total_size == "7.00 GB"
        assert repository.total_size_source == "borg2_index"

    def test_agent_check_completion_sends_check_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, check_job, repository = self._agent_check_job(test_db, agent)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/complete",
                json={"result": {"return_code": 0}},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_check_completion.assert_awaited_once()
        kwargs = notifier.send_check_completion.await_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["repository_name"] == repository.name
        assert kwargs["check_type"] == "manual"

    def test_agent_check_failure_sends_check_notification(
        self, test_client, test_db, admin_headers
    ):
        agent, headers = self._register(test_client, test_db, admin_headers)
        job, check_job, repository = self._agent_check_job(
            test_db, agent, name="agent-check-repo-2"
        )

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            response = test_client.post(
                f"/api/agents/jobs/{job.id}/fail",
                json={"error_message": "check failed", "return_code": 2},
                headers=headers,
            )

        assert response.status_code == 200
        notifier.send_check_completion.assert_awaited_once()
        kwargs = notifier.send_check_completion.await_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["error_message"] == "check failed"

    async def test_notify_backup_job_finished_prefers_plan_name(self, test_db):
        from app.services.agent_job_notifications import notify_backup_job_finished

        plan = BackupPlan(name="nightly", source_directories="[]")
        test_db.add(plan)
        test_db.commit()
        test_db.refresh(plan)
        backup_job = BackupJob(
            repository="/repo",
            status="failed",
            backup_plan_id=plan.id,
            error_message="agent session lost",
        )
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            await notify_backup_job_finished(test_db, backup_job)

        notifier.send_backup_failure.assert_awaited_once()
        args = notifier.send_backup_failure.await_args.args
        assert args[2] == "agent session lost"
        assert args[4] == "nightly"

    async def test_notify_backup_job_finished_skips_cancelled(self, test_db):
        from app.services.agent_job_notifications import notify_backup_job_finished

        backup_job = BackupJob(repository="/repo", status="cancelled")
        test_db.add(backup_job)
        test_db.commit()

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            await notify_backup_job_finished(test_db, backup_job)

        notifier.send_backup_failure.assert_not_awaited()
        notifier.send_backup_success.assert_not_awaited()

    def test_reaper_collects_failed_backup_jobs_for_notification(
        self, test_client, test_db, admin_headers
    ):
        from app.services.agent_job_reaper import reap_stale_agent_jobs

        agent, _headers = self._register(test_client, test_db, admin_headers)
        backup_job = BackupJob(repository="/repo", status="running")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)
        job = _create_agent_job(test_db, agent, status="running")
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job.backup_job_id = backup_job.id
        job.started_at = stale_at
        job.updated_at = stale_at
        test_db.commit()

        failed_backup_job_ids: list[int] = []
        reaped = reap_stale_agent_jobs(
            test_db, failed_backup_job_ids=failed_backup_job_ids
        )

        assert reaped == 1
        assert failed_backup_job_ids == [backup_job.id]

    def test_reaper_does_not_collect_terminal_backup_jobs(
        self, test_client, test_db, admin_headers
    ):
        from app.services.agent_job_reaper import reap_stale_agent_jobs

        agent, _headers = self._register(test_client, test_db, admin_headers)
        backup_job = BackupJob(repository="/repo", status="completed")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)
        job = _create_agent_job(test_db, agent, status="running")
        stale_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        job.backup_job_id = backup_job.id
        job.started_at = stale_at
        job.updated_at = stale_at
        test_db.commit()

        failed_backup_job_ids: list[int] = []
        reap_stale_agent_jobs(test_db, failed_backup_job_ids=failed_backup_job_ids)

        assert failed_backup_job_ids == []

    def test_stale_completion_report_cannot_double_finalize(
        self, test_client, test_db, admin_headers
    ):
        from sqlalchemy.orm import Session as SASession

        from app.api.agents import _complete_agent_job

        agent, _headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        # A second session models the other transport holding a stale view of
        # the still-running job (its read transaction already closed, as after
        # a completed request cycle).
        stale_db = SASession(bind=test_db.get_bind(), expire_on_commit=False)
        try:
            stale_job = stale_db.query(AgentJob).filter(AgentJob.id == job.id).first()
            assert stale_job.status == "running"
            stale_db.commit()

            # The reaper (or the other transport) finalizes the job first.
            test_db.query(AgentJob).filter(AgentJob.id == job.id).update(
                {AgentJob.status: "failed", AgentJob.error_message: "reaped"},
                synchronize_session=False,
            )
            test_db.query(BackupJob).filter(BackupJob.id == backup_job.id).update(
                {BackupJob.status: "failed", BackupJob.error_message: "reaped"},
                synchronize_session=False,
            )
            test_db.commit()

            transitioned = _complete_agent_job(
                stale_job, stale_db, result={"archive_name": "late", "return_code": 0}
            )
            stale_db.commit()
            assert transitioned is False
        finally:
            stale_db.close()

        test_db.expire_all()
        assert (
            test_db.query(AgentJob).filter(AgentJob.id == job.id).first().status
            == "failed"
        )
        linked = test_db.query(BackupJob).filter(BackupJob.id == backup_job.id).first()
        assert linked.status == "failed"
        assert linked.error_message == "reaped"

    def test_stale_start_report_does_not_claim_start_notification(
        self, test_client, test_db, admin_headers
    ):
        from sqlalchemy.orm import Session as SASession

        from app.api.agents import _mark_agent_job_started

        agent, _headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(
            test_db, agent, agent_status="claimed"
        )

        stale_db = SASession(bind=test_db.get_bind(), expire_on_commit=False)
        try:
            stale_job = stale_db.query(AgentJob).filter(AgentJob.id == job.id).first()
            stale_db.commit()

            first = _mark_agent_job_started(job, test_db)
            test_db.commit()
            assert first is not None

            second = _mark_agent_job_started(stale_job, stale_db)
            stale_db.commit()
            assert second is None
        finally:
            stale_db.close()

    async def test_expired_object_reads_stay_inside_notification_boundary(
        self, test_db
    ):
        from app.services.agent_job_notifications import notify_backup_job_finished

        backup_job = BackupJob(repository="/repo", status="failed", error_message="x")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)
        # Detached + expired: every attribute read raises, modeling a refresh
        # failure on the committed (expired) row after the job turned final.
        test_db.expire(backup_job)
        test_db.expunge(backup_job)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            await notify_backup_job_finished(test_db, backup_job)

        notifier.send_backup_failure.assert_not_awaited()

    async def test_notifier_failure_does_not_propagate(self, test_db):
        from app.services.agent_job_notifications import notify_backup_job_finished

        backup_job = BackupJob(repository="/repo", status="failed", error_message="x")
        test_db.add(backup_job)
        test_db.commit()
        test_db.refresh(backup_job)

        with patch(NOTIFIER_PATCH_TARGET, new_callable=AsyncMock) as notifier:
            notifier.send_backup_failure.side_effect = RuntimeError("boom")
            await notify_backup_job_finished(test_db, backup_job)

        notifier.send_backup_failure.assert_awaited_once()

    def test_stale_cancel_report_cannot_overwrite_terminal_state(
        self, test_client, test_db, admin_headers
    ):
        from sqlalchemy.orm import Session as SASession

        from app.api.agents import _cancel_agent_job

        agent, _headers = self._register(test_client, test_db, admin_headers)
        job, backup_job = self._linked_backup_job(test_db, agent)

        stale_db = SASession(bind=test_db.get_bind(), expire_on_commit=False)
        try:
            stale_job = stale_db.query(AgentJob).filter(AgentJob.id == job.id).first()
            stale_db.commit()

            test_db.query(AgentJob).filter(AgentJob.id == job.id).update(
                {AgentJob.status: "failed", AgentJob.error_message: "reaped"},
                synchronize_session=False,
            )
            test_db.query(BackupJob).filter(BackupJob.id == backup_job.id).update(
                {BackupJob.status: "failed", BackupJob.error_message: "reaped"},
                synchronize_session=False,
            )
            test_db.commit()

            _cancel_agent_job(stale_job, stale_db)
            stale_db.commit()
        finally:
            stale_db.close()

        test_db.expire_all()
        assert (
            test_db.query(AgentJob).filter(AgentJob.id == job.id).first().status
            == "failed"
        )
        assert (
            test_db.query(BackupJob)
            .filter(BackupJob.id == backup_job.id)
            .first()
            .status
            == "failed"
        )


@pytest.mark.unit
class TestAgentTimezone:
    """The agent's reported IANA zone interprets borg's local-time archive
    timestamps; only resolvable names may be stored."""

    def _register_with_timezone(self, test_client, admin_headers, tz):
        enrollment = _create_enrollment_token(test_client, admin_headers)
        response = test_client.post(
            "/api/agents/register",
            json={
                "enrollment_token": enrollment["token"],
                "name": "tz-agent",
                "hostname": "tz.local",
                "os": "linux",
                "arch": "amd64",
                "agent_version": "0.1.1",
                "timezone": tz,
                "borg_versions": [],
                "capabilities": ["backup.create"],
            },
        )
        assert response.status_code == 200
        return response.json()

    def test_register_persists_valid_timezone(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = self._register_with_timezone(
            test_client, admin_headers, "Europe/Berlin"
        )

        agent = _get_agent(test_db, registered["agent_id"])
        assert agent.timezone == "Europe/Berlin"

    def test_register_drops_invalid_timezone(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = self._register_with_timezone(
            test_client, admin_headers, "Not/AZone"
        )

        agent = _get_agent(test_db, registered["agent_id"])
        assert agent.timezone is None

    def test_heartbeat_updates_timezone_but_keeps_it_on_omission(
        self, test_client: TestClient, test_db, admin_headers
    ):
        registered = self._register_with_timezone(
            test_client, admin_headers, "Europe/Berlin"
        )
        headers = _agent_headers(registered["agent_token"])

        heartbeat = {
            "agent_id": registered["agent_id"],
            "hostname": "tz.local",
            "agent_version": "0.1.1",
            "borg_versions": [],
            "capabilities": ["backup.create"],
            "running_job_ids": [],
        }

        # A heartbeat without a zone (old agent) must not erase the stored one.
        response = test_client.post(
            "/api/agents/heartbeat", json=heartbeat, headers=headers
        )
        assert response.status_code == 200
        agent = _get_agent(test_db, registered["agent_id"])
        assert agent.timezone == "Europe/Berlin"

        response = test_client.post(
            "/api/agents/heartbeat",
            json={**heartbeat, "timezone": "America/New_York"},
            headers=headers,
        )
        assert response.status_code == 200
        test_db.refresh(agent)
        assert agent.timezone == "America/New_York"


@pytest.mark.unit
def test_append_operation_log_appends_lines_in_order(tmp_path):
    """The facade's `logs` setter is a no-op once the file exists; late lines
    go to the file directly, one per line, no leading blank line on an empty
    transcript, and an empty message adds nothing."""
    from types import SimpleNamespace

    from app.api.agents import _append_operation_log
    from app.services.operations.job_facade import MaintenanceJobFacade

    log = tmp_path / "operation_1.log"
    log.write_text("")
    operation = SimpleNamespace(id=1, log_file_path=str(log), kind="compact")
    facade = MaintenanceJobFacade.__new__(MaintenanceJobFacade)
    object.__setattr__(facade, "_db", None)
    object.__setattr__(facade, "operation", operation)
    object.__setattr__(facade, "_fields", ())

    _append_operation_log(facade, "first")
    _append_operation_log(facade, "")
    _append_operation_log(facade, "Repository size is 5 B in 1 objects.")
    _append_operation_log(facade, "Repository size is 5 B in 1 objects.")
    assert log.read_text() == (
        "first\nRepository size is 5 B in 1 objects.\n"
        "Repository size is 5 B in 1 objects."
    )


def _session_agent(test_db):
    from app.core.security import get_password_hash

    agent = AgentMachine(
        name="Session Agent",
        agent_id="agt_session",
        token_hash=get_password_hash("secret"),
        token_prefix="secret"[:20],
        status="online",
        capabilities=[],
    )
    test_db.add(agent)
    test_db.commit()
    test_db.refresh(agent)
    return agent


def _completed_agent_compact(test_db, tmp_path, agent, *, status="completed"):
    """The state after `/complete` ran: agent job final, operation terminal
    with the transcript collected so far in its log file."""
    repository = Repository(name=f"session-{status}", path=f"/session-{status}")
    test_db.add(repository)
    test_db.commit()
    now = datetime.now(timezone.utc)
    log = tmp_path / f"operation-{status}.log"
    log.write_text("Starting compaction / garbage collection...")
    operation = Operation(
        repository_id=repository.id,
        kind="compact",
        category="maintenance",
        status=status,
        trigger="manual",
        priority=10,
        run_id="run-session",
        started_at=now,
        completed_at=now,
        log_file_path=str(log),
    )
    test_db.add(operation)
    test_db.commit()
    job = AgentJob(
        agent_machine_id=agent.id,
        job_type="repository",
        status=status,
        payload={
            "schema_version": 1,
            "job_kind": "repository.compact",
            "repository": {"id": repository.id},
            "operation": {"maintenance_job": {"kind": "compact", "id": operation.id}},
        },
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    test_db.add(job)
    test_db.commit()
    return repository, operation, job, log


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_after_completion_is_absorbed(test_db, tmp_path):
    """The race only exists on the WebSocket session, whose log frames reach
    the server through `_handle_agent_session_message` while the completion
    went over HTTP. Drive that handler."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    frame = {
        "type": "log",
        "job_id": job.id,
        "sequence": 7,
        "stream": "stdout",
        "message": json.dumps(
            {
                "type": "log_message",
                "levelname": "INFO",
                "name": "borg.archiver.compact_cmd",
                "message": "Repository size is 502000 B in 6 objects.",
            }
        ),
    }
    await _handle_agent_session_message(test_db, agent.id, frame)
    # the same frame again (a resend) changes nothing
    await _handle_agent_session_message(test_db, agent.id, frame)

    test_db.expire_all()
    operation = test_db.get(Operation, operation.id)
    repository = test_db.get(Repository, repository.id)
    assert operation.result["stats"]["repository_size"] == 502_000
    assert operation.result["stats"]["size_precision"] == "exact"
    assert repository.total_size == "490.23 KB"
    assert repository.total_size_source == "compact_stats"
    assert log.read_text().count("Repository size") == 1
    assert (
        test_db.query(AgentJobLog).filter(AgentJobLog.agent_job_id == job.id).count()
        == 1
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_after_a_failed_compact_reaches_the_transcript(
    test_db, tmp_path
):
    """A failed compact's last frames are the lines that say why it failed;
    they belong in the operation's transcript even though there are no
    statistics to parse."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(
        test_db, tmp_path, agent, status="failed"
    )
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stderr",
            "message": "Repository size is 502000 B in 6 objects.",
        },
    )
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 8,
            "stream": "stderr",
            "message": "borg: error: Lock failed",
        },
    )
    test_db.expire_all()
    operation = test_db.get(Operation, operation.id)
    repository = test_db.get(Repository, repository.id)
    assert log.read_text().endswith("\nborg: error: Lock failed")
    assert operation.result is None
    assert repository.total_size is None


@pytest.mark.unit
def test_size_measured_since_needs_a_follow_up_that_wrote_a_size(test_db):
    from types import SimpleNamespace

    from app.api.agents import _size_measured_since

    repository = Repository(name="since", path="/since")
    test_db.add(repository)
    test_db.commit()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    compact = SimpleNamespace(id=0, repository_id=repository.id, completed_at=now)

    assert not _size_measured_since(test_db, compact)
    assert not _size_measured_since(
        test_db, SimpleNamespace(id=0, repository_id=repository.id, completed_at=None)
    )
    assert not _size_measured_since(
        test_db, SimpleNamespace(id=0, repository_id=None, completed_at=now)
    )

    def followup(**fields):
        return Operation(
            repository_id=repository.id,
            kind="stats",
            category="index",
            status="completed",
            trigger="followup",
            priority=10,
            run_id="run-since",
            completed_at=now + timedelta(seconds=1),
            **fields,
        )

    test_db.add(followup(result=None))
    test_db.commit()
    assert not _size_measured_since(test_db, compact)
    test_db.add(followup(result={"total_size": "keep", "size_refreshed": False}))
    test_db.commit()
    assert not _size_measured_since(test_db, compact)
    test_db.add(followup(result={"bytes": 5, "size_refreshed": True}))
    test_db.commit()
    assert _size_measured_since(test_db, compact)

    # a compact whose agent clock ran slow: the lookback starts no earlier
    # than the server-stamped claim, so a measurement between the two does
    # not count as newer
    slow = SimpleNamespace(
        id=0, repository_id=repository.id, completed_at=now - timedelta(hours=1)
    )
    assert _size_measured_since(test_db, slow)
    assert not _size_measured_since(
        test_db, slow, not_before=now + timedelta(seconds=2)
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_with_an_agent_clock_off_is_absorbed(test_db, tmp_path):
    """The window is anchored on the server time of the job's completion,
    not on the completion timestamp an agent may supply: one from a slow
    clock (an hour in the past) or a fast one does not close or widen it."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    operation.completed_at = operation.completed_at - timedelta(hours=1)
    job.completed_at = job.completed_at - timedelta(hours=1)
    test_db.commit()
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": "Repository size is 502000 B in 6 objects.",
        },
    )
    test_db.expire_all()
    assert test_db.get(Operation, operation.id).result["stats"]["repository_size"] == (
        502_000
    )
    assert test_db.get(Repository, repository.id).total_size == "490.23 KB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_with_rounded_units_keeps_the_size(test_db, tmp_path):
    """An agent whose Borg ignored BORG_UNITS=raw prints rounded sizes: the
    statistics land on the operation, the size stays what it was."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    repository.total_size = "1.43 MB"
    repository.total_size_source = "borg2_index"
    test_db.commit()
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": "Repository size is 502 kB in 6 objects.",
        },
    )
    test_db.expire_all()
    stats = test_db.get(Operation, operation.id).result["stats"]
    assert stats["repository_size"] == 502_000
    assert stats["size_precision"] == "rounded_to_printed_unit"
    repository = test_db.get(Repository, repository.id)
    # a measured size is kept over a rounded figure
    assert repository.total_size == "1.43 MB"
    assert repository.total_size_source == "borg2_index"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_delivers_the_whole_statistics_block_after_completion(
    test_db, tmp_path
):
    """The realistic case: the agent's outbox drains the complete block after
    the completion report. Every line lands in the transcript; the dict is
    complete once the closing lines are in."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    block = [
        "Overall statistics, considering all 2 archives in this repository:",
        "Source data size was 1000000 B in 6 files.",
        "Deduplicated size is 500000 B.",
        "Deduplication factor is 0.50.",
        "Repository size is 502000 B in 6 objects.",
        "Compression factor is 1.00.",
        "Compaction saved 0 B.",
        "Finished compaction / garbage collection...",
    ]
    for sequence, line in enumerate(block, start=7):
        await _handle_agent_session_message(
            test_db,
            agent.id,
            {
                "type": "log",
                "job_id": job.id,
                "sequence": sequence,
                "stream": "stdout",
                "message": line,
            },
        )
    test_db.expire_all()
    stats = test_db.get(Operation, operation.id).result["stats"]
    assert stats == {
        "archive_count": 2,
        "source_size": 1_000_000,
        "source_files": 6,
        "deduplicated_size": 500_000,
        "deduplication_factor": 0.5,
        "repository_size": 502_000,
        "object_count": 6,
        "compression_factor": 1.0,
        "compaction_saved": 0,
        "size_precision": "exact",
    }
    assert log.read_text().splitlines() == [
        "Starting compaction / garbage collection...",
        *block,
    ]
    assert test_db.get(Repository, repository.id).total_size == "490.23 KB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_late_statistics_do_not_undo_a_later_compact(test_db, tmp_path):
    """A second compact ran (and recorded its own exact size) while the first
    one's lines were stuck in the outbox: the older figure must not win."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    later = Operation(
        repository_id=repository.id,
        kind="compact",
        category="maintenance",
        status="completed",
        trigger="manual",
        priority=10,
        run_id="run-later",
        completed_at=operation.completed_at + timedelta(minutes=2),
        result={"stats": {"repository_size": 400_000, "size_precision": "exact"}},
    )
    repository.total_size = "390.63 KB"
    repository.total_size_source = "compact_stats"
    test_db.add(later)
    test_db.commit()
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": "Repository size is 502000 B in 6 objects.",
        },
    )
    test_db.expire_all()
    assert test_db.get(Operation, operation.id).result["stats"]["repository_size"] == (
        502_000
    )
    assert test_db.get(Repository, repository.id).total_size == "390.63 KB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_frame_with_two_lines_is_parsed_like_the_completion_path(
    test_db, tmp_path
):
    """An agent may flush two stderr lines in one frame; the late path must
    see the closing line inside it as the completion path would."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": (
                "Repository size is 1836754184 B in 2796 objects.\n"
                "Compaction saved 0 B."
            ),
        },
    )
    test_db.expire_all()
    stats = test_db.get(Operation, operation.id).result["stats"]
    assert stats["repository_size"] == 1_836_754_184
    assert stats["compaction_saved"] == 0
    assert test_db.get(Repository, repository.id).total_size == "1.71 GB"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_is_kept_when_absorbing_fails(
    test_db, tmp_path, monkeypatch
):
    """The absorb is a side effect: when it raises, the line is stored, the
    handler returns normally (the session stays up) and nothing half-done
    is left in the database."""
    from app.api import agents as agents_module
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(agents_module, "_absorb_late_compact_stats", boom)
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": "Repository size is 502000 B in 6 objects.",
        },
    )
    test_db.expire_all()
    assert (
        test_db.query(AgentJobLog).filter(AgentJobLog.agent_job_id == job.id).count()
        == 1
    )
    assert test_db.get(Operation, operation.id).result is None
    assert test_db.get(Repository, repository.id).total_size is None
    assert "Repository size" not in log.read_text()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_session_log_line_creates_the_transcript_file_when_missing(
    test_db, tmp_path, monkeypatch
):
    """An operation that reached a terminal state without a log file (the
    runner failed it before the agent reported) gets one from its late
    lines, appended, so a second line never truncates the first."""
    from app.api.agents import _handle_agent_session_message

    monkeypatch.setattr("app.config.settings.data_dir", str(tmp_path))
    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(
        test_db, tmp_path, agent, status="failed"
    )
    operation.log_file_path = None
    test_db.commit()
    for sequence, line in ((7, "first late line"), (8, "second late line")):
        await _handle_agent_session_message(
            test_db,
            agent.id,
            {
                "type": "log",
                "job_id": job.id,
                "sequence": sequence,
                "stream": "stderr",
                "message": line,
            },
        )
    test_db.expire_all()
    operation = test_db.get(Operation, operation.id)
    assert operation.log_file_path == str(
        tmp_path / "logs" / f"operation_{operation.id}.log"
    )
    assert (
        Path(operation.log_file_path).read_text() == "first late line\nsecond late line"
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("age_minutes,absorbed", [(9, True), (11, False)])
async def test_late_line_window_boundary(test_db, tmp_path, age_minutes, absorbed):
    """LATE_LOG_ABSORB_WINDOW is ten minutes from the job's server-stamped
    completion."""
    from app.api.agents import _handle_agent_session_message

    agent = _session_agent(test_db)
    repository, operation, job, log = _completed_agent_compact(test_db, tmp_path, agent)
    job.updated_at = job.updated_at - timedelta(minutes=age_minutes)
    test_db.commit()
    await _handle_agent_session_message(
        test_db,
        agent.id,
        {
            "type": "log",
            "job_id": job.id,
            "sequence": 7,
            "stream": "stdout",
            "message": "Repository size is 502000 B in 6 objects.",
        },
    )
    test_db.expire_all()
    stats = (test_db.get(Operation, operation.id).result or {}).get("stats")
    assert (stats is not None) is absorbed
    assert ("Repository size" in log.read_text()) is absorbed
