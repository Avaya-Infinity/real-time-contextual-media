# xAI Grok Provider Guide

> This guide assumes you have completed the Builder's Guide (Part 1) and have
> a working bridge with Echo confirmed end-to-end. The Infinity configuration
> you built in Part 1 carries forward unchanged.

---

## §1 Overview

The xAI integration connects Avaya Infinity to the xAI Grok Voice API —
a real-time, audio-native conversational model. The bridge is responsible
for the complete agent experience: it composes the system prompt, registers
the tools the model can invoke, manages the audio pipeline, and handles all
termination paths.

What the bridge controls:

- System prompt — composed from your configuration and caller context,
  delivered at connection time via `session.update`
- Tool definitions — `transfer_to_agent` and `end_session` are
  bridge-implemented
- Voice — set via `XAI_VOICE`
- Model — set via `XAI_MODEL` or per-call via `botId`
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

**xAI account and API key:**
- An xAI account with Grok Voice API access
- An xAI API key

**Bridge configuration:**
```
XAI_API_KEY=your-api-key
XAI_MODEL=grok-voice-think-fast-2.0
XAI_VOICE=ara
XAI_SYSTEM_PROMPT_FILE=/path/to/your/system_prompt.md
```

Set these in `bridge/.env`. `XAI_MODEL` and `XAI_VOICE` have working
defaults and `XAI_SYSTEM_PROMPT_FILE` is optional — see §3 for the full
prompt resolution chain.

---

## §3 The system prompt

The system prompt defines agent behavior — persona, tool-use protocol, and
how the agent handles the caller context it receives. The bridge composes it
before every call and delivers it to the model in the `session.update` frame
at connection time.

### Providing a system prompt

The recommended approach is `XAI_SYSTEM_PROMPT_FILE` — set this to the
path of a markdown file you maintain outside the repo:

```
XAI_SYSTEM_PROMPT_FILE=/etc/bridge/xai_system_prompt.md
```

This file is yours. It survives bridge updates without merge conflicts and
supports multi-line prompts, structured instructions, and persona definitions.

For local development and smoke testing, `XAI_SYSTEM_PROMPT` accepts an
inline single-line value:

```
XAI_SYSTEM_PROMPT="You are Grok, a helpful customer service agent."
```

**Precedence** (first non-empty wins):
1. `XAI_SYSTEM_PROMPT_FILE`
2. `XAI_SYSTEM_PROMPT`
3. `providers/xai/system_prompt.md` alongside the bridge code
4. Built-in minimal fallback

**Out of the box:** if you've cloned this repo and set only `XAI_API_KEY`,
the bridge resolves to tier 3 — the sample prompt at
`providers/xai/system_prompt.md`. That is a working starting point. Open
it and read it before your first call so you know what the agent will
sound like.

### Placeholder syntax

The bridge injects caller context into the prompt using Python
`str.format()` substitution. Use **single-brace** placeholders:

```
The caller's name is {firstName}. Their case ID is {caseId}.
Respond in {language}.
```

The bridge passes a fixed set of eleven keys to the prompt template.
Only these placeholders are supported:

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
See §9 Known Behaviors.

### Tool instructions

The bridge registers `transfer_to_agent` and `end_session` tools with the
model at connection time. Your system prompt must include instructions for
when and how to invoke each.

**For `transfer_to_agent`**, the Grok model requires a specific invocation
ordering. Your prompt must instruct the model to invoke the tool first,
then speak the acknowledgment:

```
When the caller requests a human agent, within a single response:
1. Invoke the transfer_to_agent tool. This is the mandatory commitment
   for the turn.
2. Speak the acknowledgment as a single complete utterance, audibly
   and in full.
3. The turn is not complete until the tool has fired.
```

This ordering is specific to how Grok handles tool calls in voice responses.
The bridge waits for the acknowledgment audio to finish before emitting the
handoff to Infinity — the tool invocation triggers the drain sequence, and
the acknowledgment plays out during that window. See §7 for the full
handoff sequence.

