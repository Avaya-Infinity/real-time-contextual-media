# xAI Grok Voice

Plugin: `providers/xai/bot_xai.py` — registered by the bridge as a
backend under the `xai:` Bot ID prefix. Bridges Avaya Infinity RCMS to
the xAI Realtime API over WebSocket (`wss://api.x.ai/v1/realtime`).

xAI's Realtime API is documented as OpenAI Realtime spec-compatible at
the wire-protocol level. Most of the plugin structurally mirrors the
OpenAI provider; the meaningful differences are documented below.

---

## Getting started

1. Get an xAI API key at <https://console.x.ai/>.
2. Add `XAI_API_KEY=<your-key>` to your `.env` file.
3. Set your system prompt via `XAI_SYSTEM_PROMPT` in `.env`, or point
   `XAI_SYSTEM_PROMPT_FILE` at a markdown file (see §5).
4. In the Infinity Workflow Designer, set the Bot ID to `xai:<model>`,
   e.g. `xai:grok-voice-think-fast-2.0`.

No agent pre-provisioning is required — xAI is called directly per
session via `session.update`. There is no vendor dashboard to configure
and no agent ID to manage.

---

## Prerequisites

- An xAI account with API access.
- `XAI_API_KEY` set in your `.env` file, or provided per-call via
  `botCredentials` in `bot.start` (see §4).
- The plugin defaults to `grok-voice-think-fast-2.0`; override per-call
  via the Bot ID suffix, or globally via `XAI_MODEL`.

---

## 1. Bot ID format

```
xai:<model>
```

Example: `xai:grok-voice-think-fast-2.0`.

The bridge strips the `xai:` prefix and uses the remainder as the model
in `session.update`. Empty suffix (`xai:`) falls back to a default model
name configured in the plugin.

---

## 2. Key architectural difference from agent-as-a-service providers

xAI Grok Voice is a raw model — the bridge connects directly to the API
per call and configures the session via `session.update`. There is no
pre-created agent in a vendor dashboard, no agent ID, and no platform-side
persona configuration.

This means:

- **System prompt is bridge-controlled.** Persona, instructions, and tool
  declarations are sent in `session.update.session.instructions` at session
  setup. See `system_prompt.md` in this directory for a working example.
- **Model selection is per-call.** The Bot ID suffix is the model name.
- **Call context uses Python string interpolation.** Same `{variable}`
  placeholders as the OpenAI plugin — see §5.

---

## 3. Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `XAI_API_KEY` | Fallback only | Default API key. See §4 for resolution order. |
| `XAI_MODEL` | Optional | Overrides the model parsed from the Bot ID suffix. Useful for pinning a model fleet-wide. |
| `XAI_VOICE` | Optional | TTS voice. Defaults to `ara`. |
| `XAI_SYSTEM_PROMPT` | Optional | System prompt text. If unset, a generic fallback is used. |
| `XAI_SYSTEM_PROMPT_FILE` | Optional | Path to a markdown file containing the system prompt. Takes precedence over `XAI_SYSTEM_PROMPT` if both are set. |

---

## 4. API key resolution and `botCredentials`

On `bot.start`, the plugin resolves the API key in this order:

1. `os.environ["XAI_API_KEY"]`.
2. `payload.botCredentials`, expected to be base64 of a JSON object shaped
   `{"apiKey": "xai-..."}`. Per-call path used when each Infinity tenant
   has its own xAI account.
3. If neither yields a non-empty string, the plugin emits `bot.ended`
   with failure context and does not open the xAI WebSocket.

**Note:** this resolution order is the inverse of the ElevenLabs plugin,
which checks `botCredentials` first and falls back to the env var. On xAI,
a `botCredentials` field on `bot.start` is only consulted if the
server-wide env var is unset — a per-call key cannot override a configured
server-wide key. If you need per-call keys to take precedence, leave
`XAI_API_KEY` unset in your `.env` and supply credentials exclusively via
`botCredentials`. Same behavior as the OpenAI plugin.

---

## 5. System prompt and call context

