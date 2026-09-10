# Adding a New Provider

This document is Part 3 of the Builder's Guide. It assumes you have
completed Part 1 and understand the three-phase call lifecycle described
in §3. Before you write a line of provider code, make sure you can place
a call with Echo and see clean `bot.start` / `bot.ended` in the journal.
The foundation has to be solid before the provider layer goes on top.

Adding a provider means writing a Python plugin that speaks two protocols
simultaneously: RCMS on the Infinity side (already handled by the bridge
framework) and your provider's real-time API on the other. The bridge
calls your plugin at each phase transition. Your plugin translates.

This is not a beginner task. The four providers already in this repo took
significant iteration to get right. Read this document before writing
code — the architectural considerations in §8 describe failure modes that
are expensive to discover in production.

---

## §1 The plugin contract

Your plugin is a subclass of `bridge_server.ServicePlugin`. The bridge
framework calls five methods on backend plugins:

| Method | When called | What it must do |
|---|---|---|
| `__init__(self, server)` | Startup | Store `server`, initialize `_conversations: Dict[str, BotConversation]` keyed by `f"{session_id}:{endpoint_id}"` |
| `handle_message(self, websocket, client_id, data)` | Every RCMS message | Route by `data["type"]`: handle `bot.start` and `bot.end` |
| `ingest_audio_chunk(self, session_id, endpoint_id, source, audio_bytes) -> bool` | Every inbound media frame | Transcode and forward to your provider; return `True` if handled |
| `on_session_ended(self, session_id)` | Infinity `session.end` | Drop all conversations for this session, close sockets, cancel tasks |
| `shutdown(self)` | Bridge shutdown | Same as `on_session_ended` but for every active conversation |

The `message_types` property should return `set()` for a backend plugin.
The dispatcher owns top-level message routing and calls your plugin
directly — you do not register individual message types.

### botId prefix convention

Every provider uses a prefix-based `botId`:

```
<provider_name>:<identifier>
```

The identifier is whatever your provider needs to route the call — an
agent ID, a model name, or any other per-call selector. Pick a lowercase
ASCII prefix. The dispatcher lowercases the `botId` before comparing.
The prefix is also the log and context key — use it consistently across
log lines, `bot.ended` context, and disconnect logging.

Examples from the existing providers:

| Prefix | Identifier | Example |
|---|---|---|
| `elevenlabs:` | Agent ID | `elevenlabs:agent_7a9c...` |
| `gemini:` | Model name | `gemini:gemini-3.1-flash-live-preview` |
| `openai:` | Model name | `openai:gpt-realtime-2.1` |
| `xai:` | Model name | `xai:grok-voice-think-fast-2.0` |

---

## §2 Minimal skeleton

Start from `providers/echo/bot_echo.py` for the plugin shape and from
`providers/gemini/bot_gemini.py` for the provider-streaming pattern.
A minimal stub for a new provider `foobar`:

