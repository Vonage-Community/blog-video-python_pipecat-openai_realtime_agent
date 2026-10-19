# Blog 1 notes (for the author, not the published post)

**Working title:** Build a Real-Time OpenAI Voice Agent with Vonage Video and Pipecat

**Status of the sample these notes describe:** builds and passes its automated tests
in Linux Docker. **The live Vonage + OpenAI call has not been verified yet**; run
`docs/MANUAL_TEST.md` first, then fill in the latency and screenshot items marked TODO.

## Goal of the post

Show a developer how to add a real-time OpenAI voice agent to a Vonage Video session
as a genuine participant. The browser publishes its microphone with the Vonage Video
Web SDK; a Pipecat agent joins the same routed session through the Vonage Video
Connector and answers with OpenAI Realtime speech-to-speech; the reply reaches the
browser through its Vonage subscription. Everything runs locally with
`docker compose up`. The post links to this repository and nothing later in the series.

## Intro paragraph (draft)

> Kitt Phi's [AI Video Agents With Vonage, Pipecat and AgentCore](https://developer.vonage.com/en/blog/ai-video-agents-with-vonage-pipecat-and-agentcore)
> showed how a Pipecat agent can join a Vonage Video call through the new Video
> Connector, with Amazon Nova Sonic for speech and AWS AgentCore for hosting. This
> tutorial takes the same transport down a different road: OpenAI Realtime as a single
> speech-to-speech model, and Docker on your own machine instead of a cloud runtime.
> You'll run one command, open `localhost`, and talk to an AI that hears you and
> answers through the same Vonage session as any other participant. The native
> connector runs on Linux, so the agent lives in a container; macOS and Windows work
> through Docker Desktop.

## Diagram

Reuse the diagram in the README's "Architecture" section. One sentence to go with it:
*the browser never talks to OpenAI; the agent is just another participant in a routed
Vonage session.*

## Code sections that teach the important steps

### 1. One session, two tokens, and a role in each

`app/video_sessions.py` signs a token per participant. The `connection_data` is how
each side recognizes the other later:

```python
    def create_token(self, session_id: str, role: ParticipantRole, ttl_seconds: int) -> str:
        """Sign a token for one participant. Signing is local; no network call."""
        token = self._client.video.generate_client_token(
            TokenOptions(
                session_id=session_id,
                role=TokenRole.PUBLISHER,
                connection_data=json.dumps({"role": role}),
                # Always pass exp explicitly: TokenOptions(exp=None) fails validation.
                exp=int(time.time()) + ttl_seconds,
            )
        )
        return token.decode()
```

And in `app/session_manager.py`, the browser token goes back to the page while the
agent token goes only to the agent process:

```python
            browser_token = self._video.create_token(vonage_session_id, "user", ttl)
            agent_token = self._video.create_token(vonage_session_id, "agent", ttl)
```

Talking point: the session is created **routed** (`SessionOptions(media_mode=MediaMode.ROUTED)`),
which is how Vonage's own Pipecat agent tutorials run.

### 2. The agent: Vonage transport in, OpenAI Realtime, Vonage transport out

From `app/agent.py` (trimmed for the post; keep the comments):

```python
        transport = VonageVideoConnectorTransport(
            self._connection.application_id,
            self._connection.session_id,
            self._connection.token,
            VonageVideoConnectorTransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                publisher_name="AI agent",
                audio_in_auto_subscribe=True,
            ),
        )
```

```python
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
                        ),
                    ),
                ),
            ),
            # Send no microphone audio to OpenAI until the browser can hear the agent.
            start_audio_paused=True,
        )

        context = LLMContext([{"role": "developer", "content": GREETING_PROMPT}])
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

        pipeline = Pipeline(
            [
                transport.input(),
                user_aggregator,
                self._llm,
                transport.output(),
                assistant_aggregator,
            ]
        )
```

Talking points:
- Pipecat at the pinned Vonage fork uses `PipelineWorker`, `WorkerRunner` and
  `LLMContext`. Older posts show `PipelineTask` and `OpenAILLMContext`.
- No local VAD: OpenAI's `semantic_vad` decides turns. When you talk over the agent,
  the aggregator broadcasts an interruption, and the Vonage output transport clears
  its buffered audio (`clear_buffers_on_interruption` defaults to `True`).

