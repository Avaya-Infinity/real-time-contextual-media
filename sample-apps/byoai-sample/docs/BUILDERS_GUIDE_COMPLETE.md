# Infinity RCMS BYO AI — Builder's Guide (Complete)

> Single-file export of the Builder's Guide for review and offline reading.
> Combines all twelve section files with the "Adding a Provider" guide as
> an appendix. Source files in `docs/` remain authoritative.

## Table of contents

**Part 1 — The Foundation**

- [§1 What You're Building](#1-what-youre-building)
- [§2 Architecture](#2-architecture)
- [§3 The Call Lifecycle](#3--the-call-lifecycle)
- [§4 Bridge Installation and Configuration](#4-bridge-installation-and-configuration)
- [§5 Avaya Infinity: Network Requirements](#5-avaya-infinity-network-requirements)
- [§6 Avaya Infinity: Security Keys & JWT](#6-avaya-infinity-security-keys--jwt)
- [§7 Avaya Infinity: BYO AI Integration](#7-avaya-infinity-byo-ai-integration)
- [§8 Avaya Infinity: Workflow](#8-avaya-infinity-workflow)
- [§9 Avaya Infinity: IVA Module](#9-avaya-infinity-iva-module)
- [§10 Avaya Infinity: Phone Number Routing](#10-avaya-infinity-phone-number-routing)
- [§11 Verification with Echo](#11-verification-with-echo)
- [§12 Quick Reference](#12-quick-reference)

**Appendix — [Adding a Provider](#appendix-adding-a-provider)**

- [§1 The plugin contract](#1-the-plugin-contract)
- [§2 Minimal skeleton](#2-minimal-skeleton)
- [§3 Phase 1 — Connecting your provider](#3-phase-1--connecting-your-provider)
- [§4 Phase 2 — Audio](#4-phase-2--audio)
- [§5 Phase 3 — Closing the call](#5-phase-3--closing-the-call)
- [§6 Transcripts](#6-transcripts)
- [§7 Registering your plugin](#7-registering-your-plugin)
- [§8 Real-time AI voice integration — architectural considerations](#8-real-time-ai-voice-integration--architectural-considerations)
- [§9 Testing checklist](#9-testing-checklist)

---

## §1 What You're Building

The contact center has always been where customer relationships are won or
lost. The technology underneath it — the routing, the queuing, the voice
infrastructure — has had to be reliable above all else. Enterprises don't
experiment with mission-critical voice. They standardize, they stabilize,
and they invest for the long term.

That approach made sense when the technology stack changed slowly.

AI doesn't change slowly.

In the last two years, the best conversational AI model has changed hands
multiple times. What was state of the art eighteen months ago is a baseline
today. New providers emerge, capabilities leap forward, and the developers
who bet their architecture on a single model find themselves rebuilding —
not because they made a bad decision, but because the landscape moved.

Avaya sees this differently.

Infinity is built on the principle that your AI strategy should be yours —
not ours, not any single provider's. The BYO AI program is the expression
of that principle in code: a documented, open integration layer that
connects Infinity's contact center platform to any conversational AI you
choose. You bring the model. Infinity handles everything else — the call
routing, the context, the workflow, the handoffs.

This guide is your foundation.

By the time you finish, you'll have a working bridge that connects a live
phone number to a real AI provider — with caller context flowing from your
CRM into the conversation, clean handoffs to human agents, and a call record
that captures everything. You'll have built it on Infinity's RCMS protocol,
which means your Infinity configuration — the workflow, the IVA module, the
routing — doesn't change when you change providers. Swap ElevenLabs for
Gemini. Swap Gemini for whatever ships next year. The bridge pattern stays.
The workflow stays. The integration point is modular by design.

The four providers in this guide — ElevenLabs, Gemini, OpenAI, and xAI —
aren't the point. They're the proof. Proof that the architecture holds across
fundamentally different AI platforms: an agent-as-a-service model, a raw
multimodal model, a realtime API, a voice-native telephony model. Four
different integration patterns, one bridge architecture, one Infinity
workflow.

What you're building isn't four integrations.

It's the foundation for every integration that comes after.

---

### What you'll have when you're done

A running RCMS bridge connected to Avaya Infinity, with:

- A verified end-to-end call flow — live phone number, real AI, real audio
- Caller context from your workflow flowing into every AI conversation
- Clean closure paths — self-service complete, live agent handoff, failure
  recovery — all wired to your workflow branches
- A modular provider architecture you can extend to any LLM

The guide is structured in two parts. Part 1 builds the foundation using
the Echo provider — a loopback that requires no AI credentials and proves
every layer of the stack before any AI complexity is introduced. Part 2
connects a real AI provider. By the end of Part 1, you'll have placed a
call, heard it route through your bridge, and confirmed the integration
works. Everything after that is additive.

Let's build.

## §2 Architecture

![RCMS bridge architecture](architecture/bridge-architecture.svg)

The diagram above shows the three systems involved in every call.

**Avaya Infinity** is the contact center platform. It owns the caller experience end-to-end — routing inbound calls, executing workflows, managing queues, and capturing reporting and analytics. When a call reaches the AI self-service step in a workflow, Infinity's IVA module sends a `bot.start` message to the AI Media Gateway. The AI Media Gateway is the WSS endpoint the bridge connects to. From Infinity's perspective, the bridge is just a registered virtual agent — it receives the call, handles it, and signals the outcome via `bot.ended`.

**The RCMS bridge** (this repository) sits between Infinity and the AI provider. It accepts the WebSocket connection from the AI Media Gateway, manages the RCMS protocol session lifecycle, and routes each call to the correct provider plugin based on the `botId` prefix in the `bot.start` message. The bridge owns the audio pipeline — transcoding between Infinity's negotiated codec and the format each AI provider expects — and ensures every call exits cleanly via a spec-compliant `bot.ended` regardless of how the conversation ends.

**The AI provider** supplies the intelligence. The bridge connects to the provider's API, streams caller audio to it, and relays the provider's audio and transcript events back to Infinity. The provider guide for each integration covers the specifics of that connection.

The Echo provider (used in Part 1 of this guide) replaces the AI provider entirely — it loops caller audio back without connecting to any external service. This makes it the right tool for validating the foundation before introducing AI credentials and external dependencies.

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
state. See [Appendix: Adding a Provider](#appendix-adding-a-provider) for the full implementation
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

## §4 Bridge Installation and Configuration

### Prerequisites

Before installing the bridge, ensure you have:

- Python 3.10 or later (Python 3.13+ requires `audioop-lts` — included in `requirements.txt`)
- Git
- A server with a publicly accessible HTTPS endpoint
- A valid TLS certificate from a public CA (self-signed certificates are not supported by Avaya Infinity)

For full network and security requirements, see the [Avaya Infinity Real-time Contextual Media Streaming](https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming) developer documentation.

---

### Clone and install

```bash
git clone https://github.com/bode-avaya/infinity-rcms-byoai.git
cd infinity-rcms-byoai
python3 -m venv venv
source venv/bin/activate
pip install -r bridge/requirements.txt
```

---

### Configure environment variables

Copy the example file and edit it:

```bash
cp bridge/.env.example bridge/.env
```

Open `bridge/.env` and configure the following values for Part 1. Provider-specific variables are covered in each provider's guide — the full reference is in `bridge/.env.example`.

**Bridge security:**

| Variable | Required | Description |
|---|---|---|
| `INFINITY_JWT_PRIMARY_KEY` | Required when auth is enabled | Primary JWT signing key from the Avaya Infinity Admin Dashboard |
| `INFINITY_JWT_SECONDARY_KEY` | Optional | Secondary key — used during key rotation to maintain service continuity |

**Operational:**

| Variable | Default | Description |
|---|---|---|
| `LOG_DIGITS` | `false` | Set `true` only when troubleshooting DTMF behavior. Logs raw digit values which may include sensitive data — disable when troubleshooting is complete |

---

### Start the bridge

```bash
python3 bridge/main.py --host 127.0.0.1 --port 8444 --verbose
```

On a clean startup you should see:

```
Registered plugin: bot_echo.py
Registered backend: ElevenLabsService (botId prefix: elevenlabs:)
Registered backend: GeminiService (botId prefix: gemini:)
Registered backend: OpenAIService (botId prefix: openai:)
Registered backend: XaiService (botId prefix: xai:)
Registered plugin: bot_service.py
RCMS Bridge listening on 127.0.0.1:8444
```

Only providers with their API key configured will register. If you have not set any AI provider credentials yet, only `bot_echo.py` and `bot_service.py` will appear — that is the correct state for Part 1 of this guide.

---

### Common flags

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8443` | Port to listen on |
| `--codec` | `G722` | Preferred audio codec (`PCMU` eliminates transcoding overhead for xAI Grok) |
| `--enable-auth` | off | Enable JWT bearer token validation |
| `--jwt-primary-key` | env | JWT primary key (overrides `INFINITY_JWT_PRIMARY_KEY` env var) |
| `--jwt-secondary-key` | env | JWT secondary key (overrides `INFINITY_JWT_SECONDARY_KEY` env var) |
| `--log-file` | `logs/bridge_log.txt` | Path to the on-disk Python log file. Rotated to `.bak` on every restart |
| `--message-log-file` | `logs/bridge_msg.txt` | Path to the structured protocol-frame log (INBOUND/OUTBOUND JSON only). Rotated to `.bak` on every restart |
| `--verbose` | off | Raise the root Python logger from INFO to DEBUG. Surfaces audio-pipeline diagnostic lines (`MEDIA SUMMARY`, `FIRST MEDIA EGRESS/INGRESS`) that are otherwise suppressed |

---

### Logs and observability

The bridge writes logs to two destinations on every run:

1. **stderr** — captured by whichever process manager runs the bridge. Under `systemd`, this becomes the service journal (`journalctl -u <unit-name>`); under a bare terminal, it goes to the terminal directly.
2. **On-disk files under `logs/`** — `bridge_log.txt` (full Python log) and `bridge_msg.txt` (structured INBOUND/OUTBOUND JSON protocol frames only, with `LOG_DIGITS` redaction applied). Both are rotated to `.bak` on every restart, so pull anything you need before the next restart.

The two destinations carry different data: the journal/stderr captures everything the root Python logger emits, including provider-specific diagnostic chatter; `bridge_msg.txt` captures only the RCMS wire frames between Infinity and the bridge, which makes it the cleaner artifact for spec-conformance review. Media frames are not written to `bridge_msg.txt`.

**Log levels:**

- Default root-logger level is `INFO`.
- `--verbose` (or `-v`) raises it to `DEBUG`. The audio-pipeline diagnostic lines (`MEDIA SUMMARY (1s)` per-second counters, `FIRST MEDIA EGRESS/INGRESS` stream-open banners) are at DEBUG — they appear under `--verbose` and are absent at the INFO default. Run with `--verbose` when debugging audio path problems; leave it off for normal operation.
- `bridge_msg.txt` is independent of the root level — it captures protocol frames at a fixed cadence regardless of `--verbose`.

**Filtering by Python log level: do not use `journalctl -p info`.** systemd tags every Python `stderr` write at syslog priority `info` regardless of the Python `logging` module level, so `-p info` does **not** filter Python's levels — a Python `DEBUG` line appears in the `-p info` view alongside `INFO` lines. The reliable filter is to grep on the Python-level tag embedded in the log format:

```bash
# Python-INFO lines only
journalctl -u <unit-name> --since "..." --no-pager | grep " - INFO - "

# Python-DEBUG lines only (verbose-mode noise)
journalctl -u <unit-name> --since "..." --no-pager | grep " - DEBUG - "
```

This same pattern works on the on-disk `bridge_log.txt`:

```bash
grep " - INFO - " logs/bridge_log.txt
```

**`LOG_DIGITS` redaction scope** — DTMF digits only. When `LOG_DIGITS=false` (default), the bridge replaces `session.dtmf.payload.digits` with `"<redacted>"` in both the journal and `bridge_msg.txt`. No other fields are currently redacted. Set `LOG_DIGITS=true` only when actively troubleshooting DTMF dispatch — raw digit values land in the logs and, on payment IVR or account-verification flows, may include cardholder data.

For the per-call diagnostic-marker reference (lifecycle markers by phase, transcript marker cadence per provider, common termination shapes), see **[§12 Quick Reference](#12-quick-reference)**.

## §5 Avaya Infinity: Network Requirements

The bridge must be reachable from Avaya Infinity over HTTPS. Infinity initiates all WebSocket connections outbound to your bridge URL — no inbound firewall rules are required on your side.

Key requirements:

- **Public HTTPS endpoint** — your bridge URL must be publicly accessible
- **Valid TLS certificate** — Avaya Infinity requires a certificate from a public CA; self-signed certificates are not accepted
- **TLS 1.2 or later** — required on all connections

For the complete network and security requirements, refer to the [Avaya Infinity Real-time Contextual Media Streaming](https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming) developer documentation.

## §6 Avaya Infinity: Security Keys & JWT

### How authentication works

When `--enable-auth` is active, Avaya Infinity generates a JWT on every WebSocket connection attempt and presents it to the bridge in the `Authorization: Bearer` header. The bridge validates that token using the keys you configure — it never generates or presents a token itself.

This direction is important: **Infinity is always the client**. It dials out to your bridge URL, presents its JWT, and your bridge decides whether to accept the connection. The bridge proves nothing back to Infinity via JWT — Infinity's trust in your bridge is established by your CA-signed TLS certificate at connection time.

> **This is the current, supported mechanism.** Symmetric-key JWT validation
> (HS256, using the primary/secondary keys below) is generally available and
> stable — build against it with confidence. An asymmetric option (RS256/JWKS)
> is planned for the future; integrations built on the current symmetric-key
> method will not be required to migrate.

---

### Primary and secondary keys

Avaya Infinity maintains two keys per AI Media Gateway profile: a primary key and a secondary key. Both are 32-character hex strings generated by Infinity and displayed in the Admin Dashboard.

The two-key design exists for zero-downtime key rotation. When you rotate keys in Infinity, tokens signed with the previous primary key continue to validate against the secondary key during the transition window — active calls are not dropped. Once rotation is complete, configure the new key values in your bridge and restart.

---

### Obtaining your keys

1. In the Avaya Infinity Admin Dashboard, navigate to **Voice → AI Media Gateway Profiles**
2. Select your profile and open the **Authentication Keys** tab
3. Click **Create** to generate a new key pair — Infinity creates both the primary and secondary keys together
4. Copy both key values

To rotate keys, click the **Rotate Keys** button. Infinity generates a new key pair immediately. Update your bridge configuration with the new values before the rotation window closes.

---

### Configuring the bridge

Set both keys in `bridge/.env`:

```
INFINITY_JWT_PRIMARY_KEY=07f1d21294aa42c1a50d4554ace474a5
INFINITY_JWT_SECONDARY_KEY=<secondary key value>
```

Start the bridge with authentication enabled:

```bash
python3 bridge/main.py --host 127.0.0.1 --port 8444 --enable-auth --verbose
```

The bridge validates every inbound connection against the primary key first. If primary validation fails with an invalid token error, it tries the secondary key. An expired token is rejected immediately — expiry is definitive regardless of which key signed it.

---

### Verification

With `--enable-auth` active and a valid key configured, a successful connection logs:

```
JWT bearer token auth successful for <address> (verified with primary key as UTF-8 bytes)
```

A rejected connection logs a warning with the failure reason. Common causes:

- **Key mismatch** — the key in `.env` does not match what Infinity is signing with. Verify both primary and secondary values against the Admin Dashboard.
- **Expired token** — Infinity tokens have a short validity window. Ensure your server clock is synchronized (NTP).
- **Auth not enabled on Infinity side** — confirm the AI Media Gateway profile has authentication keys configured and active.

## §7 Avaya Infinity: BYO AI Integration

### Create an AI Media Gateway Profile

The AI Media Gateway profile tells Avaya Infinity where your bridge is and how to authenticate with it.

1. In the Avaya Infinity Admin Dashboard, navigate to **Voice → AI Media Gateway Profiles**
2. Click the **+** button in the menu bar to create a new profile
3. In the **Name** field, enter a descriptive name (e.g. `RCMS Bridge - Production`)
4. In the **Service Types** field, select the required service types for your integration
5. In the **Key Pair** field, select the authentication key pair you created in §6. If you have not created one yet, follow the steps in §6 first.
6. In the **Destination WebSocket URL** field, enter your bridge's public HTTPS URL — for example:
   ```
   wss://your-bridge-domain.example.com
   ```
7. Click **Create**

The profile is created and enabled immediately.

> **The destination URL is static per profile.** Avaya Infinity does not
> template per-call identifiers into the URL path — the same WSS endpoint is
> used for every call on this profile. To route or identify calls (by org,
> channel, session, etc.), carry that data as **context** rather than in the
> URL: pass it via IVA Custom Parameters (see §9), where it arrives in the
> `bot.start` payload for your provider plugin to act on. For multi-org or
> multi-tenant deployments, encode the routing key in `botId` and/or a custom
> parameter and branch on it inside the bridge — one static endpoint, many
> logical destinations.

> **How many profiles can I create?** There is no limit on the number of AI
> Media Gateway Profiles. Profiles are not license-gated or metered — create as
> many as your routing model needs (for example, one per environment, per
> business unit, or per bridge endpoint).

---

### Passing context to the bridge

Avaya Infinity gives you two places to pass context to your bridge at call time:

**At the AI Media Gateway profile level** — use Input Variables on the profile to define key-value pairs that apply to every call using this profile. These are well-suited for static values or environment-level configuration that does not change per call.

**At the IVA module level in Workflows** — use the Custom Parameters on the IVA module to pass call-specific context resolved from workflow and CRM variables. This is the recommended approach when context varies per caller. The sample application uses this method — see §9 for details.

Both sources arrive at the bridge in the `bot.start` message payload and are available to your provider plugin for prompt injection, routing decisions, and dynamic variable substitution.

## §8 Avaya Infinity: Workflow

### Import the sample workflow

The sample workflow is pre-configured for the Echo provider. Import it into your Infinity tenant to get a working end-to-end flow without building from scratch.

1. In the Avaya Infinity Admin Dashboard, navigate to **Workflows**
2. Click the **Import** button in the menu bar
3. Select the workflow JSON file from `providers/echo/infinity-workflow/inbound-virtual-agent-echo.json`
4. Click **Import**

The workflow is imported as a draft. Review it in the Workflow Designer before publishing.

---

### Workflow structure

The sample workflow follows this sequence:

**Start → Set Variable (CRM Data) → Set Variable (Source Details) → Intelligent Virtual Agent → Decision (Self Service Complete?) → Create Interaction → End**

The two Set Variable steps populate demo CRM values before the IVA node. In a production deployment replace these with your CRM integration — the variable names (`firstName`, `lastName`, `email`, `caseId`, `caseSubject`) are what the bridge expects in the `bot.start` payload context.

## §9 Avaya Infinity: IVA Module

### IVA module configuration

The Intelligent Virtual Agent module is the step in the workflow that invokes the bridge. Open the IVA node in the Workflow Designer to review its configuration.

**Connection:**

| Field | Value |
|---|---|
| Connection Type | AI Media Gateway |
| Connection | Your AI Media Gateway profile from §7 |

**Bot ID:**

The `botId` custom parameter tells the bridge which provider to route the call to. For the Echo provider set it to `echo`. For AI providers the value follows the `<prefix>:<model>` convention — for example `gemini:gemini-3.1-flash-live-preview`. Each provider guide covers the correct Bot ID value.

**Custom Parameters:**

The IVA module passes context to the bridge at call time via custom parameters. The sample workflow passes:

| Parameter | Value | Description |
|---|---|---|
| `botId` | `echo` | Routes the call to the correct provider plugin. See Bot ID above. |
| `firstName` | `{{firstName}}` | Caller first name — resolved from CRM data in the workflow |
| `lastName` | `{{lastName}}` | Caller last name — resolved from CRM data in the workflow |
| `email` | `{{email}}` | Caller email address — resolved from CRM data in the workflow |
| `caseId` | `{{caseId}}` | Active case identifier — resolved from CRM data in the workflow |
| `caseSubject` | `{{caseSubject}}` | Case subject — resolved from CRM data in the workflow |
| `caseDescription` | `{{caseDescription}}` | Case description — resolved from CRM data in the workflow |
| `engagementId` | `{{engagementId}}` | Unique identifier for this interaction assigned by Avaya Infinity. Persists for the lifetime of the interaction — use this to correlate the AI session with the broader interaction record in reporting and CRM systems. |
| `workflowSessionId` | `{{workflowSessionId}}` | Unique identifier for this workflow execution instance. Useful for correlating bridge session logs with workflow execution traces in Infinity reporting. |

You can add, remove, or rename parameters to match your use case — the bridge passes whatever is present in `customParameters` to the provider plugin.

---

### IVA module exits

The IVA module has three exit paths:

**SUCCESSFUL** — the bridge sent `bot.ended` with a `200` status code. The sample workflow routes this to a Decision node that checks `{{variables.byobotEndContext.status.code}}` equals `200` to confirm self-service completion before proceeding.

**HANDOFF** — the bridge sent a `bot.feature LIVE_AGENT_HANDOFF` event followed by `bot.ended`. The call is ready to be routed to a live agent. The sample workflow routes this to Create Interaction — replace with your live agent routing logic.

**FAILED** — the bridge sent `bot.ended` with a non-200 status code. This covers provider errors, configuration problems, and session failures. The sample workflow routes this to Create Interaction — replace with your error handling logic.

---

### Handoff events

The IVA module has two event flags:

**Enable Handoff Events** — must be set to `true` for live agent handoff to work. When enabled, the `HANDOFF` exit fires when the bridge emits `bot.feature LIVE_AGENT_HANDOFF`. The handoff payload carries the queue ID, tags, and reason — available in the workflow as `{{variables.byobotLiveAgentHandoff}}`.

**Enable Transfer Call Events** — used when transferring a call to a destination outside Avaya Infinity, such as an external phone number or a third-party system. This is separate from the live agent handoff path which routes within Infinity. Leave this disabled unless your use case requires external transfer.

## §10 Avaya Infinity: Phone Number Routing

Route an inbound phone number to your workflow so calls reach the IVA module.

1. In the Avaya Infinity Admin Dashboard, navigate to **Voice → Numbers**
2. Click the **+** button to create a new number entry
3. In the **Name** field, enter a descriptive name
4. In the **Phone Number** field, enter the inbound phone number
5. Click **Save**
6. Click the routing button and configure:
   - **Voice Route To**: select **Workflow**
   - **Voice Route Data**: select the workflow you imported in §8
   - **Voice Route Workflow Version**: select **Current**
7. Click **Save**

The number is now routed to your workflow. Inbound calls to this number will enter the workflow and reach the IVA module.

> The IVA module and AI Media Gateway require the call to be routed to a workflow — queue routing bypasses the IVA module entirely.

## §11 Verification with Echo

With the bridge running and the workflow published, place a test call to your inbound number. Echo requires no AI credentials — it loops your audio back to confirm the full path is working.

### What to expect

Answer the call and speak. You should hear your own audio played back with a short delay — that is Echo confirming the bridge is receiving and returning audio over the RCMS connection.

### Confirm in the bridge journal

On your bridge server, check the journal:

```bash
sudo journalctl -u bridge.service -f
```

A successful Echo call produces this sequence (lines prefixed with `[<client>]`, the inbound RCMS host:port):

```
[<client>] INBOUND JSON (session.start)
[<client>] Codec negotiation - offered: [...] selected: G722
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] Received bot.start for Echo session: <session-id>
[<client>] OUTBOUND JSON (bot.started)
[binary audio frames echoing back]
[<client>] Sending bot.ended with disconnect context: provider=echo
[<client>] OUTBOUND JSON (bot.ended)
[<client>] OUTBOUND JSON (session.ended)
```

### What each line confirms

| Log line | What it confirms |
|---|---|
| `INBOUND JSON (session.start)` | Infinity reached the bridge over WSS |
| `Codec negotiation - offered: [...] selected: <codec>` | Both sides agreed on an audio codec |
| `OUTBOUND JSON (session.started)` | Bridge accepted the session |
| `INBOUND JSON (bot.start)` | Workflow reached the IVA module and invoked the bridge |
| `Received bot.start for Echo session` | Bridge routed to the Echo provider |
| `OUTBOUND JSON (bot.started)` | Echo session established |
| `Sending bot.ended with disconnect context: provider=echo` | Clean teardown on hangup |
| `OUTBOUND JSON (session.ended)` | Session closed |

If you see this sequence with no errors, your foundation is working. Move to Part 2 to add an AI provider.

### What you may also see under `--verbose`

When the bridge runs with `--verbose`, the root Python logger drops to DEBUG and the journal also surfaces audio-pipeline diagnostic lines:

```
[<client>] ========== FIRST MEDIA EGRESS (stream: ..., format: binary) ==========
[<client>] MEDIA SUMMARY (1s): id=0:1 source=tx egress: ev=4 bytes=8000 seq=4 last=False
[<client>] MEDIA SUMMARY (1s): id=0:1 source=tx egress: ev=4 bytes=8000 seq=8 last=False
...
```

`MEDIA SUMMARY (1s)` fires once per second per audio stream and is diagnostic-only — under `--verbose` it dominates the journal during any active call. At the default INFO level these lines are absent. If you see them in your journal, your bridge is running with `--verbose`; if you don't, the audio path is still working — just quietly. See **§4 — Logs and observability** for level filtering.

### Other journal patterns to recognize

The Echo path above is the success-on-hangup case. Two other lifecycle shapes will appear when you connect a real AI provider.

**Failure path — unrecognized `botId`:** if the `botId` custom parameter on the IVA module names a provider the bridge doesn't recognize (or whose API key is unset), the bridge rejects the call cleanly with a 5xx status:

```
[<client>] INBOUND JSON (session.start)
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] Sending bot.ended with failure context: code=501, reason=UNSUPPORTED_SERVICE
[<client>] OUTBOUND JSON (bot.ended)
   ... payload.context.status.description: "BACKEND_NOT_CONFIGURED: ..." or "UNRECOGNIZED_BOTID_PREFIX: ..."
[<client>] OUTBOUND JSON (session.ended)
```

The Infinity workflow receives this `bot.ended` on its IVA module SUCCESSFUL branch with `byobotEndContext.status.code = 501` — route on that to your failure recovery path (see §3 Phase 3).

**Handoff path — live agent transfer:** when the AI provider triggers a handoff (typically via a tool call), the bridge drains queued audio, then emits a `bot.feature LIVE_AGENT_HANDOFF` followed by `bot.ended` with no `status` field:

```
[<client>] INBOUND JSON (session.start)
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] <Provider> bot.start session=... endpoint=...
[<client>] OUTBOUND JSON (bot.started)
[<client>] BOT transcript: ...
[<client>] CUSTOMER transcript: ...
[<client>] OUTBOUND JSON (bot.feature)
[<client>] Emitted LIVE_AGENT_HANDOFF (reason='...')
[<client>] OUTBOUND JSON (bot.ended)
[<client>] INBOUND JSON (session.end)
[<client>] OUTBOUND JSON (session.ended)
```

The workflow takes the IVA module's HANDOFF branch in this case (provided **Enable Handoff Events** is `true` — see §9). The `byobotLiveAgentHandoff` workflow variable carries the `queueId`, `tags`, and `context` from the provider's handoff payload.

### Troubleshooting

**No audio loopback** — confirm the workflow is published (not draft) and the phone number is routed to the correct workflow version.

**Bridge not reached** — confirm your public HTTPS endpoint is reachable and the AI Media Gateway profile Destination WebSocket URL matches your bridge address.

**`bot.start` not received** — confirm the IVA module Connection is set to your AI Media Gateway profile and the profile is enabled.

## §12 Quick Reference

This section collects the most-referenced values from the guide in one place. Use it while configuring, deploying, or debugging — not as a substitute for the full sections.

---

### Environment variables

All variables live in `bridge/.env` (copy from `bridge/.env.example`). The bridge does not auto-load this file — on a VM deployment it is loaded by systemd via `EnvironmentFile=`; for local runs, source it before launching the bridge.

**Bridge security (see §6):**

| Variable | Required | Description |
|---|---|---|
| `INFINITY_JWT_PRIMARY_KEY` | When `--enable-auth` is set | Primary HS256 signing key from the Avaya Infinity Admin Dashboard |
| `INFINITY_JWT_SECONDARY_KEY` | Optional | Secondary key — used during rotation for zero-downtime key changes |

**Operational (see §4):**

| Variable | Default | Description |
|---|---|---|
| `LOG_DIGITS` | `false` | Set `true` only when troubleshooting DTMF behavior; logs raw digit values |

**Provider (set only the ones you use):**

| Variable | Description |
|---|---|
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GEMINI_MODEL` | Gemini Live model identifier (default: `gemini-3.1-flash-live-preview`) |
| `GEMINI_SYSTEM_PROMPT` / `GEMINI_SYSTEM_PROMPT_FILE` | Inline base prompt or path to a markdown prompt file |
| `GEMINI_VOICE` | Voice id (default: `Aoede`) |
| `OPENAI_API_KEY` | OpenAI Realtime API key |
| `OPENAI_MODEL` | Model id (default: `gpt-realtime-2.1`) |
| `OPENAI_VOICE` | Voice id (default: `cedar`) |
| `XAI_API_KEY` | xAI Grok Voice API key |
| `XAI_MODEL` | Model id (default: `grok-voice-think-fast-2.0`) |
| `XAI_VOICE` | Voice id (default: `ara`) |

The agent identifier for ElevenLabs comes from the `botId` at runtime (`elevenlabs:<agent_id>`) — there is no `ELEVENLABS_AGENT_ID` env var.

---

### Startup flags

```
python3 bridge/main.py [flags]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8443` | Port to listen on |
| `--codec` | `G722` | Preferred audio codec (`L16`, `PCMU`, `PCMA`, `G722`) |
| `--enable-auth` | off | Require JWT bearer token validation |
| `--jwt-primary-key` | `INFINITY_JWT_PRIMARY_KEY` env | Primary HS256 key (CLI overrides env) |
| `--jwt-secondary-key` | `INFINITY_JWT_SECONDARY_KEY` env | Secondary HS256 key (CLI overrides env) |
| `--ssl-cert` | none | Path to TLS certificate (required for WSS unless fronted by a proxy) |
| `--ssl-key` | none | Path to TLS private key |
| `--log-file` | `logs/bridge_log.txt` | Path to the on-disk Python log file. Rotated to `.bak` on every restart |
| `--message-log-file` | `logs/bridge_msg.txt` | Path to the structured protocol-frame log (INBOUND/OUTBOUND JSON only). Rotated to `.bak` on every restart |
| `--verbose` / `-v` | off | Raise the root Python logger from INFO to DEBUG. Surfaces `MEDIA SUMMARY` and `FIRST MEDIA EGRESS/INGRESS` lines that are otherwise suppressed |

JWT flags are the only ones that fall back to env vars; everything else is explicit at the command line.

---

### botId prefix convention

The bridge routes each call to a provider plugin based on the `botId` value in `bot.start.payload.customParameters` (set on the IVA module — see §9).

| `botId` value | Routes to |
|---|---|
| `echo` | Echo provider (no suffix, no credentials needed) |
| `elevenlabs:<agent_id>` | ElevenLabs (agent id supplied per call) |
| `gemini:<model>` | Gemini Live (model name supplied per call) |
| `openai:<model>` | OpenAI Realtime (model name supplied per call) |
| `xai:<model>` | xAI Grok Voice (model name supplied per call) |

**Example:** `botId = openai:gpt-realtime-2.1` routes to OpenAI Realtime with that model. Echo is the only provider whose `botId` has no suffix.

When `GEMINI_MODEL` / `OPENAI_MODEL` / `XAI_MODEL` is set in the environment, it forces that model for every call and ignores the botId suffix. Leave the env var unset to let each call pick its own model via the suffix; the bridge falls back to a built-in default only if both are empty.

---

### IVA module exits and workflow variables

The IVA module has three exit branches (see §9 for full detail):

| Branch | Fires on | Workflow variable to read |
|---|---|---|
| `SUCCESSFUL` | `bot.ended` with `context.status.code = 200` | `byobotEndContext` |
| `HANDOFF` | `bot.feature LIVE_AGENT_HANDOFF` followed by `bot.ended` | `byobotLiveAgentHandoff` |
| `FAILED` | `bot.ended` with non-200 status | `byobotEndContext` |

`HANDOFF` is the workflow branch label; `LIVE_AGENT_HANDOFF` is the `bot.feature` event type that triggers it. Enable Handoff Events must be `true` on the IVA module for the branch to fire.

---

### Diagnostic log lines

A handful of journal lines partners commonly grep when something is wrong. The bridge prefixes most lines with a per-connection `[client_id]` correlator.

**Bridge is up:**
```
RCMS Bridge listening on 127.0.0.1:8444
Registered plugin: bot_echo.py
Registered backend: ElevenLabsService (botId prefix: elevenlabs:)
```
The startup log emits one `Registered backend:` line per provider whose API key env var is set (`ElevenLabsService` / `GeminiService` / `OpenAIService` / `XaiService`). If a provider you expect is missing, that provider's API key env var is unset.

**Auth failure (when `--enable-auth` is on, see §6):**
```
Missing or invalid Authorization header from <addr>
Invalid JWT bearer token from <addr> (could not verify with any key format)
Expired JWT bearer token from <addr>
```

**Routing failure:**
```
[<client_id>] BACKEND_NOT_CONFIGURED
```
The `botId` prefix matched a known provider but its API key env var was not set at bridge startup.

**Termination shape (one of four):**
```
[<client_id>] Sending bot.ended with success context: code=200, reason=ENDPOINT_RELEASED
[<client_id>] Sending bot.ended with failure context: code=<5xx>, reason=<...>
[<client_id>] Sending bot.ended with disconnect context: provider=<name>
[<client_id>] Emitted LIVE_AGENT_HANDOFF (reason='...')
```
The first three are `bot.ended` log prefixes for self-service complete / provider error / caller hangup. The fourth marks the live agent handoff path — the bridge originates `bot.ended` with no `status` field after emitting `bot.feature LIVE_AGENT_HANDOFF`, so the `OUTBOUND JSON (bot.ended)` line follows without a `Sending bot.ended with X context` prefix. See §3 Phase 3 for the full closure logic.

---

### Lifecycle markers by phase

When debugging a single call, grep by phase to localise where things went wrong. All markers carry the `[<client_id>]` prefix — use it to isolate one call's frames from concurrent traffic.

| Phase | Grep target | Confirms |
|---|---|---|
| 1 — Start | `INBOUND JSON (session.start)` | Infinity reached the bridge over WSS |
| 1 — Start | `Codec negotiation - offered: [...] selected: <codec>` | Codec settled |
| 1 — Start | `OUTBOUND JSON (session.started)` | Bridge accepted the session |
| 1 — Start | `INBOUND JSON (bot.start)` | Workflow IVA module invoked the bridge |
| 1 — Start | `<Provider> bot.start session=... endpoint=...` | Dispatcher routed to the provider plugin (`ElevenLabs` / `Gemini` / `OpenAI` / `Xai`) |
| 1 — Start | `OUTBOUND JSON (bot.started)` | Provider session established |
| 2 — During | `BOT transcript:` / `CUSTOMER transcript:` | Per-turn / per-chunk transcript markers (see cadence note below) |
| 2 — During | `MEDIA SUMMARY (1s)` / `FIRST MEDIA EGRESS/INGRESS` | Audio pipeline health (DEBUG-only — `--verbose` required) |
| 3 — Closure | `Emitted LIVE_AGENT_HANDOFF (reason=...)` | Handoff path fired |
| 3 — Closure | `Emitted self-service-complete bot.ended` | Provider-side self-service signal flushed cleanly |
| 3 — Closure | `Sending bot.ended with success/failure/disconnect context` | Closure shape (see termination triplet above) |
| 3 — Closure | `OUTBOUND JSON (session.ended)` | Bridge closed the session — call done |

### Transcript markers — cadence varies by provider

All four providers emit `[<client_id>] BOT transcript: <text>` and `[<client_id>] CUSTOMER transcript: <text>` lines on `logger.info`. The cadence varies because of how each provider's protocol delivers transcript text:

| Provider | Cadence | Source event |
|---|---|---|
| ElevenLabs | One line per completed turn | `user_transcript` / `agent_response` (turn-completion only in EL's protocol) |
| OpenAI | One line per completed turn | `response.output_audio_transcript.done` / `conversation.item.input_audio_transcription.completed` |
| xAI | One line per completed turn | `response.output_audio_transcript.done` / `conversation.item.input_audio_transcription.completed` |
| Gemini | Per chunk (high volume per turn) | Streaming `outputTranscription` / `inputTranscription` fields on `serverContent` |

The volume difference is expected: a 30-second Gemini call may emit dozens of `BOT transcript:` lines (each a few words); an equivalent OpenAI call emits 3–5 (each a full turn). Format is identical; only the cadence varies.

### Filtering by Python log level — use `grep`, not `journalctl -p`

When the bridge runs under `systemd`, `journalctl -p info` does **not** filter Python log levels. systemd tags every Python `stderr` write at syslog priority `info` regardless of the `logging` module level, so `-p info` drops nothing for our purposes — a Python `DEBUG` line appears in the `-p info` view alongside `INFO` lines.

The reliable filter is to grep on the Python-level tag embedded in the log format string:

```bash
# Python-INFO lines only
journalctl -u <unit-name> --since "..." --no-pager | grep " - INFO - "

# Python-DEBUG lines only (verbose-mode noise)
journalctl -u <unit-name> --since "..." --no-pager | grep " - DEBUG - "
```

The same pattern works against the on-disk `logs/bridge_log.txt` file. Use this when verifying that an INFO-level marker fired (e.g. `BOT transcript:`) without being drowned out by DEBUG-level chatter (e.g. `MEDIA SUMMARY`).

---

# Appendix: Adding a Provider

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

