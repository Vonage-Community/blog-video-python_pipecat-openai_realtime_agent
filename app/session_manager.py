"""Owns every demo session and the AI agent that runs inside it.

Each session has exactly one supervisor task, and the manager keeps a handle to
it. The supervisor:

- starts the agent and waits for it to finish or for a stop request,
- stops the agent gracefully, and cancels it if it does not exit in time,
- records how the session ended (or the error that ended it).

Timers end sessions that never become ready or that reach their maximum length,
so an abandoned tab cannot keep a billable agent running.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from loguru import logger

from app.config import Settings, redact

STOP_GRACE_SECONDS = 10.0
# end_session() waits this long for the supervisor. It must exceed the whole
# escalation: graceful stop, then cancel (SIGTERM, then SIGKILL for a process).
END_WAIT_SECONDS = STOP_GRACE_SECONDS + 15.0
KEEP_FINISHED_SECONDS = 15 * 60
MAX_FINISHED_SESSIONS = 20


class SessionState(StrEnum):
    STARTING = "starting"  # the agent is joining the Vonage session
    AGENT_JOINED = "agent_joined"  # the agent is in the session and publishing
    ACTIVE = "active"  # the browser can hear the agent; conversation started
    ENDING = "ending"  # stopping the agent and closing provider connections
    ENDED = "ended"
    FAILED = "failed"


class EndReason(StrEnum):
    LEFT = "left"  # the browser called /end
    USER_STREAM_LEFT = "user_stream_left"  # the browser's stream disappeared
    MAX_DURATION = "max_duration"
    IDLE_TIMEOUT = "idle_timeout"
    READY_TIMEOUT = "ready_timeout"
    SERVER_SHUTDOWN = "server_shutdown"
    AGENT_EXITED = "agent_exited"
    ERROR = "error"


class AgentEvents(Protocol):
    """What the agent reports back to the session that owns it."""

    def agent_joined(self) -> None: ...

    def user_audio_connected(self) -> None: ...

    def user_left(self) -> None: ...

    def agent_failed(self, message: str) -> None: ...

    def idle_timeout(self) -> None: ...


@dataclass(frozen=True)
class AgentConnection:
    """Where the agent connects. The token is the agent's own and never leaves the server."""

    application_id: str
    session_id: str
    token: str


class Agent(Protocol):
    async def run(self) -> None: ...

    async def start_conversation(self) -> None: ...

    async def stop(self) -> None: ...


# Builds the agent for a session. The session is passed as the agent's AgentEvents.
AgentFactory = Callable[[Settings, AgentConnection, "DemoSession"], Agent]


class VideoService(Protocol):
    application_id: str

    def create_routed_session(self) -> str: ...

    def create_token(self, session_id: str, role: Any, ttl_seconds: int) -> str: ...


