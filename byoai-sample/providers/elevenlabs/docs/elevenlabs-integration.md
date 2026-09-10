# ElevenLabs Conversational AI

Plugin: `providers/elevenlabs/bot_elevenlabs.py` — registered by the bridge
as a backend under the `elevenlabs:` Bot ID prefix.

## Prerequisites

- An ElevenLabs account with Conversational AI enabled.
- A deployed agent (see agent setup below).
- An API key (per-call `botCredentials` in `bot.start` overrides the
  server-wide default; see §4).

---

## 1. Agent setup

The bridge assumes the agent is already configured in the ElevenLabs
platform. Use the provisioning scripts in `providers/elevenlabs/scripts/`
to create and configure your agent programmatically (recommended), or
configure manually via the ElevenLabs dashboard.

Key agent settings:

- **Voice** — pick a conversational voice. Call audio is 16kHz both
  directions, which matches ElevenLabs' default.
- **First message** — the greeting the caller hears. Use only simple
  `{{variable}}` substitution (see §6 for the known limitation).
- **System prompt** — the agent's persona and instructions. Reference
  dynamic variables with `{{variable_name}}`.
- **Dynamic variables** — declare each variable name the agent will
  reference. The bridge sends the call-context variables listed in §4.
- **Tools** — if you want live agent handoff, add a tool named
  `transfer_to_agent` with parameters `queue_id: string`,
  `reason: string`, `tags: array of string` (see §5).

Agent template and tool schema are at `providers/elevenlabs/agent/agent-template.json`
and `providers/elevenlabs/agent/tools/live-agent-handoff.json`.

---

## 2. Bot ID format

```
elevenlabs:<agent_id>
```

The bridge strips the `elevenlabs:` prefix and passes the remainder as the
`agent_id` query parameter when connecting to
`wss://api.elevenlabs.io/v1/convai/conversation`.

An empty agent ID (`elevenlabs:`) is rejected with `session.error` code 500.

---

## 3. Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `ELEVENLABS_API_KEY` | Fallback only | Default API key used when `bot.start` has no `botCredentials`. See §4. |

No other env vars are required for ElevenLabs. Language, voice, and system
prompt all live in the agent's platform configuration.

---

## 4. API key resolution and `botCredentials`

On `bot.start`, the plugin resolves the API key in this order:

1. `payload.botCredentials`, expected to be base64 of a JSON object shaped
   `{"apiKey": "sk_..."}`. This is the per-call path used when each Infinity
   tenant has its own ElevenLabs account.
2. `os.environ["ELEVENLABS_API_KEY"]`.
3. If neither yields a non-empty string, the plugin sends
   `session.error` and does not open the ElevenLabs WebSocket.

---

## 5. Dynamic variables — call context mapping

The plugin extracts fields from `bot.start.payload` and sends them as
`dynamic_variables` in the `conversation_initiation_client_data` message
ElevenLabs expects after the WebSocket opens.

| `dynamic_variables` key | Source field in `bot.start.payload` | Default |
|---|---|---|
| `call_to`        | `to`         | `""` |
| `call_from`      | `from`       | `""` |
| `ucid`           | `ucid`       | `""` |
| `call_direction` | `direction`  | `"INBOUND"` |
| `language`       | `language`   | `"en-US"` |
| `domain`         | `domain`     | `""` |

If `payload.context` is a dict, its entries are **merged in on top** of the
above — so any CRM fields you pass via `context` become first-class dynamic
variables too. Reference them from the agent's system prompt and first
message with `{{key}}`.

---

## 6. Known limitation — first-message Handlebars conditionals

The ElevenLabs agent's "first message" field supports `{{variable}}`
substitution but **does not** support Handlebars conditionals like
`{{#if variable}}...{{/if}}`. If a conditional is present, ElevenLabs closes
the WebSocket with a 1008 policy violation after the bridge has already
sent `bot.started` — the caller then hears dead air until Infinity times out.

