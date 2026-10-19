// Browser side of the demo.
//
// 1. Get microphone (and optional camera) access with the Vonage Video Web SDK.
// 2. Ask our server for a session. It returns the browser's token only.
// 3. Join the routed Vonage Video session and publish.
// 4. Subscribe to the AI agent's stream, recognised by the role in its
//    connection data, and to nothing else.
// 5. Once its audio can play, tell the server so the agent greets you once.
'use strict';

const $ = (id) => document.getElementById(id);
const ui = {
  setup: $('setup'),
  start: $('start'),
  useCamera: $('use-camera'),
  call: $('call'),
  publisher: $('publisher'),
  youPlaceholder: $('you-placeholder'),
  youLevel: $('you-level'),
  mutedBadge: $('muted-badge'),
  agentMedia: $('agent-media'),
  agentLevel: $('agent-level'),
  agentHearing: $('agent-hearing'),
  enableAudio: $('enable-audio'),
  mute: $('mute'),
  camera: $('camera'),
  leave: $('leave'),
  timeLeft: $('time-left'),
  status: $('status'),
  error: $('error'),
};

const END_MESSAGES = {
  left: 'You left the conversation.',
  user_stream_left: 'Your stream left the Vonage session, so the agent stopped.',
  max_duration: 'The session reached its maximum length.',
  idle_timeout: 'The session ended after a long silence.',
  ready_timeout: 'The session ended because the agent and browser never connected.',
  server_shutdown: 'The server shut down.',
  agent_exited: 'The AI agent left the call.',
  error: 'The AI agent stopped because of an error.',
};

let call = null; // the conversation in progress, if any

// --- Small helpers ---------------------------------------------------------------

function setStatus(text) {
  ui.status.textContent = text;
  console.info('[demo]', text);
}

function showError(message, problems = []) {
  ui.error.replaceChildren(document.createTextNode(message));
  if (problems.length) {
    const list = document.createElement('ul');
    for (const problem of problems) {
      const item = document.createElement('li');
      item.textContent = problem;
      list.append(item);
    }
    ui.error.append(list);
  }
  ui.error.hidden = false;
}

function clearError() {
  ui.error.hidden = true;
  ui.error.replaceChildren();
}

class ApiError extends Error {
  constructor(status, message, problems = []) {
    super(message);
    this.status = status;
    this.problems = problems;
  }
}

