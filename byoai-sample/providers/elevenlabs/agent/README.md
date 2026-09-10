# ElevenLabs Agent Assets

> **New here? Start with the [Builder's Guide](../../../BUILDERS_GUIDE.md)**

---

## DISCLAIMER

This repository contains example code for demonstration and educational
purposes only. It is not officially supported by Avaya and is not
intended for production use. See the [top-level README](../../../README.md)
for the full disclaimer.

---

ElevenLabs Conversational AI configuration for use with the Infinity bridge server.

## Files

| File | Purpose |
|---|---|
| `agent-template.json` | Base agent configuration — import into ElevenLabs and customise |
| `tools/live-agent-handoff.json` | Tool definition for triggering Infinity live agent handoff |

## Setup

See `../docs/setup-guide.md` for full configuration instructions.

## Dynamic variables

The bridge server passes the following variables from every Infinity `bot.start` call:

| Variable | Description |
|---|---|
| `{{call_to}}` | Called number (DNIS) — E.164 or SIP URI |
| `{{call_from}}` | Calling number (ANI) — E.164 or SIP URI |
| `{{ucid}}` | Avaya Universal Call ID |
| `{{call_direction}}` | `INBOUND` or `OUTBOUND` |
| `{{language}}` | Language code e.g. `en-US` |
| `{{domain}}` | Infinity domain name |

Additional variables from the Infinity workflow `context` object are merged in automatically.
