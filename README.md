# Real-time OpenAI voice agent in a Vonage Video call (Pipecat)

Talk to an AI voice agent that joins your Vonage Video session as a real participant:
your browser publishes your microphone, a Pipecat agent subscribes to it, and OpenAI
Realtime (speech-to-speech) answers through the same Vonage session.

> **Docker required.** The native Vonage Video Connector SDK runs on Linux, so the
> agent runs in a Linux container. macOS and Windows users can use Docker Desktop;
> your browser connects to it on `localhost`.

## What you will build

- A web page where you click **Start conversation**, allow your microphone, and talk.
- A small FastAPI server that creates a **routed** Vonage Video session and two
  separate tokens: one for your browser, one for the agent. Only the browser's
  token ever leaves the server.
- An AI participant built with [Pipecat](https://docs.pipecat.ai): it joins the same
  session with `VonageVideoConnectorTransport`, streams your audio to OpenAI Realtime,
  and publishes the spoken reply back into the session.
- You hear the agent **through your Vonage subscription**, not via a direct
  browser-to-OpenAI connection. Talk over it to interrupt; mute, leave, or let it time
  out, and the agent leaves the call and closes its OpenAI connection.

The agent is **audio-only**. The page shows a labeled placeholder for it, not a fake
video. Your camera is optional.

## Architecture

```text
┌────────────────────────────────┐                         ┌────────────────────────────────┐
│ Browser: Vonage Video Web SDK  │    POST /api/sessions   │ FastAPI server                 │
│ • publishes your microphone    │  ─────────────────────► │ • routed session + 2 tokens    │
│ • plays only the stream whose  │  ◄───────────────────── │ • limits, timeouts, ready/end  │
│   connection data says agent   │    browser token only   │ • one process per agent        │
└────────────────┬───────────────┘                         └────────────────┬───────────────┘
                 │ WebRTC                                                   │ starts it (agent token)
                 ▼                                                          ▼
┌────────────────────────────────┐                         ┌────────────────────────────────┐
│ Vonage Video API               │◄──────── WebRTC ───────►│ Agent process: Pipecat         │
│ routed session                 │                         │ VonageVideoConnectorTransport  │
└────────────────────────────────┘                         │ → OpenAIRealtimeLLMService     │
                                                           └────────────────┬───────────────┘
                                                                            │ WebSocket
                                                                            ▼
                                                           ┌────────────────────────────────┐
                                                           │ OpenAI Realtime API            │
                                                           │ speech-to-speech               │
                                                           └────────────────────────────────┘
```

| Piece | Role |
|---|---|
| Vonage Video API | The call: a routed session that carries audio between you and the agent. |
| Vonage Video Web SDK | Your side: microphone, optional camera, playing the agent's audio. |
| Vonage Video Connector + Pipecat transport | The agent's side: joins the session from Python as a WebRTC participant. |
| OpenAI Realtime | Listens, decides when you've finished speaking, and replies with speech. |
| FastAPI | Creates sessions and tokens, keeps keys server-side, starts and stops agents. |

Each session's agent runs in its own process because the Video Connector supports
one connector instance per process. It also means a stuck agent can always be killed.

## Prerequisites

- **A Vonage account** and the [Vonage CLI](https://github.com/Vonage/vonage-cli)
  3.3.1 or newer (`npm install -g @vonage/cli`), to create the Video application
  (steps below).
- **An OpenAI API key** with access to a Realtime model and API billing set up.
  A ChatGPT subscription does not include API credits.
- **Docker** with Docker Compose v2 (Docker Desktop on macOS/Windows).
- A current **Chrome, Edge, Firefox, or Safari** and a microphone. Headphones help.
- **Outbound network access** from your machine and the container to Vonage
  (HTTPS plus WebRTC media) and to `api.openai.com`.
- Open the app at `http://localhost`. Browsers only grant microphone access on
  `localhost` or HTTPS, so opening it from another device needs HTTPS.

Both services bill per use; see [Limitations and costs](#limitations-and-costs).

## Quick start

```bash
git clone https://github.com/Vonage-Community/vonage-pipecat-openai-realtime.git
cd vonage-pipecat-openai-realtime
```

### 1. Create the Vonage application

Authenticate the CLI with the API key and secret from the
[Vonage API Dashboard](https://dashboard.vonage.com), then create the application.
`--private-key-file` saves its private key straight into `secrets/`:

```bash
vonage auth set --api-key='YOUR_API_KEY' --api-secret='YOUR_API_SECRET'
vonage apps create "Pipecat OpenAI Realtime Agent" --private-key-file ./secrets/private.key
```

Copy the **Application ID** from the output, then turn on Video for it:

```bash
vonage apps capabilities update <APPLICATION_ID> video > /dev/null
vonage apps show <APPLICATION_ID>
```

At the time of writing, Vonage CLI 3.3.1 prints your stored credentials when it
updates the Video capability, so the command discards its output (errors still
show). `vonage apps show` confirms the application now lists `VIDEO`.

### 2. Configure the app

```bash
cp .env.example .env
```

Edit `.env` and set:

```dotenv
VONAGE_APPLICATION_ID=your-application-id
OPENAI_API_KEY=sk-...
```

### 3. Set the private key's permissions

```bash
chmod 700 secrets              # other users on this machine can't open the folder
chmod 644 secrets/private.key  # the container's unprivileged user can read the file
```

`secrets/` is ignored by Git and excluded from the Docker build. Compose mounts the key
read-only into the container at `/run/secrets/vonage_private_key`.

### 4. Start it

```bash
docker compose up --build
```

The first build downloads Python dependencies and takes a few minutes. When it's
ready you'll see:

```text
app-1  | … | INFO    | server | app.main:… - Ready: agent=OpenAI Realtime model gpt-realtime-2.1; max 1 session(s) of 600s; idle timeout 120s
app-1  | INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

If any setting is missing, the server still starts and lists what to fix, in the
logs and on the page.

### 5. Talk to it

Open **<http://localhost:8000>**, click **Start conversation**, and allow microphone
access. The agent joins within a few seconds and greets you once. Answer it, ask it
something, and talk over it to interrupt. Click **Leave** when you're done.

Stop the server with `Ctrl-C` (or `docker compose down`).

## Configuration

All settings live in `.env` ([`.env.example`](.env.example) documents each one).

| Variable | Default | Purpose |
|---|---|---|
| `VONAGE_APPLICATION_ID` | (required) | Vonage application ID (UUID) with Video enabled. |
| `VONAGE_PRIVATE_KEY_FILE` | `./secrets/private.key` | Host path of the private key that Compose mounts into the container. |
| `OPENAI_API_KEY` | (required for `openai` mode) | OpenAI API key. |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | Realtime model. Must be available to your API key. |
| `OPENAI_REALTIME_VOICE` | OpenAI default | Optional voice name, for example `marin`. |
| `AGENT_MODE` | `openai` | `echo` is a no-OpenAI diagnostic: the agent beeps, then echoes your mic. |
| `MAX_ACTIVE_SESSIONS` | `1` | Concurrent sessions (1–5). Each runs its own agent process. |
| `MAX_SESSION_SECONDS` | `600` | Hard limit per session. |
| `IDLE_TIMEOUT_SECONDS` | `120` | Ends a session when neither side has spoken for this long. |
| `READY_TIMEOUT_SECONDS` | `60` | Ends a session if the browser never becomes ready to play the agent. |
| `SESSION_CREATES_PER_MINUTE` | `6` | Rate limit on starting sessions. |
| `LOG_LEVEL` | `INFO` | `DEBUG` also enables the native connector's own, unredacted logs. |
| `LOG_TRANSCRIPTS` | `false` | Log what both sides say. Also turns on OpenAI input transcription (billed). |
| `PORT` | `8000` | Port on `127.0.0.1` where the page is served. |

To see which Realtime models your key can use:

```bash
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" \
  | grep -o '"id": *"[^"]*realtime[^"]*"'
```

## How to verify it works

A working call goes through these states on the page; each comes from a real SDK
or server event:

1. *Asking for microphone access…* → *Starting a session…* → *Connecting to Vonage Video…*
2. *Connected. Waiting for the AI agent to join…*
3. *The AI agent joined. Connecting to its audio…* (the agent's stream arrived)
4. *Starting the conversation…* → the agent greets you.
5. While talking: *The AI agent is speaking*, *Listening…*, or *Your turn*. These are
   driven by the audio levels of the two streams. The **Can hear you** badge appears
   once the agent's subscription to your microphone is connected.

The server logs follow along (lines from the agent process are labeled `agent <id>`):

```text
server         | Session …: created (Vonage session …)
server         | Started agent process …
agent …        | Agent joined the Vonage Video session
agent …        | Stream … received from a 'user' participant
agent …        | Agent is receiving the user's audio
server         | Session …: browser ready, conversation started
agent …        | Browser is ready: unpausing microphone audio and requesting a greeting
```

Smoke test:

- [ ] You hear one greeting, once, without speaking first.
- [ ] The agent answers a question, and stops when you talk over it.
- [ ] **Mute** stops it from hearing you; **Unmute** restores it.
- [ ] **Leave** returns you to the start screen, and the logs show `ended (left)`.
- [ ] Closing the tab mid-call also ends the session within a few seconds.

To check the Vonage audio path without OpenAI, set `AGENT_MODE=echo` and restart:
you should hear a beep, then your own voice. [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md)
has the full checklist.

To run the automated tests (Linux container, no credentials needed):

```bash
docker build --target test -t vonage-pipecat-openai-realtime:test .
```

## Troubleshooting

| Symptom | What to check |
|---|---|
| Page says **The server is not configured yet** | Fix each listed item in `.env` (or `secrets/`), then restart `docker compose up`. |
| `Cannot read the Vonage private key … permission denied` | Run `chmod 644 secrets/private.key` (keep `chmod 700 secrets`). The container runs as an unprivileged user. |
| **Vonage rejected the credentials (401)** | `VONAGE_APPLICATION_ID` and `private.key` must come from the same application. A regenerated key replaces the old one. |
| **Vonage refused the request (403)** | Turn on Video: `vonage apps capabilities update <APPLICATION_ID> video > /dev/null`. |
| `VONAGE_APPLICATION_ID looks like a legacy OpenTok project API key` | This sample needs a Vonage application ID (a UUID), not an OpenTok API key and secret. |
| Browser: **Could not connect to Vonage Video (1004)** | An authentication error: the token was rejected. Check that the app ID and key match, and that your computer's clock (and Docker Desktop's, after sleep) is correct, since tokens are time-limited. |
| Session errors after adapting this to your own session | This sample always creates routed sessions. If you reuse an existing session, create it with media mode `routed`. |
| `ModuleNotFoundError: vonage_video_connector` | You ran the agent outside Docker on macOS or Windows. The connector ships Linux wheels only; use `docker compose up`. |
| **Microphone access is blocked** | Allow the microphone in the browser's site settings. Use `http://localhost:8000`, not a LAN IP (that needs HTTPS). |
| **Your browser blocked audio playback** | Click **Enable audio**. Browsers block autoplay until you interact with the page. |
| Stuck on *Waiting for the AI agent to join* | Read the server logs. A failed join ends the session with the reason shown on the page; if nothing ends it, the ready timeout does after 60 s. |
| **Timed out connecting to Vonage Video and OpenAI** | The container can't reach one of them: a proxy or firewall is dropping traffic. It needs outbound HTTPS and WebRTC media (UDP, with TCP fallback). |
| Agent fails with `insufficient_quota` or `invalid_api_key` | Check your OpenAI API billing and key. ChatGPT plans don't include API usage. |
| Agent fails with `model_not_found` | Set `OPENAI_REALTIME_MODEL` to a Realtime model from the `curl` command in [Configuration](#configuration). |
| You hear yourself, or the agent keeps interrupting itself | Use headphones; the agent hears your speakers otherwise. Browser echo cancellation is on by default. |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Set `PORT=8001` in `.env` and open `http://localhost:8001`. |
| Build fails on an ARM machine | Wheels exist for Linux arm64, so a native build should work; update Docker Desktop first. Forcing `linux/amd64` runs under emulation and is slow. |
| **A demo session is already running** | Leave the other tab's call, or wait for it to time out. `MAX_ACTIVE_SESSIONS` sets the limit. |

`AGENT_MODE=echo` separates Vonage problems from OpenAI problems: if you hear the beep
and your echo, Vonage works end to end.

## Limitations and costs

- **A local demo, not a production service.** There are no user accounts; anyone who
  can reach the port can start a (billed) session. Compose binds it to `127.0.0.1`
  for that reason. Session keys protect each session, and limits cap usage, but
  that isn't production authentication.
- **You pay for usage.** Vonage Video bills per participant-minute; both you and the
  agent are connected for the whole call (see
  [Vonage's participant pricing](https://api.support.vonage.com/hc/en-us/articles/6646227378204-How-does-participant-based-video-pricing-work)).
  OpenAI bills Realtime audio tokens for both directions; `LOG_TRANSCRIPTS=true` adds
  transcription. The defaults cap a session at 10 minutes, 2 minutes of silence, and
  one session at a time.
- **Nothing is recorded.** The app stores no audio, video, or transcripts. Logs omit
  what you say unless you set `LOG_TRANSCRIPTS=true`.
- **The agent only listens.** It doesn't see your camera, and it has no avatar.
- **One room per session.** Each session holds your browser and one agent; other
  streams are ignored by the page.
- Tested platforms: see [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md).

## Cleanup

Stop the app and remove its image, then delete the Vonage application if you no
longer need it:

```bash
docker compose down --rmi local
vonage apps delete <APPLICATION_ID>
```

## Project layout

```text
app/main.py            FastAPI routes, static files, startup checks
app/config.py          settings from .env, validation, log redaction
app/video_sessions.py  Vonage routed session + per-participant tokens
app/session_manager.py session registry, supervisor, limits and timeouts
app/agent_process.py   one process per agent, and the pipe protocol between them
app/agent.py           the Pipecat pipeline: Vonage transport + OpenAI Realtime
web/                   index.html, app.js, styles.css (no build step)
tests/                 unit and integration tests (no network needed)
docs/                  dependency notes, manual test plan, blog notes
```

## Related reading (optional)

- Blog post: *Build a Real-Time OpenAI Voice Agent With Vonage Video and Pipecat*
  (link coming when it's published)

- [AI Video Agents With Vonage, Pipecat and AgentCore](https://developer.vonage.com/en/blog/ai-video-agents-with-vonage-pipecat-and-agentcore)
  by Kitt Phi: the same transport with AWS Nova Sonic, deployed on AWS AgentCore.
  This repository is the OpenAI Realtime, Docker-local alternative.
- [Introducing Video Connector SDK & Pipecat Transport for AI Video Apps](https://developer.vonage.com/en/blog/new-video-connector-sdk-pipecat-transport-for-ai-video-apps)
- [Vonage Video Connector guide](https://developer.vonage.com/en/video/guides/vonage-video-connector)
  and [Vonage Video getting started](https://developer.vonage.com/en/video/getting-started)
- [Vonage Video Transport for Pipecat](https://developer.vonage.com/en/video/guides/vonage-video-connector-pipecat-transport)
  and the [Pipecat documentation](https://docs.pipecat.ai)

## License, privacy and attributions

[MIT](LICENSE). Pipecat is BSD-2-Clause, © Daily. The Vonage Video Web SDK and Video
Connector are used under [Vonage's terms](https://www.vonage.com/legal/communications-apis/terms-of-use/).

**Privacy:** during a call your microphone audio travels from your browser through
Vonage Video to the agent container and on to OpenAI. OpenAI's API data policies apply
to what it receives. This app keeps nothing after the call ends.

**AI disclosure:** the agent is an AI model. It can be wrong, and the page says so.
