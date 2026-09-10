# ElevenLabs Setup Guide

Configuring an ElevenLabs Conversational AI agent for use with the Infinity bridge server.

> **Audience:** AI/bot developers
> **Prerequisite:** ElevenLabs account with Conversational AI access

---

## Step 1 — Create or select an agent

Use the provisioning scripts to create your agent programmatically:

```bash
cd providers/elevenlabs/scripts
python create_agent.py
```

This creates a configured agent and prints the Bot ID and Bot Credentials values
you will need for the Infinity AI Media Gateway configuration. See
`providers/elevenlabs/scripts/README.md` for the full run order.

Alternatively, create an agent manually:

1. Log in to [ElevenLabs](https://elevenlabs.io)
2. Navigate to **Conversational AI → Agents**
3. Create a new agent or select an existing one
4. Note the **Agent ID** — this is the value after `elevenlabs:` in your Infinity Bot ID

---

## Step 2 — Configure dynamic variables

The bridge server passes call context from Infinity's `bot.start` message to ElevenLabs via
`conversation_initiation_client_data.dynamic_variables`. Configure your agent prompt to use these:

| Variable | Source field in bot.start | Example value | Use in prompt |
|---|---|---|---|
| `{{call_to}}` | `to` | `+13322365051` | "The caller reached {{call_to}}" |
| `{{call_from}}` | `from` | `+13479872825` | Look up in CRM |
| `{{ucid}}` | `ucid` | `19529283051762805748` | Correlation ID |
| `{{call_direction}}` | `direction` | `INBOUND` | Branch on inbound vs outbound |
| `{{language}}` | `language` | `en-US` | Set agent language |
| `{{domain}}` | `domain` | `ejxfto.na.cc.avayacloud.com` | Multi-tenant routing |

Any fields in the `context` object from the Infinity workflow are also passed through
as dynamic variables (flattened).

**Example agent system prompt excerpt:**
```
You are a virtual agent for Acme Corp. The caller's number is {{call_from}}.
This is an {{call_direction}} call to {{call_to}}.
Your primary language is {{language}}.
```

---

## Step 3 — Configure audio settings

The bridge server handles codec conversion between Infinity and ElevenLabs. ElevenLabs
Conversational AI uses **16kHz PCM** internally.

In your agent settings:
- **Input audio format:** handled by bridge server (resampled to 16kHz as needed)
- **Output audio format:** bridge server resamples from 16kHz to the negotiated Infinity codec

No special audio configuration is needed in the ElevenLabs agent — the bridge handles all conversion.

---

## Step 4 — Configure live agent handoff tool

To trigger a handoff from the ElevenLabs agent to a human agent queue, add a tool to your agent.

Import `providers/elevenlabs/agent/tools/live-agent-handoff.json` or configure manually:

**Tool name:** `transfer_to_agent`
**Description:** Transfer the caller to a human agent queue

**Parameters:**
```json
{
  "queue_id": {
    "type": "string",
    "description": "The Infinity queue ID to transfer the caller to"
  },
  "reason": {
    "type": "string",
    "description": "Brief reason for the transfer, passed as context to the agent"
  }
}
```

When the agent calls this tool, the bridge server sends `bot.feature` with
`ftype: "LIVE_AGENT_HANDOFF"` to Infinity, which triggers the configured workflow action.

**Configuring queue IDs in the prompt:**

Rather than letting the agent guess queue IDs, declare them explicitly:
```
When the customer needs billing support, call transfer_to_agent with queue_id "billing-queue-001".
When the customer needs technical support, call transfer_to_agent with queue_id "tech-queue-002".
```

---

## Step 5 — Retrieve your API key

1. In ElevenLabs, navigate to **Profile → API Keys**
2. Create a dedicated API key for the Infinity integration (do not use your master key)
3. Note the key — you'll base64-encode it for use as Bot Credentials in Infinity

Encode for Infinity:
```bash
echo -n '{"apiKey":"your-key-here"}' | base64
# Output: eyJhcGlLZXkiOiJ5b3VyLWtleS1oZXJlIn0=
```

Paste the base64 output into the **Bot Credentials** field in the Infinity Workflow Designer.

---

## Step 6 — Test the agent

Before connecting to Infinity, test your ElevenLabs agent directly:

1. Use the ElevenLabs web interface to have a test conversation
2. Verify dynamic variables are working (temporarily hardcode test values in the prompt)
3. Verify the handoff tool triggers correctly
4. Verify the agent voice and response quality

Then connect to Infinity and test end-to-end.

---

## Limitations and known issues

- ElevenLabs WebSocket sessions do not survive bridge server restarts — callers will experience
  a brief interruption if the server restarts mid-call
- Audio quality is best when Infinity negotiates G722 (16kHz) — no resampling required
- The bridge server sends audio to ElevenLabs at the rate it arrives from Infinity — if
  Infinity introduces jitter, ElevenLabs may perceive gaps
