# Gemini Live

Plugin: `providers/gemini/bot_gemini.py` — registered by the bridge as a
backend under the `gemini:` Bot ID prefix. Bridges Avaya Infinity RCMS to
Google's Gemini Live BidiGenerateContent streaming endpoint.

---

## Getting started

1. Get a Google AI Studio API key at <https://aistudio.google.com/apikey>.
   Keys are free for the Live API (usage limits apply).
2. Add `GEMINI_API_KEY=<your-key>` to your `.env` file.
3. Set your system prompt via `GEMINI_SYSTEM_PROMPT` in `.env`, or point
   `GEMINI_SYSTEM_PROMPT_FILE` at a markdown file (see §4).
4. In the Infinity Workflow Designer, set the Bot ID to `gemini:<model>`,
   e.g. `gemini:gemini-2.0-flash-live-001`.

No agent pre-provisioning is required — Gemini is called directly per
session. There is no vendor dashboard to configure and no agent ID to
manage.

---

## Prerequisites

- A Google AI Studio API key.
- `GEMINI_API_KEY` set in your `.env` file. If missing, `bot.start`
  returns `session.error` and other provider paths continue to work.

---

## 1. Bot ID format

```
gemini:<model>
```

Example: `gemini:gemini-2.0-flash-live-001`.

The bridge strips the `gemini:` prefix and uses the remainder as the model
name in the setup message (`models/<model>`). If the suffix is empty
(`gemini:`), the plugin falls back to a default model name configured in
the plugin.

---

## 2. Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | **Yes** | API key passed as `?key=` in the WebSocket URL. |
| `GEMINI_SYSTEM_PROMPT` | Optional | System prompt text. If unset, a generic fallback is used. |
| `GEMINI_SYSTEM_PROMPT_FILE` | Optional | Path to a markdown file containing the system prompt. Takes precedence over `GEMINI_SYSTEM_PROMPT` if both are set. |

---

## 3. Key architectural difference from agent-as-a-service providers

Gemini is a raw model — the bridge connects directly to the Gemini Live API
per call and sends a system prompt at session setup. There is no pre-created
agent in a vendor dashboard, no agent ID, and no platform-side persona
configuration.

This means:

- **System prompt is bridge-controlled.** Voice, persona, instructions, and
  tool declarations all live in the system prompt you supply via
  `GEMINI_SYSTEM_PROMPT` or `GEMINI_SYSTEM_PROMPT_FILE`. See `system_prompt.md`
  in this directory for a working example.
- **Model selection is per-call.** The Bot ID suffix is the model name —
  you can point different Infinity workflows at different Gemini models
  without changing any bridge configuration.
- **No dynamic variables.** Unlike ElevenLabs, Gemini does not have a
  `dynamic_variables` mechanism. Call context (caller number, direction,
  case details) is injected into the system prompt at session setup — see §4.

---

## 4. System prompt and call context

On `bot.start` the plugin builds a system prompt by appending call-context
fields to the base prompt:

```
Call context: direction=INBOUND, from=+15555550123, to=+18005550456, ucid=90000:1234, language=en-US
```

`direction`, `from`, `to`, `ucid`, and `language` are read from
`bot.start.payload`. Missing fields substitute empty strings; `language`
defaults to `"en-US"`. The composed prompt is sent in the opening
`BidiGenerateContentSetup` message:

```json
{
  "setup": {
    "model": "models/gemini-2.0-flash-live-001",
    "responseModalities": ["AUDIO"],
    "systemInstruction": { "parts": [{ "text": "<composed prompt>" }] }
  }
}
```

The plugin waits for `setupComplete` before forwarding any caller audio.

**No template variable syntax.** Whatever you put in `GEMINI_SYSTEM_PROMPT`
is used verbatim with the context line appended. For per-call variation
beyond the five fixed fields, extend `_compose_system_prompt` in the plugin
to read additional fields from `bot.start.payload.context`.

---

## 5. Audio format

Gemini Live uses asymmetric sample rates — 16 kHz input, 24 kHz output.
The bridge handles all conversion transparently.

| Direction | Bridge ↔ Gemini | Conversion to/from Infinity (G.722 16kHz) |
|---|---|---|
| Input (caller → Gemini) | PCM S16LE 16 kHz | G.722 decode → 16kHz PCM → base64 → `realtimeInput.audio`. No resample needed; G.722 decode produces 16kHz directly. |
| Output (Gemini → caller) | PCM S16LE 24 kHz (in `serverContent.modelTurn.parts[].inlineData.data`) | Resample 24k→16k → G.722 encode → accumulate to chunk boundary → `IngressStreamer.queue_audio` (paced at 80ms per chunk). |

