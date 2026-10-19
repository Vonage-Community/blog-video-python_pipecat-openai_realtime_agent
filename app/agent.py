"""The AI participant: a Pipecat pipeline that joins the Vonage Video session.

    Vonage Video session (routed)
        │  your microphone audio
        ▼
    VonageVideoConnectorTransport.input()
        ▼
    LLMContextAggregatorPair (user half)
        ▼
    OpenAIRealtimeLLMService  ◄──WebSocket──►  OpenAI Realtime (speech-to-speech)
        ▼
    VonageVideoConnectorTransport.output()
        │  the agent's voice, published as a Vonage stream
        ▼
    Vonage Video session ──► your browser subscribes and plays it

The browser never talks to OpenAI directly: every sound the agent makes reaches
you through the Vonage Video session.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    LLMRunFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.realtime.events import (
    AudioConfiguration,
    AudioInput,
    AudioOutput,
    InputAudioNoiseReduction,
    InputAudioTranscription,
    SemanticTurnDetection,
    SessionProperties,
)
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.vonage.video_connector import (
    VonageVideoConnectorTransport,
    VonageVideoConnectorTransportParams,
)
from pipecat.workers.runner import WorkerRunner

from app.config import Settings
from app.session_manager import AgentConnection, AgentEvents

SYSTEM_INSTRUCTION = """\
You are a friendly AI voice assistant taking part in a Vonage Video call.
You can hear the user, but you cannot see them.
Everything you say is spoken aloud, so keep replies short and conversational:
one to three sentences unless the user asks for more. Don't use lists,
markdown, or emoji. Reply in the language the user speaks.
If you are not sure about something, say so. If anyone asks, you are an AI
model, not a human."""

GREETING_PROMPT = (
    "The user has just joined the call and can hear you now. Greet them in one short "
    "sentence and ask what they would like to talk about."
)


def connection_role(client: dict[str, Any]) -> str | None:
    """Read the role our server put in the token's connection data."""
    try:
        data = json.loads(client.get("connectionData") or "{}")
    except (TypeError, ValueError):
        return None
    return data.get("role") if isinstance(data, dict) else None


