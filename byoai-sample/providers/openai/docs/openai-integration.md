# OpenAI Realtime

Plugin: `providers/openai/bot_openai.py` — registered by the bridge as a
backend under the `openai:` Bot ID prefix. Bridges Avaya Infinity RCMS to
the OpenAI Realtime API over WebSocket (`wss://api.openai.com/v1/realtime`).

---

## Getting started

1. Get an OpenAI API key with Realtime API access at <https://platform.openai.com/api-keys>.
2. Add `OPENAI_API_KEY=<your-key>` to your `.env` file.
3. Set your system prompt via `OPENAI_SYSTEM_PROMPT` in `.env`, or point
   `OPENAI_SYSTEM_PROMPT_FILE` at a markdown file (see §5).
4. In the Infinity Workflow Designer, set the Bot ID to `openai:<model>`,
   e.g. `openai:gpt-realtime-2.1`.

No agent pre-provisioning is required — OpenAI is called directly per
session via `session.update`. There is no vendor dashboard to configure
and no agent ID to manage.

---

## Prerequisites

- An OpenAI account with Realtime API access.
- `OPENAI_API_KEY` set in your `.env` file, or provided per-call via
  `botCredentials` in `bot.start` (see §4).
- A model that supports the Realtime API. The plugin defaults to
  `gpt-realtime-2.1`; override per-call via the Bot ID suffix,
  or globally via `OPENAI_MODEL`.

---

## 1. Bot ID format

```
openai:<model>
```

Example: `openai:gpt-realtime-2.1`.

The bridge strips the `openai:` prefix and uses the remainder as the model
in the `session.update` configuration. If the suffix is empty (`openai:`),
the plugin falls back to a default model name configured in the plugin.

---

## 2. Key architectural difference from agent-as-a-service providers

OpenAI Realtime is a raw model — the bridge connects directly to the API
per call and configures the session via `session.update`. There is no
pre-created agent in a vendor dashboard, no agent ID, and no platform-side
persona configuration.

This means:

- **System prompt is bridge-controlled.** Persona, instructions, and tool
  declarations are sent in `session.update.session.instructions` at session
  setup. See `system_prompt.md` in this directory for a working example.
- **Model selection is per-call.** The Bot ID suffix is the model name —
  you can point different Infinity workflows at different OpenAI models
  without changing any bridge configuration.
- **Call context uses Python string interpolation.** Unlike ElevenLabs
  dynamic variables (`{{variable}}`), the OpenAI plugin uses Python
  `{variable}` placeholders in the system prompt template. See §5.

---

## 3. Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Fallback only | Default API key. See §4 for resolution order. |
| `OPENAI_MODEL` | Optional | Overrides the model parsed from the Bot ID suffix. Useful for pinning a model fleet-wide. |
| `OPENAI_VOICE` | Optional | TTS voice. Defaults to `cedar`. |
| `OPENAI_SYSTEM_PROMPT` | Optional | System prompt text. If unset, a generic fallback is used. |
| `OPENAI_SYSTEM_PROMPT_FILE` | Optional | Path to a markdown file containing the system prompt. Takes precedence over `OPENAI_SYSTEM_PROMPT` if both are set. |

---

## 4. API key resolution and `botCredentials`

On `bot.start`, the plugin resolves the API key in this order:

1. `os.environ["OPENAI_API_KEY"]`.
2. `payload.botCredentials`, expected to be base64 of a JSON object shaped
   `{"apiKey": "sk-..."}`. This is the per-call path used when each
   Infinity tenant has its own OpenAI account.
3. If neither yields a non-empty string, the plugin emits `bot.ended`
   with failure context and does not open the OpenAI WebSocket.

**Note:** this resolution order is the inverse of the ElevenLabs plugin,
which checks `botCredentials` first and falls back to the env var. On
OpenAI, a `botCredentials` field on `bot.start` is only consulted if the
server-wide env var is unset — a per-call key cannot override a configured
server-wide key. If you need per-call keys to take precedence, leave
`OPENAI_API_KEY` unset in your `.env` and supply credentials exclusively
via `botCredentials`.

---

## 5. System prompt and call context

On `bot.start` the plugin builds a system prompt from a template by
interpolating call-context fields and CRM data from `bot.start.payload`:

