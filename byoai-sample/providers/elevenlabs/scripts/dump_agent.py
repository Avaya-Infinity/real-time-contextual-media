#!/usr/bin/env python3
"""
dump_agent — Read-only diagnostic: dump the live agent configuration as JSON

Role:
    Read-only diagnostic — fetches the live ElevenLabs agent configuration
    and prints it as formatted JSON. Use to inspect platform-applied
    defaults (turn_detection, vad_settings, asr, client_events) and
    verify that update_agent.py applied settings correctly.

Does not own:
    Any write operations. This script never modifies the agent.

Dependencies:
    elevenlabs SDK, _env.py.

RCMS lifecycle phase:
    None — diagnostic utility. Safe to run at any time without
    affecting live calls.
"""

from __future__ import annotations

import json
import sys

import truststore
truststore.inject_into_ssl()

from elevenlabs import ElevenLabs

from _env import load_env, resolve_env_path


def to_dict(obj):
    """Coerce ElevenLabs SDK return values (Pydantic v2 / v1 / dict) to plain dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return obj
    return obj


def main() -> int:
    env_path = resolve_env_path()
    env = load_env(env_path)

    api_key = env.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        print(f"error: ELEVENLABS_API_KEY missing from {env_path}", file=sys.stderr)
        return 1

    agent_id = env.get("ELEVENLABS_AGENT_ID", "").strip()
    if not agent_id:
        print(f"error: ELEVENLABS_AGENT_ID missing from {env_path}", file=sys.stderr)
        return 1

    client = ElevenLabs(api_key=api_key)
    agent = client.conversational_ai.agents.get(agent_id=agent_id)
    print(json.dumps(to_dict(agent), indent=2, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
