"""Test doubles for the Vonage REST client and the agent. No network involved."""

from __future__ import annotations

import asyncio

from app.config import Settings
from app.session_manager import AgentConnection, DemoSession

APPLICATION_ID = "11111111-2222-3333-4444-555555555555"


class FakeVideo:
    """Stands in for VonageVideo: no network, role-specific tokens."""

    application_id = APPLICATION_ID

    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with
        self.sessions_created = 0

    def create_routed_session(self) -> str:
        if self.fail_with:
            raise self.fail_with
        self.sessions_created += 1
        return f"vonage-session-{self.sessions_created}"

    def create_token(self, session_id: str, role: str, ttl_seconds: int) -> str:
        return f"token-for-{role}-{session_id}"


class FakeAgent:
    """Behaves like an agent: joins, waits to be stopped. Behaviour is adjustable."""

    def __init__(self, connection: AgentConnection, events: DemoSession):
        self.connection = connection
        self.events = events
        self.conversations_started = 0
        self.stop_calls = 0
        self.ignore_stop = False
        self.crash_with: Exception | None = None
        self.finished = False
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        try:
            if self.crash_with:
                raise self.crash_with
            self.events.agent_joined()
            await self._stopped.wait()
        finally:
            self.finished = True

    async def start_conversation(self) -> None:
        self.conversations_started += 1

    async def stop(self) -> None:
        self.stop_calls += 1
        if not self.ignore_stop:
            self._stopped.set()


class AgentRecorder:
    """Agent factory that remembers every agent it built."""

    def __init__(self, configure=None):
        self.agents: list[FakeAgent] = []
        self.configure = configure

    def __call__(self, settings: Settings, connection: AgentConnection, session: DemoSession):
        agent = FakeAgent(connection, session)
        if self.configure:
            self.configure(agent)
        self.agents.append(agent)
        return agent


async def wait_until(predicate, within: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)