```python
"""Foobar Live plugin — bridges Infinity RCMS to Foobar's streaming API."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import websockets
from websockets.server import WebSocketServerProtocol

from bridge_server import ServicePlugin

logger = logging.getLogger(__name__)

FOOBAR_WS_URL = "wss://api.foobar.example/v1/stream"
FOOBAR_INPUT_RATE = 16000


@dataclass
class BotConversation:
    session_id: str
    endpoint_id: str
    source: str
    websocket: WebSocketServerProtocol
    client_id: str
    service: str
    codec_name: str
    sample_rate: int
    transport_encoding: str
    provider_ws: Optional[Any] = None
    provider_recv_task: Optional[asyncio.Task] = None
    active: bool = False
    ratecv_in_state: Any = None
    ratecv_out_state: Any = None
    ingress_ready: bool = False
    ingress_buffer: list = field(default_factory=list)


class FoobarService(ServicePlugin):
    name = "foobar"

    def __init__(self, server):
        super().__init__(server)
        self._conversations: Dict[str, BotConversation] = {}

    @property
    def message_types(self) -> set[str]:
        return set()

    def _key(self, session_id, endpoint_id):
        return f"{session_id}:{endpoint_id}"

    async def handle_message(self, websocket, client_id, data):
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_bot_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_bot_end(websocket, client_id, data)

    async def _handle_bot_start(self, websocket, client_id, data):
        # 1. Parse payload, validate botId prefix
        # 2. Resolve API key from env or botCredentials
        # 3. Read codec_name and sample_rate from server.session_config
        # 4. Construct BotConversation, connect to provider WSS
        # 5. Start a recv task that forwards provider audio → IngressStreamer
        # 6. Send bot.started back to Infinity
        # See providers/gemini/bot_gemini.py for a worked example
        ...

    async def _handle_bot_end(self, websocket, client_id, data):
        # Pop convo, shutdown, send bot.ended
        ...

    async def ingest_audio_chunk(
        self, session_id, endpoint_id, source, audio_bytes
    ):
        convo = self._conversations.get(self._key(session_id, endpoint_id))
        if not convo or not convo.active or convo.source != source:
            return False
        if not convo.ingress_ready:
            convo.ingress_ready = True
            for buffered in convo.ingress_buffer:
                await self._send_ingress_chunked(convo, buffered)
            convo.ingress_buffer.clear()
        pcm = self._prepare_input_audio(convo, audio_bytes)
        if not pcm:
            return False
        await convo.provider_ws.send(json.dumps({
            "audio": base64.b64encode(pcm).decode("ascii"),
        }))
        return True

    async def on_session_ended(self, session_id):
        for key in [k for k in self._conversations
                    if k.startswith(f"{session_id}:")]:
            convo = self._conversations.pop(key, None)
            if convo:
                await self._shutdown_conversation(convo)

    async def shutdown(self):
        for key in list(self._conversations.keys()):
            convo = self._conversations.pop(key, None)
            if convo:
                await self._shutdown_conversation(convo)


def register(server):
    plugin = FoobarService(server)
    server.register_service(plugin)
    return plugin
```

---

## §3 Phase 1 — Connecting your provider

### Reading bot.start

`bot.start` carries everything you need to route the call and connect
to your provider:

```python
payload    = data.get("payload", {})
bot_id     = payload.get("botId", "")
endpoint_id = payload.get("endpointId", "")
context    = payload.get("context") or {}
language   = payload.get("language", "en-US")
```

Extract the provider-specific identifier from the `botId` suffix:

```python
identifier = bot_id[len("foobar:"):].strip()
```

### Resolving credentials

Read from env first, fall back to `botCredentials` for multi-tenant
deployments:

```python
api_key = os.environ.get("FOOBAR_API_KEY", "").strip()
if not api_key and payload.get("botCredentials"):
    import base64, json
    creds = json.loads(
        base64.b64decode(payload["botCredentials"]).decode()
    )
    api_key = creds.get("apiKey", "")
```

If no API key is available, emit `bot.ended` with `status.code: 503`
(`BACKEND_START_FAILED`) and return. Do not attempt to connect.

### Reading the negotiated codec

**Never assume PCMU 8kHz.** Infinity may negotiate any of L16, PCMU,
PCMA, or G722. Read from `session_config`:

```python
codec_name = self.server.session_config[session_id].get(
    "codec_name", "L16"
).upper()
sample_rate = self.server.session_config[session_id].get(
    "sample_rate", 8000
)
transport = self.server.transport_encodings.get(session_id, "base64")
```

### Sending bot.started

Once your provider connection is established and your recv task is
running, send `bot.started` to Infinity:

```python
response = {
    "version": "1.0.0",
    "type": "bot.started",
    "sessionId": session_id,
    "sequenceNum": self.server.get_next_sequence(client_id),
    "timestamp": datetime.now(UTC).isoformat(),
    "payload": {"endpointId": endpoint_id},
}
await websocket.send(json.dumps(response))
```

---

## §4 Phase 2 — Audio

### Ingest path — Infinity → your provider

Convert the Infinity codec to whatever PCM rate your provider requires.
Using `audioop`:

```python
if codec_name == "PCMU":
    pcm8 = audioop.ulaw2lin(audio_bytes, 2)
    pcm, convo.ratecv_in_state = audioop.ratecv(
        pcm8, 2, 1, 8000, TARGET_RATE, convo.ratecv_in_state
    )
    return pcm
elif codec_name == "PCMA":
    pcm8 = audioop.alaw2lin(audio_bytes, 2)
    pcm, convo.ratecv_in_state = audioop.ratecv(
        pcm8, 2, 1, 8000, TARGET_RATE, convo.ratecv_in_state
    )
    return pcm
elif codec_name == "L16":
    # No codec decode needed — already PCM
    pcm, convo.ratecv_in_state = audioop.ratecv(
        audio_bytes, 2, 1, sample_rate, TARGET_RATE,
        convo.ratecv_in_state
    )
    return pcm
```