class SessionLimitError(Exception):
    """Too many sessions running, or created too quickly."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SessionFinishedError(Exception):
    pass


@dataclass
class DemoSession:
    """One browser + one agent in one Vonage Video session. Implements AgentEvents."""

    id: str
    key: str = field(repr=False)  # capability the browser presents for this session
    vonage_session_id: str
    created_at: float
    expires_at: float
    state: SessionState = SessionState.STARTING
    agent_joined_flag: bool = False
    user_audio_connected_flag: bool = False
    ready: bool = False
    end_reason: EndReason | None = None
    error: str | None = None
    finished_at: float | None = None
    agent: Agent | None = field(default=None, repr=False)
    supervisor: asyncio.Task[None] | None = field(default=None, repr=False)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    timers: list[asyncio.TimerHandle] = field(default_factory=list, repr=False)

    @property
    def finished(self) -> bool:
        return self.state in (SessionState.ENDED, SessionState.FAILED)

    def request_end(self, reason: EndReason) -> None:
        """Ask the supervisor to stop the agent. The first reason recorded wins."""
        if self.finished:
            return
        if self.end_reason is None:
            self.end_reason = reason
        if self.state != SessionState.ENDING:
            logger.info(f"Session {self.id}: ending ({self.end_reason})")
        self.state = SessionState.ENDING
        self.stop_requested.set()

    # --- AgentEvents ---------------------------------------------------------

    def agent_joined(self) -> None:
        self.agent_joined_flag = True
        if self.state == SessionState.STARTING:
            self.state = SessionState.AGENT_JOINED

    def user_audio_connected(self) -> None:
        self.user_audio_connected_flag = True

    def user_left(self) -> None:
        self.request_end(EndReason.USER_STREAM_LEFT)

    def agent_failed(self, message: str) -> None:
        if self.error is None:
            self.error = redact(message)[:500]
            logger.error(f"Session {self.id}: agent failed: {self.error}")
        self.request_end(EndReason.ERROR)

    def idle_timeout(self) -> None:
        self.request_end(EndReason.IDLE_TIMEOUT)

    # --- Public view -----------------------------------------------------------

    def status(self, now: float) -> dict[str, Any]:
        """Session state for the browser. Contains no tokens or keys."""
        return {
            "id": self.id,
            "state": self.state.value,
            "endReason": self.end_reason.value if self.end_reason else None,
            "error": self.error,
            "agent": {
                "joined": self.agent_joined_flag,
                "hearingUser": self.user_audio_connected_flag,
                "conversationStarted": self.ready,
            },
            "createdAt": _iso(self.created_at),
            "expiresAt": _iso(self.expires_at),
            "secondsRemaining": 0 if self.finished else max(0, int(self.expires_at - now)),
        }


@dataclass(frozen=True)
class CreatedSession:
    session: DemoSession
    application_id: str
    browser_token: str


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        video: VideoService,
        agent_factory: AgentFactory,
        clock: Callable[[], float] = time.time,
        run_blocking: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ):
        self._settings = settings
        self._video = video
        self._agent_factory = agent_factory
        self._clock = clock
        self._run_blocking = run_blocking
        self._sessions: dict[str, DemoSession] = {}
        self._create_lock = asyncio.Lock()
        self._recent_creates: deque[float] = deque()

    @property
    def active_sessions(self) -> list[DemoSession]:
        return [s for s in self._sessions.values() if not s.finished]

    async def create_session(self) -> CreatedSession:
        """Create a routed Vonage session, two tokens, and start the agent."""
        async with self._create_lock:
            self._prune_finished()
            now = self._clock()
            self._check_limits(now)
            self._recent_creates.append(now)

            vonage_session_id = await self._run_blocking(self._video.create_routed_session)
            # Tokens only need to outlive the time it takes to join.
            ttl = self._settings.max_session_seconds + 60
            browser_token = self._video.create_token(vonage_session_id, "user", ttl)
            agent_token = self._video.create_token(vonage_session_id, "agent", ttl)

            session = DemoSession(
                id=secrets.token_urlsafe(12),
                key=secrets.token_urlsafe(32),
                vonage_session_id=vonage_session_id,
                created_at=now,
                expires_at=now + self._settings.max_session_seconds,
            )
            session.agent = self._agent_factory(
                self._settings,
                AgentConnection(self._video.application_id, vonage_session_id, agent_token),
                session,
            )
            loop = asyncio.get_running_loop()
            session.timers = [
                loop.call_later(
                    self._settings.ready_timeout_seconds, self._ready_deadline, session
                ),
                loop.call_later(
                    self._settings.max_session_seconds,
                    session.request_end,
                    EndReason.MAX_DURATION,
                ),
            ]
            session.supervisor = asyncio.create_task(
                self._supervise(session), name=f"session-{session.id}"
            )
            self._sessions[session.id] = session
            logger.info(f"Session {session.id}: created (Vonage session {vonage_session_id})")
            return CreatedSession(session, self._video.application_id, browser_token)

    def get(self, session_id: str) -> DemoSession | None:
        return self._sessions.get(session_id)

    @staticmethod
    def key_matches(session: DemoSession, key: str) -> bool:
        return secrets.compare_digest(session.key.encode(), key.encode())

    async def mark_ready(self, session: DemoSession) -> bool:
        """Start the conversation once. Returns False if it had already started."""
        if session.finished or session.state == SessionState.ENDING:
            raise SessionFinishedError()
        if session.ready:
            return False
        session.ready = True
        session.state = SessionState.ACTIVE
        assert session.agent is not None
        await session.agent.start_conversation()
        logger.info(f"Session {session.id}: browser ready, conversation started")
        return True

    async def end_session(self, session: DemoSession, reason: EndReason) -> None:
        """Stop the agent and wait for cleanup. Safe to call any number of times."""
        session.request_end(reason)
        if session.supervisor is not None:
            # shield: a client disconnecting mid-request must not cancel cleanup.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(session.supervisor), END_WAIT_SECONDS)

    async def shutdown(self) -> None:
        """End every session, e.g. when the server stops."""
        sessions = list(self._sessions.values())
        for session in sessions:
            session.request_end(EndReason.SERVER_SHUTDOWN)
        await asyncio.gather(
            *(self.end_session(s, EndReason.SERVER_SHUTDOWN) for s in sessions),
            return_exceptions=True,
        )

    def _check_limits(self, now: float) -> None:
        if len(self.active_sessions) >= self._settings.max_active_sessions:
            raise SessionLimitError(
                "A demo session is already running. End it (or wait for it to time out) "
                "before starting another."
            )
        while self._recent_creates and now - self._recent_creates[0] > 60:
            self._recent_creates.popleft()
        if len(self._recent_creates) >= self._settings.session_creates_per_minute:
            retry_after = int(60 - (now - self._recent_creates[0])) + 1
            raise SessionLimitError(
                "Too many sessions created in the last minute.", retry_after=retry_after
            )

    def _ready_deadline(self, session: DemoSession) -> None:
        if not session.ready:
            session.request_end(EndReason.READY_TIMEOUT)

    def _prune_finished(self) -> None:
        now = self._clock()
        finished = sorted(
            (s for s in self._sessions.values() if s.finished),
            key=lambda s: s.finished_at or 0,
        )
        for index, session in enumerate(finished):
            too_old = now - (session.finished_at or now) > KEEP_FINISHED_SECONDS
            too_many = len(finished) - index > MAX_FINISHED_SESSIONS
            if too_old or too_many:
                del self._sessions[session.id]

    async def _supervise(self, session: DemoSession) -> None:
        assert session.agent is not None
        agent = session.agent
        run_task = asyncio.create_task(agent.run(), name=f"agent-{session.id}")
        stop_task = asyncio.create_task(session.stop_requested.wait(), name=f"stop-{session.id}")
        try:
            await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not run_task.done():
                try:
                    await agent.stop()
                except Exception:
                    logger.exception(f"Session {session.id}: error while stopping the agent")
                try:
                    await asyncio.wait_for(asyncio.shield(run_task), STOP_GRACE_SECONDS)
                except TimeoutError:
                    logger.warning(f"Session {session.id}: agent did not stop in time; cancelling")
                    run_task.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
        finally:
            # Also reached if this supervisor itself is cancelled.
            for task in (run_task, stop_task):
                task.cancel()
            await asyncio.gather(run_task, stop_task, return_exceptions=True)
            for timer in session.timers:
                timer.cancel()
            self._record_outcome(session, run_task)

    def _record_outcome(self, session: DemoSession, run_task: asyncio.Task[None]) -> None:
        exc = None if run_task.cancelled() else run_task.exception()
        if exc is not None:
            logger.opt(exception=exc).error(f"Session {session.id}: agent crashed")
            session.agent_failed(f"Agent crashed: {type(exc).__name__}: {exc}")
        if session.end_reason is None:
            session.end_reason = EndReason.AGENT_EXITED
        session.state = SessionState.FAILED if session.error else SessionState.ENDED
        session.finished_at = self._clock()
        logger.info(f"Session {session.id}: {session.state} ({session.end_reason})")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()
