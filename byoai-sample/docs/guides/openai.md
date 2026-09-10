# OpenAI Realtime Provider Guide

> This guide assumes you have completed the Builder's Guide (Part 1) and have
> a working bridge with Echo confirmed end-to-end. The Infinity configuration
> you built in Part 1 carries forward unchanged.

---

## §1 Overview

The OpenAI integration connects Avaya Infinity to the OpenAI Realtime API —
a low-latency, audio-native conversational model. The bridge is responsible
for the complete agent experience: it composes the system prompt, registers
the tools the model can invoke, manages the audio pipeline, and handles all
termination paths.

What the bridge controls:

- System prompt — composed from your configuration and caller context,
  delivered at connection time via `session.update`
- Tool definitions — `transfer_to_agent` and `end_session` are
  bridge-implemented; the OpenAI Realtime API has no platform tool registry
- Voice — set via `OPENAI_VOICE`
- Model — set via `OPENAI_MODEL` or per-call via `botId`
- Opening greeting trigger

What you control through configuration:

- The system prompt content and the caller context fields it references
- Voice selection
- Model selection
- The caller context your Infinity workflow passes to the bridge

There is no external agent dashboard. Everything the model knows about its
role and the caller comes from the bridge configuration and the Infinity
workflow.

---

## §2 Prerequisites

**OpenAI account and API key:**
- An OpenAI account with Realtime API access
- An OpenAI API key

**Bridge configuration:**
```
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-realtime-2.1
OPENAI_VOICE=cedar
OPENAI_SYSTEM_PROMPT_FILE=/path/to/your/system_prompt.md
```

Set these in `bridge/.env`. `OPENAI_MODEL` and `OPENAI_VOICE` have working
defaults and `OPENAI_SYSTEM_PROMPT_FILE` is optional — see §3 for the full
prompt resolution chain.

---

## §3 The system prompt

The system prompt defines agent behavior — persona, tool-use protocol, and
how the agent handles the caller context it receives. The bridge composes it
before every call and delivers it to the model in the `session.update` frame
at connection time.

### Providing a system prompt

The recommended approach is `OPENAI_SYSTEM_PROMPT_FILE` — set this to the
path of a markdown file you maintain outside the repo:

```
OPENAI_SYSTEM_PROMPT_FILE=/etc/bridge/openai_system_prompt.md
```

This file is yours. It survives bridge updates without merge conflicts and
supports multi-line prompts, structured instructions, and persona definitions.

For local development and smoke testing, `OPENAI_SYSTEM_PROMPT` accepts an
inline single-line value:

```
OPENAI_SYSTEM_PROMPT="You are Cedar, a helpful customer service agent."
```

**Precedence** (first non-empty wins):
1. `OPENAI_SYSTEM_PROMPT_FILE`
2. `OPENAI_SYSTEM_PROMPT`
3. `providers/openai/system_prompt.md` alongside the bridge code
4. Built-in minimal fallback

**Out of the box:** if you've cloned this repo and set only `OPENAI_API_KEY`,
the bridge resolves to tier 3 — the sample prompt at
`providers/openai/system_prompt.md`. That is a working starting point. Open
it and read it before your first call so you know what the agent will
sound like.

### Placeholder syntax

The bridge injects caller context into the prompt using Python
`str.format()` substitution. Use **single-brace** placeholders:

```
The caller's name is {firstName}. Their case ID is {caseId}.
Respond in {language}.
```

**Important:** the bridge passes a fixed set of eleven keys to the prompt
template. Only these placeholders are supported:

| Placeholder | Source | Example |
|---|---|---|
| `{firstName}` | `context.firstName` | `"Alex"` |
| `{lastName}` | `context.lastName` | `"Chen"` |
| `{email}` | `context.email` | `"alex@example.com"` |
| `{caseId}` | `context.caseId` | `"CS-10482"` |
| `{caseSubject}` | `context.caseSubject` | `"Billing inquiry"` |
| `{caseDescription}` | `context.caseDescription` | `"Customer question about..."` |
| `{direction}` | `payload.direction` | `"INBOUND"` |
| `{from_num}` | `payload.from` | `"+12068524641"` |
| `{to_num}` | `payload.to` | `"+17207940219"` |
| `{ucid}` | `payload.ucid` | `"15446593841778164126"` |
| `{language}` | `payload.language` | `"English"` (human-readable) |

