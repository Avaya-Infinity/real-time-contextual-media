# RCMS Protocol Schema — Virtual Agent (Bot) Service Only

Machine-readable JSON Schema for the Avaya Real-time Contextual Media
Streaming (RCMS) protocol, scoped to the Virtual Agent (bot) service.

**Spec reference:**
https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

---

## Scope

This schema covers three message families:

| Family | Messages | Direction |
|---|---|---|
| `session.*` | session.start, session.started, session.ping, session.pong, session.end, session.ended, and all other session messages | Both |
| `bot.*` | bot.start, bot.started, bot.end, bot.ended, bot.feature, bot.event | Both |
| `media` | Audio frames | Both |

The RCMS protocol also defines Agent Assist, Recording, TTS, ASR,
Transcription, and Translation service families. These are out of scope
for this implementation and not included in this schema. See the full
protocol specification for their definitions.

---

## Key behavioral notes

### Message envelope

Every RCMS message shares the same envelope:

```json
{
  "version": "1.0.0",
  "type": "<message-type>",
  "sessionId": "<uuid>",
  "sequenceNum": 1,
  "timestamp": "2026-05-07T14:28:50.000Z",
  "payload": { ... }
}
```

`sequenceNum` starts at **1**, not 0. Each side maintains its own
counter. A reset to 1 indicates a session restart.

Bridge-originated `bot.*` messages include a top-level `service:
"streaming"` field not defined in the spec envelope. This field is
present on `bot.started` and `bot.ended`; absent on `bot.feature`.

### media messages

Audio frames are sent as **binary frames** in all observed production
sessions. The `media` message fields (`bid`, `src`, `asn`, `ts`,
`lastf`, `audio`) are at the **top level of the message envelope**,
not nested inside a `payload` object. The spec's MediaPayload
definition nests them inside payload — the wire reality does not.

`lastf` is a **boolean** at runtime (`true`/`false`). The spec defines
it as integer.

`src` includes the value `"none"` at runtime in addition to `"rx"` and
`"tx"`.

### session.start extra fields

Infinity sends additional fields in `session.start.payload` not
defined in the spec: `allowSelfSigned` (always `false`), and a
`context.session` object containing Infinity runtime state. The schema
sets `additionalProperties: true` on `SessionStartPayload` to
accommodate these. The bridge does not read or use these extra fields.

Two fields in `session.start.payload` are valuable for post-call
correlation and should be captured if needed:

- `engagementId` — persistent identifier that follows the customer
  journey across transfers, workflow sessions, and interaction records
- `workflowSessionId` — identifier for the workflow session that
  invoked the IVA module

These are not available in `bot.start.payload` unless explicitly
passed through the IVA module's custom parameters as `{{engagementId}}`
and `{{workflowSessionId}}`.

### bot.start — botCredentials is optional

The spec marks `botCredentials` as required. At runtime it is
effectively optional — most deployments leave it empty and configure
provider API keys in the bridge `.env` file. The schema marks it as
not required.

### bot.ended — status is nested in context

The spec defines `BotEndedPayload` with an optional `context` field
and no `status` field. The bridge uses `payload.context.status` as
the outcome surface — status is nested inside context, not at the
payload level.

The Infinity workflow reads `byobotEndContext` (populated verbatim
from `bot.ended.payload.context`) to determine the outcome:

| `byobotEndContext.status.code` | Meaning |
|---|---|
| `200` with `reason: "ENDPOINT_RELEASED"` | Self-service complete |
| `200` with `reason: "CALLER_DISCONNECTED"` | Caller disconnected |
| `4xx` or `5xx` | Failure — see reason and description |
| absent | Live agent handoff — read `byobotLiveAgentHandoff` |

### bot.feature — endpointId at payload level

`bot.feature` messages carry `endpointId` at the `payload` level, not
inside the feature sub-object (`transcript`, `liveAgentHandoff`,
`transferCall`). The spec does not define `endpointId` on
`BotFeaturePayload` at all — this is a bridge-added field.

### session.ended reason value

Infinity sends `status.reason: "ended"` (lowercase) in `session.end`.
This value is not in the spec's documented reason enum. The schema
uses a free string for the reason field.

### maskDTMF

The `maskDTMF` boolean field in `MediaTransportSelected` is documented
in the RCMS spec and accepted by Infinity on the wire, but was absent
from the original schema. It is included here. When `true`, DTMF
digits are suppressed at the platform level.

---

## What changed from the source schema

This schema was derived from `bridge/schema/mim.schema.json` with the
following changes:

| Change | Reason |
|---|---|
| Removed out-of-scope service families | agentassist, recording, tts, asr, transcription, translator not implemented |
| `media` fields moved to top level | Runtime sends fields at top level, not nested in payload |
| `media.lastf` changed to boolean | Runtime sends boolean, not integer |
| `media.src` enum adds `"none"` | Runtime uses "none" in binary frame ingress path |
| `bot.ended` context adds `status` sub-object | Bridge nests status in payload.context.status |
| `bot.feature` adds `endpointId` at payload level | Bridge emits endpointId here on all bot.feature messages |
| `bot.feature` adds `PROVIDER_GOAWAY` ftype | Gemini goAway event surfaced as bot.feature |
| `MediaTransportSelected` adds `maskDTMF` | Spec-documented field, absent from source schema |
| `SessionStartPayload` sets `additionalProperties: true` | Infinity sends extra runtime fields |
| `BotStartPayload` marks `botCredentials` as not required | Optional at runtime |
| Title and description updated | Reflects RCMS name and Virtual Agent scope |
| Descriptions added to key fields | For AI tool and human readability |
