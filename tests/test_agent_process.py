"""The server <-> agent process protocol, exercised with real child processes."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from app import agent_process
from app.agent_process import AgentProcessError, ProcessAgent
from app.session_manager import AgentConnection
from tests.fakes import wait_until

FAKE_CHILD = [sys.executable, str(Path(__file__).with_name("fake_agent_process.py"))]
CONNECTION = AgentConnection("app-id", "vonage-session", "agent-token")


class RecordedEvents:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.failures: list[str] = []

    def agent_joined(self) -> None:
        self.names.append("agent_joined")

    def user_audio_connected(self) -> None:
        self.names.append("user_audio_connected")

    def user_left(self) -> None:
        self.names.append("user_left")

    def idle_timeout(self) -> None:
        self.names.append("idle_timeout")

    def agent_failed(self, message: str) -> None:
        self.failures.append(message)


@pytest.fixture
def child_env(monkeypatch, valid_env):
    for name, value in valid_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("FAKE_AGENT_MODE", "normal")


def make_agent(settings, events, command=None):
    return ProcessAgent(settings, CONNECTION, events, command=command or FAKE_CHILD)


async def test_relays_commands_and_events(settings, child_env):
    events = RecordedEvents()
    agent = make_agent(settings, events)
    run = asyncio.create_task(agent.run())

    await wait_until(lambda: "agent_joined" in events.names, within=20)
    await agent.start_conversation()
    await wait_until(lambda: "user_audio_connected" in events.names, within=5)
    await agent.stop()
    await asyncio.wait_for(run, 10)

    assert agent._process is not None and agent._process.returncode == 0
    assert events.failures == []


async def test_commands_sent_before_launch_are_delivered(settings, child_env):
    events = RecordedEvents()
    agent = make_agent(settings, events)
    await agent.start_conversation()  # browser was ready before the process started
    run = asyncio.create_task(agent.run())
    await wait_until(lambda: "user_audio_connected" in events.names, within=20)
    await agent.stop()
    await asyncio.wait_for(run, 10)


async def test_stop_before_launch_starts_nothing(settings, child_env):
    agent = make_agent(settings, RecordedEvents())
    await agent.stop()
    await agent.run()
    assert agent._process is None


async def test_crash_is_reported_with_its_cause(settings, child_env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "crash")
    events = RecordedEvents()
    agent = make_agent(settings, events)
    await asyncio.wait_for(agent.run(), 20)  # a reported failure is not an exception
    assert any("RuntimeError: fake crash" in failure for failure in events.failures)
    assert agent._process is not None and agent._process.returncode == 1


async def test_agent_that_ignores_stop_exits_on_its_own(settings, child_env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "ignore_stop")
    events = RecordedEvents()
    agent = make_agent(settings, events)
    run = asyncio.create_task(agent.run())
    await wait_until(lambda: "agent_joined" in events.names, within=20)

    await agent.stop()
    # The process gives its agent STOP_TIMEOUT_SECONDS, then exits by itself.
    await asyncio.wait_for(run, agent_process.STOP_TIMEOUT_SECONDS + 5)
    assert agent._process is not None and agent._process.returncode == 0


async def test_cancelling_terminates_a_stopping_agent_at_once(settings, child_env, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "ignore_stop")
    events = RecordedEvents()
    agent = make_agent(settings, events)
    run = asyncio.create_task(agent.run())
    await wait_until(lambda: "agent_joined" in events.names, within=20)

    await agent.stop()
    await asyncio.sleep(0.3)
    assert not run.done()  # still "stopping"

    run.cancel()  # what the session supervisor does after its grace period
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 3)  # SIGTERM while stopping: exits immediately
    assert agent._process is not None and agent._process.returncode == 1


async def test_process_ignoring_sigterm_is_killed(settings, monkeypatch):
    monkeypatch.setattr(agent_process, "TERMINATE_GRACE_SECONDS", 0.5)
    stubborn = (
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        'sys.stdin.readline(); print(\'{"event": "agent_joined"}\', flush=True); time.sleep(60)'
    )
    events = RecordedEvents()
    agent = make_agent(settings, events, [sys.executable, "-c", stubborn])
    run = asyncio.create_task(agent.run())
    await wait_until(lambda: "agent_joined" in events.names, within=20)

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 5)
    assert agent._process is not None
    assert agent._process.returncode == -signal.SIGKILL


async def test_configuration_problems_in_the_child_are_reported(settings, child_env, monkeypatch):
    monkeypatch.delenv("VONAGE_APPLICATION_ID")
    events = RecordedEvents()
    await asyncio.wait_for(make_agent(settings, events).run(), 20)
    assert any("Configuration error" in failure for failure in events.failures)


async def test_silent_nonzero_exit_raises(settings):
    agent = make_agent(settings, RecordedEvents(), [sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(AgentProcessError, match="code 3"):
        await asyncio.wait_for(agent.run(), 20)


def test_malformed_and_unknown_events_are_ignored(settings):
    events = RecordedEvents()
    agent = make_agent(settings, events)
    agent._dispatch(b"not json\n")
    agent._dispatch(b'{"event": "__init__"}\n')
    agent._dispatch(b'{"no_event": true}\n')
    agent._dispatch(b'{"event": "agent_joined"}\n')
    assert events.names == ["agent_joined"]


def test_token_is_not_passed_on_the_command_line(settings):
    agent = ProcessAgent(settings, CONNECTION, RecordedEvents())
    assert CONNECTION.token not in " ".join(agent._command)
