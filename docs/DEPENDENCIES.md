# Dependencies and verified versions

Everything installs from one manifest (`pyproject.toml`) and one lock file (`uv.lock`).
The Docker image runs `uv sync --frozen`, so a build installs exactly what the lock
says or fails.

## Pinned versions

Versions as installed in the Docker image and printed by `importlib.metadata` inside
the container (2026-11-18):

| Component | Version | Source |
|---|---|---|
| Python | 3.12.16 | `python:3.12-slim-bookworm` (Debian 12, glibc 2.36) |
| `pipecat-ai` | 1.16.0 | PyPI, with the `openai` and `vonage-video-connector` extras |
| `vonage-video-connector` | 1.0.3 | PyPI, via Pipecat's `vonage-video-connector` extra (OpenTok Linux SDK 2.32.1 inside) |
| `openai` | 3.29.0 | PyPI, via Pipecat's `openai` extra |
| `websockets` | 17.2 | PyPI |
| `vonage` (server SDK) | 4.10.0 | PyPI (`vonage-video` 1.6.1) |
| `fastapi` / `uvicorn` | 0.162.5 / 0.54.0 | PyPI |
| `pydantic` | 2.14.1 | PyPI (the connector requires `~=2.11`) |
| `numpy` / `onnxruntime` | 2.5.4 / 1.24.4 | PyPI, Pipecat dependencies |
| Vonage Video Web SDK | `@vonage/client-sdk-video@2.36.0` | npm via unpkg |
| OS packages | `libexpat1`, `libglib2.0-0` | Debian bookworm; the list Vonage's server samples install |
| uv | 0.11.32 | PyPI, build stage only |

`[tool.uv] exclude-newer = "2026-11-13T00:00:00Z"` keeps re-locking away from
same-week releases.

## The Pipecat source: PyPI `pipecat-ai`