G722 requires the optional `g722` package. Check `G722_AVAILABLE`
from `bridge_server` before attempting decode.

**Keep the `ratecv` state on `BotConversation`.** `audioop.ratecv`
returns an updated state tuple on every call. Drop it and you will
hear audible discontinuities at chunk boundaries.

### Egress path — your provider → Infinity

Transcode provider PCM to Infinity's negotiated codec, then deliver
via `IngressStreamer`:

```python
self.server.ingress_streamer.queue_audio(
    session_id, endpoint_id, audio_bytes,
    transport=convo.transport_encoding
)
```

**Do not use `send_immediate`** unless you are building an echo
provider. `send_immediate` is an unpaced pass-through — it delivers
audio to Infinity at synthesis speed, which is typically many times
faster than real-time. This over-buffers Infinity's playback queue
and breaks barge-in coherence. `queue_audio` paces delivery at
real-time cadence via the IngressStreamer.

If your provider emits audio in small chunks, accumulate to one
full pacer-aligned chunk before calling `queue_audio`. Read the
boundary from `self.server.ingress_streamer.chunk_duration_ms` so
your accumulator stays in lockstep with the streamer's pacing
interval. Any other boundary produces fragmented delivery.

### Ingress-readiness buffering

**This is required.** Infinity's ingress path is not open for several
hundred milliseconds after `bot.started`. If your provider sends
greeting audio before then, it is silently dropped and the caller
hears nothing.

On `bot.start`, initialize the buffer:

```python
convo.ingress_ready = False
convo.ingress_buffer = []
```

In your provider recv loop, before sending audio to IngressStreamer:

```python
if not convo.ingress_ready:
    if len(convo.ingress_buffer) >= 50:
        convo.ingress_buffer.pop(0)  # drop oldest on overflow
    convo.ingress_buffer.append(out_bytes)
    continue
await self._send_ingress_chunked(convo, out_bytes)
```

In `ingest_audio_chunk` — the first caller egress frame flips the
gate and flushes the buffer:

```python
if not convo.ingress_ready:
    convo.ingress_ready = True
    for buffered in convo.ingress_buffer:
        await self._send_ingress_chunked(convo, buffered)
    convo.ingress_buffer.clear()
```

Use the first egress frame as the signal — not a timer. This mirrors
what Infinity actually does to open ingress.

### Barge-in

When the caller speaks while the agent is speaking, your provider
will signal it. How it signals determines how you handle it.

**Declarative providers** send an explicit interrupt event that means
"clear your buffers now." The bridge clears unconditionally:

```python
# Example: Gemini's serverContent.interrupted = True
self.server.ingress_streamer.barge_in(session_id, endpoint_id)
convo.ingress_accumulator.clear()
```

**Advisory providers** send a "speech detected" event that means
"the caller is speaking — decide what to do." The bridge must track
whether audio is currently playing out and act accordingly:

```python
# Example: OpenAI/xAI input_audio_buffer.speech_started
if convo.audio_playing_out:
    self.server.ingress_streamer.barge_in(session_id, endpoint_id)
    convo.ingress_accumulator.clear()
    await convo.provider_ws.send(json.dumps({
        "type": "response.cancel",
        "response_id": active_response_id
    }))
```

Read your provider's protocol documentation to determine which model
applies. Do not infer from surface similarity with other providers —
the wrong model produces either missed interrupts or spurious cancels.
See §8.2 for the full architectural discussion.

---

## §5 Phase 3 — Closing the call

### The termination contract

> Every `bot.start` received must eventually produce a `bot.ended` sent.

Missing any termination path leaves the IVA module in an indeterminate
state. You must handle all four:

1. Self-service complete — agent signals it is done
2. Live agent handoff — agent requests a human
3. Platform-initiated end — Infinity sends `bot.end`
4. Failure — anything goes wrong during Phase 1 or Phase 2

### Self-service complete

When your agent signals completion, drain the audio queue before
emitting `bot.ended`:

```python
# Wait for IngressStreamer queue to empty
while True:
    queue = self.server.ingress_streamer._queues.get(
        f"{session_id}:{endpoint_id}"
    )
    if queue is None or queue.empty():
        break
    await asyncio.sleep(0.25)

# Emit bot.ended with success context
await self._send_bot_ended_success(convo)
```