class VonageAgent:
    """Runs one Pipecat pipeline inside one Vonage Video session.

    Subclasses build the pipeline. This base class wires up the Vonage transport
    events, the session lifecycle, and stopping.
    """

    def __init__(self, settings: Settings, connection: AgentConnection, events: AgentEvents):
        self._settings = settings
        self._connection = connection
        self._events = events
        self._runner: WorkerRunner | None = None
        self._worker: PipelineWorker | None = None
        self._stop_requested = False
        self._conversation_requested = False
        self._conversation_started = False
        self._joined = False
        self._failure_reported = False

    def build_pipeline(self, transport: VonageVideoConnectorTransport) -> PipelineWorker:
        raise NotImplementedError

    async def begin_conversation(self, worker: PipelineWorker) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        """Join the session and run until stopped, idle, or failed."""
        if self._stop_requested:
            return

        transport = VonageVideoConnectorTransport(
            self._connection.application_id,
            self._connection.session_id,
            self._connection.token,
            VonageVideoConnectorTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                # The browser shows this name next to the agent's stream.
                publisher_name="AI agent",
                # Subscribe to every audio stream in the session. Only the browser
                # holds the other token, so that is the user's microphone.
                audio_in_auto_subscribe=True,
                # The native SDK logs straight to stderr, bypassing our redaction.
                video_connector_log_level="INFO" if self._settings.log_level == "DEBUG" else "WARN",
            ),
        )
        self._register_transport_events(transport)

        worker = self.build_pipeline(transport)
        self._register_worker_events(worker)
        self._worker = worker
        if self._conversation_requested:
            await self._begin_once()

        runner = WorkerRunner(handle_sigint=False)
        self._runner = runner

        @runner.event_handler("on_ready")
        async def on_ready(runner: WorkerRunner) -> None:
            # stop() may have been called while the runner was starting.
            if self._stop_requested:
                await runner.cancel(reason="session ended")

        await runner.add_workers(worker)
        await runner.run()

        # Safety net: never end silently without having joined.
        if not self._joined and not self._stop_requested:
            self._report_failure(
                "The agent stopped before it joined the Vonage session. See the server logs."
            )

    async def start_conversation(self) -> None:
        """Called once the browser has subscribed to the agent and can play its audio."""
        self._conversation_requested = True
        if self._worker is not None:
            await self._begin_once()

    async def stop(self) -> None:
        """Leave the Vonage session and close provider connections. Idempotent."""
        self._stop_requested = True
        if self._runner is not None:
            await self._runner.cancel(reason="session ended")

    def _report_failure(self, message: str) -> None:
        if not self._failure_reported:
            self._failure_reported = True
            self._events.agent_failed(message)

    async def _begin_once(self) -> None:
        if self._conversation_started or self._worker is None:
            return
        self._conversation_started = True
        await self.begin_conversation(self._worker)

    def _register_transport_events(self, transport: VonageVideoConnectorTransport) -> None:
        @transport.event_handler("on_joined")
        async def on_joined(transport: VonageVideoConnectorTransport, data: dict) -> None:
            logger.info("Agent joined the Vonage Video session")
            self._joined = True
            self._events.agent_joined()

        @transport.event_handler("on_participant_joined")
        async def on_participant_joined(transport: VonageVideoConnectorTransport, client: dict):
            role = connection_role(client) or "unknown"
            logger.info(f"Stream {client['streamId']} received from a '{role}' participant")

        # Fires when the agent's subscription to a stream is connected, i.e. it
        # can now hear that participant.
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport: VonageVideoConnectorTransport, client: dict):
            if connection_role(client) == "user":
                logger.info("Agent is receiving the user's audio")
                self._events.user_audio_connected()

        @transport.event_handler("on_participant_left")
        async def on_participant_left(transport: VonageVideoConnectorTransport, client: dict):
            if connection_role(client) == "user":
                logger.info("The user's stream left the session")
                self._events.user_left()

        @transport.event_handler("on_error")
        async def on_error(transport: VonageVideoConnectorTransport, description: str) -> None:
            self._report_failure(f"Vonage Video Connector error: {description}")

    def _register_worker_events(self, worker: PipelineWorker) -> None:
        @worker.event_handler("on_pipeline_error")
        async def on_pipeline_error(worker: PipelineWorker, frame: ErrorFrame) -> None:
            # A provider error (bad API key, unknown model, quota) or a lost Vonage
            # connection. End the session visibly instead of leaving a silent agent.
            self._report_failure(frame.error)
            if self._runner is not None:
                await self._runner.cancel(reason="pipeline error")

        @worker.event_handler("on_setup_timeout")
        async def on_setup_timeout(worker: PipelineWorker) -> None:
            # Connecting to Vonage and OpenAI hung (a firewall that drops packets,
            # for example). The worker stops itself after this.
            self._report_failure(
                "Timed out connecting to Vonage Video and OpenAI. Check that the container "
                "can reach both services."
            )

        @worker.event_handler("on_idle_timeout")
        async def on_idle_timeout(worker: PipelineWorker) -> None:
            # PipelineWorker cancels itself and the runner after this.
            self._events.idle_timeout()