**How to spot it**: ElevenLabs WS closes immediately after the initiation
message; the bridge log shows `ElevenLabs WS closed: ... 1008 policy violation`.
No provider audio ever arrives.

**Workaround**: keep first-message templates to simple `{{variable}}`
substitutions. If you need branching greetings, do the branching in the
Infinity workflow and pass a single pre-rendered greeting string as a
`context` variable.

Note: Handlebars conditionals **are** supported in the system prompt field —
only the first message field has this restriction.

---

## 7. Greeting audio buffering

ElevenLabs starts speaking the greeting within ~200ms of the WebSocket
opening. Infinity's ingress path isn't accepting audio for another ~150ms
after that. Raw greeting chunks sent during that window are silently
dropped — the caller hears the greeting clipped or missing entirely.

The plugin handles this automatically with an ingress-readiness gate:

1. On `bot.start`: ingress is marked not ready; a buffer is initialized.
2. Audio from ElevenLabs is transcoded to the session codec, then held
   in the buffer until the first caller egress frame arrives from Infinity.
3. When the first egress frame arrives, the buffer is drained in order
   and ingress is marked ready — subsequent audio is forwarded directly.

You do not need to configure anything for this to work.

---

## 8. Live agent handoff

The plugin recognises an ElevenLabs tool named `transfer_to_agent`. When
the agent invokes it, the plugin emits a `bot.feature` event of type
`LIVE_AGENT_HANDOFF` so the Infinity workflow can route the call:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "...",
    "ftype": "LIVE_AGENT_HANDOFF",
    "liveAgentHandoff": {
      "queueId": "<from tool parameter queue_id>",
      "tags":    ["<from tool parameter tags[]>"],
      "context": { "reason": "<from tool parameter reason>" }
    }
  }
}
```

The tool call result `{"status": "ok", "queue_id": "..."}` is sent back to
ElevenLabs so the agent's flow continues correctly.

To enable handoff, add the tool to the agent in the dashboard or via the
provisioning scripts. A template is available at
`providers/elevenlabs/agent/tools/live-agent-handoff.json`.

**Important:** `queue_id` must be in the tool's `required` array. Removing
it causes the agent to produce duplicate transfer acknowledgments. The
`required` declaration acts as a behavioral anchor for the LLM beyond its
literal contract — it signals that the tool call has a hard contract and
the populated result satisfies it. See the inline comments in
`providers/elevenlabs/scripts/update_agent.py` for the full rationale.

---

## 9. Transcripts

Every `user_transcript` and `agent_response` event from ElevenLabs is
emitted as a `bot.feature` event of type `TRANSCRIPT`:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "...",
    "ftype": "TRANSCRIPT",
    "transcript": {
      "turnId": "<uuid>",
      "speaker": "USER" | "BOT",
      "isFinal": true,
      "text": "...",
      "confidence": 1.0,
      "language": "en-US",
      "startTsMs": 1745000000000
    }
  }
}
```

---

## 10. DTMF

Not handled. The bridge ingests `session.dtmf` events from Infinity but the
ElevenLabs plugin does not receive them — DTMF dispatch was removed as a
defense-in-depth measure to prevent cardholder data from transiting the AI
provider in any form. The bridge logs each DTMF event as observed-but-not-handled
and drops it; nothing reaches ElevenLabs.

Partners implementing payment flows should collect DTMF digits in the Infinity
workflow and route them directly to a payment processor — never forwarding digits
to the AI provider.

---

## 11. Interruption / barge-in

When ElevenLabs emits `{"type": "interruption"}` (the agent detected the
caller speaking over it), the plugin immediately cancels any queued return
audio so Infinity stops playing the bot's response.

Turn-taking sensitivity is configured on the ElevenLabs agent, not in the
bridge. If the agent interrupts too eagerly, adjust the `turn_eagerness`
setting in your agent configuration — the provisioning script in
`providers/elevenlabs/scripts/update_agent.py` sets this to `"patient"` by
default, which works well for contact center use cases where callers
pause mid-thought.
