"""A stand-in agent process for tests: the real IPC loop (serve) around a fake agent.

FAKE_AGENT_MODE selects the behaviour: "normal", "crash", or "ignore_stop".
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_process import serve  # noqa: E402

MODE = os.environ.get("FAKE_AGENT_MODE", "normal")


class FakeAgent:
    def __init__(self, events):
        self.events = events
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        if MODE == "crash":
            raise RuntimeError("fake crash")
        self.events.agent_joined()
        await self._stopped.wait()

    async def start_conversation(self) -> None:
        self.events.user_audio_connected()  # something the test can observe

    async def stop(self) -> None:
        if MODE != "ignore_stop":
            self._stopped.set()


if __name__ == "__main__":
    events_out = os.fdopen(os.dup(sys.stdout.fileno()), "w")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    print("stray stdout output must not corrupt the event stream")
    sys.exit(asyncio.run(serve(lambda settings, connection, events: FakeAgent(events), events_out)))
