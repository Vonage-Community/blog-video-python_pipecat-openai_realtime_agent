"""Runs each session's agent in its own process.

The native Vonage Video Connector is built around one client per process, so the
server starts a fresh `python -m app.agent_process` for every session. A separate
process also means a stuck or crashed agent can always be killed, which closes the
Vonage connection and the OpenAI WebSocket with it.

The two processes exchange newline-delimited JSON over pipes:

    server -> agent (stdin):   {"applicationId", "sessionId", "token", "label"}  (first line)
                               {"command": "start_conversation"} | {"command": "stop"}
    agent -> server (stdout):  {"event": "agent_joined"}, {"event": "agent_failed", "message": ...}

The agent's token travels over the pipe, not argv or the environment, so it never
shows up in `ps` output. Logs go to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, NoReturn

from loguru import logger

from app.config import ConfigError, Settings, load_settings, setup_logging
from app.session_manager import Agent, AgentConnection, AgentEvents

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Agent side: how long a stopping agent may take before its process just exits.
STOP_TIMEOUT_SECONDS = 5.0
# Server side: how long after SIGTERM before SIGKILL.
TERMINATE_GRACE_SECONDS = 5.0
EVENTS = {"agent_joined", "user_audio_connected", "user_left", "agent_failed", "idle_timeout"}


class AgentProcessError(Exception):
    pass


class ProcessAgent:
    """Server side: starts the agent process and relays commands and events."""

    def __init__(
        self,
        settings: Settings,
        connection: AgentConnection,
        events: AgentEvents,
        label: str = "agent",
        command: list[str] | None = None,
    ):
        self._connection = connection
        self._events = events
        self._label = label
        self._command = command or [sys.executable, "-m", "app.agent_process"]
        self._process: asyncio.subprocess.Process | None = None
        self._start_requested = False
        self._stop_requested = False
        self._failure_reported = False

    async def run(self) -> None:
        """Start the process and relay its events until it exits."""
        if self._stop_requested:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=PROJECT_ROOT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        process = self._process
        logger.info(f"Started agent process {process.pid}")
        try:
            await self._send(
                {
                    "applicationId": self._connection.application_id,
                    "sessionId": self._connection.session_id,
                    "token": self._connection.token,
                    "label": self._label,
                }
            )
            if self._start_requested:
                await self._send({"command": "start_conversation"})
            if self._stop_requested:
                await self._send({"command": "stop"})

            assert process.stdout is not None
            async for line in process.stdout:
                self._dispatch(line)
            code = await process.wait()
        except asyncio.CancelledError:
            # The supervisor gave up waiting for a graceful stop.
            await self._terminate()
            raise
        finally:
            if process.returncode is None:
                await self._terminate()
        logger.info(f"Agent process {process.pid} exited with code {code}")
        if code != 0 and not self._failure_reported:
            raise AgentProcessError(f"agent process exited with code {code}")

    async def start_conversation(self) -> None:
        self._start_requested = True
        if self._process is not None:
            await self._send({"command": "start_conversation"})

    async def stop(self) -> None:
        self._stop_requested = True
        if self._process is not None:
            await self._send({"command": "stop"})

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            return
        try:
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the process already exited; run() reports how

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
            event = message["event"]
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring malformed message from the agent process")
            return
        if event not in EVENTS:
            logger.warning(f"Ignoring unknown agent event {event!r}")
            return
        if event == "agent_failed":
            self._failure_reported = True
            self._events.agent_failed(str(message.get("message", "unknown error")))
        else:
            getattr(self._events, event)()

    async def _terminate(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        logger.warning(f"Terminating agent process {process.pid}")
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            logger.warning(f"Killing agent process {process.pid}")
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


# --- Agent process side --------------------------------------------------------


class PipeEvents:
    """AgentEvents implementation that writes each event as one JSON line."""

    def __init__(self, out: IO[str]):
        self._out = out

    def _emit(self, event: str, **fields: Any) -> None:
        self._out.write(json.dumps({"event": event, **fields}) + "\n")
        self._out.flush()

    def agent_joined(self) -> None:
        self._emit("agent_joined")

    def user_audio_connected(self) -> None:
        self._emit("user_audio_connected")

    def user_left(self) -> None:
        self._emit("user_left")

    def agent_failed(self, message: str) -> None:
        self._emit("agent_failed", message=message)

    def idle_timeout(self) -> None:
        self._emit("idle_timeout")


AgentBuilder = Callable[[Settings, AgentConnection, AgentEvents], Agent]


def exit_now(code: int) -> NoReturn:
    """Leave immediately, without waiting for threads stuck in native code."""
    sys.stderr.flush()
    os._exit(code)


async def serve(build_agent: AgentBuilder, events_out: IO[str]) -> int:
    """Read the connection, run the agent, and obey commands until it finishes."""
    events = PipeEvents(events_out)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

    try:
        first = json.loads(await reader.readline())
        connection = AgentConnection(first["applicationId"], first["sessionId"], first["token"])
        label = str(first.get("label", "agent"))
    except (ValueError, KeyError, TypeError):
        events.agent_failed("Agent process received no valid connection details")
        return 2

    try:
        settings = load_settings()
    except ConfigError as exc:
        events.agent_failed("Configuration error: " + "; ".join(exc.problems))
        return 2
    setup_logging(settings.log_level, label=label)
    logger.info(f"Agent process {os.getpid()} starting")

    agent = build_agent(settings, connection, events)
    stop = asyncio.Event()

    def on_signal() -> None:
        if stop.is_set():
            exit_now(1)  # asked twice: the server has given up on a graceful stop
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal)

    async def read_commands() -> None:
        while line := await reader.readline():
            try:
                command = json.loads(line).get("command")
            except (ValueError, AttributeError):
                logger.warning("Ignoring malformed command from the server")
                continue
            if command == "start_conversation":
                await agent.start_conversation()
            elif command == "stop":
                break
        stop.set()  # "stop", or the server closed the pipe

    run_task = asyncio.create_task(agent.run(), name="agent-run")
    commands_task = asyncio.create_task(read_commands(), name="agent-commands")
    stop_task = asyncio.create_task(stop.wait(), name="agent-stop")
    try:
        await asyncio.wait({run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if not run_task.done():
            logger.info("Stopping the agent")
            await agent.stop()
            try:
                await asyncio.wait_for(asyncio.shield(run_task), STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                # Usually a connection attempt that is still hanging inside the
                # native SDK. Exiting closes every socket, which is all the
                # cleanup that is left to do.
                logger.warning("The agent did not stop in time; exiting the process")
                exit_now(0)
    finally:
        for task in (commands_task, stop_task):
            task.cancel()
        await asyncio.gather(commands_task, stop_task, return_exceptions=True)

    error = None if run_task.cancelled() else run_task.exception()
    if error is not None:
        logger.opt(exception=error).error("Agent crashed")
        events.agent_failed(f"Agent crashed: {type(error).__name__}: {error}")
        return 1
    logger.info("Agent finished")
    return 0


def main() -> None:
    # Keep the real stdout for events only. Anything else written to stdout,
    # including output from the native SDK, is redirected to stderr.
    events_out = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    setup_logging(os.environ.get("LOG_LEVEL", "INFO").upper(), label="agent")

    from app.agent import create_agent  # imports Pipecat and the native connector

    sys.exit(asyncio.run(serve(create_agent, events_out)))


if __name__ == "__main__":
    main()