async function api(path, { method = 'GET', key, body, keepalive = false } = {}) {
  const headers = {};
  if (key) headers.Authorization = `Bearer ${key}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      keepalive,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the demo server. Is `docker compose up` still running?");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message =
      typeof detail === 'string' ? detail : detail?.message || `Request failed (HTTP ${response.status})`;
    throw new ApiError(response.status, message, detail?.problems || []);
  }
  return data;
}

// Our server puts {"role": "user"} or {"role": "agent"} in each token's connection data.
function roleOf(connection) {
  try {
    return JSON.parse(connection.data || '{}').role ?? null;
  } catch {
    return null;
  }
}

// Turns the SDK's raw audio levels into a meter value and a "speaking" flag.
function levelTracker(meter) {
  let average = 0;
  let lastLoud = 0;
  return {
    update(level) {
      average = level > average ? level : 0.7 * average + 0.3 * level;
      const scaled = Math.min(1, Math.max(0, Math.log10(average) / 1.5 + 1));
      meter.value = scaled;
      if (scaled > 0.35) lastLoud = performance.now();
    },
    get speaking() {
      return performance.now() - lastLoud < 500;
    },
    reset() {
      average = 0;
      lastLoud = 0;
      meter.value = 0;
    },
  };
}

const youLevel = levelTracker(ui.youLevel);
const agentLevel = levelTracker(ui.agentLevel);

function preflightProblem() {
  if (!window.isSecureContext) {
    return 'Browsers only allow microphone access on secure pages. Open http://localhost:8000 on this computer, or serve the app over HTTPS.';
  }
  if (typeof OT === 'undefined') {
    return "The Vonage Video Web SDK didn't load from unpkg.com. Check your network or content blocker, then reload.";
  }
  if (OT.checkSystemRequirements() !== 1) {
    return "This browser isn't supported by the Vonage Video Web SDK. Try a current Chrome, Edge, Firefox, or Safari.";
  }
  return null;
}

function mediaErrorMessage(error) {
  switch (error.name) {
    case 'OT_USER_MEDIA_ACCESS_DENIED':
      return 'Microphone access is blocked. Allow it in your browser\'s site settings (the icon in the address bar), then try again.';
    case 'OT_NO_DEVICES_FOUND':
    case 'OT_NO_VALID_CONSTRAINTS':
      return 'No microphone was found. Connect one and try again.';
    case 'OT_HARDWARE_UNAVAILABLE':
      return 'Your microphone or camera is being used by another app. Close it and try again.';
    default:
      return `Could not start your microphone: ${error.message}`;
  }
}

// --- Call flow -------------------------------------------------------------------

function createPublisher(useCamera) {
  return new Promise((resolve, reject) => {
    const publisher = OT.initPublisher(
      ui.publisher,
      {
        insertMode: 'append',
        width: '100%',
        height: '100%',
        name: 'You',
        videoSource: useCamera ? undefined : null, // null = audio only
        publishVideo: useCamera,
        style: {
          buttonDisplayMode: 'off',
          nameDisplayMode: 'off',
          audioLevelDisplayMode: 'off',
          archiveStatusDisplayMode: 'off',
        },
      },
      (error) => (error ? reject(new Error(mediaErrorMessage(error))) : resolve(publisher)),
    );
    publisher.on('accessDialogOpened', () =>
      setStatus('Allow microphone access in your browser\'s prompt…'),
    );
    publisher.on('audioLevelUpdated', (event) => youLevel.update(event.audioLevel));
    publisher.on('mediaStopped', () => {
      if (call && !call.ending) endCall('Your microphone stopped (was it unplugged?).', true);
    });
  });
}

function joinSession({ applicationId, vonageSessionId, token }) {
  const session = OT.initSession(applicationId, vonageSessionId);
  call.session = session;
  session.on({
    streamCreated: (event) => onStreamCreated(event.stream),
    streamDestroyed: (event) => {
      if (roleOf(event.stream.connection) === 'agent' && call && !call.ending) {
        setStatus('The AI agent left the call.');
      }
    },
    sessionReconnecting: () => setStatus('Network hiccup: reconnecting to Vonage Video…'),
    sessionReconnected: () => setStatus('Reconnected.'),
    sessionDisconnected: (event) => {
      if (call && !call.ending) {
        endCall(`Disconnected from Vonage Video (${event.reason}).`, event.reason !== 'clientDisconnected');
      }
    },
  });

  setStatus('Connecting to Vonage Video…');
  return new Promise((resolve, reject) => {
    session.connect(token, (connectError) => {
      if (connectError) {
        reject(new Error(`Could not connect to Vonage Video: ${connectError.message}`));
        return;
      }
      if (!call.agentSubscriber) setStatus('Connected. Waiting for the AI agent to join…');
      session.publish(call.publisher, (publishError) =>
        publishError
          ? reject(new Error(`Could not publish your microphone: ${publishError.message}`))
          : resolve(),
      );
    });
  });
}

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
        buttonDisplayMode: 'off',
        nameDisplayMode: 'off',
        audioLevelDisplayMode: 'off',
        videoDisabledDisplayMode: 'off',
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
  call.agentSubscriber = subscriber;
  subscriber.on('audioBlocked', offerAudioUnblock);
  subscriber.on('audioUnblocked', () => {
    ui.enableAudio.hidden = true;
    signalReady();
  });
  subscriber.on('audioLevelUpdated', (event) => agentLevel.update(event.audioLevel));
}

function offerAudioUnblock() {
  ui.enableAudio.hidden = false;
  setStatus('Your browser blocked audio playback. Click "Enable audio" to hear the agent.');
}

// Tell the server the agent's audio can play here. The agent greets you only after this.
async function signalReady() {
  if (!call || call.ready || call.ending) return;
  call.ready = true;
  setStatus('Starting the conversation…');
  try {
    await api(`/api/sessions/${call.id}/ready`, { method: 'POST', key: call.key });
    call.live = true;
    call.statusTimer = setInterval(renderLiveStatus, 250);
    renderLiveStatus();
  } catch (error) {
    endCall(error.message, true);
  }
}

// Status while talking, derived from the real audio levels of both streams.
function renderLiveStatus() {
  if (!call?.live || call.ending) return;
  if (call.agentMode === 'echo') {
    setStatusQuietly('Echo diagnostic: you should have heard a beep. Now speak and you will hear yourself.');
  } else if (agentLevel.speaking) {
    setStatusQuietly('The AI agent is speaking. Talk over it to interrupt.');
  } else if (call.muted) {
    setStatusQuietly("You're muted, so the agent can't hear you.");
  } else if (youLevel.speaking) {
    setStatusQuietly('Listening…');
  } else {
    setStatusQuietly('Your turn: say something.');
  }
}

function setStatusQuietly(text) {
  if (ui.status.textContent !== text) ui.status.textContent = text;
}

function startPolling() {
  const current = call;
  current.poll = setInterval(async () => {
    if (current.ending) return;
    let status;
    try {
      status = await api(`/api/sessions/${current.id}`, { key: current.key });
    } catch (error) {
      if (current.ending) return;
      if (error.status === 404) {
        endCall('The server no longer knows this session (did it restart?).', true, { serverEnded: true });
      } else if (++current.pollFailures >= 3) {
        endCall('Lost contact with the demo server.', true, { serverEnded: true });
      }
      return;
    }
    if (current.ending) return; // the user left while this request was in flight
    current.pollFailures = 0;
    ui.agentHearing.hidden = !status.agent.hearingUser;
    current.deadline = Date.now() + status.secondsRemaining * 1000;
    if (status.state === 'ended' || status.state === 'failed') {
      const message = END_MESSAGES[status.endReason] || 'The session ended.';
      const detail = status.error ? ` ${status.error}` : '';
      endCall(message + detail, status.state === 'failed', { serverEnded: true });
    }
  }, 2000);
}

function startCountdown() {
  const current = call;
  const tick = () => {
    const seconds = Math.max(0, Math.round((current.deadline - Date.now()) / 1000));
    ui.timeLeft.textContent = `Time left ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  tick();
  current.countdown = setInterval(tick, 1000);
}