The drain waits for all audio to be handed to Infinity over the
WebSocket. Include a safety timeout (30 seconds) to prevent deadlock
if the queue never empties. See §8.7 for an important caveat about
what "queue empty" means.

### Live agent handoff

The sequence for a clean handoff:

1. Agent signals handoff (typically a tool call)
2. Reply to the agent immediately so it can speak its
   acknowledgment line
3. Drain the audio queue — the acknowledgment must finish playing
   before the handoff fires
4. Flush any stranded transcripts from the trigger turn
5. Emit `bot.feature LIVE_AGENT_HANDOFF`
6. Emit `bot.ended` with no `status` field

```python
handoff_payload = {
    "ftype": "LIVE_AGENT_HANDOFF",
    "liveAgentHandoff": {
        "queueId": queue_id,
        "tags": tags,
        "context": {"reason": reason},
    },
}
# ... drain ...
await self._emit_session_event(
    convo, "bot.feature", handoff_payload
)
await self._send_bot_ended_handoff(convo)
```

The Infinity workflow reads `byobotLiveAgentHandoff` on the HANDOFF
branch. The `queueId` field routes to the correct agent queue — if
your provider does not supply one, the workflow's HANDOFF exit owns
routing.

### Failure paths

Emit `bot.ended` with failure context whenever something goes wrong:

```python
# Phase 1 failure
failure_context = {
    "status": {
        "code": 503,
        "reason": "BACKEND_START_FAILED",
        "description": "FOOBAR: connection failed",
    }
}
```

Common status codes:

| Code | Reason | When |
|---|---|---|
| `503` | `BACKEND_START_FAILED` | API key missing, connection failed, codec mismatch |
| `500` | `INTERNAL_ERROR` | Unhandled exception |

### Platform-initiated end

Infinity sends `bot.end` when the workflow ends the session. Your
`_handle_bot_end` must:

1. Pop the conversation
2. Close the provider WebSocket
3. Cancel any running tasks
4. Emit `bot.ended`

This path fires whether or not the agent has finished speaking.
Clean up resources regardless.

---

## §6 Transcripts

Emit one `bot.feature TRANSCRIPT` per speaker per turn:

```python
{
    "ftype": "TRANSCRIPT",
    "transcript": {
        "turnId": str(uuid.uuid4()),
        "speaker": "CUSTOMER",   # or "BOT" — never "AGENT"
        "isFinal": True,
        "text": "...",
        "confidence": 1.0,
        "language": convo.language_code,
        "startTsMs": int(time.time() * 1000),
    }
}
```

**`speaker` must be `"BOT"` or `"CUSTOMER"`** — not `"AGENT"`. Wrong
value causes bot turns to be missing from the Infinity call record.

**`startTsMs` must be captured at turn start**, not at flush time.
Transcript completion events can arrive out of order. A timestamp
captured at flush time produces incorrect ordering in the Infinity
call record.

### Partial-transcript providers

If your provider streams additive transcript partials rather than
complete turn text, you must accumulate and flush:

- Buffer partials per speaker on `BotConversation`
- Emit a single `TRANSCRIPT` per speaker on the provider's
  turn-finality signal
- Guard against empty-text flushes — tool-only turns must not
  emit blank transcript frames

Providers with a conformant per-turn signal (one event per completed
turn) do not need accumulation logic.

---

## §7 Registering your plugin

Edit `bridge/bot_service.py` to wire your plugin into
`CombinedBotService`:

```python
# Import at top of file
from .bot_foobar import FoobarService

class CombinedBotService(ServicePlugin):
    def __init__(self, server):
        super().__init__(server)
        self._elevenlabs = ElevenLabsService(server)
        self._gemini = GeminiService(server)
        self._openai = OpenAIService(server)
        self._xai = XaiService(server)
        self._foobar = FoobarService(server)       # add
        self._active: Dict[str, str] = {}

    @staticmethod
    def _is_foobar_bot_id(bot_id):                 # add
        return (bot_id or "").strip().lower().startswith("foobar:")

    async def _handle_bot_start(self, websocket, client_id, data):
        # ...existing provider checks...
        is_foobar = self._is_foobar_bot_id(bot_id) # add
        if not (is_echo or is_elevenlabs or is_gemini
                or is_openai or is_xai or is_foobar):
            # emit UNSUPPORTED_SERVICE bot.ended
            ...
        elif is_foobar:                            # add
            await self._foobar.handle_message(
                websocket, client_id, data
            )
            self._active[key] = "foobar"
```

