# ElevenLabs Provider Guide

> This guide assumes you have completed the Builder's Guide (Part 1) and have
> a working bridge with Echo confirmed end-to-end. The Infinity configuration
> you built in Part 1 carries forward unchanged.

---

## §1 Overview

ElevenLabs Conversational AI is an **agent-as-a-service** platform. The agent
you connect to this bridge is a fully configured entity that lives on the
ElevenLabs platform — it has a voice, a system prompt, a set of tools, and
its own conversation logic. The bridge does not supply any of those things.

What the bridge controls:

- **Which agent answers the call** — determined by the agent ID in `botId`
- **What context the agent has** — caller data and workflow variables, delivered
  as dynamic variables at the start of every conversation

What the ElevenLabs platform controls:

- System prompt and agent persona
- Voice and language model
- Opening message
- Tool definitions and behaviors

This division is the defining characteristic of the ElevenLabs integration.
Changing what the agent says or how it behaves means updating the agent on the
ElevenLabs platform — not changing the bridge configuration.

---

## §2 Prerequisites

**ElevenLabs account and agent:**
- An ElevenLabs account with Conversational AI access
- A configured agent (see §3)
- Your agent ID — visible in the ElevenLabs dashboard URL and agent settings
- An ElevenLabs API key

**Bridge configuration:**
```
ELEVENLABS_API_KEY=your-api-key
```

Set this in `bridge/.env`. The agent ID is not an environment variable — it
is supplied per call via `botId` (see §4).

---

## §3 Building your agent

Before the bridge can route calls to ElevenLabs, you need an agent. The
ElevenLabs platform gives you several paths: the web console, the ElevenLabs
Python or JavaScript SDK, or AI-assisted tools like Cursor using the
ElevenLabs API. The right approach depends on your workflow.

The sample application in this repo was built using the ElevenLabs SDK. The
`providers/elevenlabs/agent/` directory contains an importable agent template
(`agent-template.json`) and the tool definition for live-agent handoff
(`tools/live-agent-handoff.json`) — load these into the ElevenLabs platform
via the dashboard import flow or the SDK to bootstrap an agent that matches
what this guide expects. See that directory's README for the import steps.

**Three things your agent must have before connecting to this bridge:**

**1. Dynamic variable placeholders in your system prompt and opening message**

The bridge delivers caller context as dynamic variables. Your agent uses them
by referencing them with double curly braces in the ElevenLabs dashboard:

```
System prompt:  "The caller's name is {{firstName}}. Their case ID is {{caseId}}."
Opening message: "Hi {{firstName}}, I can see you're calling about case {{caseId}}."
```

Only simple variable substitution is supported in these fields — no
conditionals. See §4 for the full list of variables the bridge always provides.

**2. The `transfer_to_agent` client tool defined in the agent's tool configuration**

If your agent needs to hand off to a live agent, you must define a client tool
named `transfer_to_agent` with these parameters:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `queue_id` | string | Yes | The Infinity agent queue to route to |
| `reason` | string | No | Summary of why the call is being transferred |
| `tags` | array | No | Labels for routing or agent context |

The tool schema lives on the ElevenLabs dashboard. The bridge implements the
client-side handler — it receives the tool call, builds the handoff payload,
and emits `bot.feature LIVE_AGENT_HANDOFF` to Infinity. See §7 for the full
handoff sequence.

**3. Prompt instruction for tool invocation behavior**

Add this instruction to your system prompt:

```
After invoking any tool, do not generate any further response. Wait silently.
```

This is load-bearing for the handoff path. Without it, the agent may emit an
acknowledgment after invoking `transfer_to_agent`, which plays over the
transfer — the caller hears the agent speak after the handoff has already
been signaled.

---

## §4 Context injection

When Infinity routes a call to the ElevenLabs provider, the bridge delivers
caller context to the agent as `dynamic_variables` in the
`conversation_initiation_client_data` frame — the first message sent on the
ElevenLabs WebSocket, before any audio.

**The bridge always provides these six variables**, populated from the
`bot.start` payload:

| Variable | Source | Example |
|---|---|---|
| `call_to` | Called number (DNIS) | `"+17207940219"` |
| `call_from` | Calling number (ANI) | `"+12068524641"` |
| `ucid` | Avaya Universal Call ID | `"15446593841778164126"` |
| `call_direction` | Call direction | `"INBOUND"` |
| `language` | Language code | `"en-US"` |
| `domain` | Infinity domain | `"ejxfto.na.cc.avayacloud.com"` |

**Every field in `bot.start.payload.context` is also delivered** as a
top-level dynamic variable. These come from the IVA module's custom
parameters in your Infinity workflow — anything you pass there arrives at
the agent.

