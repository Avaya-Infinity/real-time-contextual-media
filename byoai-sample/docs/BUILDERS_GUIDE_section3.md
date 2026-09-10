# §3 — The Call Lifecycle

This section is the spine of the Builder's Guide. Everything else —
bridge configuration, Infinity admin setup, provider integration — is
an expansion of one of the three phases described here.

Read this section before reading any code. Every function in this repo
maps to a phase. Every Infinity configuration screen maps to a phase.
This section is where the bridge code, the Infinity configuration, and
the RCMS protocol are explained together for the first time.

**Spec reference:**
https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

---

## The three phases

```
┌─────────────────────────────────────────────────────────────────┐
│  PHASE 1: START                                                 │
│  Infinity establishes the WebSocket session and hands the       │
│  call to the bridge with full caller context.                   │
│                                                                 │
│  session.start  →  session.started                              │
│  bot.start      →  bot.started                                  │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│  PHASE 2: DURING                                                │
│  Audio flows bidirectionally. The bot listens, thinks,          │
│  and speaks. The bridge manages the audio pipeline.             │
│                                                                 │
│  media frames  (egress:  Infinity → bridge)                     │
│  media frames  (ingress: bridge  → Infinity)                    │
│  bot.feature TRANSCRIPT  (bridge → Infinity)                    │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│  PHASE 3: CLOSURE                                               │
│  The call ends. The bridge signals the outcome via bot.ended.   │
│  Infinity routes to the correct workflow branch.                │
│                                                                 │
│  bot.feature LIVE_AGENT_HANDOFF  (optional, before bot.ended)   │
│  bot.feature TRANSFER_CALL       (optional, before bot.ended)   │
│  bot.ended  →  session.end  →  session.ending  →  session.ended │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Start

### What happens

When an inbound call reaches the Virtual Agent step in your Infinity
workflow, Infinity opens a WebSocket connection to the bridge and
initiates a session. Phase 1 is complete when the bridge has accepted
the session, validated credentials, and confirmed the bot is active.

Phase 1 involves two sequential exchanges:

**Exchange 1 — Session establishment:**
```
Infinity  →  bridge:  session.start
bridge    →  Infinity: session.started
```

**Exchange 2 — Bot invitation:**
```
Infinity  →  bridge:  bot.start
bridge    →  Infinity: bot.started   (success path)
                   OR  bot.ended     (failure path, with status in context)