**For `end_session`**, instruct the agent on when to use it:

```
When the caller's need is fully resolved and no further assistance is
required, invoke end_session. After invoking the tool, do not generate
any further response. Wait silently.
```

**Tool description style:** the tool descriptions registered by the bridge
use factual, passive language. This is intentional for Grok — imperative
phrasing in tool descriptions causes the model to route the description
into conversation context, which suppresses tool dispatch. Do not rewrite
the tool descriptions to use imperative language.

The sample prompt at `providers/xai/system_prompt.md` demonstrates the
correct Transfer Protocol and End Call Protocol patterns for this provider.

---

## §4 Context injection

Caller context flows from your Infinity workflow into the xAI system
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
placeholder keys and is not directly substitutable in the prompt template
without a bridge code change.

---

## §5 Phase 1 — Connection

When Infinity routes a call to the xAI provider, the bridge follows
this sequence:

1. `bot.start` arrives with model (from `botId` suffix or `XAI_MODEL`),
   API key (from `XAI_API_KEY`), and caller context
2. Bridge opens a WebSocket to xAI:
   `wss://api.x.ai/v1/realtime?model=<model>`
3. Bridge sends `session.update` carrying the composed prompt, voice,
   tool definitions, and audio configuration
4. xAI confirms with `session.updated`
5. Bridge sends a `response.create` to trigger the opening greeting
6. Bridge sends `bot.started` to Infinity — the call is live

If the API key is missing or invalid, the bridge emits `bot.ended` with
`status.code: 503` (`BACKEND_START_FAILED`) and the Infinity workflow takes
its FAILED branch.

**botId format:**
```
xai:<model>
```

Example: `xai:grok-voice-think-fast-2.0`

`XAI_MODEL` env var takes precedence over the `botId` suffix when set —
useful for pinning a model across all calls in a deployment.

**Audio format:** xAI uses µ-law audio at 8 kHz natively — the same format
as PCMU telephony. Starting the bridge with `--codec PCMU` aligns Infinity's
codec with xAI's native format, eliminating bidirectional transcoding for
xAI calls. Other codecs (G722, PCMA, L16) are supported but require
the bridge to transcode.

---

## §6 Phase 2 — The conversation

### Audio

xAI's native audio format is µ-law at 8 kHz in both directions. The bridge
handles transcoding between Infinity's negotiated codec and xAI's wire
format automatically. For PCMU deployments, audio passes through without
resampling.

The agent sends its opening greeting after `session.updated` is confirmed.
The bridge buffers any greeting audio that arrives before Infinity's audio
path is ready, then delivers it without loss.

### Barge-in

xAI's server-side voice activity detection drives barge-in. When the
platform detects caller speech while the agent is speaking, it sends an
`input_audio_buffer.speech_started` event. The bridge:

1. Clears any queued agent audio
2. Flushes the IngressStreamer queue
3. Sends a `response.cancel` to xAI to stop any active responses

No bridge-side voice activity detection is involved.

### Transcripts

The bridge accumulates xAI's transcript events per turn and flushes one
`TRANSCRIPT` envelope per speaker per turn on turn completion. Tool-only
turns do not produce transcript bubbles.

**Note:** xAI does not currently document a caller-side transcription
stream. Caller transcripts may not be available from this provider.

---

## §7 Phase 3 — Closing the call

Every xAI call ends through one of four paths.

### Self-service complete

The agent invoked the bridge's `end_session` tool. The bridge waits for
any queued closing audio to finish playing, then emits `bot.ended` with
`status.code: 200` (`ENDPOINT_RELEASED`). The Infinity workflow takes its
SUCCESSFUL branch.

Read `byobotEndContext.status.code === 200` in your workflow Decision module
to route to the post-call flow.

### Live agent handoff