Mirror the same pattern in `_handle_bot_end`, `on_session_ended`,
`shutdown`, and `ingest_audio_chunk`.

**Do not call `server.register_service(self._foobar)`.** Backend
plugins are owned by `CombinedBotService`, not registered at the
top level. The `register(server)` function at the bottom of your
plugin file exists only so `main.py`'s plugin loader can import it.
Only `echo` and `bot` (the dispatcher) are loaded that way.

Also update `.env.example` with any new environment variables your
plugin reads, and update `bridge/.env.example` in `infinity-bridge`
to match.

---

## §8 Real-time AI voice integration — architectural considerations

These are not bridge-specific guidelines. They are patterns that emerge
from integrating real-time conversational AI into production voice
systems — patterns that are expensive to discover in the field and
cheap to know in advance.

Read this section before writing your audio path or your prompt. The
failure modes described here are deterministic given the architectural
choices that produce them. Understanding why they occur is more useful
than memorizing the rules.

### 8.1 Prompt portability — where providers enforce constraints

System prompts are not freely portable across providers. Before
transplanting a persona prompt from one provider to another, audit
where the source provider enforces behavioral constraints.

Some providers enforce voice, language, and tone at the platform level
— outside the prompt entirely. A prompt written for such a provider
may have no language directive, no voice instruction, no turn-behavior
guidance, because those are handled by dashboard configuration or
account settings. Transplanting that prompt to a provider with no
equivalent platform surface means the model receives no instruction
on those behaviors and defaults unpredictably.

**Before reusing a prompt across providers, ask:**
- Where does the source provider enforce voice, language, and tone?
  In the prompt, in platform settings, or both?
- Does the destination provider have equivalent platform surfaces?
- What behaviors must the prompt absorb that the source provider
  handled externally?

The most common failure mode is language drift: a model without an
explicit language directive will code-switch based on caller speech.
A single "Hola" is enough to flip output language for the rest of
the call. See §8.5 for the full discussion.

### 8.2 Interruption protocol shape — advisory vs. declarative

Providers signal caller interruptions in two structurally different
ways. Using the wrong pattern produces either missed interrupts or
spurious cancels.

**Declarative providers** handle cancellation server-side and push
a "this turn was interrupted, clear your buffers" signal. The bridge
clears unconditionally on receipt. No bridge-side state tracking
needed beyond clearing the audio buffer.

**Advisory providers** send a "caller speech detected" event and
leave the cancellation decision to the bridge. The bridge must track
whether audio is currently playing out — not whether the model is
generating — and cancel only when audio is actively in flight.

This distinction matters because **generation rate and playout rate
diverge**. A model may finish generating a response seconds before
that response finishes playing to the caller. Any state flag that
tracks "is the model generating" will read `False` while audio is
still playing. An interrupt that arrives during that window will be
dropped if the gate checks generation state rather than playout state.

The correct gate for advisory providers is a `audio_playing_out`
flag that is set when audio enters the IngressStreamer queue and
cleared only when the queue drains to completion — not when the
model's generation event fires.

### 8.3 Generation rate vs. playout rate

Real-time AI models generate audio faster than it plays to the caller.
The ratio varies by model and response length but is typically several
times faster than real-time for TTS-heavy responses.

The bridge accumulates audio in the IngressStreamer queue between
generation completion and playout completion. The size of that queue
at any moment reflects the lag between what the model has generated
and what the caller has heard.

**Implications for your implementation:**

**State flags must be playout-driven, not generation-driven.** Any
flag intended to gate barge-in, mark turn boundaries, or signal
end-of-segment must be set and cleared from queue activity, not from
upstream model events like "response started" or "response completed."

**Pre-emit signaling.** When your provider signals that audio
generation for a turn is complete, wire that signal to
`IngressStreamer.mark_audio_segment_complete()` so the natural-drain
path fires the playout-done callback reliably. Without this, the
bridge falls back to an idle timeout to detect drain completion.