```

Infinity may batch `bot.start` with `session.start` to reduce setup
latency. The bridge must respond to `session.start` with
`session.started` before any other messages are processed.

### session.start and session.started

`session.start` carries the media negotiation — which codecs Infinity
supports, which transport mode is in use (`avaya-wss` or
`avaya-wss-rtp`), and the media endpoint identifiers for this call.

`session.started` is the bridge's acceptance. It declares the selected
codec, transport encoding, and `preferredPTimeMs` — the bridge's
preferred audio frame size in milliseconds.

**What to look for in the bridge journal:**
```
INBOUND  session.start   {"services":["bot"],"mediaTransports":[...]}
OUTBOUND session.started {"services":["bot"],"mediaTransport":{...}}
```

See `bridge/bridge_server.py` for the session establishment
implementation.

### bot.start — the context payload

`bot.start` is the most important message in Phase 1. It carries
everything the bridge needs to route the call and everything the AI
provider needs to serve the caller intelligently.

**Full field reference:**

| Field | Type | Required | Description | Example |
|---|---|---|---|---|
| `endpointId` | string | Yes | Media endpoint identifier for this bot session | `"49c880cf-e234-45c8-99c6-6536c8405151"` |
| `botId` | string | Yes | Provider routing key — format is bridge-defined | `"elevenlabs:agent_abc123"` |
| `botCredentials` | string | Yes | Provider credentials — format is bridge-defined | base64-encoded JSON |
| `language` | string | Yes | ISO-639-1 + ISO-3166 language code | `"en-US"` |
| `domain` | string | Yes | Infinity domain name | `"ejxfto.na.cc.avayacloud.com"` |
| `to` | string | Yes | Called number (DNIS) — E.164 or SIP URI | `"+13322365051"` |
| `from` | string | Yes | Calling number (ANI) — E.164 or SIP URI | `"+13479872825"` |
| `ucid` | string | Yes | Avaya Universal Call ID | `"19529283051762805748"` |
| `direction` | string | Yes | Call direction | `"INBOUND"` or `"OUTBOUND"` |
| `context` | object | No | Opaque workflow context — arbitrary JSON | any JSON object |

**Spec reference:** RCMS spec §AI Bot Message Definitions — bot.start

**Example bot.start payload:**
```json
{
  "version": "1.0.0",
  "type": "bot.start",
  "sessionId": "e1a0d823-745a-430d-804b-c6324621bdfa",
  "sequenceNum": 2,
  "timestamp": "2026-05-07T14:28:50.000Z",
  "payload": {
    "endpointId": "51a0ffc5-c8ed-4ac9-b79f-b65678629163",
    "botId": "echo",
    "botCredentials": "",
    "language": "en-US",
    "domain": "ejxfto.na.cc.avayacloud.com",
    "to": "+17207940219",
    "from": "+12068524641",
    "ucid": "15446593841778164126",
    "direction": "INBOUND",
    "context": {
      "firstName": "Todd",
      "lastName": "Michaels",
      "email": "test@example.com",
      "caseId": "5656",
      "caseSubject": "Self Service Virtual Agents",
      "caseDescription": "Interested in a demo of Avaya Infinity",
      "engagementId": "ec625b10-7cae-4b22-a507-a23e77e38932",
      "workflowSessionId": "028d0105079c51b39a6f77e18b"
    }
  }
}
```

### botId — provider routing

The RCMS spec does not prescribe a format for `botId`. This bridge
uses a prefix convention to route calls to the correct provider plugin:

| Prefix | Routes to | Example |
|---|---|---|
| `echo` | Echo provider (loopback) | `"echo"` |
| `elevenlabs:<agent_id>` | ElevenLabs Conversational AI | `"elevenlabs:agent_8001kqafgryqe3eb8kg3dj1frpgy"` |
| `gemini:<model>` | Google Gemini Live | `"gemini:gemini-3.1-flash-live-preview"` |
| `openai:<model>` | OpenAI Realtime | `"openai:gpt-realtime-2.1"` |
| `xai:<model>` | xAI Grok Voice | `"xai:grok-voice-think-fast-2.0"` |

An unrecognized prefix causes the bridge to emit `bot.ended` with
`payload.context.status.code: 501`. The Infinity workflow receives
this via the `byobotEndContext` variable on its SUCCESSFUL branch.
See Phase 3 — Closure for the full failure path.

### botCredentials — optional per-call override

Provider API keys are configured on the bridge server in the `.env`
file. The Infinity workflow does not need to supply credentials for
normal operation — `botId` is the only required configuration in the
BYOBot node.

`botCredentials` exists for multi-tenant deployments where each
Infinity tenant authenticates against its own provider account. When
present, it carries a base64-encoded JSON object:

```bash
echo -n '{"apiKey":"your-provider-api-key"}' | base64
# eyJhcGlLZXkiOiJ5b3VyLXByb3ZpZGVyLWFwaS1rZXkifQ==
```

Resolution order varies by provider:

| Provider | API key source |
|---|---|
| ElevenLabs | `botCredentials` first, then `ELEVENLABS_API_KEY` env var |
| Gemini | `GEMINI_API_KEY` env var only — `botCredentials` not consulted |
| OpenAI | `OPENAI_API_KEY` env var first, then `botCredentials` |
| xAI | `XAI_API_KEY` env var first, then `botCredentials` |
| Echo | No credentials required |

For most deployments, leave `botCredentials` empty in the workflow
and configure provider keys in the bridge `.env` file. See
`bridge/.env.example` for the full key reference.

See `providers/*/bot_*.py` for provider-specific resolution logic.

### context — workflow variables flow through here

The `context` field is where Infinity workflow variables become AI
provider variables. It is an opaque JSON object — the RCMS spec places
no constraints on its structure. The bridge passes it directly to the
provider.

**This is the integration point partners most often miss.** Before the
BYOBot node in your Infinity workflow, you can populate workflow
variables from CRM lookups, IVR digit collection, authenticated session
data, or any other workflow action. Those variables arrive in
`bot.start.payload.context` and are available to the AI provider from
the first word of the conversation.

**Two system variables worth passing explicitly:** `engagementId` and
`workflowSessionId` are available as Infinity workflow variables but
are not automatically included in `bot.start.payload.context`. Add
them to the IVA module's custom parameters using `{{engagementId}}`
and `{{workflowSessionId}}` syntax.

`engagementId` is the persistent identifier that follows the entire
customer journey across transfers, workflow sessions, and interaction
records. Passing it through context makes it available in
`byobotEndContext` for post-call correlation with the broader Infinity
interaction record.

**Example context flow:**

```
Infinity Workflow
  ├── CRM lookup by ANI  →  sets firstName, accountId, caseId
  ├── IVR prompt         →  sets intent
  └── BYOBot node
        └── context: {
              "firstName": "Alex",
              "accountId": "ACC-00429",
              "intent": "support"
            }
                │
                ▼
        bot.start.payload.context arrives at bridge
                │
                ▼
        Bridge merges context into provider-specific injection:
        ElevenLabs  →  dynamic_variables (merged on top of call fields)
        Gemini      →  appended to system prompt
        OpenAI/xAI  →  interpolated into the system prompt template
                │
                ▼
        Aria greets: "Hi Alex, I can see you're calling
        about case CS-10482..."
```

See `bridge/bot_service.py` for context extraction and
`providers/*/bot_*.py` for provider-specific injection.

### JWT authentication

Infinity generates a signed JWT on every WebSocket connection and
sends it as a Bearer token in the HTTP upgrade request. The bridge
validates it before accepting the session.

**The authentication flow:**
```
Infinity  →  bridge:  WSS connect + Authorization: Bearer <JWT>
bridge:       validate JWT against primary key
              (fall back to secondary key if primary fails)