**End-to-end example:**

Your Infinity workflow sets these variables before the IVA module:

```
firstName       = "Alex"
caseId          = "CS-10482"
caseSubject     = "Billing inquiry"
engagementId    = {{engagementId}}
```

These are mapped in the IVA module's custom parameters (see §8). The bridge
receives them in `bot.start.payload.context` and delivers them to ElevenLabs
alongside the six fixed variables:

```json
{
  "type": "conversation_initiation_client_data",
  "dynamic_variables": {
    "call_to": "+17207940219",
    "call_from": "+12068524641",
    "ucid": "15446593841778164126",
    "call_direction": "INBOUND",
    "language": "en-US",
    "domain": "ejxfto.na.cc.avayacloud.com",
    "firstName": "Alex",
    "caseId": "CS-10482",
    "caseSubject": "Billing inquiry",
    "engagementId": "b8287b92-be9e-4efb-8210-82f35abb1d47"
  }
}
```

The agent's system prompt and opening message can reference any of these
with `{{variable_name}}` syntax.

**Collision behavior:** if a custom parameter shares a name with one of the
six bridge-fixed keys (for example, passing your own `language`), your value
wins. The context overlay happens after the fixed keys are set.

---

## §5 Phase 1 — Connection

When Infinity routes a call to the ElevenLabs provider, the bridge follows
this sequence:

1. `bot.start` arrives with agent ID (from `botId` suffix), API key (from
   `ELEVENLABS_API_KEY`), and caller context
2. Bridge opens a WebSocket to ElevenLabs:
   `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=<agent_id>`
3. Bridge immediately sends `conversation_initiation_client_data` with all
   dynamic variables
4. ElevenLabs confirms with `conversation_initiation_metadata`
5. Bridge sends `bot.started` to Infinity — the call is live

If the API key is missing or the agent ID is invalid, the bridge emits
`bot.ended` with `status.code: 503` (`BACKEND_START_FAILED`) and the
Infinity workflow takes its FAILED branch. No audio plays.

**botId format:**
```
elevenlabs:<agent_id>
```

Example: `elevenlabs:agent_8001kqafgryqe3eb8kg3dj1frpgy`

The agent ID suffix is required. `elevenlabs:` alone is not valid.

---

## §6 Phase 2 — The conversation

### Audio

The bridge transcodes audio bidirectionally between whatever codec Infinity
negotiated (G722, PCMU, PCMA, or L16) and ElevenLabs' native format. This
is handled automatically — no configuration is required.

The agent sends its opening greeting as soon as the ElevenLabs connection is
established. The bridge buffers this audio until Infinity's audio path is
ready, then delivers it without loss. Partners do not need to handle this —
it is built into the bridge.

### Barge-in

ElevenLabs drives barge-in detection entirely. When the platform detects
that the caller is speaking while the agent is speaking, it sends an
interruption signal. The bridge clears any queued agent audio and the caller
takes the floor. No bridge-side voice activity detection is involved.

### Keepalive

ElevenLabs sends periodic `ping` messages during the conversation. The
bridge responds with `pong` automatically. This is required by the ElevenLabs
protocol — if your deployment sits behind a proxy or load balancer with an
aggressive WebSocket idle timeout, ensure the timeout is longer than the
ElevenLabs ping interval.

---

## §7 Phase 3 — Closing the call

Every ElevenLabs call ends through one of three paths.

### Self-service complete

The agent determined the caller's need was met. On the ElevenLabs platform,
this is driven by the `end_call` system tool. Configure the tool with
`pre_tool_speech = "force"` in the agent settings so the agent delivers its
closing line before signaling completion.

The bridge receives the tool signal, emits `bot.ended` with
`status.code: 200` (`ENDPOINT_RELEASED`), and the Infinity workflow takes
its SUCCESSFUL branch. Read `byobotEndContext.status.code === 200` in your
workflow Decision module to route to the post-call flow.

### Live agent handoff

The agent invoked the `transfer_to_agent` client tool. The sequence:

1. ElevenLabs sends a `client_tool_call` with `tool_name: "transfer_to_agent"`
   and parameters (`queue_id`, `reason`, `tags`)
2. Bridge stashes the handoff payload and replies to ElevenLabs immediately
   so the agent can continue speaking its transfer acknowledgment line
3. Bridge waits for all queued agent audio to finish playing
4. Bridge emits `bot.feature LIVE_AGENT_HANDOFF` to Infinity with the
   `queueId`, `tags`, and `context` from the tool call
5. Bridge emits `bot.ended` with no `status` field
6. Infinity workflow reads `byobotLiveAgentHandoff` and routes to the
   agent queue

**The three-layer requirement for reliable handoff:**

