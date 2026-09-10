# ElevenLabs Workflow

> **New here? Start with the [Builder's Guide](../../BUILDERS_GUIDE.md)**

---

## DISCLAIMER

This repository contains example code for demonstration and educational
purposes only. It is not officially supported by Avaya and is not
intended for production use. See the [top-level README](../../README.md)
for the full disclaimer.

---

Reference workflow for connecting Avaya Infinity to an ElevenLabs
Conversational AI agent via the RCMS bridge.

## What ElevenLabs does

ElevenLabs is an agent-as-a-service platform. The agent you connect here
is a fully configured entity hosted on ElevenLabs — it has a voice, a
system prompt, a persona, and its own conversation logic. The bridge
delivers caller context to the agent at the start of every call and
handles the RCMS protocol on Infinity's side. Everything the agent says
and how it behaves is configured on the ElevenLabs platform.

## Before you import

This workflow ships with the Innovation Hub demo agent ID:
`elevenlabs:agent_8001kqafgryqe3eb8kg3dj1frpgy`

**You must replace this with your own agent ID before the workflow will
route calls correctly.** After import, update the `botId` custom parameter
in the IVA module to `elevenlabs:<your-agent-id>`. See the
[ElevenLabs Provider Guide](../../docs/guides/elevenlabs.md) for agent
setup instructions.

## What to expect on a call

1. Caller dials the DID assigned to this workflow
2. Bridge receives `session.start` and responds `session.started`
3. Bridge receives `bot.start` — **check the journal here**
4. `bot.start.payload.context` contains all CRM variables set in the
   CRM Data (Demo) module: `firstName`, `lastName`, `email`, `caseId`,
   `caseSubject`, `caseDescription`, `engagementId`, `workflowSessionId`
5. Bridge connects to ElevenLabs and delivers caller context as dynamic
   variables — **check the journal for `conversation_initiation_client_data`**
6. Agent greets the caller by name using context from the workflow
7. Conversation proceeds — caller speaks, agent responds
8. Call ends via one of three paths (see Closure paths)

## Closure paths

| Path | When | Workflow branch |
|---|---|---|
| Self-service complete | Agent invoked `end_call` — caller's need was met | SUCCESSFUL → Decision (code 200) → post-call flow |
| Live agent handoff | Agent invoked `transfer_to_agent` | HANDOFF → agent queue |
| Caller hangs up | Caller disconnects before completion | FAILED → Create Interaction |
| Bridge unreachable | WSS connection fails | FAILED → Create Interaction |
| JWT validation fails | Auth key mismatch | FAILED → Create Interaction |
| Provider error | ElevenLabs connection fails or API key invalid | FAILED → Create Interaction |

The SUCCESSFUL branch requires a Decision module to split on
`byobotEndContext.status.code === 200`. Non-200 values on the SUCCESSFUL
branch indicate a provider error — route these to your failure recovery
path.

## Context variables passed to the bridge

All of the following arrive in `bot.start.payload.context` and are
delivered to the ElevenLabs agent as dynamic variables:

| Variable | Demo value | Purpose |
|---|---|---|
| `firstName` | Todd | Caller first name — reference as `{{firstName}}` in agent prompt |
| `lastName` | Michaels | Caller last name |
| `email` | test@example.com | Caller email |
| `caseId` | 5656 | Case identifier |
| `caseSubject` | Self Service Virtual Agents | Case subject |
| `caseDescription` | Interested in a demo of Avaya Infinity | Case description |
| `engagementId` | `{{engagementId}}` | Persistent engagement ID — follows the full customer journey |
| `workflowSessionId` | `{{workflowSessionId}}` | Workflow session ID — for execution replay and debugging |

Update the demo values in the CRM Data (Demo) setVariable module to
match your own test data after import.

## After import — what to configure

1. **Agent ID** — in the IVA module custom parameters, replace
   `elevenlabs:agent_8001kqafgryqe3eb8kg3dj1frpgy` with
   `elevenlabs:<your-agent-id>`
2. **Connection** — in the IVA node, set Connection to your AI Media
   Gateway profile
3. **Queue** — the Create Interaction module has no queue configured
   (stripped on export). Set the queue or user for the FAILED and
   HANDOFF paths
4. **Phone number** — assign a DID to this workflow in
   Admin Dashboard → Voice → Numbers

## Verifying the integration

After a test call, check the bridge journal:

```
sudo journalctl -u bridge-server --no-pager -n 200 | grep -E "bot.start|elevenlabs|HANDOFF|bot.ended"
```

What to look for:

- `INBOUND JSON (bot.start)` — confirms Infinity delivered the call to
  the bridge
- `conversation_initiation_client_data` — confirms context was delivered
  to ElevenLabs
- `Sending bot.ended with success context: code=200` — confirms
  self-service complete
- `LIVE_AGENT_HANDOFF` — confirms handoff was signaled to Infinity

If `bot.start` arrives but `conversation_initiation_client_data` does
not appear, check `ELEVENLABS_API_KEY` and the agent ID in the IVA
module custom parameters.

## Relationship to the Builder's Guide

This workflow builds on the foundation you established in Part 1.
The Infinity configuration — AI Media Gateway profile, IVA module,
workflow branches — carries forward from the Echo workflow unchanged.
The only difference is `botId`: `elevenlabs:<your-agent-id>` instead
of `echo`.

For full provider configuration, context injection details, and known
behaviors, see the [ElevenLabs Provider Guide](../../docs/guides/elevenlabs.md).