bridge    →  Infinity: HTTP 101 Switching Protocols
```

**JWT claims:**

| Claim | Description | Example |
|---|---|---|
| `sub` | Infinity Account ID | `"001d010700c9da6ddf9acdf7c0"` |
| `iat` | Issued at (Unix epoch ms) | `1736548831000` |
| `exp` | Expiration (`iat + 300s`) | `1736549131000` |
| `jti` | Nonce | `"07359e90e4714eff-9c2a5c7d22cb6966"` |
| `alg` | Algorithm | `"HS256"` |

The token is valid for 300 seconds. Expiry has no bearing on the
WebSocket once connected — the session continues until explicitly ended.

**Key rotation:** Infinity maintains a primary and secondary key. The
bridge validates against both, in order, to survive key rotation
without downtime. Configure both keys from the Infinity admin console
— see §6 Avaya Infinity: Security Keys & JWT.

**Spec reference:** RCMS spec §Security

---

## Phase 2: During

### What happens

With `bot.started` confirmed, audio flows bidirectionally over the
WebSocket. The caller hears the bot; the bot hears the caller. The
bridge manages the audio pipeline — receiving frames from Infinity,
transcoding if needed, forwarding to the provider, receiving provider
audio, transcoding back, and returning frames to Infinity.

Simultaneously, the bridge posts transcript turns to Infinity as the
conversation progresses.

### Audio frames

Audio travels as `media` messages. Each message carries these fields
at the top level of the message envelope:

| Field | Type | Description |
|---|---|---|
| `bid` | integer | Bearer ID — identifies which endpoint this audio belongs to |
| `src` | string | Audio source: `"rx"` (received from caller), `"tx"` (transmitted to caller), or `"none"` |
| `asn` | integer | Audio sequence number — per-stream ordering |
| `ts` | integer or string | Timestamp |
| `lastf` | boolean | `true` indicates the final frame of an utterance |
| `audio` | string | Base64-encoded audio payload (base64 transport) |

**Egress** (Infinity → bridge): the caller's voice. The bridge
forwards this to the AI provider for processing.

**Ingress** (bridge → Infinity): the bot's voice. The bridge sends
provider-generated audio back to Infinity for playback to the caller.

**Frame pacing:** the bridge chunks outbound audio to match
`preferredPTimeMs` and paces frames at 1× real-time via the
`IngressStreamer`. This is mandatory — sending audio faster than
real-time over-buffers Infinity's playback queue and defeats barge-in
coherence. See `bridge/bridge_server.py` (IngressStreamer) for the
implementation.

**Spec reference:** RCMS spec §Media Encoding Options

### Barge-in

Barge-in is the mechanism by which the caller interrupts the bot
mid-utterance. The caller speaks; the bridge detects it; any queued
bot audio is cancelled; the provider receives the interruption signal.

The bridge handles barge-in uniformly regardless of provider. When
the AI provider signals an interruption, the bridge calls
`IngressStreamer.barge_in()` to immediately cancel any queued return
audio.

**Provider interruption signals vary:**

| Provider | Interruption signal |
|---|---|
| ElevenLabs | `{"type": "interruption"}` event |
| Gemini | `serverContent.interrupted = true` |
| OpenAI / xAI | `input_audio_buffer.speech_started` while response active |

All three map to the same `IngressStreamer.barge_in()` call — the
Infinity side behaves identically regardless of provider. See
per-provider guides for provider-specific signal handling.

### Echo provider — Phase 2 behavior

The Echo provider does not connect to any external AI service. It
reflects incoming audio frames back to the caller immediately using
`IngressStreamer.send_immediate()` for low-latency loopback.

This makes Echo the right first integration target: it exercises the
full audio pipeline — egress receipt, IngressStreamer, ingress
delivery — without any provider latency or API credentials. If audio
echoes correctly, the pipeline is working.

See `providers/echo/bot_echo.py` for the implementation.

### Transcript

As the conversation progresses, the bridge posts transcript turns to
Infinity using `bot.feature` with `ftype: "TRANSCRIPT"`. Infinity
renders these in the call record.

**Critical field values:**

| Field | Required value | Why |
|---|---|---|
| `speaker` | `"BOT"` or `"CUSTOMER"` | Infinity's virtual agent protocol requires `"BOT"` for bot turns — not `"AGENT"`. Wrong value causes bot turns to be missing from the call record. |
| `isFinal` | `true` for complete turns | Interim transcripts (`isFinal: false`) are supported but not required |
| `turnId` | unique UUID per turn | Used for ordering and deduplication |
| `startTsMs` | Unix epoch milliseconds | Captured at turn start, not flush time — preserves correct ordering even when transcript completion events arrive out of order |
| `confidence` | float 0.0–1.0 | Providers vary; currently emitted as a fixed value — see per-provider guides |

**Example bot.feature TRANSCRIPT:**
```json
{
  "version": "1.0.0",
  "type": "bot.feature",
  "sessionId": "c25be228-4e54-4503-974a-881588fb5d49",
  "sequenceNum": 4,
  "timestamp": "2025-01-10T22:40:35.000Z",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "ftype": "TRANSCRIPT",
    "transcript": {
      "turnId": "eafc524d-33c2-4561-96ac-d6d48cbd4f4d",
      "speaker": "BOT",
      "isFinal": true,
      "text": "Hi Alex, I can see you're calling about case CS-10482.",
      "confidence": 1.0,
      "language": "en-US",
      "startTsMs": 1766071442892
    }
  }
}
```

**Spec reference:** RCMS spec §AI Bot Message Definitions — bot.feature

---

## Phase 3: Closure

### What happens

Every call ends. Phase 3 is the sequence of messages that closes the
bot session and tells the Infinity workflow what happened. The outcome
the bridge signals determines which workflow branch Infinity takes —
and therefore what the caller experiences next.

### bot.ended — the universal termination signal

`bot.ended` is how the bridge ends every bot session. Success,
failure, handoff — all three use `bot.ended`. The outcome lives inside
`payload.context`.

```json
{
  "version": "1.0.0",
  "type": "bot.ended",
  "sessionId": "c25be228-4e54-4503-974a-881588fb5d49",
  "sequenceNum": 12,
  "timestamp": "2025-01-10T22:42:15.000Z",
  "service": "streaming",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "context": {
      "status": {
        "code": 200,
        "reason": "ENDPOINT_RELEASED",
        "description": "ELEVENLABS: Self-service interaction completed."
      }
    }
  }
}
```

Infinity responds with `session.end` → `session.ending` → `session.ended`
at sub-second latency, regardless of what is inside `payload.context`.
The bridge does not drive session teardown — Infinity owns that.

### byobotEndContext — the workflow variable

When Infinity receives `bot.ended`, the contents of
`payload.context` are available downstream in your workflow as the
`byobotEndContext` variable, populated verbatim. This is the variable
your workflow reads to determine what happened and route accordingly.

```
bot.ended.payload.context  →  byobotEndContext  (workflow variable)
```

The status object inside `payload.context` gives you a numeric
discriminator for routing:

```
byobotEndContext.status.code === 200  →  success path
byobotEndContext.status.code >= 400  →  failure path
```

Or use a presence-based discriminator:

```
byobotEndContext.status exists  →  failure path
byobotEndContext.status absent  →  success path
```

Both patterns work. Value-based (`status.code`) is more explicit and
aligns with HTTP conventions. Presence-based keeps the success path
context clean. Choose one and apply it consistently in your workflow
Decision module.

See §8 Avaya Infinity: Workflow for how to wire these patterns in the
Infinity workflow designer.

### The bridge's termination contract

A partner building their own provider must handle three termination
initiators — bot-initiated (self-service complete), error-detected
(provider failure), and platform-initiated (session end). All three
must emit `bot.ended`.

> Every `bot.start` received must eventually be paired with a
> `bot.ended` sent.

Missing any termination path leaves the IVA module in an indeterminate
state. See `docs/ADDING_A_PROVIDER.md` for the full implementation
contract.

### The workflow branch contract

The bridge's `bot.ended` emission — and any preceding `bot.feature`
directives — determine which workflow branch fires:

| Bridge emits | Workflow outcome | byobotEndContext |
|---|---|---|
| `bot.ended` with `status.code: 200` | SUCCESSFUL branch | `{status: {code: 200, reason: "ENDPOINT_RELEASED", description: "..."}}` |
| `bot.ended` with `status.code: 4xx/5xx` | SUCCESSFUL branch — route on `byobotEndContext.status.code` | `{status: {code: 5xx, reason: "...", description: "..."}}` |
| `bot.feature LIVE_AGENT_HANDOFF` + `bot.ended` | SUCCESSFUL branch — route on `byobotLiveAgentHandoff` presence | `byobotLiveAgentHandoff` populated; `byobotEndContext` may be empty |
| `bot.feature TRANSFER_CALL` + `bot.ended` | SUCCESSFUL branch — route on `byobotTransferCall` presence | `byobotTransferCall` populated |

**Important:** all `bot.ended` paths produce a SUCCESSFUL IVA module
exit. The workflow author is responsible for reading `byobotEndContext`
and routing to the appropriate downstream path — success flow, failure
recovery, agent queue, or transfer.

### Path 1 — Successful completion

The bot completed its job. The caller's need was met. No human agent
required.

```json
{
  "type": "bot.ended",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "context": {
      "status": {
        "code": 200,
        "reason": "ENDPOINT_RELEASED",
        "description": "ELEVENLABS: Self-service interaction completed."
      }
    }
  }
}
```

The `description` field carries a provider prefix (`ELEVENLABS:`,
`GEMINI:`, `OPENAI:`, `XAI:`, `ECHO:`) so the workflow author can
identify which provider handled the call.

**Workflow:** read `byobotEndContext.status.code === 200`, route to
your post-call flow — survey, disconnect, callback scheduling, etc.

### Path 2 — Live agent handoff

The bot determined the caller needs a human. Before `bot.ended`, the
bridge emits `bot.feature` with `ftype: "LIVE_AGENT_HANDOFF"`:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "ftype": "LIVE_AGENT_HANDOFF",
    "liveAgentHandoff": {
      "queueId": "support-queue-001",
      "tags": ["priority-customer", "billing"],
      "context": {
        "reason": "Customer requesting account review",
        "summary": "Alex called about case CS-10482..."
      }
    }
  }
}
```

