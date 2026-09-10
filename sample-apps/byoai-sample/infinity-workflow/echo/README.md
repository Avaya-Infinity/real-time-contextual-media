# Echo Workflow

> **New here? Start with the [Builder's Guide](../../BUILDERS_GUIDE.md)**

---

## DISCLAIMER

This repository contains example code for demonstration and educational
purposes only. It is not officially supported by Avaya and is not
intended for production use. See the [top-level README](../../README.md)
for the full disclaimer.

---

Reference workflow for validating the Avaya Infinity + RCMS bridge
integration using the Echo provider.

## What Echo does

Echo is the bridge's built-in loopback provider. It requires no AI
credentials and no external service. Audio sent by the caller is
reflected back immediately. The caller hears themselves.

Echo's purpose is to validate the complete integration pipeline —
network, TLS, JWT authentication, AI Media Gateway configuration,
workflow wiring, and audio flow — before any AI provider complexity
is introduced.

## What to expect on a call

1. Caller dials the DID assigned to this workflow
2. Bridge receives session.start and responds session.started
3. Bridge receives bot.start — **check the journal here**
4. bot.start.payload.context contains all CRM variables set in the
   CRM Data (Demo) module: firstName, lastName, email, caseId,
   caseSubject, caseDescription, engagementId, workflowSessionId
5. Audio echoes back to the caller
6. Caller hangs up — Infinity drives session.end
7. Call ends cleanly

The bridge does not emit bot.ended with status.code 200 on a normal
Echo call. The caller disconnect triggers a platform-initiated close,
not a self-service-complete signal. The Decision module's VALID (200)
path will not be taken — it is pre-wired for when you swap in a real
AI provider.

## Closure paths

| Path | When | Workflow branch |
|---|---|---|
| Caller hangs up | Normal call end | Platform-initiated — no Decision module |
| Bridge unreachable | WSS connection fails | FAILED → Create Interaction |
| JWT validation fails | Auth key mismatch | FAILED → Create Interaction |
| Bot start error | Bridge rejects bot.start | FAILED → Create Interaction |

## Context variables passed to the bridge

All of the following arrive in bot.start.payload.context:

| Variable | Demo value | Purpose |
|---|---|---|
| firstName | Todd | Caller first name |
| lastName | Michaels | Caller last name |
| email | test@example.com | Caller email |
| caseId | 5656 | Case identifier |
| caseSubject | Self Service Virtual Agents | Case subject |
| caseDescription | Interested in a demo of Avaya Infinity | Case description |
| engagementId | {{engagementId}} | Persistent engagement ID — follows the full customer journey |
| workflowSessionId | {{workflowSessionId}} | Workflow session ID — for execution replay and debugging |

Update the demo values in the CRM Data (Demo) setVariable module
to match your own test data after import.

## After import — what to configure

1. **Connection** — in the IVA node, set Connection to your AI Media
   Gateway profile
2. **Queue** — the Create Interaction module has no queue configured
   (stripped on export). Set the queue or user for the FAILED and
   IVA_PORT_HANDOFF paths
3. **Phone number** — assign a DID to this workflow in
   Admin Dashboard → Voice → Numbers

## Verifying the integration

After a test call, check the bridge journal:

```
sudo journalctl -u bridge-server --no-pager -n 100 | grep "bot.start"
```

Confirm bot.start.payload.context contains your CRM variables.
If it does, the full context injection pipeline is working.

## Relationship to provider workflows

This workflow is structurally identical to the ElevenLabs workflow.
The only difference is botId: "echo" instead of
"elevenlabs:your-agent-id". When you are ready to add a real AI
provider, import the provider workflow and follow its guide —
the Infinity configuration you built for Echo carries forward
unchanged.
