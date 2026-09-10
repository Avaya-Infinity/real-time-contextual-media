#!/usr/bin/env python3
"""
update_agent — Apply voice, prompt, tools, and turn-taking to the agent

Role:
    Applies the full agent configuration to an existing ElevenLabs agent:
    voice, system prompt, first message, handoff tool, end-call tool, and
    turn-taking settings. This is the authoritative definition of the
    Innovation Hub agent persona and its Infinity integration contract.
    Run after create_agent.py and add_knowledge.py.

Does not own:
    Agent creation (create_agent.py), knowledge base attachment
    (add_knowledge.py). Does not modify the bridge server — all
    configuration here lives entirely in the ElevenLabs platform.

Dependencies:
    elevenlabs SDK, _env.py.

RCMS lifecycle phase:
    None — provisioning-time script. Run after add_knowledge.py.
    Re-run any time the agent persona, prompt, or tool configuration changes.

Run order:
    1. create_agent.py
    2. add_knowledge.py
    3. update_agent.py  ← this script
"""

from __future__ import annotations

import sys

import truststore
truststore.inject_into_ssl()

from elevenlabs import ElevenLabs

from _env import load_env, resolve_env_path


FIRST_MESSAGE = (
    "Hi {{firstName}}, thanks for calling Innovation Hub! "
    "This is Hope — how can I help you today?"
)

# Turn-taking config. ElevenLabs platform default is turn_eagerness="normal";
# we're switching to "patient" so the agent waits longer for the customer to
# finish hesitations and incomplete thoughts. Other turn fields are sent
# explicitly so platform-default drift can't silently change them on us.
# Addresses the agent-jumps-mid-utterance class of issues separately from
# the wire-level handoff/transcript ordering behavior that bot_elevenlabs.py
# already mitigates at the wire layer with a brief monitor-emit defer.
TURN_CONFIG = {
    "mode": "turn",
    "turn_model": "turn_v2",
    "turn_eagerness": "patient",
    "turn_timeout": 7.0,
    "speculative_turn": False,
    "retranscribe_on_turn_timeout": False,
    "silence_end_call_timeout": -1.0,
    "initial_wait_time": None,
    "spelling_patience": "auto",
    "soft_timeout_config": {
        "message": "Hhmmmm...yeah.",
        "timeout_seconds": -1.0,
        "use_llm_generated_message": False,
    },
}

