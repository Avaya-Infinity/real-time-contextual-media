# Avaya Infinity BYO AI Bridge

> **New here? Start with the [Builder's Guide](BUILDERS_GUIDE.md)** —
> a complete walkthrough of the architecture, Infinity configuration, and
> end-to-end call flow before you touch any code.

---

## IMPORTANT DISCLAIMER

**This repository contains example code for demonstration and educational
purposes only.**

- **NOT FOR PRODUCTION USE:** This code is provided as an example and has
  not been tested or validated for production environments.
- **NO OFFICIAL SUPPORT:** This is an individual contribution and is not
  officially supported by Avaya. Avaya provides no warranty, support, or
  maintenance for this code.
- **USE AT YOUR OWN RISK:** Users assume all responsibility for testing,
  validation, security, and compliance before deploying in any environment.
- **NO LIABILITY:** Avaya disclaims all liability for any damages, losses,
  or issues arising from the use of this example code.
- **COMMUNITY CONTRIBUTION:** This work represents individual exploration
  and learning, not official Avaya product documentation or best practices.

**Before using this code:**

- Thoroughly review all code and configurations
- Test extensively in a safe, non-production environment
- Validate security practices for your organization
- Ensure compliance with your data privacy and security policies
- Consult with Avaya professional services for production implementations

---

## Overview

The AI landscape is moving too fast to commit to one vendor. What is
state of the art today may be a baseline in eighteen months. This
repository exists because betting your contact center AI strategy on
a single provider is a risk you do not have to take.

Avaya Infinity is AI-agnostic by design. The BYO AI program lets you
connect any conversational AI to Infinity's contact center platform —
your workflow, your data, your context, any provider. This bridge is
the proof of that principle in working code.

The repository contains a complete RCMS bridge that connects a live
Avaya Infinity phone number to four conversational AI providers using
the same architecture, the same Infinity workflow, and the same
integration pattern. Swap the provider, keep everything else.

| Component | Description |
|---|---|
| `bridge/` | RCMS bridge server — WebSocket server implementing the Infinity MIM/RCMS protocol |
| `providers/` | Provider plugins — ElevenLabs, Gemini, OpenAI, xAI, and Echo |
| `infinity-workflow/` | Importable Avaya Infinity workflow exports, one per provider |
| `docs/` | Builder's Guide, provider guides, and integration reference |
| `bridge/schema/` | RCMS protocol schema and reference documentation |

---

## Service availability

Of the services modeled by Real-time Contextual Media Streaming, **Virtual
Agent (bidirectional conversational AI) is the only one generally available for
BYO AI partners today** — which is why it is the only service this repository
demonstrates. Other modeled services (Agent Assist, Recording, Transcription,
Text-to-Speech, Speech Recognition, Translation) are on the roadmap but are not
yet ready to build against. The sample's scope reflects platform readiness, not
a limitation of the bridge.

---

## What this demonstrates

A caller dials a number. An AI agent answers — it knows the caller's
name, their open case, and the reason they called. It handles the
conversation naturally. When it is done, it either completes the
interaction cleanly or hands off to a human agent with full context.

The same call flow works across four fundamentally different AI
platforms:

| Provider | Model | Architecture |
|---|---|---|
| ElevenLabs | Conversational AI | Agent-as-a-service — prompt and tools live on the ElevenLabs platform |
| Google Gemini | Gemini Live | Bridge-orchestrated — bridge owns the prompt, tools, and session |
| OpenAI | Realtime API | Bridge-orchestrated — bridge owns the prompt, tools, and session |
| xAI | Grok Voice | Bridge-orchestrated — native µ-law telephony audio |

Four providers. One bridge. One Infinity workflow pattern. When the
next breakthrough model ships, adding it means writing a new provider
plugin — not rebuilding the integration.

---

## Quick start

### Prerequisites

- Python 3.11+
- Avaya Infinity tenant with AI Media Gateway configured
- At least one provider API key (or use Echo — no credentials needed)
- A publicly reachable HTTPS endpoint (cloud VM or ngrok for local dev)

### Install

```bash
git clone https://github.com/bode-avaya/infinity-rcms-byoai.git
cd infinity-rcms-byoai
pip install -r bridge/requirements.txt
```

### Configure

```bash
cp bridge/.env.example bridge/.env
# Edit bridge/.env — add your API keys
```

### Run

```bash
python bridge/main.py --host 0.0.0.0 --port 8443
```

### Verify

Follow **[§11 Verification with Echo](docs/BUILDERS_GUIDE_section11.md)**
to confirm the integration is working before connecting a real AI
provider. Echo requires no credentials and validates every layer of
the stack.

---

## Operations

The bridge logs to two destinations on every run: **stderr** (captured by
whichever process manager runs the bridge — `systemd` journal, container
log, or terminal) and **on-disk files under `logs/`** (`bridge_log.txt`
for the full Python log, `bridge_msg.txt` for INBOUND/OUTBOUND JSON
protocol frames). Both files are rotated to `.bak` on every restart.
Run with `--verbose` to raise the root Python logger to DEBUG and surface
audio-pipeline diagnostic lines (`MEDIA SUMMARY`, `FIRST MEDIA EGRESS/INGRESS`);
without it, the default INFO level keeps those suppressed. DTMF digits
are redacted in logs by default — set `LOG_DIGITS=true` only when
troubleshooting DTMF dispatch.

For the full logging reference (log levels, file destinations, redaction
scope, and the `journalctl -p info` gotcha — it does **not** filter
Python's log levels) see **[§4 Bridge Installation and
Configuration](docs/BUILDERS_GUIDE_section4.md)**. For a diagnostic
quick-reference (lifecycle markers by phase, transcript marker cadence
per provider, level-filtering grep pattern) see
**[§12 Quick Reference](docs/BUILDERS_GUIDE_section12.md)**.

---

## Repository structure

```
infinity-rcms-byoai/
├── README.md                          # This file
├── BUILDERS_GUIDE.md                  # Start here
│
├── bridge/                            # RCMS bridge server
│   ├── main.py                        # Entry point
│   ├── bridge_server.py               # Core RCMS/MIM protocol implementation
│   ├── bot_service.py                 # Provider dispatcher
│   ├── .env.example                   # Environment variable reference
│   ├── requirements.txt
│   └── schema/
│       ├── rcms.schema.json           # RCMS protocol schema
│       └── rcms.schema.md             # Schema reference documentation
│
├── providers/                         # Provider plugins
│   ├── echo/                          # Built-in loopback — no credentials needed
│   ├── elevenlabs/                    # ElevenLabs Conversational AI
│   ├── gemini/                        # Google Gemini Live
│   ├── openai/                        # OpenAI Realtime
│   └── xai/                           # xAI Grok Voice
│
├── infinity-workflow/                 # Importable Infinity workflow exports
│   ├── echo/
│   ├── elevenlabs/
│   ├── gemini/
│   ├── openai/
│   └── xai/
│
└── docs/                              # Documentation
    ├── BUILDERS_GUIDE_section*.md     # Builder's Guide sections
    ├── ADDING_A_PROVIDER.md           # Part 3 — adding a new provider
    └── guides/                        # Provider-specific guides
        ├── elevenlabs.md
        ├── gemini.md
        ├── openai.md
        └── xai.md
```

---

## Documentation

| Document | What it covers |
|---|---|
| [Builder's Guide](BUILDERS_GUIDE.md) | End-to-end setup — start here |
| [ElevenLabs Guide](docs/guides/elevenlabs.md) | ElevenLabs configuration, context injection, known behaviors |
| [Gemini Guide](docs/guides/gemini.md) | Gemini configuration, system prompt, known behaviors |
| [OpenAI Guide](docs/guides/openai.md) | OpenAI configuration, system prompt, known behaviors |
| [xAI Grok Guide](docs/guides/xai.md) | xAI configuration, system prompt, known behaviors |
| [Adding a Provider](docs/ADDING_A_PROVIDER.md) | Plugin contract, audio pipeline, architectural considerations |

---

## References

- [Avaya Infinity Developer Portal](https://developers.avayacloud.com/avaya-infinity)
- [RCMS/MIM Protocol Reference](https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming)
- [ElevenLabs Conversational AI](https://elevenlabs.io/docs/conversational-ai/overview)
- [Google Gemini Live API](https://ai.google.dev/api/multimodal-live)
- [OpenAI Realtime API](https://platform.openai.com/docs/guides/realtime)
- [xAI Grok Voice API](https://docs.x.ai/api)

---

## License

MIT

---

## Support

- **This repository:** Open an issue on GitHub
- **Avaya Infinity:** [Avaya Developer Hub](https://developers.avayacloud.com)
  and your Avaya account team