| Template variable | Source |
|---|---|
| `{firstName}`, `{lastName}` | `payload.context.contact.first_name` / `last_name` (or `payload.firstName` / `lastName` directly) |
| `{email}` | `payload.context.contact.email` |
| `{caseId}`, `{caseSubject}`, `{caseDescription}` | `payload.context.case.*` |
| `{direction}`, `{from_num}`, `{to_num}`, `{ucid}` | `payload.direction`, `payload.from`, `payload.to`, `payload.ucid` |
| `{language}` | `payload.language` (defaults to `"en-US"`) |

The interpolated prompt is sent in `session.update.session.instructions`.
The persona in `system_prompt.md` ("Cedar, an Innovation Hub specialist")
is a working example — replace it with your own persona. The variable set
above is the operative contract between the Infinity workflow and the
system prompt template.

---

## 6. Audio format

OpenAI Realtime accepts and emits `g711_ulaw` (µ-law 8 kHz) natively.
PCMU is the zero-transcode path; other Infinity codecs require conversion:

| Infinity codec | Conversion |
|---|---|
| PCMU 8 kHz | None — base64 wrap/unwrap only |
| PCMA 8 kHz | `alaw2lin` → `lin2ulaw` |
| L16 8 kHz | `lin2ulaw` |
| L16 16 kHz | Resample 16k→8k → `lin2ulaw` |
| G.722 16 kHz | G.722 decode → resample 16k→8k → `lin2ulaw` |

Configure your Infinity AI Media Gateway profile to negotiate PCMU for the
zero-transcode path. Output direction reverses the chain.

The same ingress-readiness buffering and chunk-aligned pacing that apply
to all bridge providers apply here — audio arriving before Infinity's
ingress path is ready is buffered and drained in order once the first
egress frame arrives.

---

## 7. Tool registration via `session.update`

The plugin registers two function tools in the opening `session.update`
message:

| Tool | Purpose |
|---|---|
| `transfer_to_agent` | Hand off to a live human agent. Accepts an optional `reason` parameter. |
| `end_session` | End the call after the caller's need is fully resolved. Accepts an optional `reason` parameter. |

Both tools use the same intercept pattern. The handler maintains an
`is_terminal_tool` flag — when `True`, the bridge sends the
`function_call_output` acknowledgment back to OpenAI but suppresses the
post-tool `response.create`. This is load-bearing for both terminal tools:
without it, the model emits a duplicate acknowledgment utterance after the
tool fires that reaches the caller during the audio drain window. The
persona prompt's no-further-response instruction is necessary but not
sufficient on its own — suppressing `response.create` at the wire level
removes the trigger entirely.

---

## 8. Live agent handoff

When the model invokes `transfer_to_agent`, the plugin:

1. Stashes a `LIVE_AGENT_HANDOFF` payload on the conversation.
2. Cancels any in-flight handoff drain task; starts a new one.
3. Sends `function_call_output` to OpenAI; suppresses `response.create`
   (per §7).
4. Returns; the drain task waits for audio quiescence before emitting.

