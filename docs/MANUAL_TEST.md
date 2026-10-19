# Manual real-media test

Automated tests cover configuration, the session lifecycle, the API, the agent
process protocol, and the Pipecat pipeline's construction. They can't prove that
audio flows through WebRTC. This checklist does, and it needs real credentials.
**Run all of it before tagging a release.**

Record each run in the table at the end.

## Before you start

- Vonage application with Video enabled, its Application ID, and `secrets/private.key`.
- OpenAI API key with Realtime access and billing set up.
- Headphones, a microphone, and an up-to-date Chrome (then repeat the core steps in
  Safari and Firefox).
- Watch the logs in a second terminal: `docker compose logs -f app`.

## Gate 1: clean build

```bash
docker compose down --rmi local
docker compose build --no-cache
docker compose up
```

- [ ] The build finishes without errors on your architecture. Note whether it's
      `x86_64` or `arm64` (`uname -m`).
- [ ] The log shows `Ready: agent=OpenAI Realtime model …`.
- [ ] `curl -s localhost:8000/api/health` returns `{"status":"ok"}`.

## Gate 2: Vonage audio both ways, without OpenAI

Set `AGENT_MODE=echo` in `.env`, then `docker compose up` again.

1. Open `http://localhost:8000`, keep the camera off, click **Start conversation**,
   and allow the microphone.
2. - [ ] The status moves through *Connecting to Vonage Video…*, *Waiting for the AI
         agent…*, *The AI agent joined…*.
   - [ ] **You hear a short beep.** This is audio the agent published into Vonage,
         played by your Vonage subscription.
   - [ ] Speak: **you hear your own voice back** after a short delay. This is your
         published audio reaching the agent and returning through Vonage.
   - [ ] The **Can hear you** badge appears.
   - [ ] Logs include `Agent joined the Vonage Video session`, `Stream … received
         from a 'user' participant`, `Agent is receiving the user's audio`,
         `browser ready, conversation started`.
3. Proof that audio comes through Vonage and not some other path: open
   `chrome://webrtc-internals` during the call.
   - [ ] There is an inbound audio RTP stream whose `bytesReceived` keeps growing.
   - [ ] In DevTools > Network, the page made no request to `api.openai.com`.
4. Click **Leave**.
   - [ ] The page returns to the start screen with *You left the conversation.*
   - [ ] Logs show `Session …: ended (left)` and `Agent process … exited`.

## Gate 3: OpenAI Realtime

Set `AGENT_MODE=openai` and restart.

- [ ] After joining, the agent **greets you once**, without you speaking first.
- [ ] Ask a question: it answers out loud within a second or two.
- [ ] Talk over a long answer: **it stops** and listens (interruption).
- [ ] **Mute**: the badge shows *Muted*, and the agent doesn't react to you speaking.
      **Unmute**: it hears you again.
- [ ] The status alternates between *The AI agent is speaking* and *Listening…* / *Your turn*.
- [ ] **Leave**: logs show `ended (left)` and the agent process exits.
- [ ] Note the greeting latency and any audio glitches below.
- [ ] Optional: with `LOG_TRANSCRIPTS=true`, logs show `Transcript user:` and
      `Transcript agent:` lines. Leave it off afterwards.

## Gate 4: lifecycle and errors

- [ ] **Close the tab** mid-call: within a few seconds the logs show the session ending
      (`left` or `user_stream_left`).
- [ ] **Second tab** while a call runs: it shows *A demo session is already running*.
- [ ] **Idle:** stay silent with `IDLE_TIMEOUT_SECONDS=30`: the page reports the
      session ended after a long silence.
- [ ] **Max length:** with `MAX_SESSION_SECONDS=60`, the call ends at one minute and
      the countdown reaches 0:00.
- [ ] **Bad OpenAI key** (`OPENAI_API_KEY=sk-invalid`): the page shows an error from
      the agent instead of silence, and the session ends.
- [ ] **Unknown model** (`OPENAI_REALTIME_MODEL=gpt-nonexistent`): same as above.
- [ ] **Microphone denied** (block it in site settings): the page explains how to allow it.
- [ ] **Autoplay blocked** (Safari: Settings for this website > Auto-Play > Never Auto-Play):
      *Enable audio* appears; clicking it plays the greeting.
- [ ] **Server stop mid-call** (`docker compose stop`): the logs show the session
      ending (`server_shutdown`), and within about 6 seconds the page reports
      *Lost contact with the demo server.*
- [ ] **Camera on:** start with the checkbox ticked; your preview shows; **Turn
      camera off/on** works; the agent behaves the same.

## Gate 5: costs

- [ ] In the OpenAI usage dashboard, Realtime usage matches the minutes you tested.
- [ ] In the Vonage dashboard, Video usage shows two participants for the test sessions.
- [ ] After all tabs are closed, no session is running (`docker compose logs` shows
      every session `ended`/`failed`), and there are no lingering agent processes
      (`docker compose top` lists only `tini` and `uvicorn`).

## Results

| Date | Tester | Host OS / arch | Browser | Gates passed | Notes (latency, glitches) |
|---|---|---|---|---|---|
| | | | | | |

Tag the release (`git tag -a v1.0.0-tutorial -m "Tutorial version"`) only after
Gates 1 to 5 pass on at least one machine and the core of Gates 2 and 3 pass in a
second browser.
