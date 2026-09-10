# ElevenLabs Agent Provisioning Scripts

These scripts use the [ElevenLabs Python SDK](https://github.com/elevenlabs/elevenlabs-python)
to create and configure the ElevenLabs Conversational AI agent that the bridge connects to.

> **New here? Start with the [Builder's Guide](../../../BUILDERS_GUIDE.md) and the
> [ElevenLabs setup guide](../docs/setup-guide.md) before running these scripts.**

---

## Prerequisites

1. An ElevenLabs account with Conversational AI access
2. The bridge Python environment active (`source .venv/bin/activate`)
3. `ELEVENLABS_API_KEY` set in your `.env` file

Install the SDK if not already present:
```bash
pip install elevenlabs
```

---

## Run order

Run the scripts once in this order to provision a new agent:

```bash
python create_agent.py      # Create the agent, print Infinity config values
python add_knowledge.py     # Attach the Innovation Hub knowledge base
python update_agent.py      # Apply voice, prompt, tools, and turn config
```

Re-run `update_agent.py` any time you change the agent persona, system prompt,
or tool configuration. The other two scripts are idempotent and safe to re-run.

---

## Scripts

| Script | Purpose | Modifies agent? |
|---|---|---|
| `create_agent.py` | Creates the agent (idempotent). Prints the Bot ID and Bot Credentials for Infinity configuration. | Creates only — no updates |
| `add_knowledge.py` | Uploads the Innovation Hub knowledge document and attaches it to the agent. | Yes |
| `update_agent.py` | Applies voice, system prompt, first message, handoff tool, end-call tool, and turn-taking config. | Yes |
| `dump_agent.py` | Prints the live agent configuration as JSON. Read-only diagnostic. | No |

---

## Configuration

All scripts read configuration from the bridge `.env` file. Required keys:

| Key | Required by | Description |
|---|---|---|
| `ELEVENLABS_API_KEY` | All scripts | Your ElevenLabs API key |
| `ELEVENLABS_AGENT_ID` | `update_agent.py`, `dump_agent.py`, `add_knowledge.py` | Agent ID from `create_agent.py` output |
| `ELEVENLABS_VOICE_ID` | `update_agent.py` | ElevenLabs shared voice library ID |
| `ELEVENLABS_VOICE_OWNER` | `update_agent.py` | Shared voice owner ID |
| `ELEVENLABS_VOICE_NAME` | `update_agent.py` | Display name for the voice |
| `BRIDGE_WEBSOCKET_URL` | `create_agent.py` | Your bridge WebSocket URL (e.g. `wss://your-host/`) |

Run `create_agent.py` first — it prints the `ELEVENLABS_AGENT_ID` value to add to `.env`
before running the remaining scripts.

---

## About the agent

The provisioned agent is the Innovation Hub virtual assistant — configured with:

- **Identity:** Hope, a specialist at Innovation Hub, an Avaya and ElevenLabs technology
  partner demonstrating enterprise contact center AI
- **Context:** receives caller identity and case details from the Infinity workflow via
  dynamic variables (`{{firstName}}`, `{{caseId}}`, `{{caseSubject}}`, etc.)
- **Handoff:** `transfer_to_agent` tool triggers Infinity's live agent handoff workflow
- **End call:** `end_call` built-in tool closes the session cleanly when the caller is done
- **Turn-taking:** configured with `patient` eagerness to handle natural hesitations

The system prompt and tool definitions in `update_agent.py` are the authoritative source
for the agent's behavior. Read the inline comments there — they document the reasoning
behind configuration decisions that aren't obvious from the ElevenLabs documentation.