SYSTEM_PROMPT = """You are Hope, a specialist at Innovation Hub — a premier technology innovation \
center helping enterprise customers explore and adopt cutting-edge contact center \
and communications solutions.

## Your identity
You work for Innovation Hub. Always say you're with Innovation Hub if asked.
Never claim to be human if sincerely asked.

## Who you're speaking with
{{#if firstName}}You are speaking with {{firstName}} {{lastName}}.{{/if}}
{{#if email}}Their email on file is {{email}}.{{/if}}
{{#if caseId}}
They have an active case:
- Subject: {{caseSubject}}
- Detail: {{caseDescription}}
{{/if}}

## Call context
- Direction: {{call_direction}}
- Called: {{call_to}}
- Caller: {{call_from}}

## How to open
Greet by first name if available. Acknowledge the case naturally — never read \
it back robotically.
Good: "Hi Todd, I see you're following up on your Avaya Infinity evaluation — \
happy to help move that forward."
Bad: "I see your case subject is Avaya Infinity Platform Demo."

## Conversation style
- Warm, confident, curious — you genuinely enjoy helping
- Phone call cadence — keep responses under 3 sentences unless asked for detail
- Ask one good question at a time
- Never fabricate details not provided to you

## Transfer Protocol (REQUIRED — follow exactly)
When the caller indicates they want to speak with a human, agent, or live \
person, you MUST immediately invoke the `transfer_to_agent` tool. Do not \
ask the caller to confirm the request, restate the topic, or gather \
additional information before invoking the tool — the caller's request is \
itself the confirmation; honor it gratefully and execute. As you invoke \
the tool, say something like:

"Of course, [first name]. I'm transferring you to a live agent who can help \
you with [brief context: their case, their question, etc.]. Thanks for \
calling Innovation Hub, and you'll be connected with someone shortly."

Adapt the wording naturally — reference what the caller asked about, mention \
their name if known, keep the tone warm. The acknowledgment should be 3-4 \
sentences (longer than a curt "transferring you now") to give the caller a \
clear, complete handoff moment.

The `transfer_to_agent` tool invocation is mandatory whenever the caller asks \
for a human or live agent. Saying the acknowledgment without invoking the \
tool is a complete failure to transfer the caller.

The `transfer_to_agent` tool is your final action in the conversation. After \
invoking it, do not generate any further response — the caller will be \
connected without further speech from you.

## End Call Protocol (REQUIRED — follow exactly)
When the caller indicates they are done, satisfied, all set, or ready \
to hang up, you MUST immediately invoke the `end_call` tool. Do not \
ask the caller to confirm — their completion signal is itself the \
confirmation; honor it gratefully and execute. As you invoke the tool, \
say something like:

"Glad I could help, [first name]. Thanks for calling Innovation Hub — \
have a great day."

Adapt the wording naturally — keep the tone warm. The closing line \
should be brief (one sentence) — the caller is ready to hang up, not \
looking for more conversation.

The `end_call` tool invocation is mandatory whenever the caller \
indicates they are done. Saying the closing line without invoking the \
tool leaves the call hanging.

The `end_call` tool is your final action in the conversation. After \
invoking it, do not generate any further response — the call will \
disconnect after your closing line plays.

Use `end_call` only when the caller is genuinely done. If they want a \
human, use `transfer_to_agent` instead."""

HANDOFF_TOOL = {
    "type": "client",
    "name": "transfer_to_agent",
    "description": (
        "Hand off to a live human agent. Use immediately when the customer asks "
        "for a human, is frustrated, or the issue is beyond your scope. Don't "
        "wait to be asked twice."
    ),
    # Force the agent to speak before firing the tool call. Without this,
    # the LLM emits transfer text via agent_response while the tool fires
    # concurrently — Infinity then tears down the session before the audio
    # finishes synthesising, so the caller hears silence. ElevenLabs's
    # analogue of the handoff-drain pattern used by other streaming
    # providers (Gemini, OpenAI Realtime).
    #
    # `pre_tool_speech` is an ENUM ("auto" | "force" | "off"), not a literal
    # string — the actual transfer line is LLM-generated from the system
    # prompt. `force_pre_tool_speech` (bool) appears redundant with the
    # "force" enum value but is sent for safety; harmless either way.
    "force_pre_tool_speech": True,
    "pre_tool_speech": "force",
    # Don't let the caller talk over the transfer line — once the handoff
    # decision is made, commit to it.
    "disable_interruptions": True,
    "parameters": {
        "type": "object",
        "properties": {
            "queue_id": {
                "type": "string",
                "description": "Queue ID to transfer to",
            },
            "reason": {
                "type": "string",
                "description": "Brief reason for the handoff",
            },
        },
        # queue_id IS load-bearing in required[]. Empirical testing showed
        # that removing queue_id from required[] re-introduces a
        # verbatim-duplicate-acknowledgment failure mode where the agent
        # speaks the transfer line twice. The `required` declaration acts
        # as a behavioral anchor for the LLM beyond its literal contract —
        # it signals "this tool call has a hard contract; honor it; the
        # populated result satisfies it; stop." Combined with
        # bot_elevenlabs.py's "default-queue" placeholder substitution and
        # the prompt's "do not generate any further response" instruction,
        # the three layers together produce clean single-acknowledgment
        # behavior. Removing any one re-introduces failure modes the others
        # don't cover.
        "required": ["queue_id", "reason"],
    },
}