Custom IVA parameters beyond these eleven are not available as placeholders.
Adding `{accountTier}` to your prompt template without a corresponding bridge
code change will either render as the literal string `{accountTier}` or trip
the prompt fallback. See §9 Known Behaviors.

### Tool instructions

The bridge registers `transfer_to_agent` and `end_session` tools with the
model at connection time. Your system prompt must include instructions for
when and how to invoke each — and the wording is load-bearing for correct
behavior.

**For `transfer_to_agent`**, the prompt must instruct the model to speak an
acknowledgment first, then invoke the tool:

```
When the caller requests a human agent:
1. First, speak your transfer acknowledgment as a single complete utterance,
   audibly and in full.
2. Immediately after the utterance ends, invoke the transfer_to_agent tool.
3. After invoking the tool, do not generate any further response.
   Wait silently.
```

The order matters. The bridge waits for the acknowledgment audio to finish
before emitting the handoff to Infinity — if the agent invokes the tool
without speaking first, the caller hears silence before the transfer. If the
agent continues speaking after invoking the tool, the caller hears a duplicate
acknowledgment. See §7 for the full handoff sequence.

**For `end_session`**, instruct the agent on when to use it:

```
When the caller's need is fully resolved and no further assistance is
required, invoke end_session. After invoking the tool, do not generate
any further response. Wait silently.
```

The sample prompt at `providers/openai/system_prompt.md` demonstrates both
patterns in its Transfer Protocol and End Call Protocol sections.

---

## §4 Context injection

Caller context flows from your Infinity workflow into the OpenAI system
prompt via `{placeholder}` substitution at connection time.

**End-to-end example:**

Your Infinity workflow sets these variables before the IVA module:

```
firstName       = "Alex"
caseId          = "CS-10482"
caseSubject     = "Billing inquiry"
engagementId    = {{engagementId}}
```

These are mapped in the IVA module's custom parameters (see §8). The bridge
receives them in `bot.start.payload.context`. A prompt containing:

```
The caller is {firstName}, case {caseId}: {caseSubject}.
```

becomes:

```
The caller is Alex, case CS-10482: Billing inquiry.
```

`engagementId` is available in `payload.context` and in `byobotEndContext`
for post-call correlation — but it is not one of the eleven bridge-fixed
placeholder keys, so it is not directly substitutable in the prompt template
without a bridge code change.

---

## §5 Phase 1 — Connection

When Infinity routes a call to the OpenAI provider, the bridge follows
this sequence:

1. `bot.start` arrives with model (from `botId` suffix or `OPENAI_MODEL`),
   API key (from `OPENAI_API_KEY`), and caller context
2. Bridge opens a WebSocket to OpenAI Realtime:
   `wss://api.openai.com/v1/realtime?model=<model>`
3. Bridge sends `session.update` carrying the composed prompt, voice, tool
   definitions, audio formats, and VAD configuration
4. OpenAI confirms with `session.updated`
5. Bridge sends a `response.create` to trigger the opening greeting
6. Bridge sends `bot.started` to Infinity — the call is live

If the API key is missing or invalid, the bridge emits `bot.ended` with
`status.code: 503` (`BACKEND_START_FAILED`) and the Infinity workflow takes
its FAILED branch.

**botId format:**
```
openai:<model>
```

Example: `openai:gpt-realtime-2.1`

`OPENAI_MODEL` env var takes precedence over the `botId` suffix when set —
useful for pinning a model across all calls in a deployment.

**Codec note:** OpenAI Realtime uses µ-law audio natively. Starting the
bridge with `--codec PCMU` eliminates bidirectional transcoding for OpenAI
calls. Other codecs (G722, PCMA, L16) are supported but require the bridge
to transcode.

### Session payload shape

