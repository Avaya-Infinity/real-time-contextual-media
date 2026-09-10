"""
bot_xai — xAI Grok Voice provider for the RCMS Virtual Agent (bot) service

Role:
    Implements the xAI provider — a translator between an Infinity
    RCMS bot session and an xAI Grok Voice Realtime session. The
    upstream is a stateful WebSocket; xAI Grok Realtime exposes the
    model directly with no hosted agent surface (no dashboard
    prompt, no platform-side tools, no first-message field). The
    bridge owns every piece of orchestration:

        * **System prompt composition** — `_load_base_prompt` resolves
          a four-tier chain (`XAI_SYSTEM_PROMPT_FILE` env →
          `XAI_SYSTEM_PROMPT` env → sibling `system_prompt.md` →
          `_DEFAULT_INSTRUCTIONS` constant), and `_compose_instructions`
          interpolates CRM context fields from
          `bot.start.payload.context` into the resolved base via Python
          `.format()`; the composed instructions are sent in the
          `session.update` frame on connect. The persona name (`Grok`) is hardcoded
          in the template; the voice name is independent (see
          `XAI_VOICE` below).
        * **Tool definitions** — `_TRANSFER_TO_AGENT_TOOL` and
          `_END_SESSION_TOOL` declared inline in this module and
          registered via `session.update.session.tools[]`. Both are
          bridge-implemented; xAI Grok Realtime does not provide
          platform tools. **Tool description text must be factual
          and passive** — describe what the tool does, not how the
          model should behave with it. Imperative or directive
          language in the `description` field of either tool caused
          `grok-voice-think-fast-1.0` to route the description into
          conversation context as if it were a system prompt and
          silently suppress `response.function_call_arguments.done`
          for the entire call. Keep descriptions factual-passive on
          `grok-voice-think-fast-2.0` until live calls prove otherwise.
        * **Conversation initiation** — `_on_session_updated` fires
          on the `session.updated` ack and sends a proactive
          `response.create` so the agent speaks first; without
          this trigger, the agent stays silent until the caller
          speaks.
        * **Output audio chunking and pacing** — xAI Grok Realtime
          emits variable-size audio chunks; the bridge buffers
          them into pacer-aligned chunks via
          `_enqueue_output_audio` so the IngressStreamer's
          `chunk_duration_ms` cadence is honored.
        * **Two-path termination orchestration** — bridge owns both
          drain pipelines: `_wait_for_quiescence_and_emit` for the
          handoff path (`transfer_to_agent` toolCall) and
          `_wait_for_quiescence_and_emit_session_end` for the
          self-service-complete path (`end_session` toolCall).
        * **Per-turn transcript handling** — bot transcript deltas
          arrive on `response.output_audio_transcript.delta` events keyed
          by `response_id`; flush on `response.audio_transcript.
          done`. Caller transcripts arrive whole on
          `conversation.item.input_audio_transcription.completed`
          when xAI emits them — the bridge consumes the events but
          does not explicitly request transcription in
          `session.update` (the xAI session schema has no
          documented opt-in field for input transcription).

xAI Grok Realtime architecture (relevant to this implementation):
    WebSocket endpoint and authentication: the bridge connects to
        `wss://api.x.ai/v1/realtime` with an `Authorization: Bearer
        <api_key>` header. xAI does not require a beta opt-in
        header on the WebSocket handshake.
    Model resolution: model id resolves in this order — `XAI_MODEL`
        environment variable (deployment override), then the suffix
        after `xai:` in the inbound `bot.start.payload.botId`, then
        the built-in default `DEFAULT_XAI_MODEL`
        (`grok-voice-think-fast-2.0`). The chosen value is sent
        as `?model=` on the WebSocket URL and stored on the
        `BotConversation`.
    Voice configuration: `XAI_VOICE` environment variable (default
        `"ara"`) selects the TTS voice and is sent on
        `session.update.session.voice`. The voice name and the
        persona name are independent — a deployment can swap the
        voice without touching the persona prompt.
    Native µ-law / 8 kHz audio: the bridge declares
        `{type: "audio/pcmu", rate: 8000}` on both
        `session.audio.input.format` and
        `session.audio.output.format`. Symmetric formats enable a
        zero-transcode operating path when the bridge is started
        with `--codec PCMU`: frames are base64-wrapped/unwrapped
        only, no audioop calls in the hot loop. L16 / PCMA / G722
        codecs transcode to/from 8 kHz µ-law via audioop.
    Two-step session bringup: `session.created` (informational,
        logged and discarded) precedes `session.updated`. The
        `session.updated` ack is the "configuration accepted"
        signal and triggers `_on_session_updated`, which sends
        the proactive greeting and emits `bot.started` to
        Infinity.
    `session.update` schema for audio: audio format is declared
        via a nested `audio` object with `input.format` and
        `output.format` blocks (each carrying `type` and `rate`),
        not as flat top-level fields. This is the xAI wire shape
        for audio configuration and is the schema the bridge
        sends in `_connect_xai`.
    Response lifecycle: a single conversation turn produces a
        stream of discrete events — `response.created`,
        `response.output_audio.delta` (many),
        `response.output_audio.done` (server-side audio
        generation finished — note the `output_audio` segment in
        the event name),
        `response.output_audio_transcript.delta` (many),
        `response.output_audio_transcript.done`, `response.done`. The
        bridge tracks two response-ID sets:
            - `active_response_ids` (list — handles overlapping
              responses) for targeted `response.cancel` on
              barge-in.
            - `completed_response_ids` (set) — server-side audio
              generation has finished, even though paced playout
              to Infinity may still be running. A barge-in for
              a completed response skips `response.cancel`
              because the cancel would always race-lose against
              the provider's already-finished state.
    Server VAD: xAI's session is configured with
        `turn_detection: {"type": "server_vad"}`. Speech-start
        events arrive as `input_audio_buffer.speech_started`;
        the bridge captures the caller turn-start timestamp on
        these events and dispatches barge-in when bot audio is
        playing out.
    Tool invocation: a function call surfaces as
        `response.function_call_arguments.done` carrying
        `call_id`, `name`, and a JSON `arguments` string. The
        bridge dispatches to `_handle_function_call`, which sends
        the result back via `conversation.item.create` with
        `type: "function_call_output"`.
    Terminal tool protocol: for terminal tools
        (`transfer_to_agent`, `end_session`), the bridge sends
        the `function_call_output` but **does not** send a
        follow-up `response.create`. Sending `response.create`
        after a terminal tool prompts the model to generate
        additional audio that lands after the tool's terminal
        envelope (LIVE_AGENT_HANDOFF or success-context
        bot.ended) has been emitted; the audio reaches Infinity
        after the bot leg is cut, leaking a transcript turn.
        Suppressing `response.create` at the wire level removes
        the trigger. The drain pipelines still run so the
        in-flight preamble or closing-line audio plays out
        before the terminal envelope is emitted. For
        non-terminal tools, the bridge sends `response.create`
        so the model can speak the tool result.
    Barge-in: triggered by `input_audio_buffer.speech_started`
        while `audio_playing_out` is True. `_handle_barge_in`
        clears the IngressStreamer queue, sends a `lastf=true`
        flag to Infinity on the current segment, and issues
        `response.cancel` for every active response that has
        not already completed server-side.

Does not own:
    Provider routing by botId (owned by the bot dispatcher in
        bot_service.py — this plugin is registered against the
        `xai:` prefix and invoked through handle_message after
        the dispatcher has matched).
    RCMS session lifecycle, JWT authentication, and Infinity-side
        WebSocket transport (owned by bridge_server.py).
    Audio frame pacing and ingress queue management (owned by
        IngressStreamer in bridge_server.py — this plugin queues
        chunks via _send_ingress_chunked and otherwise stays out
        of the cadence path).
    Speech recognition, turn detection (server VAD), language
        model inference, and voice synthesis (owned by xAI Grok
        Realtime; surfaced to the bridge via the WebSocket
        protocol's events).

Dependencies:
    websockets: the upstream xAI Realtime WebSocket client.
    audioop (stdlib; on Python 3.13+ install audioop-lts as a
        drop-in): µ-law decode/encode and resampling for the
        non-PCMU codec paths. Optional — gated by the
        `_AUDIOOP_AVAILABLE` flag. When unavailable, the bridge
        accepts only PCMU sessions and rejects L16 / PCMA / G722
        at bot.start time.
    G722 (optional): wideband codec encode/decode; gated by
        bridge_server.G722_AVAILABLE — when absent,
        G722-negotiated sessions are rejected at bot.start time
        with BACKEND_START_FAILED.
    truststore (optional): used to build outbound TLS contexts
        off the OS trust store so corporate TLS-interception
        roots (e.g. Zscaler) are honored. Falls back to the
        Python CA bundle if not installed.
    bridge_server.ServicePlugin: base class establishing the
        plugin contract (name, message_types, handle_message,
        on_session_ended, shutdown).
    bridge_server.BridgeServer: provides ingress_streamer (paced
        delivery to Infinity), get_next_sequence (per-client
        outbound sequence counter), session_config (codec /
        sample rate negotiation results), and the
        send_bot_ended_with_*_context helper family that emits
        the spec-correct success / failure / disconnect shapes.
    monitor (optional): SSE event stream for operational
        dashboards. Imported lazily; falls back to a no-op shim
        if absent.

RCMS lifecycle:
    Phase 1 (Start): handle_message routes bot.start to
        _handle_bot_start, which validates the botId prefix
        (`xai:`), resolves the model id, reads `XAI_API_KEY`
        (with `botCredentials` fallback for per-call overrides),
        negotiates codec / sample rate, composes the system
        prompt, builds a BotConversation, registers a
        playout-done callback with the IngressStreamer, connects
        upstream via _connect_xai, launches _xai_recv_loop as a
        background task, and waits for `session.updated` before
        emitting bot.started and the proactive greeting in
        _on_session_updated. Every validation failure emits
        bot.ended with a non-200 status via the failure-context
        helper.
    Phase 2 (During): ingest_audio_chunk transcodes Infinity-side
        audio to 8 kHz µ-law and forwards as
        `input_audio_buffer.append`. _xai_recv_loop drains the
        upstream WS and dispatches each event to
        _handle_xai_message. Audio deltas are transcoded and fed
        through _enqueue_output_audio's accumulator into
        pacer-aligned chunks. Bot transcript deltas accumulate
        per response and flush on transcript.done; caller
        transcripts arrive whole when the upstream emits them.
        Server VAD speech-start triggers turn-start timestamp
        capture and (when bot audio is playing out) barge-in
        dispatch.
    Phase 3 (Closure): four termination shapes converge on this
        plugin —
            (a) caller-disconnect → on_session_ended emits
                CALLER_DISCONNECTED via the disconnect-context
                helper;
            (b) Infinity-driven bot.end → _handle_bot_end acks
                with a manual bot.ended build;
            (c) `transfer_to_agent` toolCall →
                _handle_function_call stashes the
                LIVE_AGENT_HANDOFF and launches the handoff
                drain; _emit_pending_handoff fires the
                bot.feature plus a manual bot.ended (absent-
                status shape) after the audio queue drains;
            (d) `end_session` toolCall → _handle_function_call
                stashes a session-end latch and launches the
                session-end drain; _emit_session_end_complete
                fires success-context bot.ended after the audio
                queue drains.
        _shutdown_conversation is the single resource-release
        path invoked from every removal site.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start /
        bot.started / bot.end / bot.ended / bot.feature
        (TRANSCRIPT, LIVE_AGENT_HANDOFF ftypes).
    RCMS spec §Error Handling — bot.start failures answered via
        bot.ended-with-status; only schema-impossible cases
        (missing endpointId) escape via session.error.
    RCMS spec §Status Codes — 400 / 501 / 503 codes consumed by
        the failure-context helper; CALLER_DISCONNECTED reason
        value on the disconnect-context helper.
    RCMS spec §Media Encoding Options — base64 and binary
        transports.
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire
    shape and bridge/schema/rcms.schema.md for behavioral notes —
    including bot.ended's payload.context.status nesting and the
    workflow's byobotEndContext / byobotLiveAgentHandoff
    consumption patterns.

    xAI Grok Voice Realtime API protocol — `session.update` with
        nested `audio.input.format` / `audio.output.format`
        blocks, `session.created` / `session.updated`,
        `input_audio_buffer.append` /
        `input_audio_buffer.speech_started` /
        `speech_stopped` / `committed`,
        `response.create` / `response.created` /
        `response.output_audio.delta` / `response.output_audio.done` /
        `response.output_audio_transcript.delta` /
        `response.output_audio_transcript.done` / `response.done` /
        `response.cancel` /
        `response.function_call_arguments.done`,
        `conversation.item.create`,
        `conversation.item.input_audio_transcription.completed`,
        `error`.

See also:
    BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload field reference
    BUILDERS_GUIDE.md §3 Phase 2 — audio frames and IngressStreamer
    BUILDERS_GUIDE.md §3 Phase 3 — bot.ended status semantics and
        the termination ladder
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any, Dict, Optional

import ssl as _ssl

try:
    import audioop  # stdlib; on Python 3.13+ install audioop-lts as a drop-in replacement
    _AUDIOOP_AVAILABLE = True
except ImportError:
    _AUDIOOP_AVAILABLE = False
    audioop = None  # type: ignore[assignment]

try:
    import truststore as _truststore
except ImportError:
    _truststore = None

import websockets
from websockets.server import WebSocketServerProtocol

from bridge_server import (
    G722_AVAILABLE,
    ServicePlugin,
    format_compact_json,
    log_message_exchange,
)

if TYPE_CHECKING:
    from bridge_server import BridgeServer

logger = logging.getLogger(__name__)

try:
    from monitor import emit as _monitor_emit
except ImportError:
    def _monitor_emit(event_type, data):
        pass


# ── Constants ─────────────────────────────────────────────────────────────────

XAI_REALTIME_BASE_URL = "wss://api.x.ai/v1/realtime"
DEFAULT_XAI_MODEL = "grok-voice-think-fast-2.0"

XAI_SAMPLE_RATE = 8000           # µ-law is always 8 kHz; declared on the
                                  # session.update audio block, see _connect_xai.

DEFAULT_VOICE = "ara"

# Bound on the pre-ingress-ready buffer (see BotConversation.ingress_buffer
# and the gate in _handle_xai_message's response.output_audio.delta branch).
# Infinity's ingress path opens after it begins emitting egress; the buffer
# absorbs any xAI audio that lands before that point. Cap is conservative
# — exceeding it indicates an upstream stall, not normal greeting overlap.
# Oldest chunk is dropped on overflow.
_INGRESS_BUFFER_MAX_CHUNKS = 50

# Polling cadence for the two drain loops in _wait_for_quiescence_and_emit
# and _wait_for_quiescence_and_emit_session_end. Each iteration sleeps this
# long, then checks whether the IngressStreamer queue has gone empty. The
# sleep itself is the grace period — long enough for any chunk crossing the
# network during the iteration to land in the queue before the empty check.
_QUIESCENCE_POLL_MS = 250

# Safety net on both drain loops. Trips only on genuine upstream failure
# (e.g. xAI WS hung); a healthy drain completes in well under a second.
# Trip logs at WARNING and emits the terminal envelope (LIVE_AGENT_HANDOFF
# or success-context bot.ended) anyway so the workflow does not stall.
_QUIESCENCE_DEADLOCK_SAFETY_S = 30.0

# Maximum WebSocket frame size for the upstream xAI connection. 8 MiB
# accommodates the largest audio frames xAI emits without triggering
# websockets.exceptions.PayloadTooBig on long-form responses.
_WS_MAX_FRAME_BYTES = 2**23


def _resolve_language_name(code: str) -> str:
    """Map a BCP-47 / ISO 639-1 code to a human-readable language name.

    Used for prompt interpolation so directives read naturally ("Always
    respond in English" rather than "in en-US"). Unknown codes pass
    through verbatim. Empty/missing defaults to "English".
    """
    s = (code or "").strip()
    if not s:
        return "English"
    base = s.lower().split("-")[0]
    return {
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "pt": "Portuguese",
        "ja": "Japanese",
        "zh": "Chinese",
        "ko": "Korean",
    }.get(base, s)


# Tier-4 fallback for `_load_base_prompt` (see the staticmethod on
# XaiService). Also returned by `_compose_instructions` when
# `.format()` raises on a placeholder mismatch — `_DEFAULT_INSTRUCTIONS`
# carries no placeholders so it is safe to return unformatted on either
# path. The deployment-tuned base prompt lives in the sibling
# `system_prompt.md`; the design rationale (preamble pattern for
# tool-call ordering, queue-empty drain coordination for terminal
# tools) is captured in those `system_prompt.md` section headers.
_DEFAULT_INSTRUCTIONS = (
    "You are Grok, a helpful and professional AI customer service agent. "
    "Be concise, empathetic, and professional."
)

# transfer_to_agent tool definition.
#
# CRITICAL: The `description` field must use factual, passive language
# describing what the tool does — not imperative language describing
# how the model should behave with it. On `grok-voice-think-fast-1.0`,
# imperative descriptions (e.g. "MUST be called immediately…", "Do not
# end the response turn without calling this tool") are routed into
# conversation context as if they were system instructions, silently
# suppressing `response.function_call_arguments.done` for the entire
# call. Bisect confirmed N=2/2 zero tool fires with imperative
# phrasing, N=1/1 first-attempt fire after reverting to passive
# phrasing. Keep this style on 2.0 until live calls prove otherwise.
#
# `parameters.required` stays empty — the xAI Voice API cookbook tool
# examples don't declare a `required` field for similar tools.
#
# Bridge maps the result to a bot.feature LIVE_AGENT_HANDOFF with empty
# queueId; Infinity workflow routes by exit path.
_TRANSFER_TO_AGENT_TOOL = {
    "type": "function",
    "name": "transfer_to_agent",
    "description": (
        "Transfer the caller to a live human agent. "
        "Call this when the caller requests to speak with a person, agent, or human."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason for the transfer.",
            },
        },
        "required": [],
    },
}

# end_session tool definition — self-service-complete signal. The
# description uses the same factual/passive style as
# `_TRANSFER_TO_AGENT_TOOL` for the same reason: imperative descriptions
# get routed into conversation context on grok-voice-think-fast-1.0
# (re-validate on 2.0) and silently suppress tool dispatch. Bridge maps
# a fired end_session
# tool to a success-context bot.ended after the queue-empty drain.
_END_SESSION_TOOL = {
    "type": "function",
    "name": "end_session",
    "description": (
        "End the call session. "
        "Call this when the caller indicates they are done, satisfied, "
        "all set, or ready to hang up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason the call is being ended.",
            },
        },
        "required": [],
    },
}


# ── Per-call state ─────────────────────────────────────────────────────────────

@dataclass
class BotConversation:
    """Per-call state for one Infinity-side endpoint bridged to one xAI Grok Realtime session.

    Allocated in `_handle_bot_start` and stored in
    `XaiService._conversations` under `(session_id, endpoint_id)`
    for the lifetime of the call. Removed by `_handle_bot_end`
    (Infinity-driven teardown) or `on_session_ended` (caller
    disconnect / platform-driven session end).
    `_shutdown_conversation` is the single resource-release path;
    every removal site calls it.

    Field groups:

        * **Identity (set at construction, never mutated):**
          `session_id`, `endpoint_id`, `source` (rx / tx),
          `websocket` (the Infinity-side WS), `client_id`,
          `service`, `model` (resolved from botId suffix or
          `XAI_MODEL` env), `instructions` (composed by
          `_compose_instructions`), `language_code`,
          `codec_name`, `sample_rate`, `transport_encoding`.

        * **Upstream WebSocket:** `xai_ws` (the
          `websockets.WebSocketClientProtocol` connected to xAI
          Grok Realtime), `xai_recv_task` (the background
          coroutine draining `xai_ws`), `active` (cooperative
          shutdown flag consulted at the head of each recv-loop
          iteration).

        * **Termination tracking:** `bot_ended_sent` is flipped
          True by every code path that emits a terminal
          `bot.ended` (`send_bot_ended_with_*_context` helpers
          and the manual builds in `_handle_bot_end` /
          `_emit_pending_handoff`). Read by `on_session_ended`
          and the helpers themselves to suppress duplicate
          emissions when an outcome has already been signalled.

        * **Audio codec state:** `ratecv_in_state` and
          `ratecv_out_state` thread `audioop.ratecv` calls per
          direction (the stdlib resampler returns a fresh state
          per call and the two directions cannot share).
          `g722_decoder` and `g722_encoder` are lazily
          initialised when G722 is negotiated. The PCMU codec
          path bypasses these entirely — µ-law on both ends
          means base64-wrap and base64-unwrap are the only
          operations needed.

        * **Ingress readiness latch:** `ingress_ready` and
          `ingress_buffer` solve the timing skew where xAI starts
          streaming the greeting before Infinity's ingress path
          opens (Infinity opens its ingress only after emitting
          its first egress frame). Audio that lands before the
          latch flips is buffered (capped at
          `_INGRESS_BUFFER_MAX_CHUNKS`); the latch flips on the
          first call to `ingest_audio_chunk` and flushes the
          buffer.

        * **Barge-in state:** `audio_playing_out` is True
          whenever there is bot audio queued at the
          IngressStreamer that has not yet been fully paced out
          to Infinity. Set True when a chunk is queued; cleared
          by the IngressStreamer's playout-done callback
          (registered in `_handle_bot_start`) when the queue
          drains, or explicitly by `_handle_barge_in`. This flag
          — not `active_response_ids` — is the gate for
          VAD-triggered barge-in: xAI generates faster than
          playout, so `response.done` can land while audio is
          still paced out for several seconds afterwards. The
          list of active responses tracks generation, the flag
          tracks playout, and barge-in needs to react to
          playout.

        * **Output audio accumulator:** `ingress_accumulator` and
          `ingress_chunk_size`. xAI emits variable-size audio
          delta chunks; the IngressStreamer paces at
          `chunk_duration_ms` per chunk regardless of frame
          content duration. The accumulator buffers until a full
          pacer-aligned flush is available; misalignment between
          this boundary and the streamer's pacing interval would
          split each flush into mismatched chunks paced
          uniformly, producing sub-real-time delivery and buffer
          underruns at Infinity. Coupled to
          `IngressStreamer.chunk_duration_ms` via
          `_chunk_size_for`.

        * **Response lifecycle tracking:** `active_response_ids`
          (list — handles overlapping-response edge case)
          carries response IDs added on `response.created` and
          removed on `response.done`. Used to issue targeted
          `response.cancel` on barge-in.
          `completed_response_ids` (set) tracks response IDs
          whose server-side audio generation has finished
          (signalled by `response.output_audio.done`). Barge-in
          uses this set to skip `response.cancel` sends that
          would always race-lose against the provider's
          already-completed state.

        * **Transcript accumulators:** `transcript_deltas` keys
          per `response_id` for accumulating
          `response.output_audio_transcript.delta` events; popped
          into `pending_bot_text` on
          `response.output_audio_transcript.done`.
          `pending_customer_text` holds the latest cumulative
          caller transcript from
          `conversation.item.input_audio_transcription.updated`
          (xAI's name for OpenAI's `.delta`). BL-003: BOT is
          held until CUSTOMER for the turn is flushed, or until
          `response.done` when no caller turn is in flight.
          `customer_transcript_emitted` is True after the first
          CUSTOMER emit of a VAD turn so 2.0's duplicate
          `.completed` (early, then again after commit) does
          not double the Infinity transcript.

        * **Turn-start timestamps:** `customer_turn_started_at`
          is captured on `input_audio_buffer.speech_started`
          (server VAD detected speech onset).
          `bot_turn_started_at` is captured on
          `response.created` (the moment xAI accepted the
          response request). Both are used as
          `payload.transcript.startTsMs` on the eventual
          TRANSCRIPT emit and reset to None after the
          corresponding flush. Capturing turn-start at the
          actual onset rather than at flush time preserves
          correct chronological ordering on the wire even when
          the bot's transcript completion event lands ahead of
          the caller's transcription pipeline.

        * **Deferred live-agent handoff:** `pending_handoff`
          stashes the `bot.feature LIVE_AGENT_HANDOFF` payload
          while the IngressStreamer queue drains;
          `pending_handoff_args` holds the raw tool argument
          values (read by `_emit_pending_handoff` to log the
          post-emit reason string); `handoff_task` is the drain
          coroutine running `_wait_for_quiescence_and_emit`.
          The deferred path exists because the
          `transfer_to_agent` `function_call` arrives before
          the acknowledgment audio finishes streaming;
          emitting LIVE_AGENT_HANDOFF immediately would have
          Infinity tear down playback mid-utterance.

        * **Deferred self-service-complete:** `pending_session_end`
          is a boolean latch (no stashed payload — the
          success-context `bot.ended` carries no per-call
          payload, only the static success status). Set True
          on the `end_session` tool invocation;
          `session_end_task` runs
          `_wait_for_quiescence_and_emit_session_end` until the
          audio queue drains, then
          `_emit_session_end_complete` fires the success-context
          emit.
    """

    session_id: str
    endpoint_id: str
    source: str
    websocket: WebSocketServerProtocol
    client_id: str
    service: str
    model: str
    instructions: str
    language_code: str
    codec_name: str          # Infinity-side negotiated codec (PCMU, L16, PCMA, G722)
    sample_rate: int         # Infinity-side sample rate
    transport_encoding: str

    # Upstream WebSocket to xAI Realtime.
    xai_ws: Optional[Any] = None
    xai_recv_task: Optional[asyncio.Task] = None
    active: bool = False
    # Set True by send_bot_ended_with_*_context helpers after a successful
    # bot.ended emission (success / failure / disconnect). Read by
    # on_session_ended and the helpers themselves to suppress duplicate
    # disconnect emissions when self-service-complete, handoff, or failure
    # already signalled the outcome.
    bot_ended_sent: bool = False

    # Resampling state — audioop.ratecv returns a new tuple each call;
    # never share between in and out (rates may differ on non-PCMU codecs).
    ratecv_in_state: Any = None   # Infinity → xAI
    ratecv_out_state: Any = None  # xAI → Infinity

    # G722 codec objects (lazy-initialised; 16 kHz internal rate).
    g722_decoder: Any = None
    g722_encoder: Any = None

    # Ingress readiness latch — see class docstring "Ingress readiness latch"
    # group. Flipped True on the first ingest_audio_chunk; ingress_buffer
    # holds xAI audio chunks that landed before Infinity's ingress
    # path opened.
    ingress_ready: bool = False
    ingress_buffer: list = field(default_factory=list)

    # Barge-in gate — see class docstring "Barge-in state" group. Set True
    # when a chunk is queued at the IngressStreamer; cleared by the
    # streamer's playout-done callback when the queue drains, or
    # explicitly on barge-in.
    audio_playing_out: bool = False

    # Output audio accumulator — see class docstring "Output audio
    # accumulator" group. Coupled to IngressStreamer.chunk_duration_ms
    # via _chunk_size_for so each flush carries exactly one paced chunk.
    ingress_accumulator: bytearray = field(default_factory=bytearray)
    ingress_chunk_size: int = 0

    # Response lifecycle tracking — see class docstring "Response lifecycle
    # tracking" group. The list tracks generation; the set tracks
    # server-side audio completion.
    active_response_ids: list = field(default_factory=list)
    completed_response_ids: set = field(default_factory=set)

    # Transcript accumulators — see class docstring "Transcript
    # accumulators" group. Per-response keying for bot transcripts;
    # whole-turn flush for caller transcripts.
    transcript_deltas: Dict[str, str] = field(default_factory=dict)
    pending_customer_text: str = ""
    pending_bot_text: str = ""
    customer_transcript_emitted: bool = False
    # True once this response has queued at least one output_audio.delta.
    # 2.0 often fires terminal tools with no preamble audio; the
    # function-call handler uses this to decide whether to solicit a
    # follow-up spoken acknowledgment via response.create.
    response_had_audio: bool = False
    awaiting_terminal_speech: bool = False

    # Turn-start timestamps — see class docstring "Turn-start timestamps"
    # group. Captured at the actual onset event for each speaker.
    customer_turn_started_at: Optional[int] = None
    bot_turn_started_at: Optional[int] = None

    # Deferred LIVE_AGENT_HANDOFF — see class docstring "Deferred live-agent
    # handoff" group. Stashed on transfer_to_agent invocation; emitted by
    # _emit_pending_handoff after the IngressStreamer queue drains.
    pending_handoff: Optional[Dict[str, Any]] = None
    pending_handoff_args: Optional[Dict[str, Any]] = None
    handoff_task: Optional[asyncio.Task] = None

    # Deferred self-service-complete — see class docstring "Deferred
    # self-service-complete" group. Boolean latch (no stashed payload);
    # _emit_session_end_complete fires the success-context bot.ended.
    pending_session_end: bool = False
    session_end_task: Optional[asyncio.Task] = None


# ── Service plugin ─────────────────────────────────────────────────────────────

class XaiService(ServicePlugin):
    """Service plugin that proxies an Infinity RCMS bot session to an xAI Grok Voice Realtime session.

    Plugin contract:
        Subclass of `ServicePlugin`. Discovered by the plugin loader
        at bridge startup if `is_configured()` returns True
        (`XAI_API_KEY` set). Registered against the bridge's
        `ServiceRegistry` under the name `xai`. The bot dispatcher
        (`bot_service.py`) claims the RCMS `bot.start` / `bot.end`
        message types and routes per-call to this plugin based on
        the `xai:<model>` botId prefix; the plugin itself reports
        an empty `message_types` set.

    Raw-model framing:
        xAI Grok Voice Realtime exposes the model directly through
        a stateful WebSocket. There is no hosted agent surface
        (no dashboard prompt, no platform-side tools, no
        first-message field). The bridge owns every piece of
        orchestration:

            * **System prompt** — `_load_base_prompt` resolves
              a four-tier chain (`XAI_SYSTEM_PROMPT_FILE` env →
              `XAI_SYSTEM_PROMPT` env → sibling `system_prompt.md`
              → `_DEFAULT_INSTRUCTIONS` constant) for the base
              persona (Grok) and tool-protocol template;
              `_compose_instructions` interpolates CRM context
              fields from `bot.start.payload.context` and the
              language code into that base via Python `.format()`.
            * **Tool schemas** — `_TRANSFER_TO_AGENT_TOOL` and
              `_END_SESSION_TOOL` declared inline. Both are
              bridge-implemented; xAI Grok Realtime has no
              platform-side tool registry. Tool description
              text is factual and passive (describes what the
              tool does, not how the model should behave with
              it) — imperative descriptions cause the model to
              treat the description as a system prompt and
              suppress tool dispatch.
            * **Conversation initiation** —
              `_on_session_updated` fires on the
              `session.updated` ack and sends a proactive
              `response.create` so the agent speaks first.
              Without this trigger, the agent stays silent
              until the caller speaks.
            * **Output pacing** — xAI emits variable-size audio
              delta chunks; the bridge buffers them into
              pacer-aligned chunks via `_enqueue_output_audio`
              so the IngressStreamer's `chunk_duration_ms`
              cadence is honored.
            * **Termination drains** — both `transfer_to_agent`
              and `end_session` have their own bridge-side
              drain pipelines (`_wait_for_quiescence_and_emit`
              and `_wait_for_quiescence_and_emit_session_end`)
              that wait for the IngressStreamer queue to empty
              before emitting the terminal RCMS envelope, so
              the in-flight audio plays out before Infinity
              tears down playback.
            * **Terminal-tool suppression** — for
              `transfer_to_agent` and `end_session`, the bridge
              sends the `function_call_output` but does not
              follow up with `response.create`. This prevents
              the model from generating additional audio after
              the tool fires; without the suppression, that
              audio would land after the terminal envelope and
              leak a transcript turn.

        See the module docstring "Role" section for the full
        ownership boundary.

    Per-call state:
        `self._conversations` maps `(session_id, endpoint_id)` to
        a `BotConversation` instance for the lifetime of each
        call. Populated by `_handle_bot_start`, removed by
        `_handle_bot_end` and `on_session_ended`. The instance
        carries the upstream xAI WebSocket, the recv-loop task,
        codec state, the response-lifecycle tracking (active and
        completed response IDs), the deferred-handoff and
        deferred-session-end latches, the ingress-readiness
        latch, the output accumulator, the transcript
        accumulators, the turn-start timestamps, and the
        `audio_playing_out` barge-in gate.

    Lifecycle hooks:
        * `handle_message` — `bot.start` / `bot.end` dispatch.
        * `ingest_audio_chunk` — per-frame caller audio handoff
          to xAI Grok Realtime.
        * `on_session_ended` — caller-disconnect /
          platform-initiated session end.
        * `shutdown` — bridge process shutdown.

    Spec:
        RCMS spec §AI Bot Message Definitions — `bot.start` /
        `bot.started` / `bot.end` / `bot.ended` / `bot.feature`
        (TRANSCRIPT, LIVE_AGENT_HANDOFF ftypes).
        xAI Grok Voice Realtime API protocol — `session.update`
        with nested `audio.input.format` / `audio.output.format`
        blocks, `session.created` / `session.updated`,
        `input_audio_buffer.*`, `response.*`,
        `conversation.item.*`, `error`. Bearer token auth via
        `Authorization` header on the WebSocket handshake.
    """

    name = "xai"

    @classmethod
    def is_configured(cls) -> bool:
        """True iff XAI_API_KEY is set on the bridge server."""
        return bool(os.environ.get("XAI_API_KEY", "").strip())

    def __init__(self, server: "BridgeServer"):
        """Construct the plugin and bind it to the bridge server.

        Allocates the per-call conversation store keyed by
        `<session_id>:<endpoint_id>`. The bridge is responsible
        for calling `register_service` on this instance — see
        `register(server)` at module scope.

        Args:
            server: The owning `BridgeServer`. Stored as
                `self.server` (via the parent class).
        """
        super().__init__(server)
        self._conversations: Dict[str, BotConversation] = {}

    @property
    def message_types(self) -> set[str]:
        """Empty — the dispatcher routes `bot.start` / `bot.end` by botId prefix.

        The combined bot service owns the inbound message
        types; per-provider plugins are dispatched by the
        prefix on `payload.botId`. Returning an empty set here
        signals "no direct subscriptions" to the dispatcher.
        """
        return set()

    def _key(self, session_id: str, endpoint_id: str) -> str:
        """Build the conversation-store key — `<session>:<endpoint>`."""
        return f"{session_id}:{endpoint_id}"

    def _resolve_codec(self, session_id: str) -> str:
        """Look up the negotiated codec for a session, defaulting to `L16`.

        Reads from `BridgeServer.session_config` which the
        bridge populates from the inbound `session.start`.
        L16 is the spec-default when the workflow does not pin
        a codec.
        """
        return self.server.session_config.get(session_id, {}).get("codec_name", "L16")

    def _resolve_sample_rate(self, session_id: str, payload: Dict[str, Any]) -> int:
        """Resolve the negotiated sample rate, preferring the bot.start payload.

        Lookup order: `payload.sampleRate` (per-call override
        from the inbound `bot.start`) → `session_config[session_id].sample_rate`
        (set by `session.start`) → `8000` (the only spec-
        guaranteed rate for PCMU).
        """
        stored = self.server.session_config.get(session_id, {})
        return payload.get("sampleRate", stored.get("sample_rate", 8000))

    async def handle_message(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Plugin-contract entry point — dispatches `bot.start` / `bot.end` to their handlers.

        Invoked by the dispatcher after it has already routed
        by botId prefix. Branches on `data["type"]`; unknown
        types log at WARNING and are dropped.

        Args:
            websocket: Infinity-side WebSocket the message
                arrived on.
            client_id: Bridge-assigned client identifier for
                logging.
            data: Parsed inbound envelope. Reads `type`.

        Returns:
            None.
        """
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_bot_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_bot_end(websocket, client_id, data)
        else:
            logger.warning("[%s] Unhandled message '%s' in xAI service", client_id, msg_type)

    async def on_session_ended(self, session_id: str) -> None:
        """Handle Infinity-initiated session teardown for every conversation under a session id.

        Contract:
            Plugin-contract callback invoked by the bridge when
            the Infinity WebSocket signals end-of-session (the
            client connection closes, or the bridge processes
            an inbound `session.end`). Iterates every
            conversation keyed by `<session_id>:<endpoint_id>`
            and performs two actions per conversation, in
            order:

                1. **Disconnect-context bot.ended**: if no
                   termination signal has fired on this
                   conversation (`bot_ended_sent` is False) and
                   the Infinity WebSocket is still present,
                   dispatch
                   `send_bot_ended_with_disconnect_context` —
                   the shared bridge helper that emits a
                   bot.ended with `CALLER_DISCONNECTED`
                   semantics. Skipped when `bot_ended_sent` is
                   already True (the success/handoff paths set
                   it on their originator emits, so no double-
                   send).
                2. **Shutdown**: hand off to
                   `_shutdown_conversation` which cancels in-
                   flight tasks, closes the upstream xAI
                   WebSocket, and clears IngressStreamer state.

            **Why scan by prefix.** A session id can in
            principle host more than one conversation
            (concurrent bot legs). The dispatcher keys the
            store by `<session>:<endpoint>` so this scan
            captures every conversation that disconnect closes.

            Failure handling: the helper emit is independently
            try/excepted at WARNING. Shutdown still runs even
            if the wire emit fails — the in-process state must
            be reclaimed regardless.

        Args:
            session_id: The session whose conversations should
                be torn down.

        Returns:
            None. Side effects: zero or more `bot.ended`
            envelopes reach Infinity, and all matching entries
            are removed from `self._conversations`.
        """
        keys = [k for k in self._conversations if k.startswith(f"{session_id}:")]
        for key in keys:
            convo = self._conversations.pop(key, None)
            if convo:
                if not convo.bot_ended_sent and convo.websocket is not None:
                    try:
                        await self.server.send_bot_ended_with_disconnect_context(
                            convo.websocket,
                            convo.client_id,
                            convo.session_id,
                            convo.endpoint_id,
                            provider=self.name,
                            convo=convo,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[%s] disconnect-context bot.ended emit failed: %s",
                            convo.client_id, exc,
                        )
                await self._shutdown_conversation(convo)

    async def shutdown(self) -> None:
        """Tear down every conversation owned by this plugin during bridge shutdown.

        Contract:
            Plugin-contract callback invoked by the bridge on
            process shutdown. Drains the conversation store and
            runs `_shutdown_conversation` for each entry —
            cancelling drain tasks, closing upstream xAI
            WebSockets, and clearing IngressStreamer state. No
            wire emits are made (Infinity-side emits are owned
            by `on_session_ended`, which the bridge invokes
            separately when the Infinity WebSocket closes).

        Returns:
            None. Side effect: `self._conversations` is left
            empty.
        """
        for key in list(self._conversations.keys()):
            convo = self._conversations.pop(key, None)
            if convo:
                await self._shutdown_conversation(convo)

    # ---------------------------------------------------------------- bot.start

    async def _handle_bot_start(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Handle Infinity-driven `bot.start` for an xAI Grok Realtime session: validate, compose instructions, connect, ack-deferred.

        Contract:
            Phase 1 entry point dispatched by `handle_message`
            when the inbound message type is `bot.start` and the
            dispatcher has routed by `botId` prefix. Performs the
            full start-of-call sequence:

                1. **Validate `endpointId`**: missing endpointId
                   fails the `bot.ended` schema, so respond with
                   `session.error` (`MISSING_REQUIRED_FIELDS`,
                   501) instead. The only `session.error` exit
                   on this path; all subsequent failures use
                   `bot.ended`-with-status.
                2. **Validate `botId` prefix**: must start with
                   `xai:` (case-insensitive). Failures emit
                   `bot.ended` with `BAD_REQUEST` (400) /
                   `UNRECOGNIZED_BOTID_PREFIX` via the
                   failure-context helper.
                3. **Resolve model id**: `XAI_MODEL` environment
                   variable (deployment override) → suffix after
                   `xai:` from the botId → `DEFAULT_XAI_MODEL`
                   (`grok-voice-think-fast-2.0`).
                4. **Resolve API key**: prefer `XAI_API_KEY`
                   from the environment; fall back to per-call
                   credentials decoded from
                   `payload.botCredentials` (base64-wrapped JSON
                   `{"apiKey": "..."}`) via `_extract_api_key`.
                   Missing key → 503 `BACKEND_START_FAILED`.
                5. **Resolve codec / sample rate**: from
                   `session_config[session_id]` and the inbound
                   payload. Reject unsupported codecs (anything
                   outside PCMU / L16 / PCMA / G722) with 503
                   `BACKEND_START_FAILED`. Reject G722
                   negotiation when the optional G722 package is
                   not installed. Reject non-PCMU codecs when
                   audioop is unavailable — the bridge can run
                   an xAI session zero-transcode on PCMU but
                   every other codec needs audioop for the µ-law
                   conversion.
                6. **Compose instructions**: `_compose_instructions`
                   interpolates `{variable}` placeholders against
                   `payload.context` plus the language code and
                   call-context fields. The composed prompt is
                   per-call and stored on the
                   `BotConversation`.
                7. **Build `BotConversation`** from negotiated
                   codec, transport encoding from
                   `server.transport_encodings`, and the composed
                   instructions.
                8. **Register playout-done callback**: bind a
                   closure that clears
                   `convo.audio_playing_out` to the
                   IngressStreamer for this
                   `(session_id, endpoint_id)`. The streamer
                   invokes the callback when the egress queue
                   drains naturally (idle timeout or `is_last`
                   chunk sent) so VAD-triggered barge-in can
                   gate on actual playout state.
                9. **G722 decoder lazy-init** when negotiated.
                   Decoder init failure → 503
                   `BACKEND_START_FAILED`.
               10. **Connect to xAI** via `_connect_xai`. The
                   helper sends the `session.update` frame
                   including instructions, voice, audio format
                   blocks, server VAD, and tool registration.
                   Failure here → 503 `BACKEND_START_FAILED`.
               11. **Replace any pre-existing conversation**
                   under the same `(session_id, endpoint_id)`
                   by calling `_shutdown_conversation` on the
                   prior entry — protects against duplicate
                   `bot.start` racing the prior session's
                   teardown.
               12. **Activate**: set `convo.active = True`,
                   store in `self._conversations`, and start
                   `_xai_recv_loop` as a background task.

            **Note on `bot.started` deferral.** `bot.started` is
            not sent here. The `session.update` round-trip must
            complete first; `bot.started` is sent later from
            `_on_session_updated` after the `session.updated`
            ack arrives. Sending `bot.started` here would tell
            Infinity the bot is ready before xAI has actually
            accepted the configuration; the proactive greeting
            that follows would land on a half-configured
            session.

            Every error path uses
            `send_bot_ended_with_failure_context` with the
            appropriate RCMS status code, so the workflow's
            `byobotEndContext` consumption pattern routes the
            bot session to a non-200 terminal state. Per the
            RCMS spec, an unprocessable `bot.start` is failed
            via `bot.ended`-with-status, not `session.error`;
            the only `session.error` exit is the
            schema-impossible `MISSING_REQUIRED_FIELDS:
            endpointId` case in step 1.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.start`
            payload fields and `bot.started` ack shape.
            RCMS spec §Error Handling — failure-shape contract
            for unprocessable `bot.start` answered via
            `bot.ended`-with-status.
            RCMS spec §Status Codes — 400 / 501 / 503 codes
            consumed by the failure helpers.
            See `bridge/schema/rcms.schema.md` "bot.ended —
            status is nested in context".

        Args:
            websocket: The Infinity-side WebSocket; the
                `bot.started` ack and any failure `bot.ended`
                are sent here.
            client_id: Connection identifier used in log lines
                and the outbound sequence counter.
            data: Decoded `bot.start` envelope. `sessionId`,
                `service`, `payload.endpointId`,
                `payload.botId`, `payload.source`,
                `payload.botCredentials`, `payload.language`,
                `payload.context`, `payload.to`, `payload.from`,
                `payload.ucid`, `payload.direction`, and
                `payload.sampleRate` are read.

        Returns:
            None. Side effects: optional `session.error` /
            `bot.ended`-with-failure send on validation
            failure; `BotConversation` registered in
            `self._conversations`; playout-done callback
            registered with the IngressStreamer; background xAI
            recv loop launched. `bot.started` is sent later by
            `_on_session_updated`.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        service = data.get("service", "streaming")
        endpoint_id = payload.get("endpointId") or ""
        bot_id = (payload.get("botId") or "").strip()
        source = "rx" if payload.get("source") == "rx" else "tx"

        if not endpoint_id:
            # Stays on session.error (not bot.ended) per RCMS §Error Handling:
            # bot.ended's BotEndedPayload schema requires endpointId, which is
            # exactly the missing field here. Mirrors the dispatcher's
            # MISSING_REQUIRED_FIELDS exception in bot_service.py:_handle_bot_start.
            await self.server.send_session_error(
                websocket, client_id, session_id,
                message_type="bot.start",
                message_seq_num=data.get("sequenceNum"),
                code=501,
                reason="UNSUPPORTED_SERVICE",
                description="MISSING_REQUIRED_FIELDS: payload.endpointId is required for bot.start",
            )
            return

        if not bot_id.lower().startswith("xai:"):
            # RCMS §Error Handling: an unprocessable bot.start is failed via
            # bot.ended-with-status, not session.end. send_bot_ended_with_failure_context
            # emits the spec-compliant shape; the IVA module's FAILED branch wires
            # to bot.ended-with-non-200-status.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=400,
                reason="BAD_REQUEST",
                description=f"UNRECOGNIZED_BOTID_PREFIX: xAI plugin requires botId prefix 'xai:', got '{bot_id}'",
            )
            return

        # Model resolution: env var overrides botId suffix; suffix overrides
        # the built-in default.
        env_model = os.environ.get("XAI_MODEL", "").strip()
        model = env_model or bot_id[len("xai:"):].strip() or DEFAULT_XAI_MODEL

        api_key = os.environ.get("XAI_API_KEY", "").strip()
        if not api_key:
            # Also accept per-call credentials (base64 JSON {"apiKey": "..."})
            api_key = self._extract_api_key(payload) or ""
        if not api_key:
            # RCMS §Error Handling — see canonical comment at line 526 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: No xAI API key (XAI_API_KEY not set and botCredentials missing)",
            )
            return

        codec_name = self._resolve_codec(session_id).upper()
        sample_rate = self._resolve_sample_rate(session_id, payload)

        if codec_name not in ("PCMU", "L16", "PCMA", "G722"):
            # RCMS §Error Handling — see canonical comment at line 526 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: Unsupported codec '{codec_name}' for xAI provider",
            )
            return

        if codec_name == "G722" and not G722_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 526 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: Codec G722 negotiated but g722 package is not installed",
            )
            return

        if codec_name != "PCMU" and not _AUDIOOP_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 526 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=(
                    f"BACKEND_START_FAILED: Codec {codec_name} requires audioop for transcoding "
                    "but audioop is not available. Install audioop-lts or use --codec PCMU "
                    "for the zero-transcode path."
                ),
            )
            return

        language_code = payload.get("language") or "en-US"
        context = payload.get("context") or {}
        instructions = self._compose_instructions(payload, context, language_code)

        logger.info(
            "[%s] xAI bot.start session=%s endpoint=%s model=%s codec=%s/%d source=%s",
            client_id, session_id, endpoint_id, model, codec_name, sample_rate, source,
        )

        convo = BotConversation(
            session_id=session_id,
            endpoint_id=endpoint_id,
            source=source,
            websocket=websocket,
            client_id=client_id,
            service=service,
            model=model,
            instructions=instructions,
            language_code=language_code,
            codec_name=codec_name,
            sample_rate=sample_rate,
            transport_encoding=self.server.transport_encodings.get(session_id, "base64"),
        )

        # Register the playout-done callback so the streamer clears
        # convo.audio_playing_out when the egress queue drains naturally
        # (idle-timeout or is_last=True chunk sent). The closure captures
        # convo by reference; cleanup happens automatically when the
        # streamer's _stop_endpoint runs at session end.
        self.server.ingress_streamer.register_playout_done_callback(
            session_id, endpoint_id,
            lambda c=convo: self._on_playout_done(c),
        )

        if codec_name == "G722" and G722_AVAILABLE:
            try:
                import G722 as g722  # type: ignore
                convo.g722_decoder = g722.G722(sample_rate=16000, bit_rate=64000)
            except Exception as exc:
                logger.error("[%s] Failed to initialise G722 decoder: %s", client_id, exc)
                # RCMS §Error Handling — see canonical comment at line 526 above.
                await self.server.send_bot_ended_with_failure_context(
                    convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                    code=503,
                    reason="SERVICE_UNAVAILABLE",
                    description=f"BACKEND_START_FAILED: G722 decoder init failed: {exc}",
                    convo=convo,
                )
                return

        try:
            await self._connect_xai(convo, api_key)
        except Exception as exc:
            logger.error("[%s] Failed to connect to xAI: %s", client_id, exc, exc_info=True)
            # RCMS §Error Handling — see canonical comment at line 526 above.
            await self.server.send_bot_ended_with_failure_context(
                convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: xAI connect failed: {exc}",
                convo=convo,
            )
            return

        key = self._key(session_id, endpoint_id)
        existing = self._conversations.pop(key, None)
        if existing:
            await self._shutdown_conversation(existing)
        convo.active = True
        self._conversations[key] = convo
        convo.xai_recv_task = asyncio.create_task(self._xai_recv_loop(convo))

        # bot.started is sent after session.updated arrives from xAI in
        # _on_session_updated. Do NOT send it here — the session.update
        # round-trip must complete first so xAI is configured before we
        # signal Infinity that the bot is ready.

        _monitor_emit("provider.bound", {
            "session_id": session_id,
            "endpoint_id": endpoint_id,
            "provider": "xai",
            "bot_id": bot_id,
            "model": model,
        })
        _monitor_emit("call.context", {
            "session_id": session_id,
            "endpoint_id": endpoint_id,
            "direction": payload.get("direction"),
            "from_number": payload.get("from"),
            "to_number": payload.get("to"),
            "language": language_code,
            "domain": payload.get("domain"),
            "ucid": payload.get("ucid"),
            "context": context,
        })

    def _extract_api_key(self, payload: Dict[str, Any]) -> Optional[str]:
        """Decode `payload.botCredentials` and return the bearer key, or None.

        Contract:
            Per-call API key fallback used when `XAI_API_KEY`
            is not set on the bridge. The credentials field is
            a base64-wrapped JSON object of the shape
            `{"apiKey": "..."}`. Returns the decoded key on
            success, or None on any failure (missing field,
            base64 decode error, JSON parse error, missing
            `apiKey`, empty value). Decode failures log at
            ERROR — they indicate a malformed credentials
            payload that the workflow should be alerted to,
            though the start path treats the None return
            equivalently to a missing key (BACKEND_START_FAILED).

        Args:
            payload: The inbound `bot.start` payload. Reads
                `botCredentials`.

        Returns:
            Decoded API key string, or None if missing /
            malformed.
        """
        creds_b64 = payload.get("botCredentials")
        if not creds_b64:
            return None
        try:
            raw = base64.b64decode(creds_b64).decode("utf-8")
            obj = json.loads(raw)
            key = obj.get("apiKey") if isinstance(obj, dict) else None
            return key.strip() if isinstance(key, str) and key.strip() else None
        except Exception as exc:
            logger.error("Failed to decode botCredentials: %s", exc)
            return None

    @staticmethod
    def _load_base_prompt() -> str:
        """Resolve the base instructions template from a 4-tier precedence chain.

        Contract:
            Walks the precedence chain top-down and returns the
            first source that yields a non-empty string after
            stripping. Used by `_compose_instructions` once per
            call.

            Precedence order:

                1. **`XAI_SYSTEM_PROMPT_FILE` env var** —
                   explicit file path (deploy override; e.g. the
                   VM's `/opt/bridge-server/xai_system_prompt.md`).
                   Read failure or empty contents falls through.
                2. **`XAI_SYSTEM_PROMPT` env var** — inline
                   single-line override (whitespace-only is
                   treated as absent).
                3. **Repo-tracked sibling `system_prompt.md`** —
                   the canonical default that ships with the
                   bridge, located next to this module's source
                   file. Read failure or empty contents falls
                   through.
                4. **Built-in `_DEFAULT_INSTRUCTIONS`** — minimal
                   last-resort fallback. Carries no placeholders
                   so the subsequent `.format()` call is a no-op.

            File reads use UTF-8 and warn (not error) on
            `OSError` so a misconfigured path on one tier does
            not block the lower tiers.
            `_compose_instructions` performs Python `.format()`
            interpolation on the returned string; this function
            does no interpolation.

            Sibling implementation to `bot_gemini.py`'s
            `_load_base_prompt` and `bot_openai.py`'s
            `_load_base_prompt`; the three stay in sync by
            convention.

        Returns:
            The resolved base instructions string. Always
            non-empty — the built-in fallback is never empty.
        """
        prompt_file = os.environ.get("XAI_SYSTEM_PROMPT_FILE", "").strip()
        if prompt_file:
            try:
                with open(prompt_file, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return text
                logger.warning("XAI_SYSTEM_PROMPT_FILE %s is empty; falling back", prompt_file)
            except OSError as exc:
                logger.warning("Could not read XAI_SYSTEM_PROMPT_FILE %s: %s", prompt_file, exc)

        inline = os.environ.get("XAI_SYSTEM_PROMPT", "").strip()
        if inline:
            return inline

        sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
        if os.path.isfile(sibling):
            try:
                with open(sibling, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return text
            except OSError as exc:
                logger.warning("Could not read sibling system_prompt.md (%s): %s", sibling, exc)

        return _DEFAULT_INSTRUCTIONS

    @staticmethod
    def _compose_instructions(
        payload: Dict[str, Any],
        context: Dict[str, Any],
        language: str,
    ) -> str:
        """Substitute call-context fields into the resolved base persona for the per-call prompt.

        Contract:
            Calls `_load_base_prompt` to resolve the base
            instructions template (4-tier env / sibling-file /
            constant chain), then renders it with Python
            `str.format()`. Reads CRM-style fields off `context`
            (`firstName`, `lastName`, `email`, `caseId`,
            `caseSubject`, `caseDescription`) and call-routing
            fields off `payload` (`direction`, `from`, `to`,
            `ucid`), plus the language name resolved from the
            BCP-47 / ISO 639-1 code via `_resolve_language_name`.
            Missing fields render as empty strings rather than
            raising, so a partial CRM lookup still produces a
            usable prompt.

            On any template-substitution failure (a KeyError
            or ValueError raised by `str.format`), logs at
            WARNING and returns the static
            `_DEFAULT_INSTRUCTIONS` so the call still has a
            usable persona. A defensive shape-check coerces
            non-dict `context` to `{}` so a malformed inbound
            payload does not crash the start path.

        Args:
            payload: Inbound `bot.start` payload (call-routing
                source).
            context: CRM context dict (caller / case info
                source).
            language: BCP-47 / ISO 639-1 language code; passed
                through `_resolve_language_name` to a
                human-readable name for prompt interpolation.

        Returns:
            The composed instructions string (sent to xAI as
            `session.update.instructions`).
        """
        if not isinstance(context, dict):
            context = {}

        base = XaiService._load_base_prompt()
        try:
            return base.format(
                firstName=context.get("firstName", ""),
                lastName=context.get("lastName", ""),
                email=context.get("email", ""),
                caseId=context.get("caseId", ""),
                caseSubject=context.get("caseSubject", ""),
                caseDescription=context.get("caseDescription", ""),
                direction=payload.get("direction", "INBOUND"),
                from_num=payload.get("from", ""),
                to_num=payload.get("to", ""),
                ucid=payload.get("ucid", ""),
                language=_resolve_language_name(language),
            )
        except (KeyError, ValueError) as exc:
            logger.warning("Instructions template substitution failed: %s — using default", exc)
            return _DEFAULT_INSTRUCTIONS

    # ---------------------------------------------------------------- bot.end

    async def _handle_bot_end(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Handle Infinity-originated `bot.end` for an xAI session — tear down upstream and ack with bot.ended.

        Contract:
            Phase entry point dispatched by `handle_message`
            when the inbound message type is `bot.end` and the
            dispatcher has routed by `botId` prefix. Performs
            the symmetric counterpart to `_handle_bot_start`:

                1. **Validate `endpointId`**: missing endpointId
                   fails the bot.ended schema, so respond with
                   `session.error` (`MISSING_REQUIRED_FIELDS`,
                   501) instead. Same schema constraint as the
                   bot.start path.
                2. **Pop and shut down the conversation**:
                   removes `<session>:<endpoint>` from
                   `self._conversations` and runs
                   `_shutdown_conversation` which cancels drain
                   tasks and closes the upstream xAI WebSocket.
                   Logs at WARNING (no fatal error) when no
                   matching conversation exists — a benign race
                   if the conversation was already torn down by
                   a prior originator emit.
                3. **Ack with bot.ended**: builds the response
                   envelope inline, copying any `context` field
                   from the inbound payload to preserve
                   round-trip context. Sends the ack
                   regardless of whether step 2 found a
                   conversation — Infinity is waiting on the
                   ack to close its own session state.
                4. **Set `bot_ended_sent`**: so the subsequent
                   `on_session_ended` does not double-emit. The
                   shared disconnect-context helper guards on
                   this flag, but this bare-emit path bypasses
                   the helper, so the flag is set explicitly.

            Wire effect: one `bot.ended` envelope sent to
            Infinity over the inbound WebSocket.

        Args:
            websocket: Infinity-side WebSocket the bot.end
                arrived on.
            client_id: Bridge-assigned client identifier for
                logging.
            data: Parsed inbound `bot.end` envelope. Reads
                `sessionId`, `payload.endpointId`,
                `payload.context`, `service`, `sequenceNum`.

        Returns:
            None.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        service = data.get("service", "streaming")
        endpoint_id = payload.get("endpointId") or ""

        if not endpoint_id:
            # bot.ended's BotEndedPayload schema requires endpointId; emit
            # session.error instead of a malformed bot.ended.
            await self.server.send_session_error(
                websocket, client_id, session_id,
                message_type="bot.end",
                message_seq_num=data.get("sequenceNum"),
                code=501,
                reason="UNSUPPORTED_SERVICE",
                description="MISSING_REQUIRED_FIELDS: payload.endpointId is required for bot.end",
            )
            return

        convo = self._conversations.pop(self._key(session_id, endpoint_id), None)
        if convo:
            await self._shutdown_conversation(convo)
        else:
            logger.warning("[%s] No active xAI convo for %s:%s", client_id, session_id, endpoint_id)

        response = {
            "version": "1.0.0",
            "type": "bot.ended",
            "sessionId": session_id,
            "sequenceNum": self.server.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": service,
            "payload": {"endpointId": endpoint_id},
        }
        context = payload.get("context")
        if context:
            response["payload"]["context"] = context
        logger.info("[%s] OUTBOUND JSON (bot.ended): %s", client_id, format_compact_json(response))
        log_message_exchange("OUTBOUND", client_id, "bot.ended", response, is_media=False)
        await websocket.send(json.dumps(response))
        if convo is not None:
            convo.bot_ended_sent = True

    # ---------------------------------------------------------------- audio ingest

    async def ingest_audio_chunk(
        self,
        session_id: str,
        endpoint_id: str,
        source: str,
        audio_bytes: bytes,
    ) -> bool:
        """Per-frame inbound audio entry point invoked by `BridgeServer.handle_media` / `handle_binary_frame`.

        Contract:
            Looked up via `(session_id, endpoint_id)` against
            `self._conversations`. Returns `False` immediately
            if the conversation is missing, inactive, sourced
            from the wrong direction (`convo.source != source`),
            or has no upstream xAI WebSocket — the bridge falls
            through to other registered services on `False`.

            **Ingress-ready latch.** The first call to this
            method on a conversation marks
            `convo.ingress_ready = True` and flushes anything
            that `_handle_xai_message` had buffered into
            `convo.ingress_buffer` while waiting for Infinity's
            ingress path to open. The buffer holds xAI's initial
            greeting audio, which lands before Infinity's first
            egress frame arrives; without the latch + buffer
            pair, those greeting chunks would be discarded.

            After the latch handling, the inbound bytes are
            converted to 8 kHz µ-law by `_prepare_input_audio`
            (a no-op pass-through when the negotiated codec is
            PCMU). If transcoding fails (returns `None`), this
            method returns `False`. Otherwise the µ-law is
            base64-wrapped in an `input_audio_buffer.append`
            envelope and sent on `convo.xai_ws`. WS send
            failures log at ERROR and return `False`.

        Spec:
            xAI Grok Voice Realtime API protocol —
            `input_audio_buffer.append` envelope shape.

        Args:
            session_id: RCMS session identifier from the
                originating `bot.start`.
            endpoint_id: Media endpoint identifier from the
                originating `bot.start`.
            source: Frame direction sentinel (`"rx"` or
                `"tx"`); must match `convo.source`. Mismatches
                return `False` so the dispatcher can route the
                frame elsewhere.
            audio_bytes: Raw frame payload from the Infinity
                media transport (codec-encoded per
                `convo.codec_name`).

        Returns:
            True iff the frame was successfully transcoded and
            sent to xAI. False on every reject / failure path.
        """
        convo = self._conversations.get(self._key(session_id, endpoint_id))
        if not convo or not convo.active or convo.source != source or not convo.xai_ws:
            return False

        # First egress frame from Infinity: ingress path is now open.
        # Flush any xAI audio buffered before bot.started was acknowledged.
        if not convo.ingress_ready:
            convo.ingress_ready = True
            if convo.ingress_buffer:
                logger.info(
                    "[%s] Infinity ingress ready — flushing %d buffered audio chunks",
                    convo.client_id, len(convo.ingress_buffer),
                )
                for buffered in convo.ingress_buffer:
                    await self._enqueue_output_audio(convo, buffered)
                convo.ingress_buffer.clear()

        ulaw_bytes = self._prepare_input_audio(convo, audio_bytes)
        if not ulaw_bytes:
            return False

        try:
            msg = {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(ulaw_bytes).decode("ascii"),
            }
            await convo.xai_ws.send(json.dumps(msg))
        except Exception as exc:
            logger.error("[%s] Send to xAI failed: %s", convo.client_id, exc)
            return False
        return True

    def _prepare_input_audio(
        self, convo: BotConversation, audio_bytes: bytes
    ) -> Optional[bytes]:
        """Convert an inbound Infinity-side audio frame to 8 kHz µ-law for xAI Grok Realtime.

        Contract:
            Inbound (Infinity → xAI) counterpart of
            `_transcode_output_audio`. Branches on
            `convo.codec_name.upper()`:

                * **PCMU**: pass through unchanged. PCMU is
                  µ-law at 8 kHz; xAI's input format is also
                  µ-law at 8 kHz. This is the zero-transcode
                  fast path — no audioop calls, no resampling,
                  the bytes are wire-equivalent. Recommended
                  bridge configuration for xAI deployments.
                * **L16**: encode µ-law via `audioop.lin2ulaw`
                  if rate is already 8 kHz; otherwise resample
                  to 8 kHz via `audioop.ratecv` (threading
                  `convo.ratecv_in_state`) and then encode.
                * **PCMA**: decode A-law (`audioop.alaw2lin`)
                  at 8 kHz, then encode µ-law
                  (`audioop.lin2ulaw`).
                * **G722**: decode via `convo.g722_decoder`
                  (lazily initialised in `_handle_bot_start`
                  when G722 is negotiated) into 16 kHz S16LE,
                  resample to 8 kHz, then encode µ-law.

            All non-PCMU paths require `_AUDIOOP_AVAILABLE` to
            be True; when audioop is missing, the path returns
            `None` (the conversation should have been rejected
            at bot.start time, but this is a defensive check).

            `ratecv_in_state` is independent of
            `ratecv_out_state` — audioop returns a fresh state
            per call and the two directions cannot share.

            Errors from `audioop` / G722 are logged at ERROR
            and yield `None`. Unsupported codec names log at
            WARNING and yield `None`. The caller drops the
            chunk on `None`.

        Args:
            convo: The active conversation. Provides
                `codec_name`, `sample_rate`, `g722_decoder`,
                and the resampler state; the latter two are
                mutated.
            audio_bytes: Raw inbound frame bytes from Infinity.
                Empty buffers return `None`.

        Returns:
            8 kHz µ-law bytes ready for the xAI
            `input_audio_buffer.append` envelope, or `None` on
            transcoding failure or unsupported codec.
        """
        if not audio_bytes:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate

        try:
            if codec == "PCMU":
                # µ-law → µ-law: no conversion. The fast path.
                return audio_bytes

            if not _AUDIOOP_AVAILABLE:
                logger.error(
                    "[%s] audioop unavailable — cannot transcode %s input", convo.client_id, codec
                )
                return None

            if codec == "L16":
                if rate == XAI_SAMPLE_RATE:
                    return audioop.lin2ulaw(audio_bytes, 2)
                # Resample to 8 kHz first.
                resampled, convo.ratecv_in_state = audioop.ratecv(
                    audio_bytes, 2, 1, rate, XAI_SAMPLE_RATE, convo.ratecv_in_state
                )
                return audioop.lin2ulaw(resampled, 2)

            if codec == "PCMA":
                # A-law → linear → µ-law (both at 8 kHz).
                pcm8 = audioop.alaw2lin(audio_bytes, 2)
                return audioop.lin2ulaw(pcm8, 2)

            if codec == "G722":
                if not G722_AVAILABLE or not convo.g722_decoder:
                    logger.error("[%s] G722 decoder not available", convo.client_id)
                    return None
                pcm_16k = convo.g722_decoder.decode(audio_bytes).tobytes()
                # Downsample 16 kHz PCM → 8 kHz, then encode µ-law.
                pcm_8k, convo.ratecv_in_state = audioop.ratecv(
                    pcm_16k, 2, 1, 16000, XAI_SAMPLE_RATE, convo.ratecv_in_state
                )
                return audioop.lin2ulaw(pcm_8k, 2)

        except Exception as exc:
            logger.error(
                "[%s] Input audio prep failed (codec=%s rate=%d): %s",
                convo.client_id, codec, rate, exc,
            )
            return None

        logger.warning("[%s] Unsupported input codec '%s'", convo.client_id, codec)
        return None

    def _transcode_output_audio(
        self, convo: BotConversation, ulaw_bytes: bytes
    ) -> Optional[bytes]:
        """Convert 8 kHz µ-law from xAI Grok Realtime into the Infinity-side codec for this call.

        Contract:
            Outbound (xAI → Infinity) counterpart of
            `_prepare_input_audio`. xAI emits 8 kHz µ-law
            (`audio/pcmu` at 8000 Hz, declared in
            `session.update.session.audio.output.format`); the
            Infinity-side codec / sample rate are whatever was
            negotiated at session.start.

            Branches on `convo.codec_name.upper()`:

                * **PCMU**: pass through unchanged. The fast
                  path — both ends speak µ-law at 8 kHz, no
                  audioop calls, no resampling.
                * **L16**: decode µ-law (`audioop.ulaw2lin`)
                  at 8 kHz; pass through if `sample_rate`
                  matches; otherwise upsample via
                  `audioop.ratecv` (threading
                  `convo.ratecv_out_state`).
                * **PCMA**: decode µ-law to 8 kHz PCM, then
                  encode A-law (`audioop.lin2alaw`). Both ends
                  at 8 kHz.
                * **G722**: decode µ-law to 8 kHz PCM,
                  upsample to 16 kHz (G722's internal rate),
                  lazy-init `convo.g722_encoder` on first
                  call, then encode via the G722 module's
                  `encode` on a numpy int16 view.

            All non-PCMU paths require `_AUDIOOP_AVAILABLE` to
            be True; when audioop is missing, the path returns
            `None`.

            `ratecv_out_state` is independent of
            `ratecv_in_state` for the same reason as the
            inbound counterpart.

            Errors from `audioop` / G722 / numpy import are
            logged at ERROR and yield `None`. Unsupported
            codec names log at WARNING and yield `None`. The
            caller drops the chunk on `None`.

        Args:
            convo: The active conversation. Provides
                `codec_name`, `sample_rate`, `g722_encoder`,
                and the resampler state; the latter two are
                mutated.
            ulaw_bytes: 8 kHz µ-law bytes decoded from xAI's
                base64-encoded `response.output_audio.delta` payload.
                Empty buffers return `None`.

        Returns:
            Bytes ready for the Infinity wire (µ-law, PCM at
            negotiated rate, A-law, or G.722 encoded), or
            `None` on transcoding failure or unsupported
            codec.
        """
        if not ulaw_bytes:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate

        try:
            if codec == "PCMU":
                # µ-law → µ-law: no conversion.
                return ulaw_bytes

            if not _AUDIOOP_AVAILABLE:
                logger.error(
                    "[%s] audioop unavailable — cannot transcode %s output", convo.client_id, codec
                )
                return None

            if codec == "L16":
                pcm_8k = audioop.ulaw2lin(ulaw_bytes, 2)
                if rate == XAI_SAMPLE_RATE:
                    return pcm_8k
                # Upsample to target rate.
                resampled, convo.ratecv_out_state = audioop.ratecv(
                    pcm_8k, 2, 1, XAI_SAMPLE_RATE, rate, convo.ratecv_out_state
                )
                return resampled

            if codec == "PCMA":
                # µ-law → linear → A-law (both at 8 kHz).
                pcm_8k = audioop.ulaw2lin(ulaw_bytes, 2)
                return audioop.lin2alaw(pcm_8k, 2)

            if codec == "G722":
                if not G722_AVAILABLE:
                    return None
                # µ-law → 8 kHz PCM → 16 kHz PCM → G722.
                pcm_8k = audioop.ulaw2lin(ulaw_bytes, 2)
                pcm_16k, convo.ratecv_out_state = audioop.ratecv(
                    pcm_8k, 2, 1, XAI_SAMPLE_RATE, 16000, convo.ratecv_out_state
                )
                if not convo.g722_encoder:
                    import G722 as g722  # type: ignore
                    convo.g722_encoder = g722.G722(sample_rate=16000, bit_rate=64000)
                import numpy as np
                return convo.g722_encoder.encode(np.frombuffer(pcm_16k, dtype=np.int16))

        except Exception as exc:
            logger.error(
                "[%s] Output audio transcode failed (codec=%s): %s",
                convo.client_id, codec, exc,
            )
            return None

        logger.warning("[%s] Unsupported output codec '%s'", convo.client_id, codec)
        return None

    @staticmethod
    def _chunk_size_for(codec: str, sample_rate: int, duration_ms: int) -> int:
        """Return the byte count of `duration_ms` of audio in the given Infinity-side codec/rate.

        Contract:
            Pure function over the Infinity codec / sample-rate
            / duration triple. Used by `_enqueue_output_audio`
            to compute the accumulator's flush boundary so each
            flush produces exactly one paced chunk. Caller must
            pass `IngressStreamer.chunk_duration_ms` as
            `duration_ms` — passing any other value
            desynchronizes the accumulator boundary from the
            streamer's pacing interval, which causes
            `queue_audio` to split each flush into mismatched
            chunks paced uniformly, producing sub-real-time
            delivery and buffer underruns at Infinity.

            Codec mapping:
                * **L16** — `samples * 2` (S16LE = 2 bytes/sample,
                  mono).
                * **PCMU** / **PCMA** — `samples` (8-bit µ/A-law,
                  1 byte/sample).
                * **G722** — `(64000 * duration_ms) / (8 * 1000)`
                  (64 kbps fixed bit rate).
                * Anything else — fall back to S16LE sizing.

        Args:
            codec: Upper-case Infinity codec name
                (`"L16"` / `"PCMU"` / `"PCMA"` / `"G722"`).
            sample_rate: Infinity-negotiated rate in Hz.
            duration_ms: Target chunk duration in milliseconds —
                must equal `IngressStreamer.chunk_duration_ms`.

        Returns:
            Byte count for the requested duration of audio.
        """
        samples = (sample_rate * duration_ms) // 1000
        if codec == "L16":
            return samples * 2
        if codec in ("PCMU", "PCMA"):
            return samples
        if codec == "G722":
            return (64000 * duration_ms) // (8 * 1000)
        return samples * 2

    async def _enqueue_output_audio(
        self, convo: BotConversation, audio_bytes: bytes
    ) -> None:
        """Accumulate xAI's variable-size audio chunks into pacer-aligned chunks and flush each full chunk.

        Contract:
            xAI Grok Realtime emits `response.output_audio.delta`
            chunks of variable size; the bridge's
            IngressStreamer paces at `chunk_duration_ms` per
            chunk regardless of the duration of content fed to
            it. This method buffers the variable-size deltas in
            `convo.ingress_accumulator` until a full pacer-
            aligned chunk is available, then flushes one chunk
            at a time via `_send_ingress_chunked`. The leftover
            stays in the accumulator for the next call, so
            pacing tracks actual audio content rather than
            fragmenting per inbound delta.

            The accumulator's chunk size
            (`convo.ingress_chunk_size`) is computed lazily on
            first use via `_chunk_size_for`, which reads
            `IngressStreamer.chunk_duration_ms`. Using any
            other value desynchronizes the flush boundary from
            the streamer's pacing interval — the resulting
            mismatched chunks are then paced uniformly,
            producing sub-real-time delivery at Infinity.

            Empty inputs return immediately. Multiple flushes
            per call are possible if `audio_bytes` is large
            enough to cover several pacer chunks (the inner
            `while` loop).

        Args:
            convo: The active conversation. Mutated:
                `ingress_chunk_size` is set lazily on first
                call; `ingress_accumulator` is appended to and
                consumed.
            audio_bytes: Codec-converted audio ready for the
                wire (output of `_transcode_output_audio`).

        Returns:
            None. Per-flush errors are surfaced through
            `_send_ingress_chunked` (logged WARNING,
            swallowed).
        """
        if not audio_bytes:
            return
        if convo.ingress_chunk_size <= 0:
            convo.ingress_chunk_size = self._chunk_size_for(
                convo.codec_name.upper(),
                convo.sample_rate,
                self.server.ingress_streamer.chunk_duration_ms,
            )
        convo.ingress_accumulator.extend(audio_bytes)
        chunk_size = convo.ingress_chunk_size
        while len(convo.ingress_accumulator) >= chunk_size:
            chunk = bytes(convo.ingress_accumulator[:chunk_size])
            del convo.ingress_accumulator[:chunk_size]
            await self._send_ingress_chunked(convo, chunk)

    async def _flush_output_remainder(self, convo: BotConversation) -> None:
        """Flush whatever partial chunk is left in `convo.ingress_accumulator`, then clear it.

        Contract:
            Called from `_handle_xai_message`'s `response.done`
            and `response.output_audio.done` branches so the
            tail of a response (whatever doesn't make a full
            pacer-aligned chunk) plays out instead of being
            stranded in the accumulator until the next
            response. The tail is sent as a single sub-pacer-
            sized chunk; the IngressStreamer accepts it and
            paces it like any other chunk.

            Empty accumulator returns silently.

        Args:
            convo: The active conversation whose accumulator
                is drained. Mutated: `ingress_accumulator`
                cleared.

        Returns:
            None. Send errors propagate via
            `_send_ingress_chunked` (logged WARNING,
            swallowed).
        """
        if not convo.ingress_accumulator:
            return
        remainder = bytes(convo.ingress_accumulator)
        convo.ingress_accumulator.clear()
        await self._send_ingress_chunked(convo, remainder)

    async def _send_ingress_chunked(
        self, convo: BotConversation, audio_bytes: bytes
    ) -> None:
        """Forward a single pacer-aligned audio chunk to Infinity via the IngressStreamer's paced sender.

        Contract:
            Hands the chunk to `IngressStreamer.queue_audio`,
            which re-emits frames at the negotiated
            `chunk_duration_ms` cadence. Caller must have
            already sized the chunk to match the streamer's
            pacing interval — see `_enqueue_output_audio` for
            the accumulator that does this.

            Sets `convo.audio_playing_out = True` after a
            successful queue_audio call. The flag stays True
            until either the streamer signals playout-done via
            the registered callback (`_on_playout_done`) on
            natural drain, or `_handle_barge_in` clears it
            explicitly. Together with `audio_playing_out`, the
            VAD-triggered barge-in dispatch in
            `_handle_xai_message` correctly gates on actual
            playout state rather than on xAI's generation
            state.

            Errors from `queue_audio` are logged at WARNING
            and swallowed — a per-chunk send failure should
            not tear the conversation down. The flag is *not*
            set on the error path, since no audio is in
            flight.

        Args:
            convo: The active conversation. Provides the
                bridge-side websocket, identifiers, and
                transport encoding. Mutated:
                `audio_playing_out` set True on success.
            audio_bytes: Codec-converted, pacer-aligned audio
                chunk ready for the wire. Empty buffers return
                immediately.

        Returns:
            None. The send is fire-and-forget from the
            caller's perspective; pacing happens inside the
            IngressStreamer.
        """
        if not audio_bytes:
            return
        try:
            await self.server.ingress_streamer.queue_audio(
                websocket=convo.websocket,
                client_id=convo.client_id,
                session_id=convo.session_id,
                endpoint_id=convo.endpoint_id,
                audio_bytes=audio_bytes,
                is_last=False,
                transport=convo.transport_encoding,
            )
            # Bot audio is now in flight to Infinity. Stays True until
            # natural drain (playout-done callback) or barge-in.
            convo.audio_playing_out = True
        except Exception as exc:
            logger.warning("[%s] queue_audio failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- xAI WS

    async def _connect_xai(self, convo: BotConversation, api_key: str) -> None:
        """Open the upstream xAI Grok Realtime WebSocket and send the `session.update` configuration frame.

        Contract:
            Two-step connect:

                1. Open
                   `wss://api.x.ai/v1/realtime?model=<model>`
                   with the `Authorization: Bearer <api_key>`
                   header. xAI does not require a beta opt-in
                   header on the WebSocket handshake.
                   `max_size=_WS_MAX_FRAME_BYTES` (8 MiB)
                   accommodates the largest audio frames xAI
                   emits.
                2. Immediately send a single `session.update`
                   frame carrying:
                   - **`instructions`** — the per-call composed
                     prompt produced by `_compose_instructions`
                     (CRM-context-interpolated persona).
                   - **`voice`** — read from the `XAI_VOICE`
                     environment variable; defaults to
                     `DEFAULT_VOICE` (`"ara"`). The voice and
                     persona names are independent.
                   - **`turn_detection`** —
                     `{"type": "server_vad"}` so xAI handles
                     speech-start / speech-stop detection
                     server-side and emits
                     `input_audio_buffer.speech_started` /
                     `speech_stopped` / `committed` events.
                   - **`audio`** — nested object with
                     `input.format` and `output.format` blocks,
                     each carrying `{type: "audio/pcmu",
                     rate: 8000}`. This is the xAI wire shape
                     for audio configuration; symmetric µ-law
                     formats enable the zero-transcode
                     operating path when the bridge is started
                     with `--codec PCMU`.
                   - **`tools`** — registers
                     `_TRANSFER_TO_AGENT_TOOL` and
                     `_END_SESSION_TOOL`. Both are inline
                     definitions in this module; xAI Grok
                     Realtime has no platform-side tool
                     registry.
                   - **`tool_choice`** — `"auto"`. Lets the
                     model decide whether and when to invoke a
                     tool based on conversational context. The
                     system prompt's Transfer Protocol and End
                     Call Protocol sections direct *when* each
                     tool should fire.

            Stores the connected websocket on `convo.xai_ws`.
            On any failure during connect or session.update
            send, the exception propagates to
            `_handle_bot_start`, which translates it into
            `bot.ended` with `BACKEND_START_FAILED` via the
            failure-context helper.

            **TLS context selection.** When the optional
            `truststore` package is available, an outbound TLS
            context backed by the OS trust store is built per
            call so corporate TLS-interception roots (e.g.
            Zscaler) are honored. Scoping to this call site
            avoids globally replacing `ssl.SSLContext`, which
            would break the server-side WSS context the bridge
            accepts inbound connections on. When `truststore`
            is not installed, `_ssl.create_default_context()`
            falls back to the Python CA bundle.

            **Note on `bot.started` deferral.** This method
            does not emit `bot.started` to Infinity. The
            `session.update` round-trip must complete first;
            `bot.started` is sent later from
            `_on_session_updated` after the `session.updated`
            ack arrives.

            **Note on input transcription opt-in.** The xAI
            `session.update` schema has no documented
            `input_audio_transcription` field; the bridge does
            not explicitly request transcription. The
            `_handle_xai_message`
            `conversation.item.input_audio_transcription.
            completed` branch is wired to consume the events
            if the upstream emits them.

        Spec:
            xAI Grok Voice Realtime API protocol —
            `session.update` envelope shape with nested
            `audio.input.format` / `audio.output.format`
            blocks; Bearer-token auth via `Authorization`
            header; URL-query model selection.

        Args:
            convo: The conversation receiving the connection.
                Mutated: `xai_ws` is set on success.
            api_key: xAI API key, sourced from `XAI_API_KEY`
                in the bridge environment or from per-call
                `payload.botCredentials`.

        Returns:
            None. Raises any websockets / TLS / send error to
            the caller; no bridge-side bot.ended is emitted
            from here.
        """
        url = f"{XAI_REALTIME_BASE_URL}?model={convo.model}&smart_turn=true&smart_turn_timeout=2000"
        if _truststore is not None:
            ssl_context = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        else:
            ssl_context = _ssl.create_default_context()

        voice = os.environ.get("XAI_VOICE", DEFAULT_VOICE).strip()

        convo.xai_ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {api_key}",
            },
            max_size=_WS_MAX_FRAME_BYTES,
            ssl=ssl_context,
        )
        logger.info(
            "[%s] Connected to xAI Realtime model=%s voice=%s",
            convo.client_id, convo.model, voice,
        )

        # Configure the session. session.updated ack triggers bot.started
        # via _on_session_updated. The audio block uses the xAI nested
        # input.format / output.format shape — see method docstring.
        session_update = {
            "type": "session.update",
            "session": {
                "instructions": convo.instructions,
                "voice": voice,
                "turn_detection": {"type": "server_vad"},
                "audio": {
                    "input":  {"format": {"type": "audio/pcmu", "rate": XAI_SAMPLE_RATE}},
                    "output": {"format": {"type": "audio/pcmu", "rate": XAI_SAMPLE_RATE}},
                },
                "tools": [_TRANSFER_TO_AGENT_TOOL, _END_SESSION_TOOL],
                "tool_choice": "auto",
            },
        }
        await convo.xai_ws.send(json.dumps(session_update))
        logger.info(
            "[%s] Sent session.update (instructions_len=%d)",
            convo.client_id, len(convo.instructions),
        )

    async def _xai_recv_loop(self, convo: BotConversation) -> None:
        """Long-running task that reads frames from the upstream xAI WebSocket and dispatches each to `_handle_xai_message`.

        Contract:
            Spawned by `_connect_xai` after the upstream
            handshake succeeds; stored on
            `convo.xai_recv_task`. Iterates
            `convo.xai_ws` with `async for`, JSON-decoding each
            frame and dispatching the parsed event to
            `_handle_xai_message`. Exits the loop early when
            `convo.active` flips to False (set by shutdown
            paths and the `finally` block here).

            **Failure modes.**
                * Non-JSON frame: logs at WARNING and continues
                  (the rest of the stream may still be valid).
                * `CancelledError`: re-raised so the cancelling
                  caller (`_shutdown_conversation`) sees the
                  cancellation.
                * `ConnectionClosed`: logs at INFO and exits
                  cleanly; downstream cleanup is owned by
                  shutdown paths.
                * Any other exception: logs at ERROR with
                  traceback and exits — downstream cleanup
                  again owned by shutdown paths.

            On any exit path, the `finally` block sets
            `convo.active = False` so subsequent
            `ingest_audio_chunk` calls return False instead of
            attempting a send on a dead WebSocket.

        Args:
            convo: The conversation to receive for. Reads
                `xai_ws`, mutates `active`, dispatches via
                `_handle_xai_message`.

        Returns:
            None.
        """
        try:
            async for raw in convo.xai_ws:
                if not convo.active:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.warning("[%s] Non-JSON frame from xAI", convo.client_id)
                    continue
                await self._handle_xai_message(convo, msg)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("[%s] xAI WS closed: %s", convo.client_id, exc)
        except Exception as exc:
            logger.error("[%s] xAI recv loop error: %s", convo.client_id, exc, exc_info=True)
        finally:
            convo.active = False

    async def _handle_xai_message(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a single decoded event from the xAI Grok Realtime WebSocket to its handler branch.

        Contract:
            Single switch on `msg["type"]`. xAI Grok Realtime
            emits a high-volume event stream (~15+ types per
            conversation turn); only state-machine-relevant
            events act, most are logged at DEBUG and discarded.
            Branches handled, in declaration order:

                * **`session.created`** — informational session
                  bootstrap notice. Logged at INFO with the ack
                  `session.model` so a silent fallback to 1.0 is
                  visible. Warns when the ack model does not
                  match `convo.model`. The configuration-accepted
                  signal is `session.updated` below.
                * **`session.updated`** — xAI has accepted the
                  `session.update` configuration sent in
                  `_connect_xai`. Triggers
                  `_on_session_updated`, which sends the
                  proactive greeting via `response.create` and
                  emits `bot.started` to Infinity.
                * **`input_audio_buffer.speech_started`** —
                  server VAD detected speech onset. Two
                  effects: (a) capture
                  `customer_turn_started_at` for use as
                  `startTsMs` on the eventual CUSTOMER
                  TRANSCRIPT emit; (b) when
                  `audio_playing_out` is True, dispatch
                  `_handle_barge_in` to clear the
                  IngressStreamer queue and cancel any active
                  responses. The barge-in gate is
                  `audio_playing_out`, not
                  `active_response_ids`, because xAI generates
                  faster than playout — `response.done` can
                  clear the active list while audio is still
                  paced out for several seconds afterwards.
                * **`input_audio_buffer.speech_stopped`** /
                  **`input_audio_buffer.committed`** —
                  informational VAD events; logged at DEBUG.
                * **`response.created`** — xAI accepted the
                  response request. Two effects: (a) append
                  the response_id to `active_response_ids` so a
                  subsequent barge-in can issue targeted
                  `response.cancel`; (b) capture
                  `bot_turn_started_at` for use as `startTsMs`
                  on the eventual BOT TRANSCRIPT emit.
                * **`response.done`** — response generation is
                  fully complete. Remove the response_id from
                  `active_response_ids`, flush any partial
                  audio chunk left in `ingress_accumulator`,
                  and flush stashed BOT transcript when no
                  CUSTOMER turn is in flight (BL-003).
                * **`response.output_audio.done`** — server-side
                  audio generation has finished for this
                  response, even though paced playout to
                  Infinity may still be running. The xAI event
                  name has the `output_audio` segment, distinct
                  from the `response.output_audio.delta` events that
                  carry the chunks themselves. Two effects:
                  (a) add the response_id to
                  `completed_response_ids` so a subsequent
                  barge-in skips `response.cancel` for this
                  response (the cancel would always race-lose
                  against xAI's already-finished state);
                  (b) flush any audio remainder still in the
                  accumulator and call
                  `IngressStreamer.mark_audio_segment_complete`
                  so the streamer sends `lastf=true` to
                  Infinity and fires the playout-done callback
                  that clears `audio_playing_out` via the
                  natural-drain path.
                * Other response-lifecycle events
                  (`response.output_item.added`,
                  `response.content_part.added`, `.done`
                  variants) — logged at DEBUG; no
                  state-machine action.
                * **`response.output_audio.delta`** — base64-encoded
                  µ-law audio chunk. Decoded, transcoded via
                  `_transcode_output_audio` (no-op when codec
                  is PCMU), and either buffered on
                  `convo.ingress_buffer` (while
                  `convo.ingress_ready` is False) or fed
                  through `_enqueue_output_audio` for
                  pacer-aligned flush. Buffer is bounded by
                  `_INGRESS_BUFFER_MAX_CHUNKS`; oldest chunk
                  is dropped on overflow with a WARNING.
                * **`response.output_audio_transcript.delta`** —
                  per-`response_id` accumulating text fragments
                  for the bot's transcript. Appended to
                  `transcript_deltas[response_id]`.
                * **`response.output_audio_transcript.done`** — bot's
                  transcript is final for this response. Pop into
                  `pending_bot_text`. Emit immediately only when no
                  customer turn is in flight (BL-003); otherwise wait
                  for CUSTOMER `.completed` or `response.done`.
                * **`conversation.item.input_audio_transcription.
                  updated`** — cumulative caller transcript (xAI
                  name for OpenAI's `.delta`). Replaces
                  `pending_customer_text`.
                * **`conversation.item.input_audio_transcription.
                  completed`** — caller's transcript landed
                  whole. Prefer the terminal `transcript` field;
                  fall back to `pending_customer_text`. Emit
                  CUSTOMER then flush stashed BOT (BL-003).
                * **`response.function_call_arguments.done`** —
                  the function-call invocation completed.
                  Hands off to `_handle_function_call`.
                * **`error`** — xAI Grok Realtime protocol
                  error. Logged at ERROR with type / code /
                  message fields. The bot.error → Infinity
                  translation is a planned addition (currently
                  the bridge logs and lets the call continue
                  or drop based on whether the error closed
                  the WebSocket).

        Spec:
            xAI Grok Voice Realtime API protocol — full event
            catalog.
            RCMS spec §AI Bot Message Definitions —
            `bot.feature` with `TRANSCRIPT` ftype emitted via
            the transcript branches.

        Args:
            convo: The active conversation whose upstream
                WebSocket produced this event. Mutated by
                branches that update accumulators, turn-start
                fields, ingress buffer / accumulator,
                response-ID tracking sets, or
                `audio_playing_out`.
            msg: Decoded JSON object from the xAI Grok Realtime
                WebSocket.

        Returns:
            None. Outbound effects: optional audio sends to
            Infinity via the IngressStreamer, optional
            `bot.feature` TRANSCRIPT envelopes to Infinity,
            optional `response.cancel` to xAI on barge-in.
        """
        event_type = msg.get("type", "")

        # ── Session ──────────────────────────────────────────────────────────

        if event_type == "session.created":
            session = msg.get("session") or {}
            ack_model = session.get("model") or ""
            logger.info(
                "[%s] session.created id=%s model=%s",
                convo.client_id, session.get("id"), ack_model,
            )
            if ack_model and ack_model != convo.model:
                logger.warning(
                    "[%s] xAI session.model=%s does not match requested %s "
                    "(silent fallback is a known xAI footgun)",
                    convo.client_id, ack_model, convo.model,
                )
            return

        if event_type == "session.updated":
            logger.info("[%s] session.updated — configuration accepted", convo.client_id)
            await self._on_session_updated(convo)
            return

        # ── VAD / speech ──────────────────────────────────────────────────────

        if event_type == "input_audio_buffer.speech_started":
            logger.debug("[%s] VAD: speech_started", convo.client_id)
            # A new caller utterance means any BOT already generated for
            # the previous turn must go on the wire first. Infinity
            # orders by arrival, so holding that BOT until this new
            # CUSTOMER `.completed` inverts the native transcript.
            await self._flush_stashed_transcripts(convo)
            convo.customer_turn_started_at = int(time.time() * 1000)
            convo.customer_transcript_emitted = False
            convo.pending_customer_text = ""
            # Barge-in gate is audio_playing_out (queue still has audio
            # paced out to Infinity), not active_response_ids (xAI's
            # generation state) — see method docstring branch on
            # `speech_started`.
            if convo.audio_playing_out:
                logger.info(
                    "[%s] Barge-in — clearing egress queue (active_response=%s)",
                    convo.client_id,
                    convo.active_response_ids[-1] if convo.active_response_ids else None,
                )
                await self._handle_barge_in(convo)
            return

        if event_type in (
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
        ):
            logger.debug("[%s] %s", convo.client_id, event_type)  # INFORMATIONAL
            return

        # ── Response lifecycle ────────────────────────────────────────────────

        if event_type == "response.created":
            response_id = msg.get("response", {}).get("id")
            if response_id:
                convo.active_response_ids.append(response_id)
                logger.debug("[%s] response.created: %s", convo.client_id, response_id)
            # Capture bot turn-start at response.created (see method
            # docstring for ordering rationale).
            convo.bot_turn_started_at = int(time.time() * 1000)
            convo.response_had_audio = False
            return

        if event_type == "response.done":
            response_id = msg.get("response", {}).get("id")
            if response_id and response_id in convo.active_response_ids:
                convo.active_response_ids.remove(response_id)
            convo.awaiting_terminal_speech = False
            await self._flush_output_remainder(convo)
            # BL-003: greeting / BOT-only turns never get a CUSTOMER
            # transcription event. Flush any stashed BOT here. If a
            # customer turn is still in flight (VAD start or pending
            # text), wait for `.completed` so CUSTOMER lands first.
            await self._flush_stashed_transcripts(convo)
            _monitor_emit("turn.complete", {
                "session_id": convo.session_id,
                "endpoint_id": convo.endpoint_id,
                "provider": "xai",
            })
            logger.debug("[%s] response.done: %s", convo.client_id, response_id)
            return

        if event_type == "response.output_audio.done":
            # Server-side audio generation is complete for this response,
            # though paced playout may still be running. Mark the
            # response_id as completed (so subsequent barge-ins skip
            # response.cancel for it) and signal end-of-segment to the
            # streamer (lastf=true to Infinity + playout-done callback
            # that clears audio_playing_out via the natural-drain path).
            # See method docstring branch on `response.output_audio.done`.
            response_id = msg.get("response_id") or msg.get("response", {}).get("id", "")
            if response_id:
                convo.completed_response_ids.add(response_id)
            await self._flush_output_remainder(convo)
            await self.server.ingress_streamer.mark_audio_segment_complete(
                convo.session_id, convo.endpoint_id, convo.client_id,
            )
            logger.debug(
                "[%s] response.output_audio.done: %s — segment marked complete",
                convo.client_id, response_id,
            )
            return

        if event_type in (
            "response.output_item.added",
            "response.content_part.added",
            "response.output_item.done",
            "response.content_part.done",
        ):
            logger.debug("[%s] %s", convo.client_id, event_type)  # INFORMATIONAL
            return

        # ── Audio delta (egress → Infinity) ───────────────────────────────────

        if event_type == "response.output_audio.delta":
            b64 = msg.get("delta", "")
            if not b64:
                return
            oa_audio = base64.b64decode(b64)
            convo.response_had_audio = True
            out_bytes = self._transcode_output_audio(convo, oa_audio)
            if not out_bytes:
                return
            if not convo.ingress_ready:
                if len(convo.ingress_buffer) >= _INGRESS_BUFFER_MAX_CHUNKS:
                    logger.warning(
                        "[%s] Ingress buffer full (%d chunks) — discarding oldest",
                        convo.client_id, _INGRESS_BUFFER_MAX_CHUNKS,
                    )
                    convo.ingress_buffer.pop(0)
                convo.ingress_buffer.append(out_bytes)
                logger.debug(
                    "[%s] Buffering xAI audio — Infinity ingress not ready (buffered: %d)",
                    convo.client_id, len(convo.ingress_buffer),
                )
                return
            await self._enqueue_output_audio(convo, out_bytes)
            return

        # ── Transcripts ───────────────────────────────────────────────────────

        if event_type == "response.output_audio_transcript.delta":
            response_id = msg.get("item_id") or msg.get("response_id", "")
            delta = msg.get("delta", "")
            if response_id and delta:
                convo.transcript_deltas.setdefault(response_id, "")
                convo.transcript_deltas[response_id] += delta
            return

        if event_type == "response.output_audio_transcript.done":
            response_id = msg.get("item_id") or msg.get("response_id", "")
            text = convo.transcript_deltas.pop(response_id, "").strip()
            if text:
                logger.info("[%s] BOT transcript: %s", convo.client_id, text)
                if convo.pending_bot_text:
                    convo.pending_bot_text = f"{convo.pending_bot_text} {text}"
                else:
                    convo.pending_bot_text = text
            await self._flush_stashed_transcripts(convo)
            return

        if event_type == "conversation.item.input_audio_transcription.updated":
            # Cumulative caller transcript (xAI name for OpenAI's .delta).
            convo.pending_customer_text = msg.get("transcript") or ""
            return

        if event_type == "conversation.item.input_audio_transcription.completed":
            text = (msg.get("transcript") or "").strip()
            if not text:
                text = convo.pending_customer_text.strip()
            convo.pending_customer_text = text
            # 2.0 emits `.completed` twice per turn: once as soon as it
            # has enough ASR to start responding, then again after
            # speech_stopped/committed. Same text, ~300ms apart.
            if convo.customer_transcript_emitted:
                logger.debug(
                    "[%s] Skipping duplicate CUSTOMER transcript: %s",
                    convo.client_id, text,
                )
                convo.pending_customer_text = ""
                return
            if text:
                logger.info("[%s] CUSTOMER transcript: %s", convo.client_id, text)
                await self._emit_transcript(
                    convo, "CUSTOMER", text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
                convo.customer_transcript_emitted = True
            convo.pending_customer_text = ""
            convo.customer_turn_started_at = None
            await self._flush_stashed_transcripts(convo)
            return

        # ── Tool / function calls ──────────────────────────────────────────────

        # xAI's GA event names for tool invocation use the
        # `response.function_call_arguments.delta` / `.done` form. The
        # `.done` event carries the completed arguments JSON and is the
        # signal to dispatch to the handler.
        if event_type == "response.function_call_arguments.done":
            await self._handle_function_call(convo, msg)
            return

        # ── Errors ────────────────────────────────────────────────────────────

        if event_type == "error":
            error = msg.get("error", {})
            logger.error(
                "[%s] xAI error: type=%s code=%s message=%s",
                convo.client_id,
                error.get("type"),
                error.get("code"),
                error.get("message"),
            )
            _monitor_emit("provider.error", {
                "session_id": convo.session_id,
                "endpoint_id": convo.endpoint_id,
                "provider": "xai",
                "error_type": error.get("type"),
                "error_code": error.get("code"),
                "error_message": error.get("message"),
            })
            return

        # ── Everything else ───────────────────────────────────────────────────
        logger.debug("[%s] Unhandled xAI event: %s", convo.client_id, event_type)

    # ---------------------------------------------------------------- session.updated

    async def _on_session_updated(self, convo: BotConversation) -> None:
        """Handle the `session.updated` ack: send the proactive greeting and emit `bot.started` to Infinity.

        Contract:
            Triggered by `_handle_xai_message` on the
            `session.updated` event. The session-config
            round-trip sent in `_connect_xai` has now completed;
            xAI has accepted the instructions, voice, audio
            format blocks, server VAD, and tool registration.
            From this point on, the session is live and ready
            to generate audio.

            Two effects, in order:

                1. **Proactive greeting via `response.create`.**
                   A bare `response.create` frame (no
                   per-response override) is sent so the model
                   generates the greeting using the
                   session-level persona prompt. The persona
                   prompt's "Greeting" section instructs the
                   model what the first turn should include. A
                   per-response `instructions` override would
                   *replace* the session-level persona for that
                   response, stripping the name / case /
                   branding context established in the
                   session-level prompt; bare `response.create`
                   is the correct invocation for this step.
                   Send failures are logged at WARNING and
                   swallowed (the call continues; the caller
                   may have to speak first).
                2. **`bot.started` to Infinity.** Built and
                   sent directly here (not via a helper).
                   Carries the standard envelope plus
                   `payload.endpointId`. Infinity reads this as
                   the cue to begin sending caller audio;
                   sending it here (rather than at the end of
                   `_handle_bot_start`) ensures xAI is fully
                   configured before Infinity routes audio at
                   the bridge.

            **Why `session.updated` is the right anchor for
            both effects.** `session.created` fires on
            session-bootstrap but predates configuration
            acceptance; sending the greeting then would race
            the configuration apply on xAI's side.
            `session.updated` is the "configuration accepted"
            signal — voice, VAD, tool registration are all in
            effect. Any greeting audio that lands before
            Infinity's ingress path opens is caught by the
            `ingress_ready` latch on the BotConversation (see
            `BotConversation` "Ingress readiness latch" group)
            and flushed when Infinity's first egress frame
            arrives.

        Spec:
            xAI Grok Voice Realtime API protocol —
            `session.updated` is the configuration-accepted
            ack; `response.create` is the response-solicitation
            event.
            RCMS spec §AI Bot Message Definitions —
            `bot.started` envelope shape and
            `payload.endpointId` requirement.

        Args:
            convo: The active conversation whose
                `session.updated` ack triggered this handler.
                Reads `xai_ws`, `session_id`, `endpoint_id`,
                `client_id`, `service`. No mutation.

        Returns:
            None. Side effects: outbound `response.create` to
            xAI; outbound `bot.started` to Infinity.
        """
        greeting = {"type": "response.create"}
        try:
            await convo.xai_ws.send(json.dumps(greeting))
            logger.info("[%s] Sent proactive greeting response.create", convo.client_id)
        except Exception as exc:
            logger.warning("[%s] Failed to send greeting: %s", convo.client_id, exc)

        # Signal Infinity that the bot is ready.
        response = {
            "version": "1.0.0",
            "type": "bot.started",
            "sessionId": convo.session_id,
            "sequenceNum": self.server.get_next_sequence(convo.client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": convo.service,
            "payload": {"endpointId": convo.endpoint_id},
        }
        logger.info("[%s] OUTBOUND JSON (bot.started): %s",
                    convo.client_id, format_compact_json(response))
        log_message_exchange("OUTBOUND", convo.client_id, "bot.started", response, is_media=False)
        await convo.websocket.send(json.dumps(response))

    # ---------------------------------------------------------------- barge-in

    def _on_playout_done(self, convo: BotConversation) -> None:
        """Synchronous callback fired by the IngressStreamer when the egress queue drains naturally.

        Contract:
            Registered with the IngressStreamer in
            `_handle_bot_start` via
            `register_playout_done_callback`. The streamer
            invokes this synchronously from inside its
            streaming loop when the per-endpoint egress queue
            drains under either of two conditions:

                * The idle timeout fires (no chunks queued for
                  the configured idle window).
                * An `is_last=True` chunk is paced out (sent by
                  `IngressStreamer.mark_audio_segment_complete`
                  on `response.output_audio.done`).

            Effect: clears `convo.audio_playing_out` so the
            barge-in gate closes — subsequent
            `input_audio_buffer.speech_started` events skip
            barge-in dispatch because there is no audio left
            to interrupt.

            **Synchronous and called from the streaming loop.**
            Keep this cheap. No I/O, no awaits, no complex
            state traversal. The flag flip is the entire body.

        Args:
            convo: The active conversation whose egress queue
                just drained. Mutated: `audio_playing_out` →
                False (when previously True).

        Returns:
            None.
        """
        if convo.audio_playing_out:
            logger.debug("[%s] Playout done — barge-in gate closed", convo.client_id)
            convo.audio_playing_out = False

    async def _handle_barge_in(self, convo: BotConversation) -> None:
        """Stop egress pump on barge-in: clear the queue, send last-flag, and cancel any not-yet-completed response.

        Contract:
            Triggered from `_handle_xai_message`'s
            `input_audio_buffer.speech_started` branch when
            `audio_playing_out` is True. Performs the full
            barge-in sequence:

                1. **Close the barge-in gate.** Set
                   `audio_playing_out = False` explicitly. The
                   streamer's playout-done callback fires only
                   on the natural-drain path (idle timeout or
                   `is_last=True`); on the barge-in path the
                   queue is force-cleared, so the bridge clears
                   the flag itself.
                2. **Discard buffered output tail.** Clear
                   `ingress_accumulator` so any chunk fragment
                   accumulated for the current paced flush
                   doesn't leak past the barge-in into the
                   next turn.
                3. **Flush IngressStreamer queue.** Call
                   `IngressStreamer.barge_in` for this
                   `(session_id, endpoint_id)` to clear the
                   per-endpoint queue and send a `lastf=true`
                   flag to Infinity on the current segment.
                   Errors are logged at DEBUG and swallowed.
                4. **Cancel the active xAI response.** Look up
                   the most recent entry in
                   `active_response_ids`. If its server-side
                   audio generation has already completed
                   (response_id in `completed_response_ids`),
                   skip the `response.cancel` send — xAI would
                   always race-lose against an already-finished
                   response; the bridge-side queue clear above
                   is the actual barge-in mechanism. Otherwise
                   send `response.cancel` with the response_id.
                   Errors are logged at WARNING and swallowed.

            Returns silently when there are no active responses
            or no upstream WebSocket — the bridge-side queue
            clear (steps 1–3) has already happened, and there
            is nothing to cancel upstream.

        Spec:
            xAI Grok Voice Realtime API protocol —
            `response.cancel` for in-flight response
            cancellation. Calls against already-completed
            responses race-lose against the server's finished
            state.

        Args:
            convo: The active conversation. Mutated:
                `audio_playing_out` → False;
                `ingress_accumulator` cleared.

        Returns:
            None. All errors logged and swallowed.
        """
        # Explicit clear; the streamer's playout-done callback fires only
        # on the natural-drain path, not on this force-clear path.
        convo.audio_playing_out = False
        convo.ingress_accumulator.clear()
        try:
            await self.server.ingress_streamer.barge_in(convo.session_id, convo.endpoint_id)
        except Exception as exc:
            logger.debug("[%s] barge_in raised: %s", convo.client_id, exc)

        if not (convo.active_response_ids and convo.xai_ws):
            return
        response_id = convo.active_response_ids[-1]
        if response_id in convo.completed_response_ids:
            # Server-side audio generation already completed
            # (response.output_audio.done fired). response.cancel would
            # race-lose. Skip it; the bridge-side queue clear above is
            # the barge-in mechanism.
            logger.debug(
                "[%s] Skipping response.cancel for %s — audio.done already fired",
                convo.client_id, response_id,
            )
            return
        cancel = {"type": "response.cancel", "response_id": response_id}
        try:
            await convo.xai_ws.send(json.dumps(cancel))
            logger.info("[%s] Sent response.cancel for %s", convo.client_id, response_id)
        except Exception as exc:
            logger.warning("[%s] response.cancel failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- tool calls

    async def _handle_function_call(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a completed `response.function_call_arguments.done` to its handler and reply with `function_call_output`.

        Contract:
            xAI Grok Realtime delivers the tool invocation as a
            `response.function_call_arguments.done` event
            carrying `call_id`, `name`, and a JSON `arguments`
            string. The bridge implements two tools (declared
            inline in this module and registered via
            `session.update.session.tools`):

                * **`transfer_to_agent`**: stash a
                  `bot.feature LIVE_AGENT_HANDOFF` payload on
                  `convo.pending_handoff` and start the drain
                  task in `_wait_for_quiescence_and_emit`. The
                  wire-level emit is deferred until the
                  IngressStreamer queue drains so the
                  acknowledgment audio plays out before
                  Infinity tears down playback for the handoff.
                  Returns `result_text = "Transfer initiated."`
                  to xAI. `is_terminal_tool` is True.
                * **`end_session`**: set the
                  `convo.pending_session_end` latch (boolean —
                  no per-call payload to stash, since the
                  success-context `bot.ended` carries only the
                  static success status) and start the drain
                  task in
                  `_wait_for_quiescence_and_emit_session_end`.
                  Same drain logic as the handoff path; the
                  terminal envelope is the success-context
                  `bot.ended` instead of `LIVE_AGENT_HANDOFF +
                  bot.ended`. Returns `result_text = "Session
                  end initiated."` to xAI. `is_terminal_tool`
                  is True.
                * **anything else**: logged at WARNING; the
                  result text reflects the unknown tool name.
                  `is_terminal_tool` is False.

            **Terminal-tool suppression protocol.** For
            terminal tools (`transfer_to_agent`,
            `end_session`), the bridge sends the
            `function_call_output` and, when the tool response
            already carried preamble audio (`response_had_audio`),
            **does not** follow up with `response.create` —
            extra speech after the terminal envelope leaks a
            stranded transcript turn (1.0 FIND-001 drain path).
            `grok-voice-think-fast-2.0` often fires the tool
            with no audio in that response. In that case the
            bridge *does* send `response.create` so the model
            can speak the protocol acknowledgment, and the
            drain waits for that follow-up response to finish
            playing before emitting the terminal envelope.

            For non-terminal tools, the bridge always sends
            `response.create` after the
            `function_call_output` so the model can speak the
            tool result. No non-terminal tools are currently
            registered.

            **Defensive task replacement.** If a prior
            `handoff_task` or `session_end_task` is still
            running (the model invokes the terminal tool
            twice), the prior task is cancelled before the new
            one starts. In practice the model picks one
            terminal tool per call, but the guard prevents a
            duplicate-call race from leaving an orphan drain
            coroutine.

            **Function call reply protocol.** Every invocation
            with a non-empty `call_id` requires a matching
            `conversation.item.create` with
            `type: "function_call_output"` keyed by that
            call_id. Even unknown-tool branches send the output
            so the model's continuation isn't blocked waiting
            for one.

        Spec:
            xAI Grok Voice Realtime API protocol —
            `response.function_call_arguments.done` event shape
            and the `conversation.item.create` reply with
            `function_call_output`.
            RCMS spec §AI Bot Message Definitions —
            `bot.feature` with `LIVE_AGENT_HANDOFF` ftype
            (emitted later by `_emit_pending_handoff` after
            drain).

        Args:
            convo: The active conversation. Mutated to stash
                the pending handoff payload, set the
                session-end latch, and launch the drain task.
            msg: The decoded
                `response.function_call_arguments.done` event.
                `call_id`, `name`, and `arguments` (JSON
                string) are read.

        Returns:
            None. The `function_call_output` (and conditional
            `response.create`) is sent on the upstream xAI
            WebSocket; send failures are logged at WARNING and
            swallowed.
        """
        call_id = msg.get("call_id", "")
        name = msg.get("name", "")
        arguments_json = msg.get("arguments", "{}")
        logger.info("[%s] Function call: %s (call_id=%s)", convo.client_id, name, call_id)

        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError:
            args = {}

        if name == "transfer_to_agent":
            reason = str(args.get("reason") or "Caller requested live agent")
            handoff_payload = {
                "ftype": "LIVE_AGENT_HANDOFF",
                "liveAgentHandoff": {
                    "queueId": "",
                    "tags": [],
                    "context": {"reason": reason},
                },
            }
            convo.pending_handoff = handoff_payload
            convo.pending_handoff_args = {"reason": reason}
            if convo.handoff_task and not convo.handoff_task.done():
                convo.handoff_task.cancel()
            convo.handoff_task = asyncio.create_task(
                self._wait_for_quiescence_and_emit(convo)
            )
            logger.info(
                "[%s] Stashed LIVE_AGENT_HANDOFF (reason=%r) — draining queue",
                convo.client_id, reason,
            )
            result_text = "Transfer initiated."
            is_terminal_tool = True
        elif name == "end_session":
            # Self-service-complete signal. Defer the success-context
            # bot.ended emission until the goodbye audio drains; same
            # is_terminal_tool=True rationale as transfer_to_agent (the
            # terminal-tool suppression protocol applies to both).
            convo.pending_session_end = True
            if convo.session_end_task and not convo.session_end_task.done():
                convo.session_end_task.cancel()
            convo.session_end_task = asyncio.create_task(
                self._wait_for_quiescence_and_emit_session_end(convo)
            )
            logger.info(
                "[%s] Stashed self-service-complete — draining queue",
                convo.client_id,
            )
            result_text = "Session end initiated."
            is_terminal_tool = True
        else:
            logger.warning("[%s] Unknown xAI tool '%s'", convo.client_id, name)
            result_text = f"Unknown tool: {name}"
            is_terminal_tool = False

        # Send the function result. Terminal tools skip response.create
        # when the tool response already included preamble audio (1.0
        # path). 2.0 often fires the tool with no audio — solicit a
        # spoken acknowledgment and let the drain wait for it.
        if call_id and convo.xai_ws:
            try:
                await convo.xai_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_text,
                    },
                }))
                solicit_speech = (not is_terminal_tool) or (not convo.response_had_audio)
                if solicit_speech:
                    if is_terminal_tool:
                        # 2.0 may still be generating a pre-tool answer
                        # (observed: 9s leftover pitch after transfer).
                        # Cancel it so the acknowledgment is the next audio.
                        await self._handle_barge_in(convo)
                        convo.awaiting_terminal_speech = True
                        logger.info(
                            "[%s] Tool-only response — soliciting spoken acknowledgment",
                            convo.client_id,
                        )
                    await convo.xai_ws.send(json.dumps({"type": "response.create"}))
            except Exception as exc:
                logger.warning("[%s] function_call_output send failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- transcripts

    async def _flush_stashed_transcripts(self, convo: BotConversation) -> None:
        """Emit stashed BOT transcript unless it answers an in-flight CUSTOMER.

        BL-003: Infinity stamps each TRANSCRIPT by frame arrival and
        ignores `startTsMs`. Hold BOT only while the caller utterance
        *this bot turn is answering* is still in flight (started at or
        before `bot_turn_started_at`, not yet emitted). A later
        barge-in / next question must not delay that BOT — otherwise
        the next CUSTOMER lands first and Infinity shows inverted
        order. Greeting / BOT-only turns flush immediately.
        """
        answering_in_flight_customer = bool(
            convo.customer_turn_started_at
            and not convo.customer_transcript_emitted
            and (
                convo.bot_turn_started_at is None
                or convo.customer_turn_started_at <= convo.bot_turn_started_at
            )
        )
        if answering_in_flight_customer:
            return
        bot_text = convo.pending_bot_text.strip()
        if not bot_text:
            return
        await self._emit_transcript(
            convo, "BOT", bot_text,
            start_ts_ms=convo.bot_turn_started_at,
        )
        convo.pending_bot_text = ""
        convo.bot_turn_started_at = None

    async def _emit_transcript(
        self,
        convo: BotConversation,
        speaker: str,
        text: str,
        is_final: bool = True,
        start_ts_ms: Optional[int] = None,
    ) -> None:
        """Send a TRANSCRIPT bot.feature envelope plus a `transcript.sent` monitor event.

        Contract:
            Wraps a single transcript line in the RCMS
            `bot.feature` / TRANSCRIPT envelope shape and ships
            it to Infinity via `_emit_session_event`. The
            payload carries a fresh `turnId` (UUID per call),
            the conversation's `language_code`, and the
            speaker's start-of-turn timestamp.

            **Why a turn-start timestamp matters.** Transcript
            ordering in Infinity's dashboard is keyed on
            `startTsMs`, not on receive time. The bridge
            captures turn starts at protocol-defined moments —
            `input_audio_buffer.speech_started` for the caller,
            `response.created` for the bot — so two transcripts
            emitted out of order at flush time still render in
            the correct sequence on screen. Falls back to
            flush time (`time.time() * 1000`) when the caller
            does not pass a turn-start, which is acceptable for
            real-time deltas but not for the pre-handoff flush
            (the flush path always passes the turn-start).

            Also fans out a `transcript.sent` monitor event
            with provider+language metadata; the monitor channel
            is best-effort and does not block the wire emit.

        Args:
            convo: The conversation owning the transcript.
                Reads `session_id`, `endpoint_id`,
                `language_code`.
            speaker: Either `"BOT"` or `"CUSTOMER"` per the
                RCMS speaker enum.
            text: The transcript text. Caller is responsible
                for trimming and skipping empty strings.
            is_final: Whether this is a finalized line. Defaults
                to True; partial deltas (rare on xAI — only the
                pre-handoff flush emits with `is_final=False`)
                use False.
            start_ts_ms: Speaker turn-start in epoch
                milliseconds. None → fall back to current time.

        Returns:
            None. Wire effect: a `bot.feature` envelope reaches
            Infinity over `convo.websocket`.
        """
        _monitor_emit("transcript.sent", {
            "session_id": convo.session_id,
            "endpoint_id": convo.endpoint_id,
            "provider": "xai",
            "speaker": speaker,
            "text": text,
            "is_final": is_final,
            "language": convo.language_code,
        })
        payload = {
            "ftype": "TRANSCRIPT",
            "transcript": {
                "turnId": str(uuid.uuid4()),
                "speaker": speaker,
                "isFinal": is_final,
                "text": text,
                "confidence": 1.0,
                "language": convo.language_code,
                "startTsMs": start_ts_ms if start_ts_ms is not None else int(time.time() * 1000),
            },
        }
        await self._emit_session_event(convo, "bot.feature", payload)

    async def _emit_session_event(
        self,
        convo: BotConversation,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Wrap a payload in the standard RCMS envelope and send it to Infinity.

        Contract:
            Builds the protocol-required envelope around an
            arbitrary inner payload: `version="1.0.0"`,
            `type=event_type`, `sessionId`, monotonically
            increasing `sequenceNum` from the server's
            per-client counter, ISO-8601 UTC `timestamp`, and
            `payload` (the caller's payload merged with
            `endpointId` from the conversation).

            Used for every Infinity-bound event except the
            originator `bot.ended` in `_emit_pending_handoff`
            (which builds its envelope inline because it carries
            a `service` field at the top level that doesn't fit
            this signature).

            Logs each emit at INFO with the compacted JSON and
            replicates to the message-exchange log so the wire
            trace is captured. Send is awaited; any
            WebSocket-side failure propagates to the caller —
            the drain/emit methods catch and log at WARNING.

        Args:
            convo: Conversation context. Reads `endpoint_id`,
                `session_id`, `client_id`, `websocket`.
            event_type: RCMS envelope type — `"bot.started"`,
                `"bot.feature"`, `"bot.ended"`, etc.
            payload: Inner payload. `endpointId` is added
                automatically; do not pre-populate.

        Returns:
            None. Wire effect: one JSON frame reaches Infinity
            over `convo.websocket`.
        """
        body = {**payload, "endpointId": convo.endpoint_id}
        event = {
            "version": "1.0.0",
            "type": event_type,
            "sessionId": convo.session_id,
            "sequenceNum": self.server.get_next_sequence(convo.client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": body,
        }
        logger.info("[%s] OUTBOUND JSON (%s): %s",
                    convo.client_id, event_type, format_compact_json(event))
        log_message_exchange("OUTBOUND", convo.client_id, event_type, event, is_media=False)
        await convo.websocket.send(json.dumps(event))

    # ---------------------------------------------------------------- handoff drain

    def _quiescence_reached(
        self,
        convo: BotConversation,
        streamer: Any,
        endpoint_key: Any,
    ) -> bool:
        """True when generation is finished and paced audio has drained.

        2.0 can fire a terminal tool before any audio is queued. A
        queue-empty check alone then trips on the first poll (~250ms)
        and emits the terminal envelope before the solicited
        acknowledgment response has started. Wait until the follow-up
        response (if any) is done and playout has finished.
        """
        if convo.awaiting_terminal_speech or convo.active_response_ids:
            return False
        if convo.audio_playing_out:
            return False
        queue = streamer._queues.get(endpoint_key)
        return queue is None or queue.empty()

    async def _wait_for_quiescence_and_emit(
        self,
        convo: BotConversation,
        poll_ms: int = _QUIESCENCE_POLL_MS,
        deadlock_safety_s: float = _QUIESCENCE_DEADLOCK_SAFETY_S,
    ) -> None:
        """Sleep-and-check drain loop — emits the pending handoff once the IngressStreamer queue is empty.

        Contract:
            Single-rule drain: sleep `poll_ms`, then check
            whether the per-endpoint IngressStreamer queue is
            empty. Empty → emit and return. Non-empty → loop.
            The sleep itself is the grace period — long enough
            for any audio chunk crossing the network during
            the iteration to land in the queue before the
            empty check.

            Drain timing rationale: the
            `transfer_to_agent` `function_call_arguments.done`
            event arrives before the acknowledgment audio
            finishes streaming. The bridge does not send
            `response.create` after the `function_call_output`
            for terminal tools (see `_handle_function_call`),
            so the drain only needs to wait for the preamble
            audio xAI generated alongside the tool call to
            fully reach Infinity. Without the drain, the
            handoff would land while audio is still in flight
            and Infinity would cut the bot leg mid-word.

            **What "queue empty" means.** The empty check
            covers all audio handed to Infinity over the
            bridge's outbound WebSocket. It does not cover any
            buffering Infinity may apply downstream toward the
            caller's PSTN line — that's outside the bridge's
            observability. Tail-clipping symptoms past this
            point warrant a settle-delay rather than a
            different drain mechanism.

            **Safety net.** `deadlock_safety_s` is an
            upstream-failure backstop — e.g. xAI WS hung in a
            way that prevents the audio queue from ever
            draining. Trip indicates something genuinely wrong
            upstream, not a drain-timing issue. Logs at
            WARNING and emits the handoff anyway so the
            workflow does not stall indefinitely.

            Cancellation-safe: `asyncio.CancelledError` returns
            silently without emitting.
            `_shutdown_conversation` cancels `handoff_task` on
            a caller-disconnect mid-handoff so this method
            exits without firing the emit (the conversation is
            being torn down anyway).

        Args:
            convo: The conversation whose handoff is pending.
                Reads `pending_handoff`; passes through to
                `_emit_pending_handoff` on drain completion.
            poll_ms: Sleep interval per iteration in
                milliseconds. Default `_QUIESCENCE_POLL_MS`
                (250 ms).
            deadlock_safety_s: Upper bound on total wait time.
                Default `_QUIESCENCE_DEADLOCK_SAFETY_S` (30 s).

        Returns:
            None. Always either emits via
            `_emit_pending_handoff` (drain done or safety-net
            trip) or exits silently on cancellation.
        """
        start = time.monotonic()
        poll_s = poll_ms / 1000.0
        streamer = self.server.ingress_streamer
        endpoint_key = streamer._endpoint_key(convo.session_id, convo.endpoint_id)

        try:
            while convo.pending_handoff:
                if time.monotonic() - start > deadlock_safety_s:
                    logger.warning(
                        "[%s] Drain deadlock safety net fired after %.1fs — emitting anyway",
                        convo.client_id, deadlock_safety_s,
                    )
                    break
                await asyncio.sleep(poll_s)
                if self._quiescence_reached(convo, streamer, endpoint_key):
                    logger.info(
                        "[%s] Drain done after %.2fs (queue_empty on poll)",
                        convo.client_id, time.monotonic() - start,
                    )
                    break
        except asyncio.CancelledError:
            return

        await self._emit_pending_handoff(convo)

    async def _wait_for_quiescence_and_emit_session_end(
        self,
        convo: BotConversation,
        poll_ms: int = _QUIESCENCE_POLL_MS,
        deadlock_safety_s: float = _QUIESCENCE_DEADLOCK_SAFETY_S,
    ) -> None:
        """Sleep-and-check drain loop — emits the success-context bot.ended once the IngressStreamer queue is empty.

        Contract:
            Sibling to `_wait_for_quiescence_and_emit`; same
            single-rule drain (sleep `poll_ms`, then check
            queue-empty) but latches on `pending_session_end`
            and dispatches to `_emit_session_end_complete` on
            drain completion. The two pipelines run on separate
            tasks (`handoff_task` and `session_end_task`) and
            are mutually exclusive in practice — a turn fires
            at most one terminal tool.

            Drain timing rationale: `end_session` is also a
            terminal tool, so the bridge does not send
            `response.create` after the `function_call_output`
            (see `_handle_function_call`). The drain waits for
            the closing utterance audio xAI generated alongside
            the tool call to fully reach Infinity before the
            bot.ended emit, otherwise Infinity tears down the
            bot leg mid-word.

            **Safety net.** `deadlock_safety_s` is an
            upstream-failure backstop. Trip indicates an xAI WS
            stall or upstream stuck-state, not a drain-timing
            issue. Logs at WARNING and emits the success
            bot.ended anyway so the workflow does not stall.

            Cancellation-safe: `asyncio.CancelledError` returns
            silently without emitting.
            `_shutdown_conversation` cancels `session_end_task`
            on a caller-disconnect mid-drain so this method
            exits without firing the emit (the disconnect path
            owns the bot.ended in that case).

        Args:
            convo: The conversation whose self-service-complete
                end is pending. Reads `pending_session_end`;
                passes through to `_emit_session_end_complete`
                on drain completion.
            poll_ms: Sleep interval per iteration in
                milliseconds. Default `_QUIESCENCE_POLL_MS`
                (250 ms).
            deadlock_safety_s: Upper bound on total wait time.
                Default `_QUIESCENCE_DEADLOCK_SAFETY_S` (30 s).

        Returns:
            None. Always either emits via
            `_emit_session_end_complete` (drain done or
            safety-net trip) or exits silently on cancellation.
        """
        start = time.monotonic()
        poll_s = poll_ms / 1000.0
        streamer = self.server.ingress_streamer
        endpoint_key = streamer._endpoint_key(convo.session_id, convo.endpoint_id)

        try:
            while convo.pending_session_end:
                if time.monotonic() - start > deadlock_safety_s:
                    logger.warning(
                        "[%s] Session-end drain deadlock safety net fired after %.1fs — emitting anyway",
                        convo.client_id, deadlock_safety_s,
                    )
                    break
                await asyncio.sleep(poll_s)
                if self._quiescence_reached(convo, streamer, endpoint_key):
                    logger.info(
                        "[%s] Session-end drain done after %.2fs (queue_empty on poll)",
                        convo.client_id, time.monotonic() - start,
                    )
                    break
        except asyncio.CancelledError:
            return

        await self._emit_session_end_complete(convo)

    async def _emit_session_end_complete(self, convo: BotConversation) -> None:
        """Send the success-context bot.ended for a self-service-complete tool call.

        Contract:
            Final stage of the `end_session` pipeline:
            self-service-complete drain → this emit. Latches on
            `pending_session_end` (re-entrancy guard — returns
            early if cleared, e.g. caller-disconnect raced the
            drain to the emit). On entry: clears the latch,
            cancels the drain task if still alive (skipped when
            the drain task itself is the caller), then
            dispatches `send_bot_ended_with_success_context` —
            the shared bridge helper that constructs the success
            payload and sets `bot_ended_sent` so subsequent
            disconnect paths do not double-emit.

            **Why a separate emit method.** The drain method
            handles wait-for-empty timing; this method owns the
            wire emit and the latch reset. Keeping them
            separate lets `_handle_function_call` short-circuit
            the drain path on test fixtures (call this directly
            and bypass quiescence) without duplicating the
            envelope build.

            Failure handling: any exception from the bridge
            helper (WebSocket dropped, Infinity unreachable) is
            caught and logged at WARNING. The conversation
            tear-down still proceeds — `on_session_ended` is
            invoked by Infinity's `session.end` ack; if that
            never arrives, `_shutdown_conversation` runs on
            client-disconnect.

        Args:
            convo: The conversation whose success bot.ended is
                being sent. Mutates `pending_session_end`,
                `session_end_task`, and (via helper)
                `bot_ended_sent`.

        Returns:
            None. Wire effect on success: a `bot.ended` envelope
            with `disconnectReason="successCompleted"` reaches
            Infinity over `convo.websocket`.
        """
        if not convo.pending_session_end:
            return
        convo.pending_session_end = False
        task = convo.session_end_task
        convo.session_end_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        try:
            customer_text = convo.pending_customer_text.strip()
            if customer_text and not convo.customer_transcript_emitted:
                await self._emit_transcript(
                    convo, "CUSTOMER", customer_text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            convo.pending_customer_text = ""
            convo.customer_turn_started_at = None
            for resp_id, text in list(convo.transcript_deltas.items()):
                text = text.strip()
                if text:
                    await self._emit_transcript(
                        convo, "BOT", text,
                        start_ts_ms=convo.bot_turn_started_at,
                    )
            convo.transcript_deltas.clear()
            await self._flush_stashed_transcripts(convo)
            convo.pending_bot_text = ""
            convo.bot_turn_started_at = None
        except Exception as exc:
            logger.warning("[%s] Pre-session-end transcript flush failed: %s", convo.client_id, exc)

        try:
            await self.server.send_bot_ended_with_success_context(
                convo.websocket,
                convo.client_id,
                convo.session_id,
                convo.endpoint_id,
                description="XAI: Self-service interaction completed.",
                service=convo.service,
                convo=convo,
            )
            logger.info("[%s] Emitted self-service-complete bot.ended", convo.client_id)
        except Exception as exc:
            logger.warning("[%s] success-context bot.ended emit failed: %s",
                           convo.client_id, exc)

    async def _emit_pending_handoff(self, convo: BotConversation) -> None:
        """Send the LIVE_AGENT_HANDOFF bot.feature followed by an originator bot.ended.

        Contract:
            Final stage of the `transfer_to_agent` pipeline:
            handoff drain → this emit. Latches on
            `pending_handoff` (re-entrancy guard — returns
            early if cleared). On entry: snapshots payload and
            args, clears both latches, cancels the drain task
            if still alive (skipped when the drain task itself
            is the caller).

            **Three wire emits, in order.** (1) Stranded
            transcript flush: any partial customer or bot text
            buffered when the tool fired is emitted with the
            speaker's turn-start timestamp so transcript
            ordering reflects when the speaker actually started
            talking, not when the flush ran. (2)
            LIVE_AGENT_HANDOFF: a `bot.feature` envelope with
            the pending handoff payload, plus a deferred
            monitor `session.handoff` event carrying the
            transfer reason. (3) Originator bot.ended: the
            bridge sends bot.ended directly (bypassing the
            disconnect-context helpers) and sets
            `bot_ended_sent` so any subsequent
            caller-disconnect path does not double-emit.

            **Why bridge originates bot.ended on handoff.** The
            handoff is bridge-initiated (the model called
            `transfer_to_agent`, not Infinity sending
            `session.end`). The bot leg has to be torn down
            from this side so Infinity can ack with
            `session.end` and route the call to the live agent.

            Failure handling: each of the three wire emits is
            independently try/except'd at WARNING — a transcript
            flush failure does not block the handoff, a
            LIVE_AGENT_HANDOFF failure does not block the
            bot.ended (Infinity still gets a leg-end signal).

        Args:
            convo: The conversation whose pending handoff is
                being emitted. Mutates `pending_handoff`,
                `pending_handoff_args`, `handoff_task`,
                `pending_customer_text`, `transcript_deltas`,
                `bot_turn_started_at`, `customer_turn_started_at`,
                and `bot_ended_sent`.

        Returns:
            None. Wire effects on success: a `bot.feature`
            (LIVE_AGENT_HANDOFF) and a `bot.ended` envelope
            reach Infinity, plus any stranded transcripts as
            `bot.feature` (TRANSCRIPT) envelopes.
        """
        payload = convo.pending_handoff
        args = convo.pending_handoff_args or {}
        if not payload:
            return
        convo.pending_handoff = None
        convo.pending_handoff_args = None
        task = convo.handoff_task
        convo.handoff_task = None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        # Pre-handoff transcript flush. Clear customer-in-flight first so
        # BL-003 will emit stashed BOT after CUSTOMER (not before).
        try:
            customer_text = convo.pending_customer_text.strip()
            if customer_text and not convo.customer_transcript_emitted:
                await self._emit_transcript(
                    convo, "CUSTOMER", customer_text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            convo.pending_customer_text = ""
            convo.customer_turn_started_at = None
            # Older barge-in leftovers before the transfer acknowledgment.
            for resp_id, text in list(convo.transcript_deltas.items()):
                text = text.strip()
                if text:
                    await self._emit_transcript(
                        convo, "BOT", text,
                        start_ts_ms=convo.bot_turn_started_at,
                    )
            convo.transcript_deltas.clear()
            await self._flush_stashed_transcripts(convo)
            convo.pending_bot_text = ""
            convo.bot_turn_started_at = None
        except Exception as exc:
            logger.warning("[%s] Pre-handoff transcript flush failed: %s", convo.client_id, exc)

        try:
            await self._emit_session_event(convo, "bot.feature", payload)
            reason = args.get("reason", "")
            _monitor_emit("session.handoff", {
                "session_id": convo.session_id,
                "endpoint_id": convo.endpoint_id,
                "provider": "xai",
                "queue_id": "",
                "tags": [],
                "context": {"reason": reason},
                "deferred": True,
            })
            logger.info("[%s] Emitted LIVE_AGENT_HANDOFF (reason=%r)", convo.client_id, reason)
        except Exception as exc:
            logger.warning("[%s] LIVE_AGENT_HANDOFF emit failed: %s", convo.client_id, exc)

        # Originate bot.ended — bridge initiates, Infinity acks with session.end.
        try:
            bot_ended = {
                "version": "1.0.0",
                "type": "bot.ended",
                "sessionId": convo.session_id,
                "sequenceNum": self.server.get_next_sequence(convo.client_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "service": convo.service,
                "payload": {"endpointId": convo.endpoint_id},
            }
            logger.info("[%s] OUTBOUND JSON (bot.ended): %s",
                        convo.client_id, format_compact_json(bot_ended))
            log_message_exchange("OUTBOUND", convo.client_id, "bot.ended", bot_ended, is_media=False)
            await convo.websocket.send(json.dumps(bot_ended))
            # Mark flag so on_session_ended does not emit a second bot.ended.
            # The disconnect-context helper guards on bot_ended_sent but this
            # bare-emit path bypasses the helpers — set explicitly here.
            convo.bot_ended_sent = True
        except Exception as exc:
            logger.warning("[%s] bot.ended originator send failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- shutdown

    async def _shutdown_conversation(self, convo: BotConversation) -> None:
        """Tear down a single conversation: cancel pending tasks, clear ingress, close upstream WS.

        Contract:
            Single owner of the in-process tear-down sequence.
            Invoked from three call sites:

                * `_handle_bot_end` (Infinity-originated end)
                * `on_session_ended` (caller-disconnect /
                  session.end ack)
                * `shutdown` (process shutdown)

            **Tear-down order (matters).**
                1. `convo.active = False` — gates
                   `ingest_audio_chunk` so concurrent inbound
                   frames stop trying to forward to xAI.
                2. **Cancel the handoff drain task** if alive,
                   await it to completion (swallowing
                   `CancelledError` and any drain-side
                   exception). Clears `pending_handoff` /
                   `pending_handoff_args` so any in-flight
                   `_emit_pending_handoff` short-circuits on
                   the latch check.
                3. **Cancel the session-end drain task** with
                   the same pattern. Clears
                   `pending_session_end`.
                4. **Stop and clear IngressStreamer** for this
                   endpoint — drops any unsent paced audio so
                   downstream tasks don't hold references.
                5. **Cancel the xAI recv task** and await it,
                   so `_xai_recv_loop` exits before its
                   underlying WebSocket is closed.
                6. **Close the xAI WebSocket** — last, so the
                   recv task's `async for` exits cleanly via
                   `ConnectionClosed` rather than a torn-down
                   stream.

            Every cancellable await is wrapped in a broad
            try/except — shutdown must not raise back to the
            caller, since the caller is itself a shutdown path
            (and may need to shut down siblings even if one
            fails).

        Args:
            convo: The conversation to tear down. Heavily
                mutated.

        Returns:
            None.
        """
        convo.active = False

        handoff_task = convo.handoff_task
        convo.handoff_task = None
        convo.pending_handoff = None
        convo.pending_handoff_args = None
        if handoff_task and not handoff_task.done():
            handoff_task.cancel()
            try:
                await handoff_task
            except (asyncio.CancelledError, Exception):
                pass

        session_end_task = convo.session_end_task
        convo.session_end_task = None
        convo.pending_session_end = False
        if session_end_task and not session_end_task.done():
            session_end_task.cancel()
            try:
                await session_end_task
            except (asyncio.CancelledError, Exception):
                pass

        if convo.endpoint_id:
            try:
                await self.server.ingress_streamer.stop_and_clear(
                    convo.session_id, convo.endpoint_id
                )
            except Exception as exc:
                logger.debug("[%s] stop_and_clear raised: %s", convo.client_id, exc)

        task = convo.xai_recv_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        ws = convo.xai_ws
        convo.xai_ws = None
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[%s] xAI ws.close raised: %s", convo.client_id, exc)


def register(server: "BridgeServer") -> XaiService:
    """Module-level entry point — instantiate `XaiService` and register it on the bridge.

    Called by the bridge during plugin load (loader walks the
    `providers/` package and invokes each module's `register`).
    Constructs the plugin with the server reference and hands
    it to `BridgeServer.register_service`, which enrolls it in
    the dispatcher's prefix-routing table under the plugin's
    `name` (`"xai"`).

    Args:
        server: The bridge `BridgeServer` instance.

    Returns:
        The constructed `XaiService` plugin (returned for
        callers that want to keep a handle, e.g. tests).
    """
    plugin = XaiService(server)
    server.register_service(plugin)
    return plugin