# End-call built-in system tool. Closes the upstream ElevenLabs WS
# cleanly when the agent invokes it; the bridge's ElevenLabs receive
# loop catches the clean close and emits success-context bot.ended.
#
# `pre_tool_speech: "force"` is load-bearing: without it, ElevenLabs
# closes the upstream WS while the goodbye line is still synthesising
# and the caller hears silence. Same drain rationale as HANDOFF_TOOL —
# the ElevenLabs analogue of the closure-quiescence pattern used by
# other streaming providers. `disable_interruptions: True` mirrors
# HANDOFF_TOOL — once the completion signal is acknowledged, commit to
# the closing line without barge-in.
#
# `description` is set explicitly with a keyword-list trigger pattern
# rather than relying on the SDK default. The empirically-validated
# trigger keywords ("done, satisfied, all set, or ready to hang up")
# give the underlying LLM concrete completion-signal patterns to match
# against; belt-and-suspenders against the persona prompt's instruction.
END_CALL_TOOL = {
    "name": "end_call",
    "description": (
        "End the call when the customer indicates they are done, "
        "satisfied, all set, or ready to hang up."
    ),
    "force_pre_tool_speech": True,
    "pre_tool_speech": "force",
    "disable_interruptions": True,
    "response_timeout_secs": 20,
    "params": {"system_tool_type": "end_call"},
}


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

    shared_voice_id = env.get("ELEVENLABS_VOICE_ID", "").strip()
    if not shared_voice_id:
        print(f"error: ELEVENLABS_VOICE_ID missing from {env_path}", file=sys.stderr)
        return 1

    shared_voice_owner = env.get("ELEVENLABS_VOICE_OWNER", "").strip()
    if not shared_voice_owner:
        print(f"error: ELEVENLABS_VOICE_OWNER missing from {env_path}", file=sys.stderr)
        return 1

    shared_voice_name = env.get("ELEVENLABS_VOICE_NAME", "").strip()
    if not shared_voice_name:
        print(f"error: ELEVENLABS_VOICE_NAME missing from {env_path}", file=sys.stderr)
        return 1

    client = ElevenLabs(api_key=api_key)

    # Ensure the shared voice is in this account's library; reuse if already added.
    # Shared voices must first be added to the account; the SDK returns a
    # library-scoped voice_id that we then pass to the agent.
    library_voices = client.voices.get_all()
    existing_voice = next(
        (v for v in library_voices.voices if v.name == shared_voice_name),
        None,
    )
    if existing_voice:
        voice_id = existing_voice.voice_id
        print(f"Voice already in library: {voice_id} ({existing_voice.name})")
    else:
        added = client.voices.share(
            public_user_id=shared_voice_owner,
            voice_id=shared_voice_id,
            new_name=shared_voice_name,
        )
        voice_id = added.voice_id
        print(f"Voice added to library: {voice_id} ({shared_voice_name})")

    updated = client.conversational_ai.agents.update(
        agent_id=agent_id,
        conversation_config={
            "agent": {
                "prompt": {
                    "prompt": SYSTEM_PROMPT,
                    "llm": "gpt-4o-mini",
                    "tools": [HANDOFF_TOOL],
                    "built_in_tools": {
                        "end_call": END_CALL_TOOL,
                    },
                },
                "first_message": FIRST_MESSAGE,
                "language": "en",
            },
            "tts": {
                "model_id": "eleven_turbo_v2",
                "voice_id": voice_id,
                "stability": 0.45,
                "similarity_boost": 0.80,
                "speed": 1.0,
            },
            "turn": TURN_CONFIG,
        },
    )

    print(f"Agent updated: {updated.agent_id}")
    print(f"  name:       {updated.name}")
    print(f"  voice_id:   {voice_id} ({shared_voice_name})")
    print(f"  tts model:  eleven_turbo_v2")
    print(f"  stability:  0.45  similarity_boost: 0.80  speed: 1.0")
    print(f"  turn:       eagerness=patient  timeout=7.0  model=turn_v2")
    print(f"  tools:      {[HANDOFF_TOOL['name']]}")
    print(f"  built-in:   end_call (force_pre_tool_speech)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