The handoff path depends on three things working together, two of which
are your responsibility on the ElevenLabs dashboard:

| Layer | Where | What |
|---|---|---|
| Tool schema | ElevenLabs dashboard | `transfer_to_agent` defined with `queue_id` as required |
| Bridge handler | Bridge (built in) | Receives the call, builds the handoff payload, drains audio, emits to Infinity |
| Prompt instruction | ElevenLabs dashboard | "After invoking any tool, do not generate any further response. Wait silently." |

If the tool schema is missing, the agent cannot invoke the tool. If the
prompt instruction is missing, the agent may speak after invoking the tool
and the caller hears audio that plays over the transfer.

**`queue_id` fallback:** if the agent invokes `transfer_to_agent` with an
empty `queue_id`, the bridge substitutes `"default-queue"`. Ensure your
Infinity workflow handles this value or that the agent always provides an
explicit queue.

### Failure

Something went wrong before or during the call. Common causes:

| Condition | `status.code` | `reason` |
|---|---|---|
| API key missing or invalid | `503` | `BACKEND_START_FAILED` |
| Agent ID not found | `503` | `BACKEND_START_FAILED` |
| Codec negotiated that bridge cannot transcode | `503` | `BACKEND_START_FAILED` |
| Unhandled bridge exception mid-call | `500` | `INTERNAL_ERROR` |

The Infinity workflow receives all failure paths via the SUCCESSFUL branch —
read `byobotEndContext.status.code` in your Decision module and route
non-200 values to your failure recovery path.

---

## §8 Workflow

Import the reference workflow from `infinity-workflow/elevenlabs/` and
follow the README there for post-import configuration steps. The workflow
demonstrates the complete context injection pattern, IVA module
configuration, and closure branch wiring described in this guide.

> **Required: replace the agent ID before publishing the workflow.**
>
> The reference workflow ships with the Innovation Hub demo agent ID
> baked into the IVA module's `botId` parameter:
>
> ```
> elevenlabs:agent_8001kqafgryqe3eb8kg3dj1frpgy
> ```
>
> This identifier points at a demo agent in the Innovation Hub
> environment — it is not a generic template. The workflow will not
> route calls to your ElevenLabs agent until you replace it. After
> importing, open the IVA module and change `botId` to
> `elevenlabs:<your-agent-id>` using the agent ID from your ElevenLabs
> dashboard. This is a required configuration step, not a suggestion.

**IVA module custom parameters to configure:**

| Parameter | Value |
|---|---|
| `botId` | `elevenlabs:<your-agent-id>` |
| `firstName` | `{{firstName}}` (or whatever workflow variable holds the caller name) |
| `engagementId` | `{{engagementId}}` |
| `workflowSessionId` | `{{workflowSessionId}}` |
| *(additional CRM fields)* | `{{variableName}}` |

`engagementId` and `workflowSessionId` are available as Infinity system
variables but are not automatically included in `bot.start.payload.context`.
Map them explicitly here.

**Closure branch wiring:**

| IVA branch | Route to |
|---|---|
| SUCCESSFUL (code 200) | Post-call flow — survey, disconnect, callback |
| SUCCESSFUL (non-200) | Failure recovery — error message, queue transfer |
| HANDOFF | Agent queue — read `byobotLiveAgentHandoff.queueId` |
| FAILED | Failure recovery |

Use a Decision module on the SUCCESSFUL branch to split on
`byobotEndContext.status.code`.

---

## §9 Known behaviors

**Dynamic variable placeholders — simple substitution only**

The ElevenLabs platform supports `{{variable_name}}` substitution in the
system prompt and opening message fields. Conditional syntax
(`{{#if variable}}...{{/if}}`) is not supported in these fields. Use the
system prompt body for conditional logic — keep first message and greeting
fields to plain variable references.

**`engagementId` and `workflowSessionId` require explicit mapping**

These Infinity system variables are available in your workflow but are not
automatically included in `bot.start.payload.context`. If you want the agent
to have them — for post-call correlation or tool calls — map them explicitly
in the IVA module custom parameters using `{{engagementId}}` and
`{{workflowSessionId}}`.

**Agent ID belongs in `botId`, not in the environment**

Unlike model-based providers where `GEMINI_MODEL` or `OPENAI_MODEL` can fix
a model across all calls, ElevenLabs agent configuration belongs in `botId`.
The env var `ELEVENLABS_API_KEY` holds the credential; the agent ID is
per-call routing. This keeps the agent selection in the workflow where it
belongs — visible to anyone configuring the IVA module.

---

*Back to [Builder's Guide](../../BUILDERS_GUIDE.md) · Next: [ElevenLabs Workflow README](../../infinity-workflow/elevenlabs/README.md)*
