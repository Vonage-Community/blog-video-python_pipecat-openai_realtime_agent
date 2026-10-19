# Changelog

All notable changes to this sample. After the tutorial release, only security fixes
and documentation corrections land here, as patch versions.

## [Unreleased] 1.0.0 (tutorial)

Not tagged yet: waiting for the live manual test (`docs/MANUAL_TEST.md`).

- Browser app with the Vonage Video Web SDK 2.35.3: microphone (camera optional),
  subscribes only to the agent's stream, autoplay handling, mute, leave.
- Pipecat agent with `VonageVideoConnectorTransport` and `OpenAIRealtimeLLMService`
  (Vonage Pipecat fork `be8ede1`), greeting once after the browser is ready, and
  interruption through OpenAI server-side turn detection.
- FastAPI session API with per-session keys, a session cap, rate limiting, and
  ready, idle, and maximum-duration timeouts.
- One process per agent, with graceful stop, then SIGTERM, then SIGKILL.
- `AGENT_MODE=echo` diagnostic that checks Vonage audio in both directions without OpenAI.
- Docker image on Python 3.12 / Debian bookworm, running as a non-root user, with
  the Vonage private key mounted as a Compose secret.
