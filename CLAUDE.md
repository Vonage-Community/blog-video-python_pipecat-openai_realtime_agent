# CLAUDE.md

Maintainer notes for **vonage-pipecat-openai-realtime**, the Part 1 sample of a
three-part Vonage Developer Blog series. This repository is the companion to a
published tutorial and must keep matching it.

## Scope: keep it Part 1

- One speech-to-speech provider: **OpenAI Realtime**, through Pipecat's
  `OpenAIRealtimeLLMService`.
- Do **not** add Deepgram, ElevenLabs, other STT/LLM/TTS providers, provider
  abstractions, personas (no Einstein), avatars, AWS/AgentCore, or multi-agent
  features. Those belong to the separate Part 2 and Part 3 repositories, which must
  not become dependencies of this one, or this one of them.
- Keep the Vonage **Video** Connector transport (`VonageVideoConnectorTransport`).
  Don't substitute the Audio Connector or the Voice API.
- After the `v1.0.0-tutorial` tag: only security fixes and doc corrections, released
  as patch versions with a `CHANGELOG.md` entry. Never move or rewrite the tag.

## Commands

```bash
uv sync                                      # local venv (Linux has the native connector)
uv run pytest -q                             # tests; Linux-only ones skip on macOS/Windows
uv run ruff check app tests && uv run ruff format --check app tests
uv run mypy
docker build --target test .                 # lint + types + tests inside Linux
docker compose up --build                    # run the app on http://localhost:8000
```

## Architecture rules

- `app/agent.py` is the code the blog post teaches. Keep it readable and commented.
- One agent per OS process (`app/agent_process.py`): the Video Connector supports one
  connector instance per process. Don't move agents back into the server process.
- Every session has a tracked supervisor task (`app/session_manager.py`) with ready,
  idle, and maximum-duration limits, and an idempotent end. No untracked
  `asyncio.create_task`.
- The browser identifies the agent's stream by the token's connection data
  (`{"role": "agent"}`), and the agent greets only after `POST /ready`.
- Only the browser token is returned to the browser. The agent token travels over a
  pipe, never argv or env. API keys and the private key never leave the server.
- Logs go through `app.config.setup_logging`, which redacts keys and tokens. Don't log
  transcripts unless `LOG_TRANSCRIPTS=true`.
- FastAPI resolves annotations against module globals (`from __future__ import
  annotations`). Header aliases like `AuthHeader` must stay at module level.

## Dependencies

- Pipecat comes from the Vonage fork at one pinned commit; see
  `docs/DEPENDENCIES.md` before changing it. Never also install `pipecat-ai` from PyPI.
- `uv.lock` is committed; Docker uses `uv sync --frozen`.
- Check the real source of a new Pipecat or Vonage version before using an API. Don't
  rely on memory or older examples.

## Verification honesty

Mocked tests don't prove WebRTC audio. Any change touching media, the agent, or
dependencies needs `docs/MANUAL_TEST.md` with real credentials. Report anything not
run as NOT VERIFIED.

## Publishing

Don't create remotes, push, or tag without the maintainer's explicit approval.