The agent invoked the bridge's `transfer_to_agent` tool. The sequence:

1. The agent invokes `transfer_to_agent` (tool-first — per the prompt
   instructions in §3, the tool fires before the acknowledgment)
2. xAI sends the tool call
3. Bridge stashes the handoff payload and sends the tool result to xAI
   immediately — without soliciting a follow-up response
4. The agent speaks its transfer acknowledgment (audio streams during
   the drain window)
5. Bridge waits for the acknowledgment audio to finish playing
6. Bridge flushes any stranded transcripts from the trigger turn
7. Bridge emits `bot.feature LIVE_AGENT_HANDOFF` to Infinity with the
   reason from the tool call
8. Bridge emits `bot.ended` with no `status` field
9. Infinity workflow reads `byobotLiveAgentHandoff` and routes to the
   agent queue

**The prompt instructions in §3 are load-bearing for this path.** The
tool-first ordering in your Transfer Protocol section is what makes the
acknowledgment audio available during the drain window. If you modify
the system prompt, preserve the Transfer Protocol structure from the
sample prompt.

**`queueId`:** the bridge does not stamp a queue ID on the handoff payload.
The Infinity workflow's HANDOFF exit owns queue routing.

### Caller disconnect

The caller hangs up. Infinity sends `session.end`. The bridge emits
`bot.ended` with `reason: "CALLER_DISCONNECTED"` and cleans up the xAI
session.

### Failure

Something went wrong. Common causes:

| Condition | `status.code` | `reason` |
|---|---|---|
| API key missing or invalid | `503` | `BACKEND_START_FAILED` |
| xAI WebSocket connection failed | `503` | `BACKEND_START_FAILED` |
| Unhandled bridge exception mid-call | `500` | `INTERNAL_ERROR` |

Read `byobotEndContext.status.code` in your Decision module and route
non-200 values to your failure recovery path.

---

## §8 Workflow

Import the reference workflow from `infinity-workflow/xai/workflow.json`
and follow the README in that directory for post-import configuration
steps. The workflow is preconfigured with `botId =
xai:grok-voice-think-fast-2.0` and the standard nine `customParameters`
populated from CRM-style workflow variables.

**IVA module custom parameters (already set in the imported workflow):**

| Parameter | Value |
|---|---|
| `botId` | `xai:grok-voice-think-fast-2.0` (override per call by editing this field, or pin via `XAI_MODEL` env var) |
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
`_compose_instructions` in `providers/xai/bot_xai.py`.

**Transfer prompt instructions are load-bearing**

The `transfer_to_agent` handoff path depends on the tool-first ordering
in your Transfer Protocol prompt section. If you fork
`providers/xai/system_prompt.md`, preserve the Transfer Protocol structure.
Changing the ordering or removing the "turn is not complete until the tool
has fired" anchor produces incorrect caller experience at the handoff moment.

**Tool descriptions must use factual, passive language**

The `transfer_to_agent` and `end_session` tool descriptions registered by
the bridge use intentionally factual, passive wording. Rewriting them with
imperative phrasing causes the Grok model to route the description into
conversation context, which suppresses tool dispatch. If you extend the
bridge with additional tools, apply the same passive-language convention.

**`XAI_MODEL` env var overrides the `botId` suffix**

When `XAI_MODEL` is set, every call uses that model regardless of what
the IVA module passes in `botId`. Useful for pinning a model across a
deployment. Leave it unset to select the model per call via the suffix.

**No provider session cap**

xAI does not send a session-expiry notice during a call. Long calls end
via the standard connection-closed path if xAI's session limits are reached.

**Caller transcripts may not be available**

xAI does not currently document a caller-side transcription stream. Bot
transcripts are available; caller-side transcripts may be absent from
the Infinity call record for this provider.

---

*Back to [Builder's Guide](../../BUILDERS_GUIDE.md) · Next: [xAI Workflow README](../../infinity-workflow/xai/README.md)*