class RealtimeAgent(VonageAgent):
    """Speech-to-speech with OpenAI Realtime."""

    def build_pipeline(self, transport: VonageVideoConnectorTransport) -> PipelineWorker:
        settings = self._settings
        assert settings.openai_api_key is not None

        audio_output = (
            AudioOutput(voice=settings.openai_realtime_voice)
            if settings.openai_realtime_voice
            else None
        )
        self._llm = OpenAIRealtimeLLMService(
            api_key=settings.openai_api_key.get(),
            settings=OpenAIRealtimeLLMService.Settings(
                model=settings.openai_realtime_model,
                system_instruction=SYSTEM_INSTRUCTION,
                session_properties=SessionProperties(
                    audio=AudioConfiguration(
                        input=AudioInput(
                            # OpenAI detects when the user starts and stops talking.
                            # Speaking over the agent interrupts it.
                            turn_detection=SemanticTurnDetection(),
                            noise_reduction=InputAudioNoiseReduction(type="near_field"),
                            # Transcribing your speech is billed separately, so it is
                            # only enabled when you ask for transcripts in the logs.
                            transcription=(
                                InputAudioTranscription() if settings.log_transcripts else None
                            ),
                        ),
                        output=audio_output,
                    ),
                ),
            ),
            # Send no microphone audio to OpenAI until the browser can hear the agent.
            start_audio_paused=True,
        )

        context = LLMContext([{"role": "developer", "content": GREETING_PROMPT}])
        # With a realtime service, the aggregator pair follows OpenAI's server-side
        # turn detection and broadcasts interruptions when the user barges in.
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

        if settings.log_transcripts:

            @user_aggregator.event_handler("on_user_turn_message_added")
            async def on_user_message(aggregator: Any, message: Any) -> None:
                logger.info(f"Transcript user: {message.content}")

            @assistant_aggregator.event_handler("on_assistant_turn_stopped")
            async def on_assistant_message(aggregator: Any, message: Any) -> None:
                logger.info(f"Transcript agent: {message.content}")

        pipeline = Pipeline(
            [
                transport.input(),
                user_aggregator,
                self._llm,
                transport.output(),
                assistant_aggregator,
            ]
        )
        return PipelineWorker(
            pipeline,
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
            idle_timeout_secs=settings.idle_timeout_seconds,
            processor_unusable_policy=ProcessorUnusablePolicy.END,
            # The browser follows the call through Vonage events, not Pipecat's RTVI protocol.
            enable_rtvi=False,
        )

    async def begin_conversation(self, worker: PipelineWorker) -> None:
        logger.info("Browser is ready: unpausing microphone audio and requesting a greeting")
        self._llm.set_audio_input_paused(False)
        # LLMRunFrame sends the context (the greeting prompt) and asks for a response.
        await worker.queue_frames([LLMRunFrame()])


class MicrophoneEcho(FrameProcessor):
    """Turns incoming microphone audio into outgoing audio once enabled."""

    def __init__(self) -> None:
        super().__init__()
        self.enabled = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self.enabled and isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            )
        await self.push_frame(frame, direction)


def beep(seconds: float = 0.6, hz: float = 440.0, sample_rate: int = 24000) -> OutputAudioRawFrame:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    samples = (0.3 * 32767 * np.sin(2 * np.pi * hz * t)).astype(np.int16)
    return OutputAudioRawFrame(audio=samples.tobytes(), sample_rate=sample_rate, num_channels=1)


class EchoAgent(VonageAgent):
    """Diagnostic mode (AGENT_MODE=echo): no OpenAI, no API cost beyond Vonage.

    Plays a beep when the browser is ready, then echoes your microphone back.
    Hearing both proves audio flows in each direction through Vonage Video.
    """

    def build_pipeline(self, transport: VonageVideoConnectorTransport) -> PipelineWorker:
        self._echo = MicrophoneEcho()
        pipeline = Pipeline([transport.input(), self._echo, transport.output()])
        return PipelineWorker(
            pipeline,
            idle_timeout_secs=self._settings.idle_timeout_seconds,
            processor_unusable_policy=ProcessorUnusablePolicy.END,
            enable_rtvi=False,
        )

    async def begin_conversation(self, worker: PipelineWorker) -> None:
        logger.info("Browser is ready: playing a test beep, then echoing the microphone")
        await worker.queue_frames([beep()])
        self._echo.enabled = True


def create_agent(
    settings: Settings, connection: AgentConnection, events: AgentEvents
) -> VonageAgent:
    if settings.agent_mode == "echo":
        return EchoAgent(settings, connection, events)
    return RealtimeAgent(settings, connection, events)
