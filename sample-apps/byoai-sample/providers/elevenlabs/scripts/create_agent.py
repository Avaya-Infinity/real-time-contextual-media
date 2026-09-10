#!/usr/bin/env python3
"""
create_agent — Create the ElevenLabs Conversational AI agent for Infinity

Role:
    Creates a new ElevenLabs Conversational AI agent configured for use
    with the Avaya Infinity bridge. Idempotent — if an agent named
    AGENT_NAME already exists, prints its ID and skips creation. Prints
    the Bot ID and Bot Credentials values needed for the Infinity
    AI Media Gateway configuration.

Does not own:
    Voice selection, system prompt, or tool configuration — those are
    applied by update_agent.py after creation. Knowledge base attachment
    is handled by add_knowledge.py.

Dependencies:
    elevenlabs SDK, _env.py

RCMS lifecycle phase:
    None — provisioning-time script. Run once before connecting to Infinity.

Run order:
    1. create_agent.py   ← this script
    2. add_knowledge.py
    3. update_agent.py
"""

from __future__ import annotations

import base64
import json
import sys

import truststore
truststore.inject_into_ssl()

from elevenlabs import ElevenLabs

from _env import load_env, resolve_env_path


AGENT_NAME = "infinity-rcms-byoai-demo"


def main() -> int:
    env_path = resolve_env_path()
    env = load_env(env_path)

    api_key = env.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        print(f"error: ELEVENLABS_API_KEY missing from {env_path}", file=sys.stderr)
        return 1

    bridge_ws_url = env.get("BRIDGE_WEBSOCKET_URL", "").strip()
    if not bridge_ws_url:
        print(f"error: BRIDGE_WEBSOCKET_URL missing from {env_path}", file=sys.stderr)
        return 1

    client = ElevenLabs(api_key=api_key)

    agents = client.conversational_ai.agents.list()
    existing = [a for a in agents.agents if a.name == AGENT_NAME]
    if existing:
        agent_id = existing[0].agent_id
        print(f"Agent already exists: {agent_id}")
    else:
        agent = client.conversational_ai.agents.create(
            name=AGENT_NAME,
            conversation_config={
                "agent": {
                    "prompt": {
                        "prompt": (
                            "You are a helpful virtual assistant. You are on a live phone call.\n"
                            "Keep responses short and natural — this is a voice conversation, not a chat.\n"
                            "When the caller asks to speak to a human or be transferred, use the "
                            "transfer_to_agent tool immediately.\n"
                            "Do not ask for confirmation before transferring."
                        ),
                        "llm": "gpt-4o-mini",
                        "tools": [
                            {
                                "type": "client",
                                "name": "transfer_to_agent",
                                "description": (
                                    "Transfer the caller to a human agent. Use when the caller "
                                    "requests a human or escalation."
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "queue_id": {
                                            "type": "string",
                                            "description": "Queue ID to transfer to",
                                        },
                                        "reason": {
                                            "type": "string",
                                            "description": "Reason for transfer",
                                        },
                                    },
                                    "required": ["queue_id", "reason"],
                                },
                            }
                        ],
                    },
                    "first_message": "Hello, thank you for calling. How can I help you today?",
                    "language": "en",
                },
                "tts": {
                    "model_id": "eleven_turbo_v2",
                    "voice_id": "nPczCjzI2devNBz1zQrb",
                },
            },
        )
        agent_id = agent.agent_id
        print(f"Agent created: {agent_id}")

    credentials = json.dumps({"apiKey": api_key})
    credentials_b64 = base64.b64encode(credentials.encode()).decode()

    print("")
    print("========== INFINITY CONFIGURATION ==========")
    print(f"WebSocket URL:   {bridge_ws_url}")
    print(f"botId:           elevenlabs:{agent_id}")
    print(f"botCredentials:  {credentials_b64}")
    print("============================================")
    print("")
    print("Next steps:")
    print(f"  1. Add ELEVENLABS_AGENT_ID={agent_id} to your .env")
    print("  2. Run: python add_knowledge.py")
    print("  3. Run: python update_agent.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