**Queue depth planning.** Peak queue depth per conversation is
approximately `max_response_duration × (generation_rate - playout_rate)`.
For long responses at high generation rates, this can reach tens of
seconds of buffered audio. Factor this into memory planning when
scaling concurrent calls.

### 8.4 Tool behavior — multi-signal interaction

Real-time LLM tool behavior emerges from the interaction of multiple
signals: the tool schema, the prompt instructions, and the tool result
content. These are not independent. A constraint that appears redundant
from the protocol perspective may be load-bearing from the model's
behavioral perspective.

The failure mode of removing a "redundant" constraint: a tool that
worked reliably breaks, and the breakage is non-obvious because the
protocol still accepts the modified configuration without error.

**Practical rules:**

- A tool schema `required` declaration signals a behavioral contract
  to the model beyond its literal protocol meaning. Relaxing it can
  weaken the model's adherence to the surrounding prompt instructions.

- Tool result content is part of the signal. A populated result
  ("Transfer initiated to queue-001") carries different behavioral
  weight than an empty one. When a tool has a parameter that is
  sometimes empty, consider substituting a meaningful default rather
  than forwarding the empty value.

- Prompt instructions for post-tool behavior ("after invoking this
  tool, do not generate any further response") are necessary but may
  not be sufficient on their own. The tool schema and result content
  work with the prompt instruction — all three together produce
  reliable behavior; any one alone may not.

- **Test signal removal experimentally.** If you believe a constraint
  is redundant, remove it, place a real call, and verify the behavior
  is preserved. Cheap to verify, expensive to assume wrong.

### 8.5 Language pinning — a deployment property, not a model default

Without explicit language instruction, real-time LLMs will code-switch
based on caller speech. A single word in another language is sufficient
to flip the model's output language for the remainder of the turn —
sometimes the remainder of the call.

For contact center deployments, the language of a call is a deployment
property determined by the workflow, the caller's CRM record, and the
supported agent pool — not by what the caller happens to say. Treat it
as such in your prompt.

**Preferred framing:**

```
Respond in {language} as specified by the workflow. If the caller
speaks another language, continue responding in {language}.
```

This is more durable than a list of "do not switch" directives because
it generalizes to multilingual deployments without rewriting — change
what `{language}` resolves to and the behavior follows.

**Where language enforcement lives differs by provider.** Some
providers enforce language at the platform level (dashboard or API
configuration outside the prompt). Others have no equivalent surface —
every behavioral constraint must live inside the prompt. Audit before
transplanting a prompt across providers.

### 8.6 Transfer protocol — no confirmation gating

When a caller requests a human agent, the correct bot response is:
acknowledge the request, state the transfer, invoke the tool. No
confirmation turn.

The caller's request to speak with a human is itself the confirmation.
Introducing a confirmation question adds turns to the most
reliability-critical moment in the call and contradicts what the
caller just said.

**Remove or refuse to add:**
- Instructions to ask the caller to confirm before transferring
- Instructions to verify the topic, gather more information, or
  restate the request before invoking the tool
- Any clause that gates the tool invocation on a follow-up caller
  response

**The correct shape:**
1. Warm acknowledgment, by name, with brief context
2. Statement that the caller will be connected
3. Tool invocation as the final action of the turn
4. Explicit instruction to suppress post-tool generation

The post-tool suppression instruction is load-bearing. Real-time LLMs
default to generating a follow-up turn against the tool result. That
follow-up often re-introduces a confirmation question that the rest of
the prompt avoided. An explicit "after invoking this tool, do not
generate any further response — wait silently" is required alongside
the tool schema and result content to reliably suppress it.

### 8.7 The drain confirms send-side completion, not caller-side completion

The `_wait_for_quiescence_and_emit` drain pattern waits until the
IngressStreamer queue is empty before firing `LIVE_AGENT_HANDOFF`
and `bot.ended`. Queue-empty means all audio has been handed to
Infinity over the WebSocket. It does not mean the caller has heard
it.

Infinity has its own downstream buffering between WebSocket receipt
and PSTN delivery. The bridge cannot observe that buffer. When the
bridge fires `bot.ended`, Infinity may still have several seconds of
audio queued for the caller. Whether Infinity flushes or drops that
buffer on the bot.ended transition determines whether the caller
hears the agent's complete goodbye.

The RCMS protocol provides no "playout complete to caller"
acknowledgment. The bridge operates on send-side completion only.

**For your implementation:**
- Treat queue-empty as "audio delivered to Infinity," not "caller
  heard audio"
- If validation calls show the agent's closing line being clipped,
  the downstream buffer is the likely cause — not a bridge bug
- A fixed post-drain settle delay before emitting `bot.ended` is
  the mitigation shape if clipping is observed; the appropriate
  value depends on observed downstream buffer depth

### 8.8 LLM behavioral validation discipline

Fixes that target LLM-driven behavior — prompt instructions, tool
schema constraints, platform configuration — require different
validation discipline than fixes that target deterministic protocol
behavior.

Protocol fixes can be validated with a single call. Either the wire
shape matches the spec or it does not. LLM behavioral fixes cannot:
the model is non-deterministic by construction. A fix that works on
one call may fail on another with no observable difference in
conditions.

**Recommended discipline:**

- Run a minimum of five calls before claiming a behavioral fix is
  stable. Ten is better when feasible. The goal is failure-rate
  characterization, not success demonstration.
- Document the failure rate as part of your validation. "Zero
  regressions in ten calls" is meaningful evidence. "Worked on the
  first try" is not.
- Treat behavioral closure as time-bounded. Provider-side model
  updates, prompt drift, and non-determinism mean a fix validated
  today may regress next week. Periodic re-validation is appropriate
  for fixes that depend on prompt adherence.
- When time pressure forces closure with insufficient validation,
  document the validation gap explicitly. Do not frame N=1 success
  as "validated."

**Verifying load-bearing field assumptions:**

Before shipping a fix that hypothesizes "the consumer reads field Y
for behavior Z," verify it with a test case where the candidate
field's ordering disagrees with all other plausible candidates.
Emit two adjacent events whose ordering by the candidate field
inverts ordering by wire-arrival, sequence number, and timestamp.
Observe which order the consumer renders. If the consumer renders
by your candidate field, the hypothesis is supported. If it renders
by another, the hypothesis is refuted before any code ships.

### 8.9 Tool description style is provider-specific

Tool `description` fields are not neutral metadata. Some providers
treat them as behavioral instructions and route them into conversation
context accordingly.

On `grok-voice-think-fast-1.0`, imperative language in tool
descriptions ("must be called immediately," "do not end the response
without calling this tool") causes the model to verbalize the
description as part of the conversation rather than treating it as
tool metadata. The effect is complete suppression of the tool event
on the wire — the model says the words but never fires the tool.

Passive, factual descriptions do not trigger this behavior:

```
# Triggers verbalization on Grok — avoid
"description": "MUST be called immediately when the caller requests a human agent."

# Works correctly on Grok
"description": "Transfer the caller to a live human agent. Call this when the caller requests to speak with a person."
```

The same passive phrasing works correctly on other providers. The
inverse — imperative phrasing — may work on some providers and fail
silently on others. Test tool description changes against your
specific provider. Do not assume phrasing validated on one provider
transfers to another.

---

## §9 Testing checklist

Before deploying a new provider:

- [ ] Echo round-trip passes with your provider's `botId` — call
      connects, audio flows both ways, `bot.ended` is sent cleanly
- [ ] PCMU 8kHz and L16 8kHz codec paths both work end-to-end
- [ ] Greeting buffering works — first word of the greeting is
      audible, not clipped
- [ ] Barge-in works — speaking over the bot stops its audio
      immediately
- [ ] Clean termination — `bot.end` from Infinity produces
      `bot.ended`, closes the provider socket, and leaves no tasks
      or queues behind
- [ ] No cross-talk between concurrent calls — two simultaneous
      sessions do not bleed audio into each other
- [ ] Provider-side disconnect mid-call does not crash the bridge
      or affect other calls
- [ ] Self-service complete path produces `bot.ended` with
      `status.code: 200` and the Infinity workflow takes SUCCESSFUL
- [ ] Live agent handoff produces `bot.feature LIVE_AGENT_HANDOFF`
      followed by `bot.ended` with no `status` field, and the
      Infinity workflow takes HANDOFF
- [ ] Failure path (kill the API key) produces `bot.ended` with
      `status.code: 503` and the Infinity workflow takes FAILED
- [ ] `.env.example` updated with all new environment variables
- [ ] `bridge/.env.example` in `infinity-bridge` updated to match

---

*Back to [Builder's Guide](../BUILDERS_GUIDE.md)*
