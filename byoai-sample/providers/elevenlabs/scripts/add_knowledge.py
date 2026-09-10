#!/usr/bin/env python3
"""
add_knowledge — Upload the knowledge base document and attach it to the agent

Role:
    Uploads the Innovation Hub knowledge base document to ElevenLabs and
    attaches it to the agent. Idempotent — reuses the existing document
    if already uploaded, refreshes content if the source file changed.

Does not own:
    Agent creation (create_agent.py), voice or prompt configuration
    (update_agent.py).

Dependencies:
    elevenlabs SDK, _env.py. Reads the knowledge document from
    providers/elevenlabs/agent/knowledge/.

RCMS lifecycle phase:
    None — provisioning-time script. Run after create_agent.py.

Run order:
    1. create_agent.py
    2. add_knowledge.py  ← this script
    3. update_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from elevenlabs import ElevenLabs

from _env import load_env, resolve_env_path


PROVIDER_ROOT = Path(__file__).resolve().parents[1]
KB_SOURCE = PROVIDER_ROOT / "agent" / "knowledge" / "innovation-hub-elevenlabs.md"
KB_NAME = "Innovation Hub – ElevenLabs Partnership"


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

    if not KB_SOURCE.is_file():
        print(f"error: knowledge source not found: {KB_SOURCE}", file=sys.stderr)
        return 1

    client = ElevenLabs(api_key=api_key)
    text = KB_SOURCE.read_text()

    # Reuse existing doc if present, otherwise upload a fresh one.
    existing = client.conversational_ai.knowledge_base.list(search=KB_NAME, page_size=30)
    doc = next(
        (d for d in existing.documents if getattr(d, "name", "") == KB_NAME),
        None,
    )
    if doc:
        doc_id = doc.id
        print(f"KB doc already exists: {doc_id} ({KB_NAME})")
        # Refresh contents in case the source text changed.
        client.conversational_ai.knowledge_base.documents.update(
            documentation_id=doc_id,
            name=KB_NAME,
        )
    else:
        created = client.conversational_ai.knowledge_base.documents.create_from_text(
            text=text,
            name=KB_NAME,
        )
        doc_id = created.id
        print(f"KB doc created: {doc_id} ({KB_NAME})")

    # Fetch current agent config and merge the KB reference into prompt.knowledge_base.
    agent = client.conversational_ai.agents.get(agent_id=agent_id)
    config = agent.conversation_config.dict() if hasattr(agent.conversation_config, "dict") else dict(agent.conversation_config)
    prompt_cfg = config.setdefault("agent", {}).setdefault("prompt", {})
    kb_list = prompt_cfg.get("knowledge_base") or []
    # Dedupe by id — idempotent re-runs.
    kb_list = [entry for entry in kb_list if entry.get("id") != doc_id]
    kb_list.append({
        "id": doc_id,
        "name": KB_NAME,
        "type": "text",
        "usage_mode": "auto",
    })
    prompt_cfg["knowledge_base"] = kb_list

    # After the initial update via `tools`, ElevenLabs stores canonical entries in
    # `tool_ids`. Submitting both in the same update is rejected — keep only the
    # reference list.
    prompt_cfg.pop("tools", None)

    updated = client.conversational_ai.agents.update(
        agent_id=agent_id,
        conversation_config=config,
    )

    print(f"Agent updated: {updated.agent_id}")
    print(f"  knowledge_base entries: {len(kb_list)}")
    for entry in kb_list:
        print(f"    - {entry['id']}  {entry['name']}  ({entry['usage_mode']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