On `bot.start` the plugin builds a system prompt from a template by
interpolating the same call-context fields and CRM data as the OpenAI
plugin — see the [OpenAI integration guide §5](../openai/docs/openai-integration.md#5-system-prompt-and-call-context)
for the full variable mapping. The interpolated prompt is sent in
`session.update.session.instructions`.

The persona in `system_prompt.md` ("Grok, an Innovation Hub specialist")
is a working example — replace it with your own persona. The variable set
is the operative contract between the Infinity workflow and the system
prompt template.

**Two prompt patterns are load-bearing on `grok-voice-think-fast-1.0`**
and should not be removed when adapting the prompt — see §13 for the full
rationale:

- **Inverted step order in protocol sections.** The prompt must instruct
  the model to invoke the tool first, then speak the acknowledgment. This
  is the opposite of the OpenAI prompt's step order.
- **Plain text only in protocol sections.** No backtick-quoted tool names,
  no bold markdown. Tool names appear as bare identifiers.

---

## 6. Audio format

xAI Grok Voice supports `audio/pcmu` (µ-law 8 kHz) natively on input and
output. PCMU is the zero-transcode path; other Infinity codecs require
conversion — the transcoding chain is identical to the OpenAI plugin. See
the [OpenAI integration guide §6](../openai/docs/openai-integration.md#6-audio-format)
for the full codec table.

Configure your Infinity AI Media Gateway profile to negotiate PCMU for the
zero-transcode path.

**Audio format declaration differs from OpenAI.** xAI's `session.update`
uses a nested `audio.input.format` / `audio.output.format` block with
`type` and `rate` keys, vs OpenAI's flat `input_audio_format` /
`output_audio_format` strings. The bridge handles this difference
transparently — no integrator action required.

---

## 7. OpenAI Realtime API compatibility

xAI's Realtime API is wire-compatible with OpenAI Realtime — most event
types are identical (`session.created`, `session.updated`,
`response.created`, `response.output_audio.delta`,
`response.function_call_arguments.done`, etc.). The plugin re-uses the
same intercept patterns as the OpenAI provider.

Known event-shape differences:

- **Audio format declaration in `session.update`** — nested block vs flat
  strings (see §6).
- xAI's documentation describes some response/event differences in
  text-only response paths; the plugin handles audio-only flows and has
  not exercised those paths.

Partners using OpenAI client libraries can typically switch to xAI by
changing the WebSocket base URL and re-formatting the `session.update`
audio block. Persona prompts must be re-tuned per §13 — the prompt
patterns that work on OpenAI do not transfer directly to Grok.

---

## 8. Tool registration via `session.update`

The plugin registers two function tools in the opening `session.update`
message:

| Tool | Purpose |
|---|---|
| `transfer_to_agent` | Hand off to a live human agent. Optional `reason` parameter. |
| `end_session` | End the call after the caller's need is fully resolved. Optional `reason` parameter. |

Both tools use the same intercept pattern. The handler maintains an
`is_terminal_tool` flag — when `True`, the bridge sends the
`function_call_output` acknowledgment back to xAI but suppresses the
post-tool `response.create`. Without this suppression, Grok generates a
continuation utterance after `function_call_output` that lands after the
handoff has been emitted and Infinity has cut the bot leg, leaking a
stranded transcript turn.

---

## 9. Live agent handoff

Identical wire shape to the OpenAI plugin. When the model invokes
`transfer_to_agent`, the plugin stashes a `LIVE_AGENT_HANDOFF` payload,
suppresses the post-tool `response.create`, drains the preamble audio
playout, then emits `bot.feature LIVE_AGENT_HANDOFF` followed by
`bot.ended`. See the [OpenAI integration guide §8](../openai/docs/openai-integration.md#8-live-agent-handoff)
for the full payload shape and drain rationale — the behavior is the same.

Queue routing is workflow-controlled by design — same pattern as OpenAI
and Gemini.

---

## 10. Self-service complete

Identical wire shape to the OpenAI plugin. When the model invokes
`end_session`, the plugin sets a pending session-end flag, suppresses the
post-tool `response.create`, drains preamble audio, then emits `bot.ended`
with success-context status carrying the `XAI:` description prefix. See
the [OpenAI integration guide §9](../openai/docs/openai-integration.md#9-self-service-complete)
for the full payload shape.

---

## 11. Transcripts

Same shape as the OpenAI plugin — `response.output_audio_transcript.delta` /
`.done` accumulators for bot speech,
`conversation.item.input_audio_transcription.completed` for caller speech,
both emitted as `bot.feature TRANSCRIPT` events with turn-start
timestamps. See the [OpenAI integration guide §10](../openai/docs/openai-integration.md#10-transcripts)
for the full shape.

---

## 12. Interruption / barge-in

Same pattern as OpenAI: `input_audio_buffer.speech_started` while the bot
is generating triggers `response.cancel` plus immediate cancellation of
queued return audio. The barge-in gate is keyed off whether audio is
actively playing out to the caller rather than whether the model is
actively generating — because Grok finishes generating responses several
seconds before the audio finishes playing out.

---

## 13. Known limitations

- **Tool-call firing is sensitive to prompt structure on
  `grok-voice-think-fast-1.0`.** Empirical testing established four
  conditions for reliable tool firing on this model. Failing to satisfy
  any of them can suppress `response.function_call_arguments.done` events,
  leaving the model speaking the acknowledgment without firing the tool:

  1. **Inverted step order in protocol sections.** The persona prompt must
     instruct the model to invoke the tool first, then speak the
     acknowledgment. The speak-first / invoke-second pattern that works on
     OpenAI suppresses tool firing on Grok.

  2. **Passive tool description with concrete keyword-list trigger.**
     Tool descriptions must use surface phrases callers actually speak
     (e.g., `"Call this when the caller indicates they are done,
     satisfied, all set, or ready to hang up."`) rather than abstract
     semantic predicates (e.g., `"Call this when the caller has fully
     resolved their need."`). Imperative directives in descriptions
     ("MUST", "Do not...") are confirmed harmful — they get routed into
     conversation context rather than tool metadata and can suppress
     firing.

  3. **Schema parallels existing working tool.** Use an optional `reason`
     parameter rather than empty `properties: {}`. Tool-shape parity
     removes one variable from Grok's tool-selection logic.

  4. **Plain text in the persona-prompt protocol sections.** No backticks
     around tool names, no bold markdown. Tool names appear as bare
     identifiers. The Transfer Protocol and End Call Protocol sections in
     `system_prompt.md` follow this pattern.

  The tool definitions and `system_prompt.md` in this directory satisfy
  all four conditions. If you adapt the persona prompt or add new tools,
  preserve these patterns.

- **First-attempt tool firing is probabilistic even with all four
  conditions satisfied.** Observed first-attempt firing rate is
  approximately 80% on `grok-voice-think-fast-1.0`. When the first attempt
  misses, the model speaks the acknowledgment without firing the tool; on
  the caller's re-prompt, the tool fires reliably. If the failure rate
  exceeds tolerance in production, the next move is to try the mitigation
  directions in the OpenAI integration guide §13 and escalate to xAI with
  your observed data.

- **Telephony urgency directives suppress tool firing.** Phrases like
  "respond immediately — silence is indistinguishable from a dropped call"
  bias the model toward continuous speech generation and away from
  non-speech tool emission. Do not add this kind of directive to the
  persona prompt.

- **Pre-tool reasoning gap.** Grok's background-reasoning window means the
  model emits a `response.created` event approximately 2.5–3 seconds before
  the first audio chunk on tool-call turns. The bridge's drain logic
  accommodates this — no integrator action required — but it surfaces as a
  noticeable pause for the caller on transfer turns.

- **DTMF not handled.** Same as OpenAI — see the
  [OpenAI integration guide §13](../openai/docs/openai-integration.md#13-known-limitations).

- **No customizable greeting trigger logic.** Same as OpenAI — the plugin
  sends a fixed proactive `response.create` after `session.updated`.
  Greeting variation must be encoded in the persona prompt.
