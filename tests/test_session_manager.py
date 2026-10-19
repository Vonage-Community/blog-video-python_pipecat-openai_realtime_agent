from __future__ import annotations

import asyncio

import pytest

from app import session_manager as sm
from app.session_manager import (
    EndReason,
    SessionFinishedError,
    SessionLimitError,
    SessionManager,
    SessionState,
)
from app.video_sessions import VideoApiError
from tests.conftest import with_settings
from tests.fakes import AgentRecorder, FakeVideo, wait_until


async def run_inline(func, *args):
    return func(*args)


def make_manager(settings, agents=None, video=None):
    agents = agents or AgentRecorder()
    manager = SessionManager(settings, video or FakeVideo(), agents, run_blocking=run_inline)
    return manager, agents


async def test_create_gives_browser_and_agent_separate_tokens(settings):
    manager, agents = make_manager(settings)
    created = await manager.create_session()

    agent = agents.agents[0]
    assert created.browser_token == "token-for-user-vonage-session-1"
    assert agent.connection.token == "token-for-agent-vonage-session-1"
    assert agent.connection.session_id == created.session.vonage_session_id
    assert created.session.key and created.session.id != created.session.key

    await wait_until(lambda: created.session.state == SessionState.AGENT_JOINED)
    await manager.shutdown()


async def test_only_one_session_at_a_time_by_default(settings):
    manager, _ = make_manager(settings)
    first = await manager.create_session()
    with pytest.raises(SessionLimitError) as info:
        await manager.create_session()
    assert info.value.retry_after is None  # a conflict, not a rate limit

    await manager.end_session(first.session, EndReason.LEFT)
    second = await manager.create_session()  # allowed once the first has ended
    await manager.end_session(second.session, EndReason.LEFT)


async def test_session_creation_is_rate_limited(settings):
    manager, _ = make_manager(
        with_settings(settings, max_active_sessions=5, session_creates_per_minute=2)
    )
    await manager.create_session()
    await manager.create_session()
    with pytest.raises(SessionLimitError) as info:
        await manager.create_session()
    assert info.value.retry_after and 0 < info.value.retry_after <= 61
    await manager.shutdown()


async def test_vonage_failure_creates_no_session_or_agent(settings):
    manager, agents = make_manager(settings, video=FakeVideo(fail_with=VideoApiError("boom")))
    with pytest.raises(VideoApiError):
        await manager.create_session()
    assert agents.agents == []
    assert manager.active_sessions == []


async def test_ready_starts_the_conversation_exactly_once(settings):
    manager, agents = make_manager(settings)
    created = await manager.create_session()

    assert await manager.mark_ready(created.session) is True
    assert await manager.mark_ready(created.session) is False
    assert agents.agents[0].conversations_started == 1
    assert created.session.state == SessionState.ACTIVE
    await manager.shutdown()


async def test_end_stops_the_agent_and_is_idempotent(settings):
    manager, agents = make_manager(settings)
    created = await manager.create_session()
    session, agent = created.session, agents.agents[0]

    await manager.end_session(session, EndReason.LEFT)
    await manager.end_session(session, EndReason.LEFT)

    assert agent.finished
    assert agent.stop_calls == 1
    assert session.state == SessionState.ENDED
    assert session.end_reason == EndReason.LEFT
    assert session.supervisor is not None and session.supervisor.done()
    with pytest.raises(SessionFinishedError):
        await manager.mark_ready(session)


async def test_session_ends_if_the_browser_never_becomes_ready(settings):
    manager, agents = make_manager(with_settings(settings, ready_timeout_seconds=0.05))
    created = await manager.create_session()
    await wait_until(lambda: created.session.finished)
    assert created.session.end_reason == EndReason.READY_TIMEOUT
    assert agents.agents[0].finished


async def test_ready_cancels_the_ready_timeout(settings):
    manager, _ = make_manager(with_settings(settings, ready_timeout_seconds=0.05))
    created = await manager.create_session()
    await manager.mark_ready(created.session)
    await asyncio.sleep(0.15)
    assert not created.session.finished
    await manager.shutdown()