`bot.ended` follows immediately after the caller's final audio has
finished playing:

```json
{
  "type": "bot.ended",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "context": {}
  }
}
```

**Workflow:** check `byobotLiveAgentHandoff` presence, read `queueId`,
route to the specified agent queue. The `context` object carries
transcript summary, reason, and any CRM data for the receiving agent.

**The drain pattern is load-bearing here.** The bridge waits for all
queued bot audio to finish playing before emitting
`bot.feature LIVE_AGENT_HANDOFF`. Without this drain, Infinity tears
down the bot leg while the bot's final acknowledgment is still
streaming — the caller hears clipped audio at the most important
moment of the call. See per-provider guides for provider-specific
drain implementation.

**Spec reference:** RCMS spec §AI Bot Message Definitions —
bot.feature LIVE_AGENT_HANDOFF

### Path 3 — Transfer call

The bot requested a PSTN or SIP transfer to a specific URI. Before
`bot.ended`, the bridge emits `bot.feature` with
`ftype: "TRANSFER_CALL"`:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "ftype": "TRANSFER_CALL",
    "transferCall": {
      "uri": "tel:+14692221234",
      "context": {
        "reason": "Transferring to specialist line"
      }
    }
  }
}
```

**Workflow:** check `byobotTransferCall` presence, read `uri`, execute
the transfer. Supported URI schemes: `tel:`, `sip:`.

**Spec reference:** RCMS spec §AI Bot Message Definitions —
bot.feature TRANSFER_CALL

### Path 4 — Failure

Something went wrong. The bridge could not start or sustain the bot
session. `bot.ended` carries the failure details in
`payload.context.status`:

```json
{
  "type": "bot.ended",
  "payload": {
    "endpointId": "49c880cf-e234-45c8-99c6-6536c8405151",
    "context": {
      "status": {
        "code": 501,
        "reason": "UNSUPPORTED_SERVICE",
        "description": "BACKEND_NOT_CONFIGURED: no provider registered for prefix 'unknown'"
      }
    }
  }
}
```

**Common status codes:**

| Code | Reason | When |
|---|---|---|
| `400` | `MISSING_FIELD` | Required field absent from bot.start payload |
| `400` | `INVALID_FIELD` | botId format unrecognized |
| `501` | `UNSUPPORTED_SERVICE` | botId prefix not registered on bridge |
| `503` | `SERVICE_UNAVAILABLE` | Provider API unreachable or credentials invalid |
| `500` | `INTERNAL_ERROR` | Unhandled bridge-side exception |

**Workflow:** read `byobotEndContext.status.code`, route to your
failure recovery path — graceful message, queue transfer, voicemail,
callback offer. Never route failures to a dead end. The
`description` field carries a machine-readable prefix
(`BACKEND_NOT_CONFIGURED:`, `BACKEND_START_FAILED:`, etc.) that
can drive more specific routing logic if needed.

**Spec reference:** RCMS spec §Status Codes
**Spec reference:** RCMS spec §Error Handling

### Path 5 — Platform-initiated end

Infinity ends the session — workflow timeout, caller disconnect, or
operator action. Infinity sends `bot.end`; the bridge acknowledges
with `bot.ended` and cleans up provider resources.

```
Infinity  →  bridge:  bot.end
bridge:       teardown provider session
bridge    →  Infinity: bot.ended
```

The bridge's job here is clean resource teardown: cancel in-flight
provider requests, drain the IngressStreamer, close the provider
WebSocket. No workflow branch decision is involved — Infinity already
owns the call.

See `bridge/bot_service.py` for the platform-initiated end handler.

---

## Lifecycle sequence

```
Infinity                    Bridge                      Provider
   │                           │                           │
   │── session.start ─────────>│                           │
   │<─ session.started ────────│                           │
   │                           │                           │
   │── bot.start ─────────────>│                           │
   │   (context, credentials)  │── connect + auth ────────>│
   │                           │<─ ready ──────────────────│
   │<─ bot.started ────────────│                           │
   │                           │                           │
   │                    [ Phase 2: During ]                │
   │                           │                           │
   │── media (egress) ────────>│── audio ─────────────────>│
   │<─ media (ingress) ────────│<─ audio ──────────────────│
   │<─ bot.feature TRANSCRIPT ─│                           │
   │                           │                           │
   │        [ barge-in ]       │                           │
   │── media (speech) ────────>│                           │
   │                           │── interruption ──────────>│
   │<─ (queued audio cleared) ─│                           │
   │                           │                           │
   │                    [ Phase 3: Closure ]               │
   │                           │<─ tool: handoff/end ──────│
   │                           │   (drain audio queue)     │
   │<─ bot.feature HANDOFF ────│                           │
   │<─ bot.ended ──────────────│── close ─────────────────>│
   │                           │                           │
   │── bot.end ───────────────>│   (platform ack)          │
   │── session.end ───────────>│                           │
   │<─ session.ending ─────────│                           │
   │<─ session.ended ──────────│                           │
```

---

*Next: §4 — Bridge Configuration*