- **Why PyPI:** the [Vonage Video Transport for Pipecat guide](https://developer.vonage.com/en/video/guides/vonage-video-connector-pipecat-transport)
  installs `pipecat-ai[vonage-video-connector]` from PyPI, so readers get the same
  package the official docs describe. Pinning an exact version in `pyproject.toml`
  means the tutorial can't change underneath its readers.
- **History:** versions up to 1.0.0 of this sample used the
  [Vonage Pipecat fork](https://github.com/Vonage/pipecat) at commit `be8ede1`
  (fork tag `v1.11.0.post1`). Its Vonage transport differed from upstream only by a
  `get_client_id()` helper this sample doesn't use, and the fork has had no commits
  since 2026-10-06.
- **One source only:** never install the fork from Git alongside PyPI `pipecat-ai`;
  one replaces the other.

## APIs verified at the pinned version

Read in Pipecat's source and exercised by `tests/test_agent.py` with the real classes:

- `VonageVideoConnectorTransport(application_id, session_id, token, params)` with
  `VonageVideoConnectorTransportParams(audio_in_enabled, audio_out_enabled,
  publisher_name, audio_in_auto_subscribe, video_connector_log_level)`;
  `clear_buffers_on_interruption` defaults to `True`.
- Transport events: `on_joined`, `on_participant_joined`/`on_participant_left`
  (with `connectionData` from the token), `on_client_connected` (the agent's
  subscription to a stream is connected), `on_error`.
- `OpenAIRealtimeLLMService(api_key=..., settings=OpenAIRealtimeLLMService.Settings(
  model, system_instruction, session_properties), start_audio_paused=...)`, import
  path `pipecat.services.openai.realtime.llm`. Default model `gpt-realtime-2.1`.
  It opens the OpenAI WebSocket when the pipeline is set up.
- `PipelineWorker`, `WorkerRunner`, `LLMContext`, `LLMContextAggregatorPair`,
  `ProcessorUnusablePolicy`, and worker events `on_pipeline_error`,
  `on_idle_timeout`, `on_setup_timeout` (setup timeout 20 s).
- `WorkerRunner.run()` clears its shutdown flag when it starts, so a `cancel()` sent
  before `run()` is lost. `app/agent.py` re-checks in the runner's `on_ready` handler.

## Where sources disagreed, and what this sample does

| Topic | Sources | Choice |
|---|---|---|
| Turn detection | The fork's `transports-vonage.py` adds local Silero VAD; upstream `realtime-openai.py` says local VAD is unnecessary with OpenAI's server VAD. | OpenAI server-side `semantic_vad`. In realtime mode the aggregator turns OpenAI's speech-start events into `InterruptionFrame`s, and the Vonage output transport clears its buffers on them. |
| Pipeline classes | Older project notes used `PipelineTask` and `OpenAILLMContext`. | Pipecat 1.16's `PipelineWorker`, `WorkerRunner`, `LLMContext`. |
| Connector instances | The Vonage Video Connector guide's limits list one connector instance per process. The native SDK logs "Trying to create a second instance of VonageVideoClient", even after the first is freed. | One OS process per session (`app/agent_process.py`). |
| Routed sessions | The project brief requires routed sessions. Vonage docs require them for the Audio Connector; the Video Connector guide doesn't say either way. Kitt Phi's tested tutorial uses routed sessions. | Always routed. |
| Native log level | Pipecat's transport defaults the connector to `INFO`; the connector's own default is `WARN`. | `WARN`, or `INFO` when `LOG_LEVEL=DEBUG`. Native logs go straight to stderr and bypass the app's redaction. |
| Web SDK URL | Vonage docs show the unversioned `https://video.standard.vonage.com/v2/js/opentok.min.js` and unpkg `@latest`. | unpkg with an exact version, so the tutorial doesn't drift. |
| Realtime model | Pipecat's default is `gpt-realtime-2.1`; OpenAI announced `gpt-realtime-2.1` and `gpt-realtime-2.1-mini` in July 2026. | Default `gpt-realtime-2.1`, configurable. **Not checked against an OpenAI account** (see below). |

## Vonage server SDK details worth knowing

- `TokenOptions(exp=None)` fails validation with a `TypeError`. Always pass `exp`.
- `HttpClientOptions.timeout` defaults to no timeout. The app sets 10 seconds.
- The JWT client treats a string ending in `.something` as a file path, and otherwise
  requires `-----BEGIN PRIVATE KEY-----` (PKCS#8). The app reads the key file itself
  and passes the PEM text, and explains how to convert a PKCS#1 key.
- Token `connection_data` reaches the Web SDK as `stream.connection.data` and the
  Pipecat transport as `connectionData`. Both sides use it to identify roles.

## Platforms

| Platform | Status |
|---|---|
| Linux x86_64 (Docker) | **Verified with 1.0.0 (fork):** image builds, imports load, the native library resolves every shared library (`ldd`), and the tests pass. Not yet re-run with Pipecat 1.16.0. |
| Linux arm64 (Docker Desktop on Apple Silicon) | **Verified 2026-11-18 with Pipecat 1.16.0:** the image builds from a normal network, lint, types, and tests pass inside it, and in `echo` mode the agent joins a real routed Vonage session and publishes its audio stream. |
| macOS host | **Partly verified:** Docker Desktop 29.5 on Apple Silicon, as above. Browser audio not yet tested. |
| Windows host | **Not verified.** Supported through Docker Desktop: the agent runs in the Linux container, and the browser uses `localhost`. |
| Running the agent outside Docker on macOS/Windows | Not supported: the connector ships Linux wheels only. The API and its unit tests do run there; the Linux-only tests skip. |

The wheels are `manylinux_2_35`, so any base image needs glibc 2.35 or newer.
Vonage recommends Debian Bookworm for the connector.

## How this was verified, and what wasn't

On 2026-11-18, on macOS with Docker Desktop (arm64), on a normal network:

- `docker build --target test .` built the image, including `apt-get` from Debian
  mirrors, and passed ruff, mypy, and pytest (79 passed, 1 skipped).
- In `echo` mode, `POST /api/sessions` created a routed Vonage session, the agent
  process joined it, published audio, and exited cleanly when the ready timeout
  ended the session.
- The Web SDK URL on unpkg returns 200.
- **Not yet verified:** browser audio in both directions, and any OpenAI Realtime
  call. See `docs/MANUAL_TEST.md`.

## Updating dependencies

1. Change the pin in `pyproject.toml` (and `exclude-newer` if needed), then run `uv lock`.
2. If the Pipecat version changes, re-read `transports/vonage/video_connector.py`,
   `transports/vonage/client.py`, and `services/openai/realtime/llm.py` at the new
   commit, and update the tables above.
3. Run `docker build --target test .` and the full manual test.
4. Ship a new patch or minor release with a CHANGELOG entry. Never move the
   `v1.0.0-tutorial` tag.