When the drain completes, the plugin emits `bot.feature LIVE_AGENT_HANDOFF`
followed by `bot.ended`:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "...",
    "ftype": "LIVE_AGENT_HANDOFF",
    "liveAgentHandoff": {
      "queueId": "",
      "tags": [],
      "context": { "reason": "<from tool parameter reason>" }
    }
  }
}
```

**Queue routing is workflow-controlled by design.** The IVA module exits
on handoff, the workflow reads `byobotEndContext` or intent signals, and
routes dynamically. This keeps routing logic visible, auditable, and
changeable without touching the AI configuration.

**Quiescence drain.** The handoff emission is deferred until the audio
queue drains — the model's `transfer_to_agent` invocation arrives while
the acknowledgment audio is still in-flight. Emitting `bot.ended`
immediately would terminate the session before the caller hears the
transfer acknowledgment. The bridge waits for the ingress queue to go
quiet before emitting the terminal events.

---

## 9. Self-service complete

When the model invokes `end_session`, the plugin:

1. Sets a pending session-end flag on the conversation.
2. Cancels any in-flight session-end drain task; starts a new one.
3. Sends `function_call_output` to OpenAI; suppresses `response.create`
   (same `is_terminal_tool` reasoning as handoff).

When the drain completes, the plugin emits `bot.ended` with success-context
status:

```json
{
  "type": "bot.ended",
  "service": "streaming",
  "payload": {
    "endpointId": "...",
    "context": {
      "status": {
        "code": 200,
        "reason": "ENDPOINT_RELEASED",
        "description": "OPENAI: Self-service interaction completed."
      }
    }
  }
}
```

The Infinity workflow's IVA module routes this through its SUCCESSFUL exit
branch. The full status object is available downstream as
`byobotEndContext.status` — use `byobotEndContext.status.code === 200` to
discriminate successful self-service from failure exits in your workflow.

---

## 10. Transcripts

OpenAI emits `response.output_audio_transcript.delta` events (model speech) and
`conversation.item.input_audio_transcription.completed` events (caller
speech). The plugin accumulates deltas per response ID, flushes on
`response.output_audio_transcript.done`, and emits each as a `bot.feature TRANSCRIPT`
event:

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "...",
    "ftype": "TRANSCRIPT",
    "transcript": {
      "turnId": "<uuid>",
      "speaker": "BOT" | "CUSTOMER",
      "isFinal": true,
      "text": "...",
      "confidence": 1.0,
      "language": "en-US",
      "startTsMs": 1745000000000
    }
  }
}
```

`startTsMs` is captured at turn-start rather than at flush time, preserving
correct chronological ordering even when the model's transcript completion
event lands ahead of the caller's transcription pipeline.

---

## 11. Greeting and ingress buffering

The plugin sends a proactive `response.create` after `session.updated`
arrives, prompting the model to speak the greeting from the persona prompt.
Until the first caller egress frame arrives, ingress audio is buffered
(capped at 50 chunks, oldest discarded when full) and flushed in order as
soon as Infinity's ingress path is ready.

---

## 12. Interruption / barge-in

When OpenAI emits `input_audio_buffer.speech_started` while the model is
generating a response, the plugin issues `response.cancel` on the active
response and immediately cancels any queued return audio.

The barge-in gate is keyed off whether audio is actively playing out to
the caller rather than whether the model is actively generating — because
the model can finish generating a response several seconds before the
caller actually hears it. This ensures barge-in is coherent from the
caller's perspective, not just from the model's.

---

## 13. Known limitations

- **Pre-tool acknowledgment audio is probabilistic.** The model can invoke
  `transfer_to_agent` or `end_session` without preceding acknowledgment
  audio — the caller hears silence at the moment of transfer, then the
  call ends or routes away. The system prompt mitigates this with an
  explicit sequencing instruction (speak a complete acknowledgment as a
  single utterance, then invoke the tool immediately after). This reduces
  but does not eliminate the failure mode. Partners experiencing this at
  a customer-visible rate have three additional mitigation directions
  available:
  - **`tool_choice` parameter.** The plugin sends `tool_choice: "auto"`
    in `session.update`; the OpenAI Realtime API also accepts `"required"`
    and specific function names, which may force acknowledgment-first
    behavior.
  - **Bridge-side audio precondition.** Track transcript delta events
    per response ID; if a tool invocation arrives without preceding audio
    in the same response, send a follow-up `response.create` before
    returning the `function_call_output`, forcing the model to speak first.
  - **Explicit two-turn instruction.** Restructure the Transfer Protocol
    in the system prompt so the acknowledgment is one complete response
    with no tool call, and the tool fires only on a subsequent turn.

- **Post-tool duplicate acknowledgment is fully suppressed.** Without
  intervention, the model emits a second acknowledgment utterance after
  the tool fires. The plugin suppresses `response.create` for terminal
  tools, which removes the trigger. This works correctly out of the box —
  no action required from integrators.

- **DTMF not handled.** The bridge ingests `session.dtmf` events from
  Infinity and drops them — digits never reach OpenAI. Partners
  implementing payment flows should collect DTMF in the Infinity workflow
  and route directly to a payment processor, never forwarding digits to
  the AI provider in any form.

- **No customizable greeting trigger logic.** The plugin sends a fixed
  proactive `response.create` after `session.updated`. If you need greeting
  variation gated on call context (e.g. different greeting for inbound vs
  callback), encode the variation in the system prompt — the bridge does
  not currently expose a knob for this.
