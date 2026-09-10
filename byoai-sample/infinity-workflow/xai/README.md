# xAI Grok Workflow

> **New here? Start with the [Builder's Guide](../../BUILDERS_GUIDE.md)**

---

## DISCLAIMER

This repository contains example code for demonstration and educational
purposes only. It is not officially supported by Avaya and is not
intended for production use. See the [top-level README](../../README.md)
for the full disclaimer.

---

Reference workflow for connecting Avaya Infinity to xAI Grok Voice
via the RCMS bridge.

## What xAI Grok does

xAI Grok Voice is a real-time, audio-native conversational model with
native µ-law telephony audio support. The bridge composes the system
prompt, registers the tools the model can invoke, and manages the full
conversation lifecycle. What the agent says and how it behaves is
determined by the system prompt configured on the bridge — there is no
external agent dashboard.

## What to expect on a call

1. Caller dials the DID assigned to this workflow
2. Bridge receives `session.start` and responds `session.started`
3. Bridge receives `bot.start` — **check the journal here**
4. `bot.start.payload.context` contains all CRM variables set in the
   CRM Data (Demo) module: `firstName`, `lastName`, `email`, `caseId`,
   `caseSubject`, `caseDescription`, `engagementId`, `workflowSessionId`
5. Bridge connects to xAI and sends `session.update` with the composed
   system prompt and caller context — **check the journal for
   `session.update`**
6. Bridge sends `response.create` — Grok speaks first
7. Conversation proceeds — caller speaks, agent responds
8. Call ends via one of three paths (see Closure paths)

## Closure paths

| Path | When | Workflow branch |
|---|---|---|
| Self-service complete | Agent invoked `end_session` — caller's need was met | SUCCESSFUL → Decision (code 200) → post-call flow |
| Live agent handoff | Agent invoked `transfer_to_agent` | HANDOFF → agent queue |
| Caller hangs up | Caller disconnects before completion | FAILED → Create Interaction |
| Bridge unreachable | WSS connection fails | FAILED → Create Interaction |
| JWT validation fails | Auth key mismatch | FAILED → Create Interaction |
| Provider error | xAI connection fails or API key invalid | FAILED → Create Interaction |

The SUCCESSFUL branch requires a Decision module to split on
`byobotEndContext.status.code === 200`. Non-200 values on the SUCCESSFUL
branch indicate a provider error — route these to your failure recovery
path.

## Context variables passed to the bridge

All of the following arrive in `bot.start.payload.context`. The bridge
passes a fixed set of keys to the system prompt template as
`{placeholder}` substitutions:

| Variable | Demo value | Purpose |
|---|---|---|
| `firstName` | Todd | Caller first name — reference as `{firstName}` in prompt |
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

1. **Connection** — in the IVA node, set Connection to your AI Media
   Gateway profile
2. **Queue** — the Create Interaction module has no queue configured
   (stripped on export). Set the queue or user for the FAILED and
   HANDOFF paths
3. **Phone number** — assign a DID to this workflow in
   Admin Dashboard → Voice → Numbers
4. **Codec** — for best results, start the bridge with `--codec PCMU`
   to match xAI's native µ-law telephony audio format and eliminate
   bidirectional transcoding

## Verifying the integration

After a test call, check the bridge journal:

```
sudo journalctl -u bridge-server --no-pager -n 200 | grep -E "bot.start|xai|HANDOFF|bot.ended"
```

What to look for:

- `INBOUND JSON (bot.start)` — confirms Infinity delivered the call to
  the bridge
- `Registered backend: XaiService` — confirms the provider is active
- `Sending bot.ended with success context: code=200` — confirms
  self-service complete
- `LIVE_AGENT_HANDOFF` — confirms handoff was signaled to Infinity

If `bot.start` arrives but the xAI connection does not appear, check
`XAI_API_KEY` and the `botId` value in the IVA module custom parameters.

## Relationship to the Builder's Guide

This workflow builds on the foundation you established in Part 1.
The Infinity configuration — AI Media Gateway profile, IVA module,
workflow branches — carries forward from the Echo workflow unchanged.
The only difference is `botId`: `xai:<model>` instead of `echo`.

For full provider configuration, system prompt setup, and known
behaviors, see the [xAI Grok Provider Guide](../../docs/guides/xai.md).