function showCallUi() {
  ui.setup.hidden = true;
  ui.call.hidden = false;
  ui.publisher.hidden = !call.useCamera;
  ui.youPlaceholder.hidden = call.useCamera;
  ui.camera.hidden = !call.useCamera;
  ui.camera.textContent = 'Turn camera off';
  ui.mute.textContent = 'Mute';
  ui.mute.setAttribute('aria-pressed', 'false');
  ui.mutedBadge.hidden = true;
  ui.agentHearing.hidden = true;
}

async function start() {
  clearError();
  const problem = preflightProblem();
  if (problem) {
    showError(problem);
    return;
  }
  ui.start.disabled = true;
  call = {
    useCamera: ui.useCamera.checked,
    muted: false,
    ready: false,
    live: false,
    ending: false,
    pollFailures: 0,
  };
  youLevel.reset();
  agentLevel.reset();

  try {
    setStatus('Asking for microphone access…');
    call.publisher = await createPublisher(call.useCamera);

    setStatus('Starting a session…');
    const created = await api('/api/sessions', { method: 'POST', body: {} });
    Object.assign(call, { id: created.id, key: created.sessionKey, agentMode: created.agentMode });
    call.deadline = Date.now() + created.maxSessionSeconds * 1000;

    showCallUi();
    startPolling();
    startCountdown();
    await joinSession(created);
  } catch (error) {
    await endCall(error.message, true, { problems: error.problems || [] });
  }
}

async function endCall(message, isError = false, { serverEnded = false, problems = [] } = {}) {
  if (!call || call.ending) return;
  const ending = call;
  ending.ending = true;
  clearInterval(ending.poll);
  clearInterval(ending.countdown);
  clearInterval(ending.statusTimer);

  // Leave the Vonage session right away; stopping the agent can take a few seconds.
  try {
    ending.session?.disconnect();
  } catch {}
  try {
    ending.publisher?.destroy();
  } catch {}
  if (ending.id && !serverEnded) {
    setStatus('Ending the session…');
    try {
      await api(`/api/sessions/${ending.id}/end`, { method: 'POST', key: ending.key });
    } catch (error) {
      console.warn('[demo] Could not end the session cleanly:', error.message);
    }
  }

  call = null;
  ui.call.hidden = true;
  ui.setup.hidden = false;
  ui.start.disabled = false;
  ui.enableAudio.hidden = true;
  ui.timeLeft.textContent = '';
  setStatus(message);
  if (isError) showError(message, problems);
}

// --- Controls --------------------------------------------------------------------

ui.start.addEventListener('click', start);

ui.leave.addEventListener('click', () => endCall('You left the conversation.'));

ui.mute.addEventListener('click', () => {
  if (!call?.publisher) return;
  call.muted = !call.muted;
  call.publisher.publishAudio(!call.muted);
  ui.mute.textContent = call.muted ? 'Unmute' : 'Mute';
  ui.mute.setAttribute('aria-pressed', String(call.muted));
  ui.mutedBadge.hidden = !call.muted;
  renderLiveStatus();
});

ui.camera.addEventListener('click', () => {
  if (!call?.publisher) return;
  const on = ui.camera.textContent === 'Turn camera on';
  call.publisher.publishVideo(on);
  ui.camera.textContent = on ? 'Turn camera off' : 'Turn camera on';
});

ui.enableAudio.addEventListener('click', async () => {
  try {
    await OT.unblockAudio(); // must run inside a click handler
  } catch (error) {
    showError(`Could not enable audio: ${error.message}`);
  }
});

// Best effort: stop the agent if the tab closes. No navigation, no prompts.
window.addEventListener('pagehide', () => {
  if (call?.id && !call.ending) {
    api(`/api/sessions/${call.id}/end`, { method: 'POST', key: call.key, keepalive: true }).catch(
      () => {},
    );
  }
});
