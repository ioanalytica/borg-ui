"""An agent process that dies leaves its children running under launchd.

Borg and script hooks run in a session of their own. launchd stops only the
agent's own process group, so after a crash and a KeepAlive restart (or with
an agent started by hand) a hook keeps running while the server runs its job
again. The agent records each child it starts and, before its first hello,
ends the groups a dead agent process left behind.
"""

import json
import os
import subprocess
import threading
import time

import pytest

from agent.borg_ui_agent import children
from agent.borg_ui_agent.children import (
    ChildRegistry,
    boot_identity,
    default_children_dir,
    process_start_token,
    track_child,
)
from agent.borg_ui_agent.config import AgentConfig


def _spawn(tmp_path):
    """A child in a session of its own, reaped by a thread so an ended child
    does not stay a zombie in its group."""
    process = subprocess.Popen(["sleep", "60"], start_new_session=True, cwd=tmp_path)
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _left_by(root, process, *, agent=None, boot=None, start=None):
    """A child file as a dead agent process (pid 2**22 - 1 never started
    at this time) would have left it."""
    root.mkdir(parents=True, exist_ok=True)
    entry = {
        "pid": process.pid,
        "pgid": process.pid,
        "start": start or process_start_token(process.pid),
        "boot": boot or boot_identity(),
        "agent": agent or {"pid": 4194303, "start": "ps:long ago"},
    }
    (root / f"{process.pid}.json").write_text(json.dumps(entry))


@pytest.fixture(autouse=True)
def _no_global_registry():
    yield
    children.install(None)


@pytest.mark.unit
def test_a_group_a_dead_agent_process_left_is_ended(tmp_path):
    process = _spawn(tmp_path)
    try:
        root = tmp_path / "children"
        _left_by(root, process)

        ChildRegistry(root).end_leftovers(grace_seconds=5)

        assert _wait_for(lambda: process.poll() is not None)
        assert list(root.glob("*.json")) == []
    finally:
        if process.poll() is None:
            process.kill()


@pytest.mark.unit
def test_the_children_of_a_live_agent_process_are_left_alone(tmp_path):
    """An agent started by hand next to the service must not end the
    service's running hooks."""
    process = _spawn(tmp_path)
    try:
        root = tmp_path / "children"
        me = {"pid": os.getpid(), "start": process_start_token(os.getpid())}
        _left_by(root, process, agent=me)

        ChildRegistry(root).end_leftovers(grace_seconds=0.5)

        assert process.poll() is None
        assert (root / f"{process.pid}.json").exists()
    finally:
        process.kill()


@pytest.mark.unit
def test_a_pid_another_process_took_is_left_alone(tmp_path):
    process = _spawn(tmp_path)
    try:
        root = tmp_path / "children"
        _left_by(root, process, start="ps:another start")

        ChildRegistry(root).end_leftovers(grace_seconds=0.5)

        assert process.poll() is None
        assert list(root.glob("*.json")) == []
    finally:
        process.kill()


@pytest.mark.unit
def test_the_children_of_an_earlier_boot_are_left_alone(tmp_path):
    """After a reboot the recorded ids name other processes."""
    process = _spawn(tmp_path)
    try:
        root = tmp_path / "children"
        _left_by(root, process, boot="linux:an-earlier-boot")

        ChildRegistry(root).end_leftovers(grace_seconds=0.5)

        assert process.poll() is None
        assert list(root.glob("*.json")) == []
    finally:
        process.kill()


@pytest.mark.unit
def test_a_child_is_recorded_while_it_runs_and_forgotten_once_it_ended(tmp_path):
    root = tmp_path / "children"
    registry = ChildRegistry(root)
    children.install(registry)

    process = subprocess.Popen(["sleep", "0.2"], start_new_session=True)
    track_child(process)
    entry = json.loads((root / f"{process.pid}.json").read_text())
    assert entry["pid"] == entry["pgid"] == process.pid
    assert entry["start"] == process_start_token(process.pid)
    assert entry["agent"] == {
        "pid": os.getpid(),
        "start": process_start_token(os.getpid()),
    }
    assert entry["boot"] == boot_identity()

    registry.forget_ended()
    assert (root / f"{process.pid}.json").exists()  # still running

    process.wait()
    registry.forget_ended()
    assert list(root.glob("*.json")) == []


@pytest.mark.unit
def test_without_a_registry_nothing_is_recorded(tmp_path):
    process = subprocess.Popen(["true"])
    track_child(process)  # must not raise
    process.wait()


@pytest.mark.unit
def test_the_children_directory_sits_beside_the_config(tmp_path):
    assert default_children_dir(tmp_path / "config.toml") == tmp_path / "children"


@pytest.mark.unit
def test_the_session_ends_leftovers_once_before_its_first_hello(tmp_path, monkeypatch):
    """Not on a reconnect: by then the files are this process's own."""
    from agent.borg_ui_agent.session import AgentSessionRuntime

    monkeypatch.setattr(
        "agent.borg_ui_agent.session.detect_platform",
        lambda: {"hostname": "host.local", "os": "linux", "arch": "amd64"},
    )
    monkeypatch.setattr("agent.borg_ui_agent.session.detect_borg_binaries", lambda: [])

    class Socket:
        sent = []

        def settimeout(self, timeout):
            pass

        def send(self, payload):
            self.sent.append(json.loads(payload))

        def recv(self):
            raise EOFError("closed")

        def close(self):
            pass

    calls = []
    monkeypatch.setattr(
        ChildRegistry, "end_leftovers", lambda self, **kw: calls.append(self.root)
    )
    runtime = AgentSessionRuntime(
        AgentConfig("https://borgui.example.com", "agt_123", "secret"),
        connect=lambda *args, **kwargs: Socket(),
        children_dir=tmp_path / "children",
    )

    runtime.run_session(max_messages=0)
    runtime.run_session(max_messages=0)

    assert calls == [tmp_path / "children"]
    assert children._registry is runtime._children


@pytest.mark.unit
def test_cli_run_records_children_beside_the_config(monkeypatch, tmp_path):
    from agent.borg_ui_agent import cli

    seen = []

    class FakeRuntime:
        def __init__(self, config, *, children_dir=None):
            seen.append(children_dir)

        def run_forever(self, **kwargs):
            pass

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'server_url = "https://borgui.example.com"\n'
        'agent_id = "agt_1"\nagent_token = "secret"\n'
    )
    monkeypatch.setattr(cli, "AgentRuntime", FakeRuntime)
    monkeypatch.setattr(cli.truststore, "inject_into_ssl", lambda: None)

    assert cli.main(["--config", str(config_path), "run"]) == 0
    assert seen == [tmp_path / "children"]


@pytest.mark.unit
def test_a_group_that_ignores_sigterm_is_killed_after_the_grace(tmp_path):
    process = subprocess.Popen(
        ["sh", "-c", 'trap "" TERM; sleep 60'], start_new_session=True
    )
    threading.Thread(target=process.wait, daemon=True).start()
    try:
        time.sleep(0.2)  # let the trap take effect
        root = tmp_path / "children"
        _left_by(root, process)

        ChildRegistry(root).end_leftovers(grace_seconds=0.5)

        assert _wait_for(lambda: process.poll() is not None)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill()
