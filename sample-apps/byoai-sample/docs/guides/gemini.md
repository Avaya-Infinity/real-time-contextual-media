# Gemini Provider Guide

> This guide assumes you have completed the Builder's Guide (Part 1) and have
> a working bridge with Echo confirmed end-to-end. The Infinity configuration
> you built in Part 1 carries forward unchanged.

---

## §1 Overview

Google Gemini Live is a **bridge-orchestrated** integration. Unlike ElevenLabs
(where the agent is a fully-configured entity on the ElevenLabs platform),
Gemini Live exposes the model directly through a bidirectional WebSocket —
no dashboard, no platform-side prompt, no native tools, no first-message
field. The bridge owns every piece of orchestration.

What the bridge controls:

- The **system prompt** the model receives (composed at bridge startup
  from one of four sources, see §3)
- **Tool definitions** (`transfer_to_agent` and `end_session` are bridge-
  implemented; Gemini has no platform tools)
- The **proactive greeting trigger** that makes the agent speak first
- **Voice selection** (via `GEMINI_VOICE` env var)
- **Model selection** (via `GEMINI_MODEL` env var or `botId` suffix)

What Google Gemini provides:

- Native audio understanding and generation
- The conversational language model itself
- Voice synthesis (one of eight prebuilt voices)
- Live transcript streams (input and output)

This division means: customizing what the agent says or how it behaves
happens in the bridge — by editing a system prompt file or setting an
env var. There is no Gemini dashboard to configure.

---

## §2 Prerequisites