For narrowband codecs (L16/PCMU/PCMA) the input path adds 8k→16k resample
and the output path resamples 24k→8k followed by codec-specific encode.

**Important:** Gemini outputs audio as fast as possible — bursts arrive at
roughly 14× real-time. The bridge accumulates and paces output to 1×
real-time before forwarding to Infinity. This is required infrastructure,
not a workaround — sending bursts directly would overflow Infinity's ingress
queue and defeat barge-in coherence.

---

## 6. Interruption / barge-in

When Gemini emits `serverContent.interrupted = true` (the model detected
the caller speaking over it), the plugin immediately cancels any queued
return audio so Infinity stops playing the bot's response.

---

## 7. Live agent handoff

The plugin implements a `transfer_to_agent` function tool. When the agent
invokes it, the plugin emits `bot.feature LIVE_AGENT_HANDOFF` to trigger
the Infinity workflow's routing logic.

**Queue routing is workflow-controlled by design.** Unlike a DTMF-menu IVR
where the bot selects a queue directly, the recommended pattern for
production deployments is to let the Infinity workflow own routing
intelligence. The IVA module exits on handoff, the workflow reads
`byobotEndContext` or intent signals from the conversation, and routes
dynamically. This keeps routing logic visible, auditable, and changeable
without touching the AI configuration.

The handoff emission shape:

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

**Quiescence drain.** The handoff emission is deferred until the audio
queue drains. This is required because Gemini's `toolCall` message arrives
while the model's goodbye audio is still in-flight — emitting `bot.ended`
immediately would terminate the session before the caller hears the
transfer acknowledgment. The bridge waits for the ingress queue to go quiet
before emitting the terminal events.

**Declare queue IDs in the system prompt.** Even though routing is
workflow-controlled, the system prompt should tell the agent what to say
when transferring — and the `reason` parameter carries context to the
workflow for routing decisions. See `system_prompt.md` for the transfer
protocol instruction pattern.

---

## 8. Self-service complete — end call

The plugin implements an `end_session` function tool. When the agent
invokes it, the plugin emits `bot.ended` with a success context so the
Infinity workflow knows the interaction completed without requiring a live
agent.

Same quiescence drain applies — the emission is deferred until the goodbye
audio drains, so the caller hears the closing line before the session ends.

The `end_session` tool must be declared in the system prompt's tool
configuration and the agent must be instructed when to use it. See
`system_prompt.md` for the end-call protocol instruction pattern.

---

## 9. Session cap and goAway

Gemini Live enforces a per-session time limit (approximately 15 minutes).
When the limit is approaching, Gemini sends a `goAway` frame with a
`timeLeft` field. The plugin:

1. Logs a warning.
2. Emits `bot.feature PROVIDER_GOAWAY` so the Infinity workflow can react
   (e.g. route to a live agent before disconnection):

```json
{
  "type": "bot.feature",
  "payload": {
    "endpointId": "...",
    "ftype": "PROVIDER_GOAWAY",
    "providerGoAway": { "provider": "gemini", "timeLeft": "30s" }
  }
}
```

3. Marks the session inactive so no further audio is forwarded. The actual
   `bot.ended` arrives from Infinity when it decides to terminate — the
   bridge does not tear the session down unilaterally.

---

## 10. Known limitations

- **Transcripts not emitted.** The plugin does not currently surface
  Gemini's text output as `bot.feature TRANSCRIPT` events — only audio is
  forwarded. Gemini's `outputTranscription` and `inputTranscription` fields
  in `serverContent` are the upstream source if you want to add this.

- **System prompt has no template engine.** `GEMINI_SYSTEM_PROMPT` is used
  verbatim with the fixed call-context line appended. For per-call variation
  beyond the five fixed fields, extend the plugin's system prompt composition
  to read additional fields from `bot.start.payload.context`.

- **DTMF not handled.** The bridge ingests `session.dtmf` events from
  Infinity and drops them — digits never reach Gemini. Partners implementing
  payment flows should collect DTMF in the Infinity workflow and route
  directly to a payment processor, never forwarding digits to the AI
  provider in any form.

- **15-minute session cap.** Gemini Live enforces a hard session time limit.
  Handle `PROVIDER_GOAWAY` in your Infinity workflow to route gracefully
  before disconnection (see §9).