The bridge sends a single `session.update` after the WebSocket opens.
The payload is structured per the OpenAI Realtime API spec:

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "instructions": "<composed system prompt>",
    "audio": {
      "input": {
        "format": {"type": "audio/pcmu"},
        "turn_detection": {"type": "server_vad"},
        "transcription": {"model": "whisper-1"}
      },
      "output": {
        "format": {"type": "audio/pcmu"},
        "voice": "<voice from OPENAI_VOICE env, default: cedar>"
      }
    },
    "tools": [/* transfer_to_agent, end_session */],
    "tool_choice": "auto"
  }
}
```

Key shape requirements:

- **`session.type: "realtime"`** is the API's session discriminator and
  must be present.
- **Audio configuration is nested under `session.audio`** with `input`
  and `output` sub-objects. `format` is an object with a MIME-style
  `type` key (`audio/pcmu` for µ-law 8 kHz, the default codec OpenAI
  Realtime emits and accepts). `turn_detection` and `transcription`
  live under `audio.input`.
- **`tools` and `tool_choice` are top-level fields of `session`**,
  alongside `audio`, `instructions`, and `type`.
- **Current model name: `gpt-realtime-2.1`**. The `gpt-realtime` alias is removed from the API on 2027-01-20.

### Server events handled by the bridge

After `session.updated` is received, the bridge dispatches on these
event types from OpenAI:

| Event | What the bridge does with it |
|---|---|
| `response.created` | Starts a new bot turn; arms the playout pipeline |
| `response.output_audio.delta` | Decodes base64 audio chunk and forwards to Infinity ingress |
| `response.output_audio.done` | Marks segment complete; signals end-of-utterance to ingress streamer |
| `response.output_audio_transcript.delta` | Accumulates bot transcript text per response ID |
| `response.output_audio_transcript.done` | Flushes bot transcript as a `bot.feature TRANSCRIPT` envelope |
| `conversation.item.input_audio_transcription.completed` | Emits caller transcript as `bot.feature TRANSCRIPT` |
| `input_audio_buffer.speech_started` | Triggers barge-in: cancels response, clears ingress queue |
| `response.function_call_arguments.done` | Dispatches `transfer_to_agent` or `end_session` tool handler |
| `error` | Logged at ERROR level (see §9 for the `response_cancel_not_active` race) |

---

## §6 Phase 2 — The conversation

### Audio

The bridge handles transcoding between Infinity's negotiated codec and
OpenAI's native µ-law format. For PCMU deployments, audio passes through
without resampling. No audio configuration is required.

The agent sends its opening greeting after `session.updated` is confirmed.
The bridge buffers any greeting audio that arrives before Infinity's audio
path is ready, then delivers it without loss.

### Barge-in

OpenAI's server-side voice activity detection drives barge-in. When the
platform detects caller speech while the agent is speaking, it sends an
`input_audio_buffer.speech_started` event. The bridge:

1. Clears any queued agent audio
2. Flushes the IngressStreamer queue
3. Sends a `response.cancel` to OpenAI to stop any active responses

No bridge-side voice activity detection is involved.

### Transcripts

The bridge accumulates OpenAI's transcript events per turn and flushes one
`TRANSCRIPT` envelope per speaker per turn on turn completion. Tool-only
turns do not produce transcript bubbles.

---

## §7 Phase 3 — Closing the call

Every OpenAI call ends through one of four paths.

### Self-service complete

The agent invoked the bridge's `end_session` tool. The bridge waits for
any queued closing audio to finish playing, then emits `bot.ended` with
`status.code: 200` (`ENDPOINT_RELEASED`). The Infinity workflow takes its
SUCCESSFUL branch.

Read `byobotEndContext.status.code === 200` in your workflow Decision module
to route to the post-call flow.

### Live agent handoff

The agent invoked the bridge's `transfer_to_agent` tool. The sequence
depends on the two-layer protocol described in §3:

1. The agent speaks its transfer acknowledgment (prompt-driven — the model
   speaks before invoking the tool)
2. OpenAI sends the `transfer_to_agent` tool call
3. Bridge stashes the handoff payload and sends the tool result to OpenAI
   immediately — **without** soliciting a follow-up response (suppressing
   `response.create` prevents the model from generating a duplicate
   acknowledgment)
4. Bridge waits for the acknowledgment audio to finish playing
5. Bridge flushes any stranded transcripts from the trigger turn
6. Bridge emits `bot.feature LIVE_AGENT_HANDOFF` to Infinity with the
   reason from the tool call
7. Bridge emits `bot.ended` with no `status` field
8. Infinity workflow reads `byobotLiveAgentHandoff` and routes to the
   agent queue

**The prompt instructions in §3 are load-bearing for this path.** If the
"speak first, then invoke" instruction is removed from the system prompt,
the agent invokes the tool silently and the caller hears nothing before the
transfer. If the "do not generate any further response" instruction is
removed, the caller hears a duplicate acknowledgment during the drain window.

**`queueId`:** the bridge does not stamp a queue ID on the handoff payload.
The Infinity workflow's HANDOFF exit owns queue routing.

### Caller disconnect

The caller hangs up. Infinity sends `session.end`. The bridge emits
`bot.ended` with `reason: "CALLER_DISCONNECTED"` and cleans up the OpenAI
session.

### Failure

Something went wrong. Common causes:

| Condition | `status.code` | `reason` |
|---|---|---|
| API key missing or invalid | `503` | `BACKEND_START_FAILED` |
| OpenAI WebSocket connection failed | `503` | `BACKEND_START_FAILED` |
| Unhandled bridge exception mid-call | `500` | `INTERNAL_ERROR` |

Read `byobotEndContext.status.code` in your Decision module and route
non-200 values to your failure recovery path.

---

## §8 Workflow

Import the reference workflow from `infinity-workflow/openai/workflow.json`
and follow the README in that directory for post-import configuration
steps. The workflow is preconfigured with `botId =
openai:gpt-realtime-2.1` and the standard nine
`customParameters` populated from CRM-style workflow variables.

**IVA module custom parameters (already set in the imported workflow):**

| Parameter | Value |
|---|---|
| `botId` | `openai:gpt-realtime-2.1` (override per call by editing this field, or pin via `OPENAI_MODEL` env var) |
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

## §9 Known behaviors

**Prompt placeholder surface is fixed at eleven keys**

The bridge passes exactly eleven keys to the prompt template. Custom IVA
parameters beyond `firstName`, `lastName`, `email`, `caseId`,
`caseSubject`, `caseDescription`, `direction`, `from_num`, `to_num`,
`ucid`, and `language` are not available as `{placeholder}` substitutions.
Adding an unsupported placeholder will either render as a literal string or
trip the prompt fallback. To add a new key requires editing
`_compose_instructions` in `providers/openai/bot_openai.py`.

**Transfer prompt instructions are load-bearing**

The `transfer_to_agent` handoff path depends on two prompt instructions
working together: the agent must speak before invoking the tool, and must
not generate further output after invoking it. If you fork
`providers/openai/system_prompt.md`, preserve both instructions in your
Transfer Protocol section. Removing either one produces incorrect caller
experience at the handoff moment.

**`OPENAI_MODEL` env var overrides the `botId` suffix**

When `OPENAI_MODEL` is set, every call uses that model regardless of what
the IVA module passes in `botId`. Useful for pinning a model across a
deployment. Leave it unset to select the model per call via the suffix.

**No provider session cap**

OpenAI Realtime does not send a session-expiry notice the way some other
providers do. Long calls end via the standard connection-closed path if
OpenAI's session limits are reached.

**`response_cancel_not_active` ERROR on rapid barge-in is benign**

When a caller barges in just as the bot finishes its turn, the bridge
issues `response.cancel` for the active response. If the response
completes naturally in the same window, OpenAI returns an `error` event:

```
type=invalid_request_error code=response_cancel_not_active
message=Cancellation failed: no active response found
```

This surfaces as an ERROR-level log in the bridge journal. It is a known
race between barge-in and natural response completion — audio flow
continues normally. No action required.

---

## §10 Backlog

Open improvements identified during development that are not yet
addressed:

- **Cedar system prompt — add a "What you don't know" section.** During
  S5 extended-session validation, Cedar hallucinated that "MCP" stands
  for "Avaya's Media Control Platform" when asked about the Model Context
  Protocol. The current `providers/openai/system_prompt.md` does not
  instruct Cedar to defer rather than guess on technical acronyms not
  explicitly covered in the prompt. Adding a brief "What you don't know"
  section to the system prompt would mitigate this class of hallucination
  for partner demos that wander into adjacent technical topics.

---

*Back to [Builder's Guide](../../BUILDERS_GUIDE.md) · Next: [OpenAI Workflow README](../../infinity-workflow/openai/README.md)*