**Google AI Studio account and API key:**
- A Google AI Studio account with Gemini API access
- A Gemini API key (free tier available at https://aistudio.google.com/apikey)

**Bridge configuration:**
```
GEMINI_API_KEY=your-api-key
```

Set this in `bridge/.env`. Model and voice have working defaults — no
configuration required for a first call.

---

## §3 The system prompt

The base persona, tool-use protocol, and call context all live in a
single system prompt that the bridge sends in the connection-time
`setup` frame, before any audio.

The bridge resolves the base prompt from a four-tier precedence chain
(highest precedence first):

| Tier | Source | When used |
|---|---|---|
| 1 | `GEMINI_SYSTEM_PROMPT_FILE` env var | Path to a markdown file. Overrides everything else when set. |
| 2 | `GEMINI_SYSTEM_PROMPT` env var | Single-line inline prompt. Used when tier 1 is unset. |
| 3 | `providers/gemini/system_prompt.md` (sibling file) | The repo-tracked default that ships with this guide. |
| 4 | Built-in minimal fallback | `"You are a helpful customer service agent."` — only reached if tiers 1-3 all fail. |

**Recommended path for partners:** set `GEMINI_SYSTEM_PROMPT_FILE` to a
markdown file that lives outside source control (the deployment
convention is `/opt/bridge-server/gemini_system_prompt.md`, alongside
`.env`). This lets you edit the prompt in place without touching the
repo, and the bridge re-reads the file at every `bot.start` so changes
apply without a service restart.

**Out of the box:** if you've cloned this repo and set only
`GEMINI_API_KEY`, the bridge resolves to tier 3 — the sample prompt
at `providers/gemini/system_prompt.md`. That is a working starting
point. Open it and read it before your first call so you know what
the agent will sound like.

### Placeholder syntax

The bridge interpolates placeholders into the resolved base prompt at
every call using **regex substitution** (not Python `str.format()` — that
matters for the syntax). Use double-brace placeholders:

```markdown
The caller's name is {{firstName}}. Their case ID is {{caseId}}.
You are speaking in {{language}}.
```

**Available placeholders:**

The bridge always provides these from the `bot.start.payload`:

| Placeholder | Source | Notes |
|---|---|---|
| `{{language}}` | `payload.language` | Resolved to a human-readable name (e.g. `"English"` not `"en-US"`) |

Plus **every key in `bot.start.payload.context`** is available as a
placeholder. From the IVA module's custom parameters in your Infinity
workflow:

| Placeholder | Typical source |
|---|---|
| `{{firstName}}` | CRM caller name |
| `{{lastName}}` | CRM caller name |
| `{{email}}` | CRM caller email |
| `{{caseId}}` | Active case |
| `{{caseSubject}}` | Case subject line |
| `{{caseDescription}}` | Case description |
| `{{engagementId}}` | Avaya engagement identifier |
| `{{workflowSessionId}}` | Workflow execution identifier |
| *(any other key in `customParameters`)* | Whatever you pass |

Missing placeholders interpolate as empty strings — so a partial CRM
lookup still produces a usable prompt.

### Always-appended call context

After interpolating placeholders, the bridge appends one fixed line at
the end of every prompt:

```
Call context: direction=INBOUND, from=+12068524641, to=+17207940219, ucid=15446593841778164126, language=en-US
```

This gives the model wire-level call metadata regardless of whether
your base prompt references those values. You don't need to include it
in your template.

---

## §4 Phase 1 — Connection

When Infinity routes a call to the Gemini provider, the bridge:

1. Receives `bot.start` with the model from the `botId` suffix (or
   `GEMINI_MODEL` env override) and caller context
2. Opens a WebSocket to Gemini Live:
   `wss://generativelanguage.googleapis.com/.../BidiGenerateContent?key=<api_key>`
3. Sends a `setup` frame containing:
   - The resolved + interpolated **system prompt**
   - **Tool definitions** for `transfer_to_agent` and `end_session`
   - **Voice selection** (`generation_config.speech_config`)
   - **Both transcript streams enabled**
   - `thinking_config: {thinking_level: "minimal"}` for lowest-latency
     real-time voice
4. Receives `setupComplete` from Gemini
5. **Sends a proactive trigger** — `realtimeInput.text = "Hello, please
   greet the customer now."` — so the agent speaks first. Gemini Live
   has no first-message field; without this trigger the agent stays
   silent until the caller speaks. The trigger string is fixed in the
   bridge; partners cannot customize it without a code change.
6. Sends `bot.started` to Infinity — the call is live

If the API key is missing or invalid, the bridge emits `bot.ended`
with `status.code: 503` (`BACKEND_START_FAILED`) and the Infinity
workflow takes its FAILED branch.

**botId format:**
```
gemini:<model>
```

Example: `gemini:gemini-3.1-flash-live-preview` (the bridge default if
you don't override). The `gemini:` prefix is required; the model
suffix is overridden by `GEMINI_MODEL` env when set (env wins).

---

## §5 Phase 2 — The conversation

### Audio

Gemini Live uses different sample rates for input and output:
- **Input** (caller → Gemini): 16 kHz S16LE PCM
- **Output** (Gemini → caller): **24 kHz** S16LE PCM

The bridge handles transcoding bidirectionally between Infinity's
negotiated codec (G722, PCMU, PCMA, or L16) and these rates. No
configuration is required.

### Output pacer

The bridge buffers Gemini's audio chunks against an output pacer
aligned to Infinity's ingress chunk size, then flushes a sub-100ms
tail when a turn completes — so the end of a sentence plays out
cleanly instead of being held for the next pacer tick.

### Greeting

Because Gemini has no first-message field, the bridge sends a fixed
proactive trigger on `setupComplete` to make the agent speak first.
What the agent actually says in the greeting is shaped by your system
prompt — write your prompt to instruct the agent on opening behavior.

The sample prompt (tier 3) demonstrates this:

> "When the call connects and you have not yet spoken, your first
> response is a greeting. Reference the caller's name and case context
> naturally..."

### Barge-in

When the caller speaks while the agent is speaking, Gemini sends a
`serverContent.interrupted` flag. The bridge clears any queued agent
audio and the caller takes the floor. No bridge-side voice activity
detection is involved — Gemini drives detection.

### Transcripts

Gemini emits `inputTranscription` and `outputTranscription` as
streaming partials. The bridge accumulates them per turn and flushes
one TRANSCRIPT envelope per speaker per turn on `turnComplete` or
`generationComplete`. Tool-only turns (when the agent invokes
`transfer_to_agent` or `end_session`) skip the transcript flush so
your workflow doesn't see blank transcript bubbles.

### Provider goAway

Gemini Live sessions have a 15-minute hard cap. Approaching the cap,
Gemini sends a `goAway` notice. The bridge:
1. Emits a `bot.feature` envelope to Infinity with
   `ftype: PROVIDER_GOAWAY` carrying the time remaining
2. Marks the call inactive so no further audio is forwarded
3. **Does not** originate `bot.ended` — Infinity drives the
   termination via `session.end`

This is unique to Gemini. Plan for it in long-running workflows: read
`PROVIDER_GOAWAY` events in your workflow if you need to act on the
imminent disconnect.

---

## §6 Phase 3 — Closing the call

Every Gemini call ends through one of four paths. **Gemini has no
platform-side termination tools**, so the bridge implements both
self-service-complete and live-agent-handoff via tool calls.

### Self-service complete

The agent invoked the bridge's `end_session` tool. The bridge:
1. Stashes a deferred-end latch and starts a drain task
2. Waits for queued audio (the closing line) to play out
3. Emits `bot.ended` with `status.code: 200` (`ENDPOINT_RELEASED`)
4. Infinity workflow takes its SUCCESSFUL branch

Read `byobotEndContext.status.code === 200` in your workflow Decision
module to route to the post-call flow.

The `end_session` tool is bridge-defined — no schema configuration is
required on your side. Its behavior is shaped by your system prompt:
the prompt should instruct the agent on when to use it (typically
after a clear "I'm all set" / "that's all I needed" signal). The
sample prompt has a complete End Call Protocol section demonstrating
the pattern.

### Live agent handoff

The agent invoked the bridge's `transfer_to_agent` tool with an
optional `reason` parameter. The sequence:

1. Gemini sends a `toolCall` with `functionCalls[].name = "transfer_to_agent"`
2. Bridge stashes the handoff payload, replies to Gemini immediately
   with `toolResponse` so the agent can keep speaking the
   transfer-acknowledgment line
3. Bridge waits for queued agent audio to finish playing
4. Bridge **flushes any stranded transcripts** from the trigger turn
   (Gemini's tool calls preempt `turnComplete`, so the bridge
   explicitly flushes here so your call record has the customer's
   handoff request and the agent's acknowledgment)
5. Bridge emits `bot.feature LIVE_AGENT_HANDOFF` to Infinity with the
   `reason` from the tool call
6. Bridge emits `bot.ended` with no `status` field
7. Infinity workflow reads `byobotLiveAgentHandoff` and routes to the
   agent queue

**Note on `queueId`:** Gemini's bridge implementation does not stamp a
`queueId` on the handoff payload. The Infinity workflow's HANDOFF
exit owns queue routing. The `GEMINI_HANDOFF_QUEUE_ID` env var is
reserved for a future enhancement that would let the bridge stamp a
specific queue.

### Caller disconnect

The caller hangs up. Infinity sends `session.end`. The bridge emits
`bot.ended` with `status.context.reason = "CALLER_DISCONNECTED"` and
the workflow takes its FAILED branch (or routes by reason if your
workflow distinguishes disconnect from other failures).

### Failure

Something went wrong. Common causes:

| Condition | `status.code` | `reason` |
|---|---|---|
| API key missing or invalid | `503` | `BACKEND_START_FAILED` |
| Gemini WebSocket connection failed | `503` | `BACKEND_START_FAILED` |
| Codec negotiated that bridge cannot transcode | `503` | `BACKEND_START_FAILED` |
| Unhandled bridge exception mid-call | `500` | `INTERNAL_ERROR` |

Read `byobotEndContext.status.code` in your Decision module and route
non-200 values to your failure recovery path.

---

## §7 Workflow

Import the reference workflow from `infinity-workflow/gemini/workflow.json`
and follow the README in that directory for post-import configuration
steps. The workflow is preconfigured with `botId =
gemini:gemini-3.1-flash-live-preview` and the standard nine
`customParameters` populated from CRM-style workflow variables.

**IVA module custom parameters (already set in the imported workflow):**

| Parameter | Value |
|---|---|
| `botId` | `gemini:gemini-3.1-flash-live-preview` (override per call by editing this field, or pin via `GEMINI_MODEL` env var) |
| `firstName` | `{{firstName}}` |
| `lastName` | `{{lastName}}` |
| `email` | `{{email}}` |
| `caseId` | `{{caseId}}` |
| `caseSubject` | `{{caseSubject}}` |
| `caseDescription` | `{{caseDescription}}` |
| `engagementId` | `{{engagementId}}` |
| `workflowSessionId` | `{{workflowSessionId}}` |

After import you'll need to:

1. Set the IVA module **Connection** to your AI Media Gateway profile
   (the same one you configured in §7 of the Builder's Guide)
2. Configure the **Create Interaction** module's queue or user for the
   FAILED and HANDOFF exit paths (queue IDs are stripped on workflow
   export)
3. Assign a phone number to this workflow in **Admin Dashboard →
   Voice → Numbers**

**Closure branch wiring:**

| IVA branch | Route to |
|---|---|
| SUCCESSFUL (code 200) | Post-call flow — survey, disconnect, callback |
| SUCCESSFUL (non-200) | Failure recovery — error message, queue transfer |
| HANDOFF | Agent queue — read `byobotLiveAgentHandoff` |
| FAILED | Failure recovery |

Use a Decision module on the SUCCESSFUL branch to split on
`byobotEndContext.status.code`.

---

## §8 Known behaviors

**The greeting trigger string is fixed.** The bridge sends `"Hello,
please greet the customer now."` to Gemini on every call to make the
agent speak first. Partners cannot customize this string without
editing `bot_gemini.py`. The greeting *content* is shaped by your
system prompt — write your prompt to instruct the agent on what to
say when invited to greet.

**`GEMINI_MODEL` env var overrides the botId suffix.** When
`GEMINI_MODEL` is set in the environment, every call uses that model
regardless of what the IVA module passes in `botId`. Useful for
forcing a specific model in dev. Leave the env var unset to let each
call pick its model via the suffix. (Same precedence rule applies to
`GEMINI_VOICE`.)

**`GEMINI_VOICE` options are fixed prebuilt voices.** Aoede (default),
Puck, Charon, Kore, Fenrir, Leda, Orus, Zephyr. Custom voices are
not supported — Google's API exposes only the prebuilt set on the
Live endpoint.

**15-minute session cap.** Gemini Live sessions terminate after 15
minutes via `goAway`. For long-running workflows, plan to handle the
`PROVIDER_GOAWAY` event — either by warning the caller or initiating
a graceful handoff.

**Placeholder syntax differs from OpenAI/xAI.** Gemini uses
`{{double-brace}}` regex substitution. OpenAI and xAI use
`{single-brace}` Python `.format()` substitution. If you write a
prompt for one provider and copy it to another, update the placeholder
syntax accordingly.

---

*Back to [Builder's Guide](../../BUILDERS_GUIDE.md) · Next: [Gemini Workflow README](../../infinity-workflow/gemini/README.md)*