### 3. Subscribe to the agent, and only the agent

From `web/app.js`:

```js
function onStreamCreated(stream) {
  const role = roleOf(stream.connection);
  if (role !== 'agent') {
    console.info(`[demo] Ignoring a stream from a "${role}" participant`);
    return;
  }
  if (!call || call.agentSubscriber) return;

  setStatus('The AI agent joined. Connecting to its audio…');
  const subscriber = call.session.subscribe(
    stream,
    ui.agentMedia,
    {
      insertMode: 'append',
      width: 1,
      height: 1,
      subscribeToVideo: false, // the agent publishes audio only
      style: {
        audioBlockedDisplayMode: 'off', // we show our own "enable audio" button
        // …
      },
    },
    (error) => {
      if (error) {
        endCall(`Could not subscribe to the AI agent: ${error.message}`, true);
      } else if (subscriber.isAudioBlocked()) {
        offerAudioUnblock();
      } else {
        signalReady();
      }
    },
  );
```

### 4. Greet exactly once, when the user can hear it

The browser calls `POST /api/sessions/{id}/ready` once its subscription is connected
and audio isn't blocked. The server forwards that to the agent once; the agent
unpauses the microphone and asks for a response:

```python
    async def begin_conversation(self, worker: PipelineWorker) -> None:
        logger.info("Browser is ready: unpausing microphone audio and requesting a greeting")
        self._llm.set_audio_input_paused(False)
        # LLMRunFrame sends the context (the greeting prompt) and asks for a response.
        await worker.queue_frames([LLMRunFrame()])
```

Talking point: `on_first_participant_joined` fires when the agent sees your stream,
not when you can hear the agent. Waiting for the browser avoids greeting into the
void, and Vonage autoplay rules make the browser the only one who knows.

### 5. (Optional, short) One process per agent, and always able to stop

`app/agent_process.py` runs each agent as `python -m app.agent_process`. The Vonage
Video Connector supports one connector instance per process. A separate process also
means Leave, timeouts, and server shutdown can always end the agent: a stop command,
then SIGTERM, then SIGKILL. Exiting the process closes both the Vonage connection and
the OpenAI WebSocket, so nothing keeps billing.

## Exact demo flow (for the post and the video)

```bash
cp .env.example .env            # set VONAGE_APPLICATION_ID and OPENAI_API_KEY
cp ~/Downloads/private.key secrets/private.key
chmod 700 secrets && chmod 644 secrets/private.key
docker compose up --build
```

Open `http://localhost:8000` → **Start conversation** → allow the microphone → the agent
greets you → ask a question → interrupt it mid-answer → **Mute** / **Unmute** →
**Leave**.

## Screenshot and video beats

1. The Vonage dashboard's application page with **Video** enabled (blur the
   Application ID).
2. `.env.example` next to the README's quick start. Never show a filled-in `.env`.
3. Terminal: `docker compose up --build` reaching `Ready: agent=OpenAI Realtime model …`.
4. Browser: start screen with the AI disclosure.
5. Browser mid-call: the audio-only agent tile with its level meter and the
   **Can hear you** badge; the status line reading *The AI agent is speaking…*.
6. Terminal: the log sequence from joining to `browser ready, conversation started`.
7. Video only: an interruption: talk over a long answer and show it stop.
8. Optional: `chrome://webrtc-internals` with the inbound audio stream, as proof that
   the voice arrives over Vonage WebRTC.
9. **Leave**, then the logs showing `ended (left)` and the agent process exiting.

TODO after the live test: measured time from **Start conversation** to greeting, and
typical response latency, on the author's machine. Don't quote numbers that weren't
measured.

## Limitations to state in the post

- Local demo: no user accounts; the port is bound to `localhost`. Sessions are capped
  (one at a time, 10 minutes, 2 minutes of silence) to protect the bill.
- Costs: Vonage Video participant-minutes plus OpenAI Realtime audio tokens.
- The agent only listens and speaks: no vision, no avatar.
- Docker is required for the agent (Linux-only native connector).
- Not production authentication; not a benchmark of models.

## Next in the series (one line, optional)

Part 2 swaps the speech-to-speech model for a modular Deepgram → OpenAI → ElevenLabs
pipeline, for when you need separate control of listening, reasoning, and voice. Link
it only after it's published.