async def test_session_ends_at_its_maximum_duration(settings):
    manager, agents = make_manager(with_settings(settings, max_session_seconds=0.1))
    created = await manager.create_session()
    await manager.mark_ready(created.session)
    await wait_until(lambda: created.session.finished)
    assert created.session.end_reason == EndReason.MAX_DURATION
    assert agents.agents[0].stop_calls == 1


async def test_agent_that_ignores_stop_is_cancelled(settings, monkeypatch):
    monkeypatch.setattr(sm, "STOP_GRACE_SECONDS", 0.1)
    manager, agents = make_manager(
        settings, AgentRecorder(lambda a: setattr(a, "ignore_stop", True))
    )
    created = await manager.create_session()
    await manager.end_session(created.session, EndReason.LEFT)
    assert agents.agents[0].finished  # its run() was cancelled
    assert created.session.state == SessionState.ENDED


async def test_agent_crash_marks_the_session_failed(settings):
    agents = AgentRecorder(lambda a: setattr(a, "crash_with", RuntimeError("native boom")))
    manager, _ = make_manager(settings, agents)
    created = await manager.create_session()
    await wait_until(lambda: created.session.finished)
    assert created.session.state == SessionState.FAILED
    assert "RuntimeError: native boom" in (created.session.error or "")


async def test_reported_agent_failure_is_redacted_and_ends_the_session(settings):
    manager, agents = make_manager(settings)
    created = await manager.create_session()
    created.session.agent_failed("OpenAI rejected key sk-fake-0123456789abcdef")
    await wait_until(lambda: created.session.finished)
    assert created.session.state == SessionState.FAILED
    assert "sk-fake-0123456789abcdef" not in (created.session.error or "")
    assert agents.agents[0].stop_calls == 1


@pytest.mark.parametrize(
    ("event", "reason"),
    [("user_left", EndReason.USER_STREAM_LEFT), ("idle_timeout", EndReason.IDLE_TIMEOUT)],
)
async def test_agent_events_end_the_session(settings, event, reason):
    manager, _ = make_manager(settings)
    created = await manager.create_session()
    getattr(created.session, event)()
    await wait_until(lambda: created.session.finished)
    assert created.session.end_reason == reason


async def test_first_end_reason_wins(settings):
    manager, _ = make_manager(settings)
    created = await manager.create_session()
    await manager.end_session(created.session, EndReason.LEFT)
    created.session.user_left()  # the stream disappears after the browser left
    assert created.session.end_reason == EndReason.LEFT


async def test_shutdown_ends_every_session(settings):
    manager, agents = make_manager(with_settings(settings, max_active_sessions=3))
    for _ in range(3):
        await manager.create_session()
    await manager.shutdown()
    assert all(agent.finished for agent in agents.agents)
    assert manager.active_sessions == []


async def test_status_never_contains_credentials(settings):
    manager, _ = make_manager(settings)
    created = await manager.create_session()
    status = created.session.status(created.session.created_at)
    text = repr(status)
    assert created.session.key not in text
    assert "token" not in text
    assert status["state"] == "starting"
    assert status["secondsRemaining"] == settings.max_session_seconds
    await manager.shutdown()


async def test_key_comparison(settings):
    manager, _ = make_manager(settings)
    created = await manager.create_session()
    assert manager.key_matches(created.session, created.session.key)
    assert not manager.key_matches(created.session, created.session.key + "x")
    await manager.shutdown()


async def test_finished_sessions_are_pruned(settings, monkeypatch):
    monkeypatch.setattr(sm, "MAX_FINISHED_SESSIONS", 2)
    manager, _ = make_manager(settings)
    ids = []
    for _ in range(4):
        created = await manager.create_session()
        ids.append(created.session.id)
        await manager.end_session(created.session, EndReason.LEFT)
    await manager.create_session()  # pruning happens on create
    assert manager.get(ids[0]) is None
    assert manager.get(ids[-1]) is not None
    await manager.shutdown()
