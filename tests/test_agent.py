"""The Pipecat pipeline, built with the real (pinned) Pipecat and Vonage classes.

These tests need the native Vonage Video Connector, which installs only on Linux,
so they are skipped elsewhere. Run them in Docker:
    docker build --target test .
Nothing here connects to Vonage or OpenAI successfully.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("vonage_video_connector", reason="native connector is Linux-only")

from pipecat.frames.frames import (  # noqa: E402
    InputAudioRawFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
)
from pipecat.tests.utils import run_test  # noqa: E402
from pipecat.transports.vonage.video_connector import (  # noqa: E402
    VonageVideoConnectorTransport,
    VonageVideoConnectorTransportParams,
)

from app.agent import (  # noqa: E402
    EchoAgent,
    MicrophoneEcho,
    RealtimeAgent,
    beep,
    connection_role,
    create_agent,
)
from app.agent_process import ProcessAgent  # noqa: E402
from app.session_manager import AgentConnection  # noqa: E402
from tests.conftest import with_settings  # noqa: E402
from tests.test_agent_process import RecordedEvents  # noqa: E402

CONNECTION = AgentConnection("app-id", "session-id", "agent-token")


@pytest.fixture(scope="module")
def transport():
    # One native client per test process: the connector warns on a second instance.
    return VonageVideoConnectorTransport(
        "app-id",
        "session-id",
        "agent-token",
        VonageVideoConnectorTransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


def realtime_session_properties(agent: RealtimeAgent) -> dict:
    return agent._llm._settings.session_properties.model_dump(exclude_none=True)


def test_realtime_defaults(settings, transport):
    agent = RealtimeAgent(settings, CONNECTION, RecordedEvents())
    agent.build_pipeline(transport)

    props = realtime_session_properties(agent)
    assert props["model"] == "gpt-realtime-2.1"
    assert agent._llm.base_url.endswith("?model=gpt-realtime-2.1")
    assert props["audio"]["input"]["turn_detection"] == {"type": "semantic_vad"}
    assert props["audio"]["input"]["noise_reduction"] == {"type": "near_field"}
    assert "transcription" not in props["audio"]["input"]  # not billed unless asked for
    assert "output" not in props["audio"]  # OpenAI's default voice
    assert "Vonage Video call" in props["instructions"]
    assert agent._llm._audio_input_paused is True  # nothing is sent before ready


def test_realtime_optional_settings(settings, transport):
    custom = with_settings(
        settings,
        openai_realtime_model="gpt-realtime-2.1-mini",
        openai_realtime_voice="marin",
        log_transcripts=True,
    )
    agent = RealtimeAgent(custom, CONNECTION, RecordedEvents())
    agent.build_pipeline(transport)

    props = realtime_session_properties(agent)
    assert props["model"] == "gpt-realtime-2.1-mini"
    assert props["audio"]["output"] == {"voice": "marin"}
    assert props["audio"]["input"]["transcription"]["model"] == "gpt-realtime-whisper"


async def test_conversation_starts_once_and_unpauses_audio(settings, transport):
    agent = RealtimeAgent(settings, CONNECTION, RecordedEvents())
    worker = agent.build_pipeline(transport)
    queued: list = []

    async def record(frames):
        queued.extend(frames)

    worker.queue_frames = record  # type: ignore[method-assign]

    await agent.start_conversation()  # before the worker exists: deferred
    assert queued == []
    agent._worker = worker
    await agent._begin_once()
    await agent.start_conversation()  # again: ignored

    assert [type(frame) for frame in queued] == [LLMRunFrame]
    assert agent._llm._audio_input_paused is False


async def test_stop_before_run_never_joins(settings):
    agent = RealtimeAgent(settings, CONNECTION, RecordedEvents())
    await agent.stop()
    await asyncio.wait_for(agent.run(), 1)
    assert agent._runner is None


def test_create_agent_picks_the_mode(settings):
    assert isinstance(create_agent(settings, CONNECTION, RecordedEvents()), RealtimeAgent)
    echo = with_settings(settings, agent_mode="echo", openai_api_key=None)
    assert isinstance(create_agent(echo, CONNECTION, RecordedEvents()), EchoAgent)


@pytest.mark.parametrize(
    ("client", "role"),
    [
        ({"connectionData": '{"role": "user"}'}, "user"),
        ({"connectionData": '{"role": "agent"}'}, "agent"),
        ({"connectionData": "not json"}, None),
        ({"connectionData": "[1, 2]"}, None),
        ({}, None),
    ],
)
def test_connection_role(client, role):
    assert connection_role(client) == role


def test_beep_is_audible_mono_pcm():
    frame = beep(seconds=0.5, sample_rate=16000)
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1
    assert len(frame.audio) == 0.5 * 16000 * 2
    assert max(frame.audio) > 0


async def test_microphone_echo_only_when_enabled():
    mic = InputAudioRawFrame(audio=b"\x01\x00" * 160, sample_rate=16000, num_channels=1)

    echo = MicrophoneEcho()
    down, _ = await run_test(echo, frames_to_send=[mic])
    assert not any(isinstance(f, OutputAudioRawFrame) for f in down)

    echo = MicrophoneEcho()
    echo.enabled = True
    down, _ = await run_test(echo, frames_to_send=[mic])
    echoed = [f for f in down if isinstance(f, OutputAudioRawFrame)]
    assert len(echoed) == 1 and echoed[0].audio == mic.audio


async def test_real_agent_process_reports_a_failed_join(settings, valid_env, monkeypatch):
    """Full agent process with Pipecat and the native connector, bogus credentials.

    Whether or not the machine can reach Vonage, the join must fail, be reported,
    and the process must exit on its own.
    """
    for name, value in valid_env.items():
        monkeypatch.setenv(name, value)
    events = RecordedEvents()
    agent = ProcessAgent(
        settings,
        AgentConnection(valid_env["VONAGE_APPLICATION_ID"], "1_MX4wfmZha2V-fn4", "not-a-token"),
        events,
    )
    await asyncio.wait_for(agent.run(), 120)
    assert events.failures, "the failed join was not reported"
    assert "agent_joined" not in events.names
    assert agent._process is not None and agent._process.returncode is not None
