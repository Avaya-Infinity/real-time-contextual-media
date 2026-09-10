"""
bot_gemini — Gemini Live provider for the RCMS Virtual Agent (bot) service

Role:
    Implements the Gemini provider — a translator between an Infinity
    RCMS bot session and a Gemini Live BidiGenerateContent session. The
    architectural distinction from the ElevenLabs provider is the
    single most important framing for this module: **Gemini is a raw
    model, not an agent-as-a-service.** ElevenLabs hosts the agent
    (system prompt, voice, native tools, dialogue policy live in the
    ElevenLabs dashboard); Gemini exposes only the model itself. As
    a result, the bridge owns substantially more orchestration on the
    Gemini path than it does on ElevenLabs:

        * **System prompt** — composed in the bridge from a 4-tier
          resolution chain (`GEMINI_SYSTEM_PROMPT_FILE` env var →
          `GEMINI_SYSTEM_PROMPT` env var → sibling `system_prompt.md`
          file → `DEFAULT_SYSTEM_PROMPT` constant), with
          `{{variable}}` interpolation against `bot.start.payload.context`.
          See `_load_base_prompt` and `_compose_system_prompt`.
        * **Tool definitions** — `transfer_to_agent` and `end_session`
          declared inline in `_connect_gemini`'s `setup` payload,
          not in a Gemini dashboard. The bridge owns the schemas.
        * **Conversation initiation** — bridge sends an explicit
          `realtimeInput.text = "Hello, please greet the customer
          now"` after `setupComplete` so the agent speaks first.
          ElevenLabs has a configured first-message field; Gemini
          has no equivalent.
        * **Output audio chunking and pacing** — Gemini emits raw
          ~40 ms PCM frames; `_enqueue_output_audio` buffers them
          into pacer-aligned chunks matching
          `IngressStreamer.chunk_duration_ms` so each paced tick
          carries exactly one chunk's worth of audio. ElevenLabs
          streams pacer-aligned audio directly.
        * **Two-path termination orchestration** — bridge owns both
          drain pipelines: `_wait_for_quiescence_and_emit` for the
          handoff path (`transfer_to_agent` toolCall) and
          `_wait_for_quiescence_and_emit_session_end` for the
          self-service-complete path (`end_session` toolCall). On
          ElevenLabs, the handoff drain is bridge-side but
          self-service-complete is platform-side (the EL `end_call`
          system tool with `pre_tool_speech="force"`); Gemini has
          no system tools, so the bridge owns both.
        * **Per-turn transcript accumulation** — Gemini Live audio
          mode emits `inputTranscription` / `outputTranscription`
          as additive partials with no per-message finality flag
          (`finished` is always false in practice). The bridge
          accumulates per turn in `BotConversation.pending_*_text`
          and flushes a single `bot.feature` TRANSCRIPT per
          speaker on `turnComplete` / `generationComplete`.
        * **Asymmetric audio rates** — Gemini Live takes 16 kHz
          S16LE PCM on input and emits 24 kHz S16LE PCM on output.
          Unique among the four AI providers (ElevenLabs is
          symmetric 16 kHz; OpenAI / xAI use 8 kHz µ-law). The
          asymmetry is reflected in `_prepare_input_audio` and
          `_transcode_output_audio`.

    The bridge does not run any LLM or speech recognition logic on
    its own; those execute on the Gemini side. But everything in the
    list above is bridge-implemented and would have to be reimplemented
    by a partner who swapped in a different raw-model provider.

Does not own:
    Provider routing by botId (owned by the bot dispatcher in
        bot_service.py — this plugin is registered against the
        `gemini:` prefix and invoked through handle_message after
        the dispatcher has matched).
    RCMS session lifecycle, JWT authentication, and Infinity-side
        WebSocket transport (owned by bridge_server.py).
    Audio frame pacing and ingress queue management (owned by
        IngressStreamer in bridge_server.py — this plugin queues
        chunks via _send_ingress_chunked and otherwise stays out of
        the cadence path).
    Speech recognition, language model inference, voice synthesis,
        and turn detection (owned by Gemini Live; surfaced to the
        bridge via the WebSocket protocol's `serverContent` /
        `toolCall` / `setupComplete` / `goAway` messages).

Dependencies:
    websockets: the upstream Gemini Live WebSocket client.
    audioop (stdlib; on Python 3.13+ install audioop-lts as a
        drop-in): µ-law / A-law encode/decode and resampling
        between Infinity codec rates and Gemini's 16/24 kHz.
    G722 (optional): wideband codec encode/decode; gated by
        bridge_server.G722_AVAILABLE — if absent, G722-negotiated
        sessions are rejected at bot.start time with
        BACKEND_START_FAILED.
    truststore (optional): used to build outbound TLS contexts off
        the OS trust store so corporate TLS-interception roots
        (e.g. Zscaler) are honored. Falls back to the Python CA
        bundle if not installed.
    bridge_server.ServicePlugin: base class establishing the plugin
        contract (name, message_types, handle_message,
        on_session_ended, shutdown).
    bridge_server.BridgeServer: provides ingress_streamer (paced
        delivery to Infinity), get_next_sequence (per-client
        outbound sequence counter), session_config (codec / sample
        rate negotiation results), and the
        send_bot_ended_with_*_context helper family that emits the
        spec-correct success / failure / disconnect shapes.

RCMS lifecycle:
    Phase 1 (Start): handle_message routes bot.start to
        _handle_bot_start, which validates the botId prefix
        (`gemini:`), resolves the model id (env-var override or
        suffix after the prefix), reads `GEMINI_API_KEY`, negotiates
        codec / sample rate, composes the system prompt, builds a
        BotConversation, connects upstream via _connect_gemini
        (the URL carries `?key=` for auth — there is no API-key
        header on the WS handshake), launches _gemini_recv_loop as
        a background task, and emits bot.started on success. Every
        validation failure emits bot.ended with a non-200 status
        via the failure-context helper.
    Phase 2 (During): ingest_audio_chunk transcodes Infinity-side
        audio to 16 kHz S16LE and forwards as
        `realtimeInput.audio.data`. _gemini_recv_loop drains the
        upstream WS and dispatches each message to
        _handle_gemini_message, whose seven branches handle the
        Gemini Live protocol (`goAway`, `serverContent` carrying
        modelTurn parts plus inputTranscription /
        outputTranscription / control flags, `toolCall`,
        `setupComplete`). Audio frames are transcoded and fed
        through _enqueue_output_audio's accumulator into
        pacer-aligned chunks; transcripts accumulate per turn and
        flush on turnComplete / generationComplete.
    Phase 3 (Closure): five termination shapes converge on this
        plugin —
            (a) caller-disconnect → on_session_ended emits
                CALLER_DISCONNECTED via the disconnect-context
                helper;
            (b) Infinity-driven bot.end → _handle_bot_end acks
                with a manual bot.ended build;
            (c) `transfer_to_agent` toolCall → _handle_tool_call
                stashes the LIVE_AGENT_HANDOFF and launches the
                handoff drain; _emit_pending_handoff fires the
                bot.feature plus a manual bot.ended (absent-
                status shape) after the audio queue drains;
            (d) `end_session` toolCall → _handle_tool_call stashes
                a session-end latch and launches the
                session-end drain; _emit_session_end_complete
                fires success-context bot.ended after the audio
                queue drains;
            (e) `goAway` from Gemini (15-min session cap or
                similar) → bot.feature PROVIDER_GOAWAY, convo
                marked inactive; the actual bot.end will arrive
                from Infinity when it decides to terminate.
        _shutdown_conversation is the single resource-release path
        invoked from every removal site.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start / bot.started
        / bot.end / bot.ended / bot.feature (TRANSCRIPT,
        LIVE_AGENT_HANDOFF, PROVIDER_GOAWAY ftypes).
    RCMS spec §Error Handling — bot.start failures answered via
        bot.ended-with-status; only schema-impossible cases
        (missing endpointId) escape via session.error.
    RCMS spec §Status Codes — 400 / 501 / 503 codes consumed by the
        failure-context helper; CALLER_DISCONNECTED reason value on
        the disconnect-context helper.
    RCMS spec §Media Encoding Options — base64 and binary transports.
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire
    shape and bridge/schema/rcms.schema.md for behavioral notes —
    including bot.ended's payload.context.status nesting, the
    workflow's byobotEndContext / byobotLiveAgentHandoff consumption
    patterns, and the bridge-added PROVIDER_GOAWAY ftype.

    Gemini Live BidiGenerateContent protocol — message catalog
        (`setup`, `setupComplete`, `realtimeInput`, `serverContent`,
        `toolCall`, `toolResponse`, `goAway`), additive transcript
        partials with no finality flag, and the asymmetric 16/24 kHz
        audio framing.

See also:
    BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload field reference
    BUILDERS_GUIDE.md §3 Phase 2 — audio frames and IngressStreamer
    BUILDERS_GUIDE.md §3 Phase 3 — bot.ended status semantics and
        the termination ladder
"""

from __future__ import annotations

import asyncio
import audioop  # stdlib; on Python 3.13+ install audioop-lts as a drop-in replacement
import base64
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any, Dict, Optional

import ssl as _ssl

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

GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
GEMINI_INPUT_RATE = 16000   # Gemini Live expects 16 kHz S16LE PCM on input
GEMINI_OUTPUT_RATE = 24000  # Gemini Live emits 24 kHz S16LE PCM on output
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_SYSTEM_PROMPT = "You are a helpful customer service agent."

# Bound on the pre-ingress-ready buffer (see BotConversation.ingress_buffer
# and the gate in _handle_gemini_message). Infinity's ingress path opens
# ~150 ms after Gemini starts the greeting; the buffer absorbs that gap.
# Cap is conservative — exceeding it indicates an upstream stall, not normal
# greeting overlap. Oldest chunk is dropped on overflow.
_INGRESS_BUFFER_MAX_CHUNKS = 50

# Polling cadence for the two drain loops in _wait_for_quiescence_and_emit
# and _wait_for_quiescence_and_emit_session_end. Each iteration sleeps this
# long, then checks whether the IngressStreamer queue has gone empty. The
# sleep itself is the grace period — long enough for any chunk crossing the
# network during the iteration to land in the queue before the empty check.
_QUIESCENCE_POLL_MS = 250

# Safety net on both drain loops. Trips only on genuine upstream failure
# (e.g. Gemini WS hung); a healthy drain completes in well under a second.
# Trip logs at WARNING and emits the terminal envelope (LIVE_AGENT_HANDOFF
# or success-context bot.ended) anyway so the workflow does not stall.
_QUIESCENCE_DEADLOCK_SAFETY_S = 30.0

# Maximum WebSocket frame size for the upstream Gemini connection. 8 MiB
# accommodates the largest audio frames Gemini emits without triggering
# websockets.exceptions.PayloadTooBig on long-form responses.
_WS_MAX_FRAME_BYTES = 2**23

@dataclass
class BotConversation:
    """Per-call state for one Infinity-side endpoint bridged to one Gemini Live session.

    Allocated in `_handle_bot_start` and stored in
    `GeminiService._conversations` under `(session_id, endpoint_id)`
    for the lifetime of the call. Removed by `_handle_bot_end`
    (Infinity-driven teardown) or `on_session_ended` (caller
    disconnect / platform-driven session end).
    `_shutdown_conversation` is the single resource-release path;
    every removal site calls it.

    Field groups:

        * **Identity (set at construction, never mutated):**
          `session_id`, `endpoint_id`, `source` (rx / tx),
          `websocket` (the Infinity-side WS), `client_id`, `service`,
          `model` (resolved from botId suffix or `GEMINI_MODEL` env),
          `system_prompt` (composed by `_load_base_prompt` +
          `_compose_system_prompt`), `language_code`, `codec_name`,
          `sample_rate`, `transport_encoding`.

        * **Upstream WebSocket:** `gm_ws` (the
          `websockets.WebSocketClientProtocol` connected to Gemini),
          `gm_recv_task` (the background coroutine draining `gm_ws`),
          `active` (cooperative shutdown flag consulted at the head
          of each recv-loop iteration; also flipped to False on
          Gemini `goAway`).

        * **Termination tracking:** `bot_ended_sent` is flipped True
          by every code path that emits a terminal `bot.ended`
          (`send_bot_ended_with_*_context` helpers and the manual
          builds in `_handle_bot_end` / `_emit_pending_handoff`).
          Read by `on_session_ended` and the helpers themselves to
          suppress duplicate emissions when an outcome has already
          been signalled.

        * **Audio codec state:** `ratecv_in_state` /
          `ratecv_out_state` thread `audioop.ratecv` calls (the
          stdlib resampler returns a fresh state per call and the
          two directions cannot share). `g722_decoder` /
          `g722_encoder` are lazily initialised when G722 is
          negotiated. Note that the input/output rates are
          **asymmetric** for Gemini — 16 kHz in, 24 kHz out — so
          `ratecv_out_state` resamples from 24 kHz, not 16 kHz.

        * **Ingress readiness latch:** `ingress_ready` and
          `ingress_buffer` solve the timing skew where Gemini
          starts streaming the greeting before Infinity's ingress
          path opens. Audio that lands before the latch flips is
          buffered (capped at `_INGRESS_BUFFER_MAX_CHUNKS`); the
          latch flips on the first call to `ingest_audio_chunk`
          and flushes the buffer.

        * **Deferred live-agent handoff:** `pending_handoff` stashes
          the `bot.feature LIVE_AGENT_HANDOFF` payload while the
          IngressStreamer queue drains; `handoff_task` is the
          drain coroutine running `_wait_for_quiescence_and_emit`.
          The deferred path exists because Gemini's `toolCall`
          arrives *before* the goodbye-line audio finishes
          streaming; emitting LIVE_AGENT_HANDOFF immediately
          would have Infinity tear down playback mid-utterance.

        * **Deferred self-service-complete:** `pending_session_end`
          is a boolean latch (not a stashed payload — the
          success-context `bot.ended` carries no per-call payload,
          only the static success status). Set True on the
          `end_session` toolCall; `session_end_task` runs
          `_wait_for_quiescence_and_emit_session_end` until the
          audio queue drains, then `_emit_session_end_complete`
          fires the success-context emit. Sibling drain to the
          handoff path; same drain logic, different terminal
          envelope.

        * **Output audio accumulator:** `ingress_accumulator` and
          `ingress_chunk_size`. Gemini emits raw ~40 ms PCM frames,
          but the IngressStreamer paces at `chunk_duration_ms` per
          chunk regardless of frame content duration. The
          accumulator buffers until a full pacer-aligned flush is
          available; misalignment between this boundary and the
          streamer's pacing interval would split each flush into
          mismatched chunks paced uniformly, producing sub-real-time
          delivery and buffer underruns at Infinity. Coupled to
          `IngressStreamer.chunk_duration_ms` via `_chunk_size_for`.

        * **Per-turn transcript accumulators:** `pending_bot_text`
          and `pending_customer_text`. Gemini Live audio mode emits
          `inputTranscription` / `outputTranscription` as additive
          partials with no per-message finality flag (`finished` is
          always False in practice), so the bridge concatenates per
          turn and flushes a single `bot.feature` TRANSCRIPT per
          speaker on `turnComplete` / `generationComplete`. Reset
          to empty after each flush.

        * **Turn-start timestamps:** `bot_turn_started_at` and
          `customer_turn_started_at` carry the moment each
          speaker's turn began (captured when the corresponding
          accumulator transitions from empty → non-empty on the
          first chunk of the turn). Used as
          `payload.transcript.startTsMs`. Capturing turn-start at
          accumulator-fill time rather than flush time matters
          because the turn-end flush emits BOT first and CUSTOMER
          second from the same turn boundary; flush-time
          timestamps would either tie or invert their actual
          chronological ordering on the wire and in Infinity's call record.
          Reset to None alongside the accumulators after each
          flush.
    """

    session_id: str
    endpoint_id: str
    source: str
    websocket: WebSocketServerProtocol
    client_id: str
    service: str
    model: str
    system_prompt: str
    language_code: str
    codec_name: str
    sample_rate: int
    transport_encoding: str
    gm_ws: Optional[Any] = None
    gm_recv_task: Optional[asyncio.Task] = None
    active: bool = False
    # Set True by send_bot_ended_with_*_context helpers after a successful
    # bot.ended emission (success / failure / disconnect). Read by
    # on_session_ended and the helpers themselves to suppress duplicate
    # disconnect emissions when self-service-complete, handoff, or failure
    # already signalled the outcome.
    bot_ended_sent: bool = False
    # Resampling state — audioop.ratecv returns a new tuple each call;
    # never share between in and out (rates differ: 16 kHz in / 24 kHz out).
    ratecv_in_state: Any = None
    ratecv_out_state: Any = None
    # G722 codec objects (lazy-initialised; 16 kHz internal rate).
    g722_decoder: Any = None
    g722_encoder: Any = None
    # Ingress readiness latch — see class docstring "Ingress readiness latch"
    # group. Flipped True on the first ingest_audio_chunk; ingress_buffer
    # holds Gemini greeting chunks that landed before Infinity's ingress
    # path opened.
    ingress_ready: bool = False
    ingress_buffer: list = field(default_factory=list)
    # Deferred LIVE_AGENT_HANDOFF — see class docstring "Deferred live-agent
    # handoff" group. Stashed on transfer_to_agent toolCall; emitted by
    # _emit_pending_handoff after the IngressStreamer queue drains.
    pending_handoff: Optional[Dict[str, Any]] = None
    handoff_task: Optional[asyncio.Task] = None
    # Deferred self-service-complete — see class docstring "Deferred
    # self-service-complete" group. Boolean latch (no stashed payload);
    # _emit_session_end_complete fires the success-context bot.ended.
    pending_session_end: bool = False
    session_end_task: Optional[asyncio.Task] = None
    # Output audio accumulator — see class docstring "Output audio
    # accumulator" group. Coupled to IngressStreamer.chunk_duration_ms
    # via _chunk_size_for so each flush carries exactly one paced chunk.
    ingress_accumulator: bytearray = field(default_factory=bytearray)
    ingress_chunk_size: int = 0
    # Per-turn transcript accumulators — see class docstring "Per-turn
    # transcript accumulators" group. Concatenated additive partials;
    # flushed on turnComplete / generationComplete.
    pending_bot_text: str = ""
    pending_customer_text: str = ""
    # Turn-start timestamps — see class docstring "Turn-start timestamps"
    # group. Captured at accumulator-fill time, used as
    # payload.transcript.startTsMs on the eventual TRANSCRIPT emit.
    bot_turn_started_at: Optional[int] = None
    customer_turn_started_at: Optional[int] = None


class GeminiService(ServicePlugin):
    """Service plugin that proxies an Infinity RCMS bot session to a Gemini Live BidiGenerateContent session.

    Plugin contract:
        Subclass of `ServicePlugin`. Discovered by the plugin loader
        at bridge startup if `is_configured()` returns True
        (`GEMINI_API_KEY` set). Registered against the bridge's
        `ServiceRegistry` under the name `gemini`. The bot
        dispatcher (`bot_service.py`) claims the RCMS `bot.start` /
        `bot.end` message types and routes per-call to this plugin
        based on the `gemini:<model>` botId prefix; the plugin
        itself reports an empty `message_types` set.

    Raw-model framing:
        Gemini Live exposes the model directly — there is no hosted
        agent surface (no dashboard-configured prompt, no platform-
        side tools, no first-message field). The bridge owns
        every piece of orchestration that ElevenLabs would have
        owned platform-side:

            * **System prompt** — `_load_base_prompt` resolves a
              4-tier chain (file env var → inline env var →
              sibling `system_prompt.md` → built-in default);
              `_compose_system_prompt` interpolates
              `{{variable}}` placeholders against
              `bot.start.payload.context` and appends a
              call-context line.
            * **Tool schemas** — `transfer_to_agent` and
              `end_session` declared inline in
              `_connect_gemini`'s `setup` payload. Both are
              bridge-implemented: there are no Gemini-platform
              system tools.
            * **Conversation initiation** — bridge sends an
              explicit `realtimeInput.text = "Hello, please
              greet the customer now"` after `setupComplete`;
              without this, Gemini stays silent until the caller
              speaks first.
            * **Output pacing** — Gemini emits raw ~40 ms PCM
              frames; the bridge buffers them into pacer-aligned
              chunks via `_enqueue_output_audio` so the
              IngressStreamer's `chunk_duration_ms` cadence is
              honored.
            * **Termination drains** — both `transfer_to_agent`
              and `end_session` have their own bridge-side drain
              pipelines (`_wait_for_quiescence_and_emit` and
              `_wait_for_quiescence_and_emit_session_end`) that
              wait for the IngressStreamer queue to empty before
              emitting the terminal RCMS envelope, so the goodbye
              line plays out before Infinity tears down playback.

        See the module docstring "Role" section for the full
        ownership-boundary discussion against ElevenLabs.

    Per-call state:
        `self._conversations` maps `(session_id, endpoint_id)` to
        a `BotConversation` instance for the lifetime of each call.
        Populated by `_handle_bot_start`, removed by
        `_handle_bot_end` and `on_session_ended`. The instance
        carries the upstream Gemini WebSocket, the recv-loop task,
        codec state, the deferred-handoff and deferred-session-end
        latches, the ingress-readiness latch, the output
        accumulator, the per-turn transcript accumulators, and the
        turn-start timestamps (see `BotConversation`).

    Lifecycle hooks:
        * `handle_message` — `bot.start` / `bot.end` dispatch.
        * `ingest_audio_chunk` — per-frame caller audio handoff
          to Gemini.
        * `on_session_ended` — caller-disconnect /
          platform-initiated session end.
        * `shutdown` — bridge process shutdown.

    Spec:
        RCMS spec §AI Bot Message Definitions — `bot.start` /
        `bot.started` / `bot.end` / `bot.ended` / `bot.feature`
        (TRANSCRIPT, LIVE_AGENT_HANDOFF, PROVIDER_GOAWAY ftypes).
        Gemini Live BidiGenerateContent protocol — `setup` /
        `setupComplete` / `realtimeInput` / `serverContent` /
        `toolCall` / `toolResponse` / `goAway`. URL-query API key
        auth (no header).
    """

    name = "gemini"

    @classmethod
    def is_configured(cls) -> bool:
        """Return True iff `GEMINI_API_KEY` is set in the bridge process environment.

        Consulted by the plugin loader at bridge startup. When
        False, the plugin is skipped — `bot.start` envelopes
        carrying a `gemini:` `botId` will then surface as
        `BACKEND_START_FAILED` from the dispatcher because no
        plugin claims the prefix.

        Unlike ElevenLabs, Gemini does not support per-call
        `botCredentials` — Gemini Live's authentication is
        URL-query-parameter on the WebSocket handshake, applied
        once at connection time from `GEMINI_API_KEY`. There is
        only one source of credentials.
        """
        return bool(os.environ.get("GEMINI_API_KEY", "").strip())

    def __init__(self, server):
        """Initialise per-plugin state used across the call lifecycle.

        Sets up the per-(session_id, endpoint_id) conversation
        map. Keys follow the `_key` convention; values are
        `BotConversation` instances, populated by
        `_handle_bot_start` and removed by `_handle_bot_end` /
        `_shutdown_conversation`.

        Args:
            server: The owning `BridgeServer`. Stored on
                `self.server` by the `ServicePlugin` base.
        """
        super().__init__(server)
        self._conversations: Dict[str, BotConversation] = {}

    @property
    def message_types(self) -> set[str]:
        """Return the empty set — this plugin does not register any RCMS message types directly.

        The bot dispatcher
        (`bot_service.py:CombinedBotService`) claims `bot.start`
        / `bot.end` with the `ServiceRegistry` and routes to
        provider plugins by `botId` prefix. This plugin is
        invoked through `handle_message` only after that
        prefix-match has already happened.
        """
        return set()

    def _key(self, session_id: str, endpoint_id: str) -> str:
        """Build the `(session_id, endpoint_id)` lookup key for `self._conversations`.

        A session can host multiple endpoints concurrently (one
        Infinity session, multiple media legs), so the
        composite key is required to keep per-endpoint state
        distinct.
        """
        return f"{session_id}:{endpoint_id}"

    def _resolve_codec(self, session_id: str) -> str:
        """Return the negotiated codec name for `session_id`, or `"L16"` if no negotiation has stored one.

        Reads `BridgeServer.session_config[session_id]["codec_name"]`,
        which is populated during `session.start` codec
        negotiation in `BridgeServer.handle_session_start`.
        Caller upper-cases the value before comparing.
        """
        return self.server.session_config.get(session_id, {}).get("codec_name", "L16")

    def _resolve_sample_rate(self, session_id: str, payload: Dict[str, Any]) -> int:
        """Return the negotiated sample rate, preferring `payload.sampleRate`, then session config, then 8000.

        Resolution order matches the precedence of explicit
        per-call override (`payload.sampleRate` from
        `bot.start`) over session-wide negotiation
        (`session_config[session_id]["sample_rate"]`) over the
        codec-agnostic default. The final value flows into
        `BotConversation.sample_rate` and is read by the audio
        transcoders to drive resampling between Gemini's
        16/24 kHz rates and the Infinity-side codec.
        """
        stored = self.server.session_config.get(session_id, {})
        return payload.get("sampleRate", stored.get("sample_rate", 8000))

    async def handle_message(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Plugin-entry RCMS message dispatcher: route `bot.start` / `bot.end` to the matching handler.

        Contract:
            Invoked directly by the bot dispatcher
            (`bot_service.py:_handle_bot_start` and
            `_handle_bot_end`) after it has matched the inbound
            `botId` prefix (`gemini:`) against the registered
            providers and selected this plugin. The dispatcher
            hands off the raw envelope; this method switches on
            `data["type"]` and forwards.

            Only `bot.start` and `bot.end` are expected — the
            dispatcher's prefix-routing contract guarantees no
            other type ever lands here. Anything else logs at
            WARNING and is dropped (defensive — a future
            dispatcher change must not silently misroute frames).

        Args:
            websocket: The Infinity-side WebSocket the message
                arrived on; passed through to the matching
                handler.
            client_id: Connection identifier for log lines and
                the outbound sequence counter.
            data: The decoded RCMS envelope.

        Returns:
            None. All effects happen inside `_handle_bot_start`
            / `_handle_bot_end`.
        """
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_bot_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_bot_end(websocket, client_id, data)
        else:
            logger.warning("[%s] Unhandled message '%s' in Gemini service", client_id, msg_type)

    async def on_session_ended(self, session_id: str) -> None:
        """Phase 3 hook — emit `bot.ended` (CALLER_DISCONNECTED) for any active Gemini conversation under this session.

        Contract:
            Plugin lifecycle hook fired by `BridgeServer` when an
            inbound `session.end` arrives on the Infinity
            WebSocket (caller disconnect or platform-initiated
            session close). Walks `self._conversations` for
            every key prefixed with `f"{session_id}:"` — there
            can be multiple endpoints under one session — and
            for each conversation:

                1. If `bot_ended_sent` is False (no termination
                   signal has gone out yet), emit `bot.ended`
                   with `payload.context.status` carrying
                   `CALLER_DISCONNECTED` via
                   `send_bot_ended_with_disconnect_context`.
                   Without this, the workflow's IVA module
                   records an error ("session ended with no
                   bot.ended on an active bot session"). The
                   helper sets `bot_ended_sent` itself so
                   subsequent code paths see the flag flipped.
                2. Call `_shutdown_conversation` to release the
                   per-conversation resources (handoff drain,
                   session-end drain, IngressStreamer queue,
                   recv loop, Gemini WS).

            **Why this method emits and `_shutdown_conversation`
            does not.** `_shutdown_conversation` is also called
            from `_handle_bot_end`, whose own `bot.ended` ack
            is a separate emission with a different payload
            shape — placing the disconnect emit inside the
            shared shutdown path would cause `_handle_bot_end`
            to send two `bot.ended` envelopes tagged
            differently. Keeping the disconnect emit here means
            it only fires on the caller-disconnect path.

        Spec:
            RCMS spec §Session Message Definitions —
            `session.end`.
            RCMS spec §AI Bot Message Definitions — `bot.ended`
            with `CALLER_DISCONNECTED` reason. See
            `bridge/schema/rcms.schema.md` "bot.ended — status
            is nested in context".

        Args:
            session_id: The RCMS session identifier whose
                `session.end` triggered this hook.

        Returns:
            None. Side effects: zero or more outbound
            `bot.ended`-with-disconnect-context envelopes plus
            `_shutdown_conversation` cleanup per matching
            conversation.
        """
        keys = [k for k in self._conversations if k.startswith(f"{session_id}:")]
        for key in keys:
            convo = self._conversations.pop(key, None)
            if convo:
                # Emit bot.ended with CALLER_DISCONNECTED if no termination
                # signal has been sent yet on this conversation — see method
                # docstring step 1.
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
        """Tear down every active Gemini conversation — called on bridge process shutdown.

        Contract:
            ServicePlugin shutdown hook. Iterates a snapshot of
            `self._conversations.keys()` (snapshot because
            `_shutdown_conversation` mutates the dict) and calls
            `_shutdown_conversation` on each entry. Does NOT emit
            any outbound `bot.ended` — process shutdown is its
            own teardown shape, distinct from the per-call
            termination contract: Infinity sees the WebSocket
            close at the transport layer and reconciles via its
            own session timeout path.

            Idempotent and exception-tolerant: each
            conversation's cleanup path swallows its own errors,
            so this method always completes.

        Returns:
            None. Effects are confined to closing per-
            conversation resources: handoff drain task cancelled,
            session-end drain task cancelled, IngressStreamer
            queues drained, Gemini recv loops cancelled, Gemini
            WebSockets closed.
        """
        for key in list(self._conversations.keys()):
            convo = self._conversations.pop(key, None)
            if convo:
                await self._shutdown_conversation(convo)

    # ------------------------------------------------------------------ bot.start

    async def _handle_bot_start(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Handle Infinity-driven `bot.start` for a Gemini session: validate, compose prompt, connect, ack.

        Contract:
            Phase 1 entry point dispatched by `handle_message` when the
            inbound message type is `bot.start` and the dispatcher has
            routed by `botId` prefix. Performs the full start-of-call
            sequence:

                1. **Validate `endpointId`**: missing endpointId fails
                   the `bot.ended` schema, so respond with
                   `session.error` (`MISSING_REQUIRED_FIELDS`, 501)
                   instead. The only `session.error` exit on this
                   path; all subsequent failures use
                   `bot.ended`-with-status.
                2. **Validate `botId` prefix**: must start with
                   `gemini:` (case-insensitive). Failures emit
                   `bot.ended` with `BAD_REQUEST` (400) /
                   `UNRECOGNIZED_BOTID_PREFIX` via the
                   failure-context helper.
                3. **Resolve model id**: prefer the `GEMINI_MODEL`
                   environment variable (deployment override);
                   otherwise read the suffix after `gemini:` from the
                   botId; fall back to `DEFAULT_GEMINI_MODEL`.
                4. **Resolve API key**: read `GEMINI_API_KEY` from the
                   environment. Unlike ElevenLabs, this provider does
                   not support per-call `botCredentials` — Gemini's
                   WebSocket auth is via `?key=` query parameter on
                   the URL, applied at connection time. Missing key →
                   503 `BACKEND_START_FAILED`.
                5. **Resolve codec / sample rate**: from
                   `session_config[session_id]` and the inbound
                   payload. Reject unsupported codecs (anything
                   outside PCMU / L16 / PCMA / G722) with 503
                   `BACKEND_START_FAILED`. Reject G722 negotiation
                   when the optional G722 package is not installed,
                   with the same status.
                6. **Compose system prompt**: `_load_base_prompt`
                   resolves a 4-tier source chain;
                   `_compose_system_prompt` performs `{{variable}}`
                   interpolation against `payload.context` and
                   appends a call-context line. The composed prompt
                   is per-call and stored on the `BotConversation`.
                7. **Build `BotConversation`** from negotiated codec,
                   transport encoding from
                   `server.transport_encodings`, and the composed
                   system prompt.
                8. **G722 decoder lazy-init** when negotiated:
                   instantiate the decoder up front; the encoder is
                   created on demand by `_transcode_output_audio`.
                   Decoder init failure → 503
                   `BACKEND_START_FAILED`.
                9. **Connect to Gemini** via `_connect_gemini`. The
                   helper sends the `setup` frame including
                   `system_instruction`, tool declarations, voice
                   config, and `output_audio_transcription` /
                   `input_audio_transcription` toggles. Failure here
                   → 503 `BACKEND_START_FAILED`.
               10. **Replace any pre-existing conversation** under
                   the same `(session_id, endpoint_id)` by calling
                   `_shutdown_conversation` on the prior entry —
                   protects against duplicate `bot.start` racing
                   the prior session's teardown.
               11. **Activate**: set `convo.active = True`, store in
                   `self._conversations`, and start
                   `_gemini_recv_loop` as a background task.
               12. **Emit `bot.started`** ack on the Infinity side —
                   manual envelope build (the helper family is for
                   `bot.ended`).

            Every error path uses
            `send_bot_ended_with_failure_context` with the
            appropriate RCMS status code, so the workflow's
            `byobotEndContext` consumption pattern routes the bot
            session to a non-200 terminal state. Per the RCMS spec,
            an unprocessable `bot.start` is failed via
            `bot.ended`-with-status, *not* `session.error`; the only
            `session.error` exit is the schema-impossible
            `MISSING_REQUIRED_FIELDS: endpointId` case in step 1.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.start`
            payload fields and `bot.started` ack shape.
            RCMS spec §Error Handling — failure-shape contract for
            unprocessable `bot.start` answered via
            `bot.ended`-with-status.
            RCMS spec §Status Codes — 400 / 501 / 503 codes
            consumed by the failure helpers.
            See `bridge/schema/rcms.schema.md` "bot.ended — status
            is nested in context".

        Args:
            websocket: The Infinity-side WebSocket; the
                `bot.started` ack and any failure `bot.ended` are
                sent here.
            client_id: Connection identifier used in log lines and
                the outbound sequence counter.
            data: Decoded `bot.start` envelope. `sessionId`,
                `service`, `payload.endpointId`, `payload.botId`,
                `payload.source`, `payload.language`,
                `payload.context`, `payload.to`, `payload.from`,
                `payload.ucid`, `payload.direction`, and
                `payload.sampleRate` are read.

        Returns:
            None. Side effects: optional `session.error` /
            `bot.ended`-with-failure send on validation failure;
            `BotConversation` registered in `self._conversations`;
            background Gemini recv loop launched; `bot.started`
            ack sent on success.
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

        if not bot_id.lower().startswith("gemini:"):
            # RCMS §Error Handling: an unprocessable bot.start is failed via
            # bot.ended-with-status, not session.end. send_bot_ended_with_failure_context
            # emits the spec-compliant shape; the IVA module's FAILED branch wires
            # to bot.ended-with-non-200-status.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=400,
                reason="BAD_REQUEST",
                description=f"UNRECOGNIZED_BOTID_PREFIX: Gemini plugin requires botId prefix 'gemini:', got '{bot_id}'",
            )
            return
        env_model = os.environ.get("GEMINI_MODEL", "").strip()
        if env_model:
            model = env_model
        else:
            model = bot_id[len("gemini:"):].strip() or DEFAULT_GEMINI_MODEL

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            # RCMS §Error Handling — see canonical comment at line 220 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: GEMINI_API_KEY is not set on the bridge server",
            )
            return

        codec_name = self._resolve_codec(session_id).upper()
        sample_rate = self._resolve_sample_rate(session_id, payload)

        if codec_name not in ("PCMU", "L16", "PCMA", "G722"):
            # RCMS §Error Handling — see canonical comment at line 220 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: Unsupported codec '{codec_name}' for Gemini provider",
            )
            return

        if codec_name == "G722" and not G722_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 220 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: Codec G722 negotiated but g722 package is not installed on the bridge server",
            )
            return

        language_code = payload.get("language") or "en-US"
        base_prompt = self._load_base_prompt()
        system_prompt = self._compose_system_prompt(base_prompt, payload, language_code)

        logger.info(
            "[%s] Gemini bot.start session=%s endpoint=%s model=%s codec=%s/%d source=%s",
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
            system_prompt=system_prompt,
            language_code=language_code,
            codec_name=codec_name,
            sample_rate=sample_rate,
            transport_encoding=self.server.transport_encodings.get(session_id, "base64"),
        )

        if codec_name == "G722" and G722_AVAILABLE:
            try:
                import G722 as g722  # type: ignore
                convo.g722_decoder = g722.G722(sample_rate=16000, bit_rate=64000)
            except Exception as exc:
                logger.error("[%s] Failed to initialise G722 decoder: %s", client_id, exc)
                # RCMS §Error Handling — see canonical comment at line 220 above.
                await self.server.send_bot_ended_with_failure_context(
                    convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                    code=503,
                    reason="SERVICE_UNAVAILABLE",
                    description=f"BACKEND_START_FAILED: G722 decoder init failed: {exc}",
                    convo=convo,
                )
                return

        try:
            await self._connect_gemini(convo, api_key)
        except Exception as exc:
            logger.error("[%s] Failed to connect to Gemini: %s", client_id, exc, exc_info=True)
            # RCMS §Error Handling — see canonical comment at line 220 above.
            await self.server.send_bot_ended_with_failure_context(
                convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: Gemini connect failed: {exc}",
                convo=convo,
            )
            return

        key = self._key(session_id, endpoint_id)
        existing = self._conversations.pop(key, None)
        if existing:
            await self._shutdown_conversation(existing)
        convo.active = True
        self._conversations[key] = convo
        convo.gm_recv_task = asyncio.create_task(self._gemini_recv_loop(convo))

        response = {
            "version": "1.0.0",
            "type": "bot.started",
            "sessionId": session_id,
            "sequenceNum": self.server.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": service,
            "payload": {"endpointId": endpoint_id},
        }
        logger.info("[%s] OUTBOUND JSON (bot.started): %s", client_id, format_compact_json(response))
        log_message_exchange("OUTBOUND", client_id, "bot.started", response, is_media=False)
        await websocket.send(json.dumps(response))

    @staticmethod
    def _load_base_prompt() -> str:
        """Resolve the base system prompt from a 4-tier precedence chain.

        Contract:
            Walks the precedence chain top-down and returns the
            first source that yields a non-empty string after
            stripping. Used by `_handle_bot_start` (via
            `_compose_system_prompt`) once per call.

            Precedence order:

                1. **`GEMINI_SYSTEM_PROMPT_FILE` env var** —
                   explicit file path (deploy override; e.g.
                   the VM's
                   `/opt/bridge-server/gemini_system_prompt.md`).
                   Read failure or empty contents falls through.
                2. **`GEMINI_SYSTEM_PROMPT` env var** — inline
                   single-line override (whitespace-only is
                   treated as absent).
                3. **Repo-tracked sibling `system_prompt.md`** —
                   the canonical default that ships with the
                   bridge, located next to this module's source
                   file. Read failure or empty contents falls
                   through.
                4. **Built-in `DEFAULT_SYSTEM_PROMPT`** — minimal
                   last-resort fallback.

            File reads use UTF-8 and warn (not error) on `OSError`
            so a misconfigured path on one tier does not block
            the lower tiers. `_compose_system_prompt` performs
            `{{variable}}` interpolation on the returned string;
            this function does no interpolation.

        Returns:
            The resolved base prompt string. Always non-empty —
            the built-in fallback is never empty.
        """
        prompt_file = os.environ.get("GEMINI_SYSTEM_PROMPT_FILE", "").strip()
        if prompt_file:
            try:
                with open(prompt_file, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return text
                logger.warning("GEMINI_SYSTEM_PROMPT_FILE %s is empty; falling back", prompt_file)
            except OSError as exc:
                logger.warning("Could not read GEMINI_SYSTEM_PROMPT_FILE %s: %s", prompt_file, exc)

        inline = os.environ.get("GEMINI_SYSTEM_PROMPT", "").strip()
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

        return DEFAULT_SYSTEM_PROMPT

    @staticmethod
    def _resolve_language_name(code: str) -> str:
        """Map a BCP-47 / ISO 639-1 language code to a human-readable language name.

        Contract:
            Used by `_compose_system_prompt` so `{{language}}`
            interpolation in the system prompt reads naturally
            ("Respond in English") rather than as a code
            ("Respond in en-US"). The mapping is lower-cased on
            the base subtag (`"en-US"` → `"en"` → `"English"`).
            Unknown codes pass through verbatim — the prompt
            then receives the raw code, which is still
            comprehensible to the model. Empty / missing input
            defaults to `"English"`.

            Sibling implementation to OpenAI's
            `_resolve_language_name`; the two stay in sync by
            convention.

        Args:
            code: BCP-47 or ISO 639-1 language tag (e.g.
                `"en-US"`, `"es"`, `"zh-Hant"`).

        Returns:
            Human-readable language name when the base subtag is
            recognized; the input string otherwise; `"English"`
            on empty / whitespace-only input.
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

    @staticmethod
    def _compose_system_prompt(
        base: str, payload: Dict[str, Any], language: str
    ) -> str:
        """Interpolate `{{variable}}` placeholders against `bot.start.payload.context` and append a call-context line.

        Contract:
            Two-step composition:

                1. **Placeholder interpolation.** Every
                   `{{variable}}` token in `base` is replaced with
                   the matching key from
                   `payload.context` (treated as a `dict`;
                   non-dict context yields an empty interpolation
                   map). Missing keys interpolate as empty
                   string. Whitespace inside the braces is
                   tolerated (`{{ key }}` works the same as
                   `{{key}}`). The `{{language}}` token resolves
                   to the human-readable name from
                   `_resolve_language_name(language)`, not the
                   raw code.
                2. **Call-context line.** A single line is
                   appended: `"Call context: direction=...,
                   from=..., to=..., ucid=..., language=..."`.
                   This gives the model wire-level call metadata
                   regardless of whether the base prompt
                   references those values explicitly.

            **Why language is interpolated rather than left as
            passive context.** Exposing `{{language}}` lets the
            persona prompt act on the deployment's language
            signal as a property (e.g. "Respond exclusively in
            {{language}}") rather than relying on the model to
            infer it from caller speech. The bridge passes the
            BCP-47 code through to Gemini's
            `generation_config.speech_config` separately, so the
            voice synthesis side is also language-aware; the
            prompt-side exposure is the persona-side counterpart.

        Args:
            base: The unrendered prompt template, typically the
                output of `_load_base_prompt`.
            payload: The decoded `bot.start.payload`. `context`,
                `direction`, `from`, `to`, `ucid` are read.
            language: BCP-47 / ISO 639-1 code from
                `payload.language` (defaults to `"en-US"` upstream).

        Returns:
            The interpolated prompt followed by a blank line
            and the call-context line.
        """
        context = payload.get("context") or {}
        if not isinstance(context, dict):
            context = {}

        interpolation = dict(context)
        interpolation["language"] = GeminiService._resolve_language_name(language)

        def _sub(match: "re.Match[str]") -> str:
            key = match.group(1)
            val = interpolation.get(key, "")
            return "" if val is None else str(val)

        interpolated = re.sub(r"\{\{\s*(\w+)\s*\}\}", _sub, base)

        context_line = (
            "Call context: "
            f"direction={payload.get('direction', 'INBOUND')}, "
            f"from={payload.get('from', '')}, "
            f"to={payload.get('to', '')}, "
            f"ucid={payload.get('ucid', '')}, "
            f"language={language}"
        )
        return f"{interpolated}\n\n{context_line}"

    # ------------------------------------------------------------------ bot.end

    async def _handle_bot_end(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Handle Infinity-driven `bot.end` for a Gemini conversation: tear down upstream and ack.

        Contract:
            Inverse of `_handle_bot_start`. Two outcomes:

                * **Missing `endpointId`**: the `bot.ended`
                  schema requires `endpointId`, so the failure
                  cannot be expressed via `bot.ended`; respond
                  with `session.error`
                  (`MISSING_REQUIRED_FIELDS`, status 501)
                  instead. Mirrors the dispatcher's rejection
                  path in `bot_service.py:_handle_bot_start`
                  for the symmetric case on `bot.start`.
                * **Normal teardown**: pop the conversation
                  from `self._conversations` (warn if absent,
                  e.g. the caller already disconnected), call
                  `_shutdown_conversation` to cancel any
                  pending handoff or session-end drain, drain
                  and close the IngressStreamer, cancel the
                  Gemini recv loop, and close the upstream WS.
                  Then send `bot.ended` directly (manual
                  envelope build — this is the bot.end ack
                  shape, with no `payload.context.status`; the
                  `send_bot_ended_with_*_context` helpers all
                  originate a status object and so do not fit
                  this path).

            **Why `bot_ended_sent` is set explicitly here.** The
            `send_bot_ended_with_disconnect_context` helper
            flips this flag as part of its emit; the manual
            build here bypasses the helper, so
            `on_session_ended` (which would otherwise try to
            emit a disconnect-context `bot.ended` on the
            following `session.end`) needs the flag set
            explicitly to avoid a duplicate emission.

            `payload.context` from the inbound `bot.end` is
            preserved on the outbound `bot.ended` ack so any
            caller-specified metadata round-trips back as the
            spec allows.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.ended`
            envelope shape and `payload.endpointId`
            requirement.
            RCMS spec §Error Handling — `session.error` is the
            correct response when an inbound message fails
            schema validation so badly that the ack envelope
            itself cannot be built.
            See `bridge/schema/rcms.schema.md` "bot.ended —
            status is nested in context".

        Args:
            websocket: The Infinity-side WebSocket the inbound
                `bot.end` arrived on; the ack is sent on the
                same connection.
            client_id: Connection identifier used in log lines
                and the outbound sequence counter.
            data: Decoded `bot.end` envelope. `sessionId`,
                `payload.endpointId`, `payload.context`,
                `service`, and `sequenceNum` are read.

        Returns:
            None. Side effects: optional `session.error` send;
            `_shutdown_conversation` for the matching
            conversation; outbound `bot.ended` send;
            `convo.bot_ended_sent` set after the send.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        service = data.get("service", "streaming")
        endpoint_id = payload.get("endpointId") or ""

        if not endpoint_id:
            # Stays on session.error (not bot.ended) per RCMS §Error Handling:
            # bot.ended's BotEndedPayload schema requires endpointId, which is
            # exactly the missing field here. Same schema constraint as the
            # _handle_bot_start A1 site above; mirrors the dispatcher's
            # MISSING_REQUIRED_FIELDS exception in bot_service.py:_handle_bot_start.
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
            logger.warning("[%s] No active Gemini convo for %s:%s", client_id, session_id, endpoint_id)

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
            # Mark flag so on_session_ended does not emit a second bot.ended.
            # The disconnect-context helper guards on bot_ended_sent but this
            # bare-emit path bypasses the helpers — set explicitly here.
            convo.bot_ended_sent = True

    # ------------------------------------------------------------------ audio ingest

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
            `self._conversations`. Returns `False` immediately if
            the conversation is missing, inactive, sourced from
            the wrong direction (`convo.source != source`), or
            has no upstream Gemini WebSocket — the bridge falls
            through to other registered services on `False`.

            **Ingress-ready latch.** The first call to this method
            on a conversation marks `convo.ingress_ready = True`
            and flushes anything that `_handle_gemini_message`
            had buffered into `convo.ingress_buffer` while waiting
            for Infinity's ingress path to open. The buffer holds
            Gemini's opening greeting audio, which lands before
            Infinity emits its first egress frame; without the
            latch + buffer pair, those greeting chunks would be
            discarded. See `_handle_gemini_message` for the
            gating side and the buffer-overflow handling.

            After the latch handling, the inbound bytes are
            converted to 16 kHz S16LE PCM by
            `_prepare_input_audio`. If transcoding fails (returns
            `None`), this method returns `False`. Otherwise the
            PCM is base64-wrapped in a `realtimeInput.audio`
            envelope (with `mimeType: "audio/pcm;rate=16000"`)
            and sent on `convo.gm_ws`. WS send failures log at
            ERROR and return `False`.

        Args:
            session_id: RCMS session identifier from the
                originating `bot.start`.
            endpoint_id: Media endpoint identifier from the
                originating `bot.start`.
            source: Frame direction sentinel (`"rx"` or `"tx"`);
                must match `convo.source`. Mismatches return
                `False` so the dispatcher can route the frame
                elsewhere.
            audio_bytes: Raw frame payload from the Infinity
                media transport (codec-encoded per
                `convo.codec_name`).

        Returns:
            True iff the frame was successfully transcoded and
            sent to Gemini. False on every reject / failure
            path.
        """
        convo = self._conversations.get(self._key(session_id, endpoint_id))
        if not convo or not convo.active or convo.source != source or not convo.gm_ws:
            return False

        # First egress frame from Infinity signals ingress is open. Flush any Gemini audio
        # that was buffered while we waited.
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

        pcm_16k = self._prepare_input_audio(convo, audio_bytes)
        if not pcm_16k:
            return False

        try:
            msg = {
                "realtimeInput": {
                    "audio": {
                        "data": base64.b64encode(pcm_16k).decode("ascii"),
                        "mimeType": f"audio/pcm;rate={GEMINI_INPUT_RATE}",
                    }
                }
            }
            await convo.gm_ws.send(json.dumps(msg))
        except Exception as exc:
            logger.error("[%s] Send to Gemini failed: %s", convo.client_id, exc)
            return False
        return True

    def _prepare_input_audio(self, convo: BotConversation, audio_bytes: bytes) -> Optional[bytes]:
        """Convert an inbound Infinity-side audio frame to 16 kHz S16LE PCM for Gemini Live.

        Contract:
            Inbound (Infinity → Gemini) counterpart of
            `_transcode_output_audio`. Branches on
            `convo.codec_name.upper()`:

                * **L16**: pass through if `sample_rate == 16000`;
                  otherwise resample to `GEMINI_INPUT_RATE` via
                  `audioop.ratecv`, threading
                  `convo.ratecv_in_state`.
                * **PCMU**: decode µ-law (`audioop.ulaw2lin`) at
                  8 kHz, then resample to 16 kHz.
                * **PCMA**: decode A-law (`audioop.alaw2lin`) at
                  8 kHz, then resample to 16 kHz.
                * **G722**: decode via `convo.g722_decoder`
                  (lazily initialised in `_handle_bot_start` when
                  G722 is negotiated) into 16 kHz S16LE; the
                  G722 module yields a numpy view, materialised
                  via `.tobytes()`.

            `ratecv_in_state` is independent of
            `ratecv_out_state` — audioop returns a fresh state
            per call and the two directions cannot share, doubly
            so on Gemini where the rates differ (16 kHz in /
            24 kHz out).

            Errors from `audioop` / G722 are logged at ERROR and
            yield `None`. Unsupported codec names log at WARNING
            and yield `None`. The caller drops the chunk on
            `None`.

        Args:
            convo: The active conversation. Provides
                `codec_name`, `sample_rate`, `g722_decoder`, and
                the resampler state; the latter two are mutated.
            audio_bytes: Raw inbound frame bytes from Infinity.
                Empty buffers return `None`.

        Returns:
            16 kHz S16LE little-endian PCM, mono, ready for the
            Gemini `realtimeInput.audio` envelope, or `None` on
            transcoding failure or unsupported codec.
        """
        if not audio_bytes:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate
        try:
            if codec == "L16":
                if rate == GEMINI_INPUT_RATE:
                    return audio_bytes
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    audio_bytes, 2, 1, rate, GEMINI_INPUT_RATE, convo.ratecv_in_state
                )
                return pcm
            if codec == "PCMU":
                pcm8 = audioop.ulaw2lin(audio_bytes, 2)
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    pcm8, 2, 1, 8000, GEMINI_INPUT_RATE, convo.ratecv_in_state
                )
                return pcm
            if codec == "PCMA":
                pcm8 = audioop.alaw2lin(audio_bytes, 2)
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    pcm8, 2, 1, 8000, GEMINI_INPUT_RATE, convo.ratecv_in_state
                )
                return pcm
            if codec == "G722":
                if not G722_AVAILABLE or not convo.g722_decoder:
                    return None
                return convo.g722_decoder.decode(audio_bytes).tobytes()
        except Exception as exc:
            logger.error(
                "[%s] Input audio prep failed (codec=%s rate=%d): %s",
                convo.client_id, codec, rate, exc,
            )
            return None

        logger.warning("[%s] Unsupported input codec '%s' rate=%d", convo.client_id, codec, rate)
        return None

    def _transcode_output_audio(
        self, convo: BotConversation, pcm_24k: bytes
    ) -> Optional[bytes]:
        """Convert 24 kHz S16LE PCM from Gemini Live into the Infinity-side codec for this call.

        Contract:
            Outbound (Gemini → Infinity) counterpart of
            `_prepare_input_audio`. **Note the asymmetric input
            rate** — Gemini emits at `GEMINI_OUTPUT_RATE`
            (24 kHz), unique among the four AI providers. The
            output codec / sample rate are whatever Infinity
            negotiated.

            Branches on `convo.codec_name.upper()`:

                * **L16**: pass through if `sample_rate == 24000`;
                  otherwise resample 24 kHz → `convo.sample_rate`
                  via `audioop.ratecv`, threading
                  `convo.ratecv_out_state`.
                * **PCMU**: resample 24 kHz → 8 kHz, then encode
                  µ-law (`audioop.lin2ulaw`).
                * **PCMA**: resample 24 kHz → 8 kHz, then encode
                  A-law (`audioop.lin2alaw`).
                * **G722**: resample 24 kHz → 16 kHz (G722's
                  internal rate), lazy-init `convo.g722_encoder`
                  on first call, then encode via the G722
                  module's `encode` on a numpy int16 view.

            `ratecv_out_state` is independent of
            `ratecv_in_state` for the same reason as the
            inbound counterpart.

            Errors from `audioop` / G722 / numpy import are
            logged at ERROR and yield `None`. Unsupported codec
            names log at WARNING and yield `None`. The caller
            drops the chunk on `None`.

        Args:
            convo: The active conversation. Provides
                `codec_name`, `sample_rate`, `g722_encoder`,
                and the resampler state; the latter two are
                mutated.
            pcm_24k: Raw 24 kHz S16LE little-endian PCM, mono,
                from Gemini's `serverContent.modelTurn.parts[].
                inlineData.data` after base64 decoding. Empty
                buffers return `None`.

        Returns:
            Bytes ready for the Infinity wire (PCM at
            negotiated rate, µ-law, A-law, or G.722 encoded), or
            `None` on transcoding failure or unsupported codec.
        """
        if not pcm_24k:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate
        try:
            if codec == "L16":
                if rate == GEMINI_OUTPUT_RATE:
                    return pcm_24k
                pcm, convo.ratecv_out_state = audioop.ratecv(
                    pcm_24k, 2, 1, GEMINI_OUTPUT_RATE, rate, convo.ratecv_out_state
                )
                return pcm
            if codec == "PCMU":
                pcm8, convo.ratecv_out_state = audioop.ratecv(
                    pcm_24k, 2, 1, GEMINI_OUTPUT_RATE, 8000, convo.ratecv_out_state
                )
                return audioop.lin2ulaw(pcm8, 2)
            if codec == "PCMA":
                pcm8, convo.ratecv_out_state = audioop.ratecv(
                    pcm_24k, 2, 1, GEMINI_OUTPUT_RATE, 8000, convo.ratecv_out_state
                )
                return audioop.lin2alaw(pcm8, 2)
            if codec == "G722":
                if not G722_AVAILABLE:
                    return None
                pcm_16k, convo.ratecv_out_state = audioop.ratecv(
                    pcm_24k, 2, 1, GEMINI_OUTPUT_RATE, 16000, convo.ratecv_out_state
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
            Pure function over the Infinity codec / sample-rate /
            duration triple. Used by `_enqueue_output_audio` to
            compute the accumulator's flush boundary so each
            flush produces exactly one paced chunk. Caller must
            pass `IngressStreamer.chunk_duration_ms` as
            `duration_ms` — passing any other value desynchronizes
            the accumulator boundary from the streamer's pacing
            interval, which causes `queue_audio` to split each
            flush into mismatched chunks paced uniformly.
            Empirically that shape produces sub-real-time delivery
            (~0.625× real-time) and buffer underruns at Infinity.

            Codec mapping:
                * **L16** — `samples * 2` (S16LE = 2 bytes/sample,
                  mono).
                * **PCMU** / **PCMA** — `samples` (8-bit µ/A-law,
                  1 byte/sample).
                * **G722** — `(64000 * duration_ms) / (8 * 1000)`
                  (64 kbps fixed bit rate, regardless of
                  `sample_rate`).
                * Anything else — fall back to S16LE sizing.

        Args:
            codec: Upper-case Infinity codec name
                (`"L16"` / `"PCMU"` / `"PCMA"` / `"G722"`).
            sample_rate: Infinity-negotiated rate in Hz.
            duration_ms: Target chunk duration in milliseconds —
                must equal `IngressStreamer.chunk_duration_ms`
                for accumulator-boundary correctness.

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
        """Accumulate Gemini's raw output frames into pacer-aligned chunks and flush each full chunk.

        Contract:
            Gemini Live emits raw audio frames at roughly 40 ms each;
            the bridge's IngressStreamer paces at
            `chunk_duration_ms` per chunk regardless of the duration
            of the content fed to it. This method buffers the raw
            frames in `convo.ingress_accumulator` until a full
            pacer-aligned chunk is available, then flushes one
            chunk at a time via `_send_ingress_chunked`. The
            leftover stays in the accumulator for the next call,
            so pacing tracks actual audio content rather than
            fragmenting per inbound frame.

            **Why pacer-alignment matters for Gemini but not for
            ElevenLabs.** ElevenLabs's WebSocket emits audio in
            larger pacer-aligned blocks already; the ElevenLabs
            provider can hand each block straight to
            `IngressStreamer.queue_audio`. Gemini's ~40 ms grain
            is finer than the streamer's pacing interval (default
            80 ms), so without this accumulator each Gemini frame
            would become a paced chunk of its own and the
            streamer would deliver audio at multiples of the
            target cadence — staccato gaps and out-of-order
            fragmentation at Infinity. The accumulator
            re-aggregates upstream-of-pacer.

            The accumulator's chunk size (`convo.ingress_chunk_size`)
            is computed lazily on first use via `_chunk_size_for`,
            which reads `IngressStreamer.chunk_duration_ms`. Using
            any other value desynchronizes the flush boundary from
            the streamer's pacing interval — the resulting
            mismatched chunks are then paced uniformly, producing
            sub-real-time delivery at Infinity.

            Empty inputs return immediately. Multiple flushes per
            call are possible if `audio_bytes` is large enough to
            cover several pacer chunks (the inner `while` loop).

        Args:
            convo: The active conversation. Mutated:
                `ingress_chunk_size` is set lazily on first call;
                `ingress_accumulator` is appended to and
                consumed.
            audio_bytes: Codec-converted PCM/encoded audio ready
                for the wire (output of `_transcode_output_audio`).

        Returns:
            None. Per-flush errors are surfaced through
            `_send_ingress_chunked` (logged WARNING, swallowed).
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
            Called from `_handle_gemini_message`'s turn-end branch
            so the tail of a sentence (whatever doesn't make a full
            pacer-aligned chunk) plays out instead of being
            stranded in the accumulator until the next turn. The
            tail is sent as a single sub-pacer-sized chunk; the
            IngressStreamer accepts it and paces it like any other
            chunk.

            Empty accumulator returns silently — turn-end fires on
            tool-only turns too, where there may be no audio
            tail to flush.

        Args:
            convo: The active conversation whose accumulator is
                drained. Mutated: `ingress_accumulator` cleared.

        Returns:
            None. Send errors propagate via
            `_send_ingress_chunked` (logged WARNING, swallowed).
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
            `chunk_duration_ms` cadence. Caller must have already
            sized the chunk to match the streamer's pacing
            interval — see `_enqueue_output_audio` for the
            accumulator that does this. Calling
            `_send_ingress_chunked` with sub- or super-pacer-
            sized chunks works at the streamer level but breaks
            the cadence-tracking the streamer relies on for
            barge-in coherence.

            Errors from `queue_audio` are logged at WARNING and
            swallowed — a per-chunk send failure should not tear
            the conversation down.

        Args:
            convo: The active conversation. Provides the
                bridge-side websocket, identifiers, and
                transport encoding.
            audio_bytes: Codec-converted, pacer-aligned audio
                chunk ready for the wire. Empty buffers return
                immediately.

        Returns:
            None. The send is fire-and-forget from the caller's
            perspective; pacing happens inside the
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
        except Exception as exc:
            logger.warning(
                "[%s] Failed to queue ingress audio: %s", convo.client_id, exc
            )

    # ------------------------------------------------------------------ Gemini WS

    async def _connect_gemini(self, convo: BotConversation, api_key: str) -> None:
        """Open the upstream Gemini Live WebSocket and send the `setup` frame.

        Contract:
            Two-step connect:

                1. Open
                   `wss://generativelanguage.googleapis.com/ws/...
                   /BidiGenerateContent?key=<api_key>` with the API
                   key passed as a URL query parameter (Gemini Live
                   does not use a header for auth on this WebSocket
                   handshake; this is the protocol's chosen
                   mechanism, not a bridge implementation choice).
                   `max_size=_WS_MAX_FRAME_BYTES` (8 MiB)
                   accommodates large audio frames.
                2. Immediately send a single `setup` frame carrying:
                   - **`model`** — `f"models/{convo.model}"`,
                     resolved by `_handle_bot_start` from
                     `GEMINI_MODEL` env or the botId suffix.
                   - **`generation_config`** —
                     `response_modalities: ["AUDIO"]`, voice
                     selection (`speech_config.voice_config.
                     prebuilt_voice_config.voice_name`, default
                     "Aoede" via the `GEMINI_VOICE` env var), and
                     `thinking_config: {thinking_level: "minimal"}`
                     (the lowest-latency thinking mode for
                     real-time voice; Gemini 3.1+ replacement for
                     the older `thinking_budget` integer per
                     Google's migration guide for
                     `gemini-3.1-flash-live-preview`).
                   - **`system_instruction`** — the per-call system
                     prompt composed by `_load_base_prompt` +
                     `_compose_system_prompt`. Gemini has no
                     dashboard prompt surface, so this is the only
                     way the agent gets its persona.
                   - **`tools`** — inline `functionDeclarations`
                     for `transfer_to_agent` (single string
                     parameter `reason`, optional) and
                     `end_session` (no parameters). Both are
                     bridge-implemented; there are no
                     Gemini-platform system tools.
                   - **`output_audio_transcription`** /
                     **`input_audio_transcription`** — empty
                     objects to enable both transcript streams
                     (Gemini emits them as additive partials in
                     `serverContent`).

            Stores the connected websocket on `convo.gm_ws`. On
            any failure during connect or setup-frame send, the
            exception propagates to `_handle_bot_start`, which
            translates it into `bot.ended` with
            `BACKEND_START_FAILED` via the failure-context helper.

            **TLS context selection.** When the optional
            `truststore` package is available, an outbound TLS
            context backed by the OS trust store is built per call
            so corporate TLS-interception roots (e.g. Zscaler) are
            honored. Scoping to this call site avoids globally
            replacing `ssl.SSLContext`, which would break the
            server-side WSS context the bridge accepts inbound
            connections on. When `truststore` is not installed,
            `_ssl.create_default_context()` falls back to the
            Python CA bundle.

            **Note on agent activation.** The `setup` frame does
            not cause Gemini to speak. The agent stays silent
            until either the caller speaks or the bridge sends a
            proactive `realtimeInput.text` trigger. The trigger
            send happens in `_handle_gemini_message`'s
            `setupComplete` branch, not here, so this method
            returns as soon as `setup` is on the wire.

        Spec:
            Gemini Live BidiGenerateContent protocol — `setup`
            envelope shape, URL-query API-key auth, and the
            `setupComplete` handshake.

        Args:
            convo: The conversation receiving the connection.
                Mutated: `gm_ws` is set on success.
            api_key: Gemini API key, sourced from
                `GEMINI_API_KEY` in the bridge environment.

        Returns:
            None. Raises any websockets / TLS / send error to the
            caller; no bridge-side bot.ended is emitted from here.
        """
        url = f"{GEMINI_WS_URL}?key={api_key}"
        if _truststore is not None:
            ssl_context = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        else:
            ssl_context = _ssl.create_default_context()
        convo.gm_ws = await websockets.connect(
            url,
            max_size=_WS_MAX_FRAME_BYTES,
            ssl=ssl_context,
        )
        voice_name = os.environ.get("GEMINI_VOICE", "Aoede")
        setup = {
            "setup": {
                "model": f"models/{convo.model}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voice_config": {
                            "prebuilt_voice_config": {"voice_name": voice_name}
                        }
                    },
                    # Lowest-latency thinking mode for real-time voice (Gemini 3.1+
                    # replacement for the older `thinking_budget` integer). Per
                    # Google's migration guide for gemini-3.1-flash-live-preview.
                    "thinking_config": {"thinking_level": "minimal"},
                },
                "system_instruction": {"parts": [{"text": convo.system_prompt}]},
                "tools": [
                    {
                        "functionDeclarations": [
                            {
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
                                            "description": "Brief reason for the transfer",
                                        },
                                    },
                                    "required": [],
                                },
                            },
                            {
                                "name": "end_session",
                                "description": (
                                    "End the call when you have fully resolved the caller's need "
                                    "and no further assistance is required. The caller will hear "
                                    "your final response, then the call will disconnect."
                                ),
                                "parameters": {
                                    "type": "object",
                                    "properties": {},
                                    "required": [],
                                },
                            },
                        ]
                    }
                ],
                "output_audio_transcription": {},
                "input_audio_transcription": {},
            }
        }
        await convo.gm_ws.send(json.dumps(setup))
        logger.info(
            "[%s] Connected to Gemini model=%s (prompt_len=%d)",
            convo.client_id, convo.model, len(convo.system_prompt),
        )

    async def _gemini_recv_loop(self, convo: BotConversation) -> None:
        """Drain the Gemini Live WebSocket and dispatch each frame to `_handle_gemini_message`.

        Contract:
            Long-lived coroutine started from `_handle_bot_start`
            and cancelled from `_shutdown_conversation`. Iterates
            `convo.gm_ws`, decoding each frame as JSON
            (Gemini sometimes sends bytes; UTF-8 decoded first
            when so), and hands the parsed message to
            `_handle_gemini_message`. Non-UTF-8 and non-JSON
            frames are logged at WARNING and skipped — never
            raised. The loop ends on one of:

                * `convo.active = False` observed at the
                  iteration head (cooperative exit, set by
                  `_shutdown_conversation` or by the
                  `goAway` branch of
                  `_handle_gemini_message`).
                * `asyncio.CancelledError` — re-raised so the
                  cancelling coroutine sees it.
                * `ConnectionClosed` — log at INFO and exit;
                  no bridge-side `bot.ended` emission from this
                  branch (the relevant termination shapes are
                  handled by `_emit_pending_handoff`,
                  `_emit_session_end_complete`,
                  `on_session_ended`, and the `goAway` branch
                  in `_handle_gemini_message`).
                * Any other exception — log at ERROR with full
                  traceback and exit; `convo.active` is flipped
                  False in the `finally` block so any in-flight
                  ingest sees the dead conversation and stops
                  sending.

            Unlike ElevenLabs's recv loop, this method does
            **not** emit a success-context `bot.ended` on a
            clean upstream close. Gemini Live does not signal
            self-service-complete via WS-close — it signals via
            the `end_session` `toolCall`, which lands in
            `_handle_gemini_message` and follows the
            `_wait_for_quiescence_and_emit_session_end` →
            `_emit_session_end_complete` path. A clean WS close
            from Gemini means either `goAway` (already handled)
            or some upstream timeout we should not interpret as
            success.

        Args:
            convo: The conversation whose upstream WebSocket
                this loop drains. Mutated only on exit
                (`convo.active = False`).

        Returns:
            None. Cancellation re-raises; all other exceptions
            are logged and swallowed so the bridge does not
            crash on upstream protocol errors.
        """
        try:
            async for raw in convo.gm_ws:
                if not convo.active:
                    break
                if isinstance(raw, (bytes, bytearray)):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        logger.warning("[%s] Non-UTF8 frame from Gemini", convo.client_id)
                        continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.warning("[%s] Non-JSON frame from Gemini", convo.client_id)
                    continue
                await self._handle_gemini_message(convo, msg)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("[%s] Gemini WS closed: %s", convo.client_id, exc)
        except Exception as exc:
            logger.error("[%s] Gemini recv loop error: %s", convo.client_id, exc, exc_info=True)
        finally:
            convo.active = False

    async def _handle_gemini_message(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a single decoded message from the Gemini Live WebSocket to its handler branch.

        Contract:
            Switch on the top-level keys of the inbound Gemini Live
            message. Each branch performs its work inline (control
            envelope emit, audio playback, transcript accumulation,
            tool dispatch, conversation initiation) before returning.
            Unknown messages are logged at DEBUG with their key set
            and dropped — the bridge does not echo unrecognized
            frames back to Gemini.

            Branches handled, in declaration order:

                * **`goAway`**: Gemini's notice that the upstream is
                  about to close (e.g. the 15-minute session cap).
                  Emit a `bot.feature` with `ftype: PROVIDER_GOAWAY`
                  so the Infinity workflow can re-enter, then mark
                  the conversation inactive so no further audio is
                  forwarded. The actual `bot.end` arrives from
                  Infinity when it decides to terminate; this branch
                  does not originate a `bot.ended`.
                * **`serverContent`**: Gemini's primary content
                  envelope. Carries any combination of:
                      - `modelTurn.parts[].inlineData` — base64
                        24 kHz PCM audio chunks (the bot's voice).
                      - `outputTranscription.text` — additive
                        partials of the bot's spoken text.
                      - `inputTranscription.text` — additive
                        partials of the caller's recognized speech.
                      - Control flags `interrupted` / `turnComplete`
                        / `generationComplete`.
                  Audio chunks are decoded, transcoded via
                  `_transcode_output_audio`, and either buffered
                  on `convo.ingress_buffer` (while
                  `convo.ingress_ready` is False) or fed through
                  `_enqueue_output_audio` for pacer-aligned flush.
                  Transcripts are concatenated into the per-turn
                  accumulators (`pending_bot_text` /
                  `pending_customer_text`); turn-start timestamps
                  are captured on the empty→non-empty transition
                  of each accumulator and reset alongside the
                  accumulator on flush.
                  `interrupted` triggers a `barge_in` flush of the
                  IngressStreamer queue and discards the stranded
                  tail in `ingress_accumulator`.
                  `turnComplete` or `generationComplete` ends the
                  turn: flush any sub-chunk audio remainder, emit
                  per-speaker TRANSCRIPT envelopes for whichever
                  accumulators are non-empty (skipping empty ones
                  so tool-only turns don't render blank bubbles),
                  and reset accumulators and turn-start fields.
                * **`toolCall`**: bridge-implemented tool
                  invocation (`transfer_to_agent` or `end_session`);
                  delegated to `_handle_tool_call`. There are no
                  Gemini-platform system tools; everything tool-
                  related is bridge-side.
                * **`setupComplete`**: handshake completion. Send
                  the proactive `realtimeInput.text = "Hello,
                  please greet the customer now"` trigger so the
                  agent speaks first — Gemini Live has no
                  configured first-message field, so without this
                  the agent stays silent until the caller speaks.

            Why bridge-side transcript accumulation. Gemini's audio
            mode emits `inputTranscription` / `outputTranscription`
            as additive partial chunks with no per-message finality
            flag (`finished` is always False in practice), so the
            bridge cannot use per-chunk emit. Concatenating per
            turn and flushing on `turnComplete` /
            `generationComplete` produces one wire-level TRANSCRIPT
            per speaker per turn, matching the partner-facing
            transcript shape expected on the workflow side. ElevenLabs,
            by contrast, emits one final transcript per turn directly
            — no bridge-side accumulation needed there.

            Why turn-start timestamps live on the BotConversation
            rather than being computed at flush time. The turn-end
            flush emits BOT first and CUSTOMER second from the same
            turn boundary; flush-time timestamps (each computed at
            the moment of emit) would either tie or invert the two
            speakers' actual chronological order. Capturing
            turn-start at the empty→non-empty transition of each
            accumulator preserves correct ordering on the wire and
            in Infinity's call record regardless of flush-side ordering.

        Spec:
            Gemini Live BidiGenerateContent protocol — message
            types (`serverContent`, `toolCall`, `setupComplete`,
            `goAway`) and the additive-partial transcript shape.
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            with `TRANSCRIPT` and `PROVIDER_GOAWAY` ftypes.
            See `bridge/schema/rcms.schema.md` "bot.feature —
            endpointId at payload level" and the
            `bot.feature` PROVIDER_GOAWAY ftype note.

        Args:
            convo: The active conversation whose upstream WebSocket
                produced this message. Mutated by branches that
                update accumulators, turn-start fields, ingress
                buffer / accumulator, or `active`.
            msg: Decoded JSON object from the Gemini Live WebSocket.

        Returns:
            None. Outbound effects: optional audio sends to Infinity
            via the IngressStreamer, optional `bot.feature`
            envelopes (TRANSCRIPT, PROVIDER_GOAWAY) to Infinity,
            optional proactive `realtimeInput.text` to Gemini.
        """
        if "goAway" in msg:
            go_away = msg["goAway"]
            logger.warning(
                "[%s] Gemini goAway time_left=%s — emitting bot.feature PROVIDER_GOAWAY",
                convo.client_id, go_away.get("timeLeft"),
            )
            await self._emit_session_event(convo, "bot.feature", {
                "ftype": "PROVIDER_GOAWAY",
                "providerGoAway": {
                    "provider": "gemini",
                    "timeLeft": go_away.get("timeLeft"),
                },
            })
            convo.active = False
            return

        server_content = msg.get("serverContent")
        if server_content:
            model_turn = server_content.get("modelTurn") or {}
            parts = model_turn.get("parts", []) or []
            audio_parts = 0
            audio_bytes_24k = 0
            text_parts = 0
            text_chars = 0
            for part in parts:
                inline = part.get("inlineData") or {}
                b64 = inline.get("data")
                if b64:
                    audio_parts += 1
                    audio_bytes_24k += (len(b64) * 3) // 4
                text_val = part.get("text")
                if text_val:
                    text_parts += 1
                    text_chars += len(text_val)
            control_flags = [
                k for k in ("interrupted", "turnComplete", "generationComplete")
                if server_content.get(k)
            ]
            other_keys = sorted(
                k for k in server_content.keys()
                if k not in ("modelTurn", "interrupted", "turnComplete", "generationComplete",
                             "outputTranscription", "inputTranscription")
            )
            logger.info(
                "[%s] serverContent audio_parts=%d audio_bytes_24k=%d text_parts=%d text_chars=%d flags=%s other=%s",
                convo.client_id, audio_parts, audio_bytes_24k, text_parts, text_chars,
                control_flags or "[]", other_keys or "[]",
            )

            output_transcription = server_content.get("outputTranscription") or {}
            output_text_raw = output_transcription.get("text") or ""
            if output_text_raw:
                # Capture bot turn-start on the empty→non-empty transition
                # of the accumulator (see method docstring rationale). Used
                # as startTsMs on the eventual BOT TRANSCRIPT emit.
                if not convo.pending_bot_text:
                    convo.bot_turn_started_at = int(time.time() * 1000)
                # Accumulate raw (un-stripped) so inter-token spacing survives
                # the per-turn concatenation. Flushed in the `turn_ended`
                # block below as a single bot.feature TRANSCRIPT.
                convo.pending_bot_text += output_text_raw
            output_text = output_text_raw.strip()
            if output_text:
                logger.info("[%s] BOT transcript: %s", convo.client_id, output_text)

            input_transcription = server_content.get("inputTranscription") or {}
            input_text_raw = input_transcription.get("text") or ""
            if input_text_raw:
                # Capture caller turn-start on first chunk — mirrors the
                # bot-side capture above (same rationale).
                if not convo.pending_customer_text:
                    convo.customer_turn_started_at = int(time.time() * 1000)
                convo.pending_customer_text += input_text_raw
            input_text = input_text_raw.strip()
            if input_text:
                logger.info("[%s] CUSTOMER transcript: %s", convo.client_id, input_text)

            for part in parts:
                inline = part.get("inlineData") or {}
                b64 = inline.get("data")
                if not b64:
                    continue
                pcm_24k = base64.b64decode(b64)
                out_bytes = self._transcode_output_audio(convo, pcm_24k)
                if not out_bytes:
                    continue
                if not convo.ingress_ready:
                    if len(convo.ingress_buffer) >= _INGRESS_BUFFER_MAX_CHUNKS:
                        logger.warning(
                            "[%s] Ingress buffer full (%d chunks) — discarding oldest",
                            convo.client_id, _INGRESS_BUFFER_MAX_CHUNKS,
                        )
                        convo.ingress_buffer.pop(0)
                    convo.ingress_buffer.append(out_bytes)
                    continue
                await self._enqueue_output_audio(convo, out_bytes)

            if server_content.get("interrupted"):
                # Drop any buffered tail from the interrupted turn so we don't
                # play stale audio after the barge-in clears the queue.
                convo.ingress_accumulator.clear()
                try:
                    await self.server.ingress_streamer.barge_in(
                        convo.session_id, convo.endpoint_id
                    )
                except Exception as exc:
                    logger.debug("[%s] barge_in call raised: %s", convo.client_id, exc)

            turn_ended = bool(
                server_content.get("turnComplete") or server_content.get("generationComplete")
            )
            if turn_ended:
                # Flush the sub-100ms tail so the end of the sentence plays out.
                await self._flush_output_remainder(convo)

                # Flush accumulated per-turn transcripts to Infinity as
                # bot.feature TRANSCRIPT frames (see method docstring "Why
                # bridge-side transcript accumulation"). Empty accumulators
                # are skipped: turnComplete also fires on tool-only turns
                # (the handoff path), and an empty TRANSCRIPT would render
                # as a blank bubble on the workflow side. Ordering vs. the
                # handoff drain spawned at toolCall time: that task emits
                # LIVE_AGENT_HANDOFF only after the audio queue drains
                # (seconds while the goodbye plays out), so this inline
                # flush always lands first on the wire.
                bot_text = convo.pending_bot_text.strip()
                if bot_text:
                    await self._emit_transcript(
                        convo, "BOT", bot_text,
                        start_ts_ms=convo.bot_turn_started_at,
                    )
                customer_text = convo.pending_customer_text.strip()
                if customer_text:
                    await self._emit_transcript(
                        convo, "CUSTOMER", customer_text,
                        start_ts_ms=convo.customer_turn_started_at,
                    )
                convo.pending_bot_text = ""
                convo.pending_customer_text = ""
                # Reset turn-start fields alongside the accumulators so the
                # next turn captures fresh timestamps on first chunk.
                convo.bot_turn_started_at = None
                convo.customer_turn_started_at = None

            return

        tool_call = msg.get("toolCall")
        if tool_call:
            await self._handle_tool_call(convo, tool_call)
            return

        if "setupComplete" in msg:
            logger.info("[%s] Gemini setupComplete", convo.client_id)
            greeting_trigger = {
                "realtimeInput": {
                    "text": "Hello, please greet the customer now."
                }
            }
            try:
                await convo.gm_ws.send(json.dumps(greeting_trigger))
                logger.info("[%s] Sent proactive greeting trigger to Gemini", convo.client_id)
            except Exception as exc:
                logger.warning("[%s] Failed to send greeting trigger: %s", convo.client_id, exc)
            return

        logger.debug("[%s] Unhandled Gemini message keys=%s", convo.client_id, list(msg.keys()))

    async def _handle_tool_call(
        self, convo: BotConversation, tool_call: Dict[str, Any]
    ) -> None:
        """Dispatch a Gemini Live `toolCall` to its handler and reply with `toolResponse`.

        Contract:
            Gemini sends `toolCall.functionCalls` as a list — one
            envelope can carry multiple invocations. The bridge
            implements two tools (declared inline in `_connect_gemini`):

                * **`transfer_to_agent`**: stash a
                  `bot.feature LIVE_AGENT_HANDOFF` payload on
                  `convo.pending_handoff` and start the drain task
                  in `_wait_for_quiescence_and_emit`. The wire-level
                  emit is deferred until the IngressStreamer queue
                  drains so the transfer-announcement audio plays
                  out before Infinity tears down playback for the
                  handoff. Returns `result_text = "Transfer
                  initiated."` to Gemini.
                * **`end_session`**: set the
                  `convo.pending_session_end` latch (boolean — no
                  per-call payload to stash, since the
                  success-context `bot.ended` carries only the
                  static success status) and start the drain task
                  in `_wait_for_quiescence_and_emit_session_end`.
                  Same drain logic as the handoff path; the
                  terminal envelope is the success-context
                  `bot.ended` instead of `LIVE_AGENT_HANDOFF +
                  bot.ended`. Returns `result_text = "Session end
                  initiated."` to Gemini.
                * **anything else**: logged at WARNING; the result
                  text reflects the unknown tool name.

            The two-drain-pipeline distinction is the load-bearing
            structural difference from the ElevenLabs provider:
            ElevenLabs handles self-service-complete platform-side
            via the `end_call` system tool with
            `pre_tool_speech="force"`, so the audio is already done
            when the bridge sees `agent_tool_response`. Gemini has
            no platform-side system tools, so the bridge owns both
            drain pipelines.

            **Defensive task replacement.** If a prior `handoff_task`
            or `session_end_task` is still running (the LLM
            invokes the tool twice), the prior task is cancelled
            before the new one starts. In practice the LLM picks
            one terminal tool per call, but the guard prevents a
            duplicate-toolCall race from leaving an orphan drain
            coroutine.

            **`toolResponse` reply.** Per the Gemini Live protocol,
            every functionCall with a non-empty `id` requires a
            matching `functionResponses[]` envelope keyed by that
            id. Even unknown-tool branches send a response so the
            agent's continuation isn't blocked waiting for one.

        Spec:
            Gemini Live BidiGenerateContent protocol —
            `toolCall.functionCalls[]` and the `toolResponse.
            functionResponses[]` reply shape (matched by id).
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            with `LIVE_AGENT_HANDOFF` ftype (emitted later by
            `_emit_pending_handoff` after drain).

        Args:
            convo: The active conversation. Mutated to stash the
                pending handoff payload, set the session-end
                latch, and launch the drain task.
            tool_call: The decoded `toolCall` body. The
                `functionCalls` list is read; each entry's `name`,
                `id`, and `args` are extracted.

        Returns:
            None. The `toolResponse` is sent on the upstream
            Gemini WebSocket; send failures are logged at WARNING
            and swallowed.
        """
        function_calls = tool_call.get("functionCalls") or []
        for call in function_calls:
            name = call.get("name") or ""
            call_id = call.get("id") or ""
            args = call.get("args") or {}
            if not isinstance(args, dict):
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
                # Defer emission until Gemini's goodbye turn finishes; otherwise
                # Infinity's session.end arrives mid-sentence and the goodbye is cut off.
                convo.pending_handoff = handoff_payload
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
            elif name == "end_session":
                # Self-service-complete signal: defer the success-context
                # bot.ended emission until the goodbye audio drains —
                # otherwise bot.ended lands while audio is still in-flight
                # to Infinity and the caller never hears the goodbye line.
                # Sibling to the handoff path; the terminal emission goes
                # through send_bot_ended_with_success_context.
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
            else:
                logger.warning("[%s] Unknown Gemini tool '%s'", convo.client_id, name)
                result_text = f"Unknown tool: {name}"

            if call_id:
                response = {
                    "toolResponse": {
                        "functionResponses": [
                            {"id": call_id, "name": name, "response": {"result": result_text}}
                        ]
                    }
                }
                try:
                    await convo.gm_ws.send(json.dumps(response))
                except Exception as exc:
                    logger.warning("[%s] toolResponse send failed: %s", convo.client_id, exc)

    async def _emit_pending_handoff(self, convo: BotConversation) -> None:
        """Flush stranded transcripts, emit `bot.feature` LIVE_AGENT_HANDOFF, then originate `bot.ended`.

        Contract:
            Terminus of the deferred-handoff path: drain has
            already completed in `_wait_for_quiescence_and_emit`,
            and this method performs three sequenced wire-level
            emits:

                1. **Pre-handoff transcript flush.** Gemini's
                   `toolCall` preempts `turnComplete` on the
                   trigger turn, so any
                   `inputTranscription` / `outputTranscription`
                   accumulated on that turn never reaches the
                   `_handle_gemini_message` turn-end flush. Without
                   this catch-up flush, the workflow's call
                   record loses the customer's handoff-trigger
                   utterance — which is exactly the utterance the
                   live agent picking up the call most needs to
                   understand the transfer. Customer first
                   (higher-value signal for the human picking up
                   the next leg); bot second (in case the bot was
                   mid-sentence at toolCall time). Empty-text
                   guards skip blank emits on tool-only turns.
                   Wrapped in its own `try` block so a flush
                   failure cannot block the LIVE_AGENT_HANDOFF
                   emit below.
                2. **`bot.feature` LIVE_AGENT_HANDOFF.** The
                   stashed `pending_handoff` payload is sent via
                   `_emit_session_event`. Carries `payload.ftype:
                   "LIVE_AGENT_HANDOFF"` and
                   `payload.liveAgentHandoff: {queueId, tags,
                   context}`.
                3. **Manual `bot.ended` (handoff-shape).** Built
                   directly here rather than via the
                   `send_bot_ended_with_*_context` helper family —
                   the helpers all originate a
                   `payload.context.status` object (success,
                   failure, or disconnect), but the handoff
                   termination shape has **no status object**. The
                   workflow consumes the `byobotLiveAgentHandoff`
                   value populated from the prior `bot.feature`
                   rather than `byobotEndContext.status`. Bridge-
                   originating `bot.ended` here triggers Infinity
                   to send `session.end` as a clean teardown ack;
                   staying silent leads to a teardown timeout
                   race observable as a delayed `session.ended`.
                   `convo.bot_ended_sent` is set explicitly here
                   because the helper-flag-flip path is bypassed.

            **Cancellation-self guard.** This method is invoked at
            the end of the `_wait_for_quiescence_and_emit` drain
            loop, where `asyncio.current_task()` IS
            `handoff_task`. Cancelling that task here would raise
            `CancelledError` at the next await (inside
            `_emit_session_event`) and the handoff would die
            silently. The `task is not asyncio.current_task()`
            guard prevents self-cancellation. The same guard
            appears in `_emit_session_end_complete` for the
            session-end drain path.

            Clears `pending_handoff` and `handoff_task` before
            emitting so a duplicate trigger sees an
            already-flushed conversation and returns silently
            (the `if not payload: return` early-out at the top).

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            envelope and `LIVE_AGENT_HANDOFF` ftype.
            `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context" — the absent-status row pins the
            handoff termination shape.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            convo: The active conversation. Reads `pending_handoff`
                / `pending_*_text` accumulators / turn-start
                fields. Mutates: `pending_handoff` → None,
                `handoff_task` → None, accumulators cleared,
                turn-start fields → None, `bot_ended_sent` → True
                after the bot.ended send completes.

        Returns:
            None. Errors on any of the three emits are logged at
            WARNING and swallowed; the method always returns
            cleanly.
        """
        payload = convo.pending_handoff
        if not payload:
            return
        convo.pending_handoff = None
        task = convo.handoff_task
        convo.handoff_task = None
        # Cancellation-self guard — see method docstring "Cancellation-self
        # guard". current_task() IS handoff_task on the drain-completion
        # path; skip the cancel to avoid raising CancelledError into our
        # own awaits below.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        # Pre-handoff transcript flush — see method docstring step 1.
        # Customer-first / bot-second ordering and empty-text guards are
        # documented there; the try/except here isolates flush errors
        # from the LIVE_AGENT_HANDOFF emit below.
        try:
            customer_text = convo.pending_customer_text.strip()
            if customer_text:
                await self._emit_transcript(
                    convo, "CUSTOMER", customer_text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            bot_text = convo.pending_bot_text.strip()
            if bot_text:
                await self._emit_transcript(
                    convo, "BOT", bot_text,
                    start_ts_ms=convo.bot_turn_started_at,
                )
            convo.pending_customer_text = ""
            convo.pending_bot_text = ""
            # Reset turn-start fields alongside the accumulators.
            convo.customer_turn_started_at = None
            convo.bot_turn_started_at = None
        except Exception as exc:
            logger.warning(
                "[%s] Pre-handoff transcript flush failed: %s",
                convo.client_id, exc,
            )

        try:
            await self._emit_session_event(convo, "bot.feature", payload)
            reason = (
                payload.get("liveAgentHandoff", {})
                .get("context", {})
                .get("reason", "")
            )
            logger.info("[%s] Emitted LIVE_AGENT_HANDOFF (reason=%r)", convo.client_id, reason)
        except Exception as exc:
            logger.warning("[%s] LIVE_AGENT_HANDOFF emit failed: %s", convo.client_id, exc)

        # Manual bot.ended (handoff-shape) — see method docstring step 3.
        # No payload.context.status; the workflow consumes
        # byobotLiveAgentHandoff populated from the bot.feature above.
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
            logger.info(
                "[%s] OUTBOUND JSON (bot.ended): %s",
                convo.client_id, format_compact_json(bot_ended),
            )
            log_message_exchange(
                "OUTBOUND", convo.client_id, "bot.ended", bot_ended, is_media=False,
            )
            await convo.websocket.send(json.dumps(bot_ended))
            # Mark flag so on_session_ended does not emit a second bot.ended.
            # The disconnect-context helper guards on bot_ended_sent but this
            # bare-emit path bypasses the helpers — set explicitly here.
            convo.bot_ended_sent = True
        except Exception as exc:
            logger.warning("[%s] bot.ended originator send failed: %s", convo.client_id, exc)

    async def _wait_for_quiescence_and_emit(
        self,
        convo: BotConversation,
        poll_ms: int = _QUIESCENCE_POLL_MS,
        deadlock_safety_s: float = _QUIESCENCE_DEADLOCK_SAFETY_S,
    ) -> None:
        """Sleep-and-check drain loop — emits the pending handoff once the IngressStreamer queue is empty.

        Contract:
            Single-rule drain: sleep `poll_ms`, then check whether
            the per-endpoint IngressStreamer queue is empty.
            Empty → emit and return. Non-empty → loop. The sleep
            itself is the grace period — long enough for any audio
            chunk crossing the network during the iteration to
            land in the queue before the empty check.

            Gemini-specific timing: the `transfer_to_agent`
            `toolCall` arrives *before* the goodbye audio starts
            streaming. The first poll after the initial sleep
            will typically see a non-empty queue (Gemini has begun
            streaming the goodbye chunks); subsequent polls
            continue until the queue is observably empty. The
            grace period absorbs network jitter so a momentarily-
            empty queue doesn't trigger a premature emit on a
            still-streaming goodbye.

            **Safety net.** `deadlock_safety_s` is an
            upstream-failure backstop — e.g. Gemini WS hung in a
            way that prevents the audio queue from ever draining.
            Trip indicates something genuinely wrong upstream,
            not a drain-timing issue. Logs at WARNING and emits
            the handoff anyway so the workflow does not stall
            indefinitely.

            Cancellation-safe: `asyncio.CancelledError` returns
            silently without emitting.
            `_shutdown_conversation` cancels `handoff_task` on a
            caller-disconnect mid-handoff so this method exits
            without firing the emit (the conversation is being
            torn down anyway).

        Args:
            convo: The conversation whose handoff is pending.
                Reads `pending_handoff`; passes through to
                `_emit_pending_handoff` on drain completion.
            poll_ms: Sleep interval per iteration in milliseconds.
                Default `_QUIESCENCE_POLL_MS` (250 ms) is the
                cross-provider grace period.
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

                queue = streamer._queues.get(endpoint_key)
                if queue is None or queue.empty():
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
        """Sleep-and-check drain loop — emits success-context `bot.ended` once the audio queue is empty.

        Contract:
            Sibling to `_wait_for_quiescence_and_emit`. Same
            single-rule drain (sleep `poll_ms`, check
            queue-empty, loop) and same safety net (`deadlock_safety_s`).
            The two methods differ in the latch they observe and
            the terminal envelope they fire:

                * Handoff drain → latches on `pending_handoff`,
                  fires `_emit_pending_handoff` (which sends
                  `bot.feature` LIVE_AGENT_HANDOFF + manual
                  absent-status `bot.ended`).
                * Session-end drain (this method) → latches on
                  `pending_session_end`, fires
                  `_emit_session_end_complete` (which sends
                  success-context `bot.ended` via the helper).

            Same drain-timing reasoning applies: the `end_session`
            `toolCall` arrives before the goodbye audio finishes
            streaming, so emitting the success-context `bot.ended`
            immediately would cause Infinity to tear down playback
            mid-utterance and the caller would miss the goodbye
            line. The drain ensures the audio plays out first.

            Latching on a separate field
            (`pending_session_end` vs `pending_handoff`) means the
            two paths cannot collide if both ever activate on the
            same call. In practice the LLM should pick one
            terminal tool per call; the field separation is a
            defensive safeguard.

            Cancellation-safe via the same `CancelledError`
            return as the handoff drain.

        Args:
            convo: The conversation with `pending_session_end`
                latched True.
            poll_ms: Sleep interval per iteration in milliseconds.
                Default `_QUIESCENCE_POLL_MS` (250 ms).
            deadlock_safety_s: Upper bound on total wait time.
                Default `_QUIESCENCE_DEADLOCK_SAFETY_S` (30 s).

        Returns:
            None. Always either emits via
            `_emit_session_end_complete` (drain done or safety-
            net trip) or exits silently on cancellation.
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

                queue = streamer._queues.get(endpoint_key)
                if queue is None or queue.empty():
                    logger.info(
                        "[%s] Session-end drain done after %.2fs (queue_empty on poll)",
                        convo.client_id, time.monotonic() - start,
                    )
                    break
        except asyncio.CancelledError:
            return

        await self._emit_session_end_complete(convo)

    async def _emit_session_end_complete(self, convo: BotConversation) -> None:
        """Emit success-context `bot.ended` via `send_bot_ended_with_success_context`.

        Contract:
            Terminus of the deferred-session-end path: drain has
            already completed in
            `_wait_for_quiescence_and_emit_session_end`, and this
            method sends the success-context `bot.ended` via the
            shared bridge helper. The helper builds the spec-
            correct shape with `payload.context.status.code: 200,
            reason: "ENDPOINT_RELEASED"`, and the description
            string passed through here.

            **Why this is shorter than `_emit_pending_handoff`.**
            The success path has no `bot.feature` precursor (no
            handoff payload to flush) and no per-turn transcript
            catch-up flush (the `end_session` toolCall does not
            preempt `turnComplete` the way `transfer_to_agent`
            does). All that's needed is the bot.ended emit.

            Cancellation-self guard mirrors
            `_emit_pending_handoff`: when this method is invoked
            at the end of the drain loop,
            `asyncio.current_task()` IS `session_end_task`, and
            cancelling it would raise `CancelledError` into the
            in-flight `send_bot_ended_with_success_context` call.
            Skip the cancel for the running task.

            Clears `pending_session_end` and `session_end_task`
            before emitting so a duplicate trigger sees an
            already-flushed conversation and returns silently
            (the `if not convo.pending_session_end: return`
            early-out at the top).

        Args:
            convo: The active conversation. Reads
                `pending_session_end`; mutates
                `pending_session_end` → False, `session_end_task`
                → None, and (via the helper) `bot_ended_sent`
                → True.

        Returns:
            None. Helper send errors are logged at WARNING and
            swallowed.
        """
        if not convo.pending_session_end:
            return
        convo.pending_session_end = False
        task = convo.session_end_task
        convo.session_end_task = None
        # Cancellation-self guard — see method docstring "Cancellation-self
        # guard". current_task() IS session_end_task on the drain-completion
        # path; mirror of _emit_pending_handoff's guard.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        try:
            await self.server.send_bot_ended_with_success_context(
                convo.websocket,
                convo.client_id,
                convo.session_id,
                convo.endpoint_id,
                description="GEMINI: Self-service interaction completed.",
                service=convo.service,
                convo=convo,
            )
            logger.info("[%s] Emitted self-service-complete bot.ended", convo.client_id)
        except Exception as exc:
            logger.warning("[%s] success-context bot.ended emit failed: %s",
                           convo.client_id, exc)

    async def _emit_transcript(
        self,
        convo: BotConversation,
        speaker: str,
        text: str,
        is_final: bool = True,
        start_ts_ms: Optional[int] = None,
    ) -> None:
        """Emit a `bot.feature` TRANSCRIPT envelope for a single transcript line.

        Contract:
            Wraps a single transcript line in the bridge-standard
            transcript shape: `payload.ftype = "TRANSCRIPT"` plus
            `payload.transcript = {turnId, speaker, isFinal,
            text, confidence, language, startTsMs}`. A fresh
            UUID4 turnId is generated per emit; `confidence` is
            set to 1.0 (Gemini Live audio mode does not surface a
            per-utterance confidence value); `language` is read
            from `convo.language_code`.

            **`startTsMs` derivation.** When `start_ts_ms` is
            supplied (the normal path from
            `_handle_gemini_message`'s turn-end flush and from
            `_emit_pending_handoff`'s pre-handoff flush), it is
            the moment the speaker's turn started — captured at
            the empty→non-empty transition of the corresponding
            per-turn accumulator. Using turn-start timestamps
            preserves correct chronological ordering on the wire
            and in Infinity's call record, especially on the turn-end
            flush path where BOT and CUSTOMER are flushed back-
            to-back from the same turn boundary; flush-time
            timestamps would either tie or invert their actual
            ordering. Falls back to flush-time `int(time.time()
            * 1000)` when no value is supplied.

            Sibling implementations live in
            `providers/openai/bot_openai.py` and
            `providers/elevenlabs/bot_elevenlabs.py`; the
            envelope shape is identical across the three.

        Spec:
            RCMS spec §AI Bot Message Definitions —
            `bot.feature` with `TRANSCRIPT` ftype. See
            `bridge/schema/rcms.schema.md` "bot.feature —
            endpointId at payload level".

        Args:
            convo: The active conversation; provides
                `session_id`, `endpoint_id`, `client_id`, and
                `language_code`.
            speaker: `"CUSTOMER"` or `"BOT"` per the bridge's
                transcript speaker convention.
            text: The transcript text. Empty strings are still
                emitted (callers guard against blank emits where
                semantically appropriate).
            is_final: Whether this is the final transcript for
                the turn. The bridge accumulates per-turn before
                emit, so this is True in practice on every
                bridge-side emit.
            start_ts_ms: Approximate turn-start in epoch
                milliseconds. When `None`, falls back to flush
                time.

        Returns:
            None. Send exceptions propagate to the caller (via
            `_emit_session_event`).
        """
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
        """Build and send a generic outbound RCMS envelope (`bot.feature`, etc.) for this conversation.

        Contract:
            Internal helper used by `_emit_transcript` and
            `_emit_pending_handoff`. Wraps the caller-supplied
            `payload` in the standard RCMS envelope (`version`,
            `type`, `sessionId`, `sequenceNum`, `timestamp`,
            `payload`), allocates the next outbound sequence
            number from the bridge's per-client counter, logs
            the message at INFO, and sends it.

            `endpointId` is unconditionally injected into
            `payload` at the **payload level** rather than
            inside any feature sub-object. This matches the
            bridge-added `endpointId` placement documented in
            `bridge/schema/rcms.schema.md` "bot.feature —
            endpointId at payload level".

            The original caller's dict is shallow-copied via
            `{**payload, "endpointId": ...}`, so passing the
            same `payload` to two emits does not mutate it.

        Spec:
            RCMS message envelope — see
            `bridge/schema/rcms.schema.md` "Message envelope".
            Bridge-originated `bot.feature` carries `endpointId`
            at payload level (bridge-added field).

        Args:
            convo: The active conversation; provides
                `session_id`, `client_id`, `endpoint_id`, and
                the WebSocket to send on.
            event_type: RCMS message type (e.g.
                `"bot.feature"`).
            payload: The message-specific payload object.
                Shallow-copied before the `endpointId`
                injection so the caller's dict is not mutated.

        Returns:
            None. Send exceptions propagate to the caller.
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
        logger.info("[%s] OUTBOUND JSON (%s): %s", convo.client_id, event_type, format_compact_json(event))
        log_message_exchange("OUTBOUND", convo.client_id, event_type, event, is_media=False)
        await convo.websocket.send(json.dumps(event))

    # ------------------------------------------------------------------ shutdown

    async def _shutdown_conversation(self, convo: BotConversation) -> None:
        """Tear down every per-conversation resource: drain tasks, ingress streamer, Gemini WS.

        Contract:
            Idempotent shutdown for a single `BotConversation`.
            Called on every exit path: `_handle_bot_end`,
            `on_session_ended`, and `shutdown` (process
            shutdown). Marks `convo.active = False` first so any
            in-flight `_gemini_recv_loop` iteration sees the
            flag and exits at its next message boundary.

            Order of cleanup:
                1. Cancel the **handoff drain task**
                   (`handoff_task`) if still running. A caller
                   hangup mid-handoff-wait means Infinity is
                   already tearing the session down — there is
                   nothing to drain to and nothing useful to
                   emit. Cancel without emitting; do not invoke
                   `_emit_pending_handoff` from this path.
                   Clears `pending_handoff`.
                2. Cancel the **session-end drain task**
                   (`session_end_task`) symmetrically. Same
                   reasoning: a caller hangup mid-session-end-
                   wait means the success-context `bot.ended`
                   is moot. Clears `pending_session_end`.
                3. Drain and tear down the IngressStreamer for
                   this `(session_id, endpoint_id)` via
                   `stop_and_clear`. Purges the per-endpoint
                   queue and cancels the streaming task.
                4. Cancel the Gemini receive loop task and
                   await its exit so no further messages are
                   dispatched after this point.
                5. Close the upstream Gemini WebSocket. Errors
                   here are logged at DEBUG and swallowed —
                   the connection is being torn down anyway.

            Does NOT emit `bot.ended`. The disconnect-context
            emit lives in `on_session_ended` (caller-driven
            path); the bot.end ack lives in `_handle_bot_end`.
            Both call this method *after* their respective
            bot.ended emissions.

        Args:
            convo: The conversation to tear down. Mutated in
                place: every resource attribute is cleared or
                cancelled.

        Returns:
            None. All exceptions raised by cleanup steps are
            logged and swallowed; the method always returns
            cleanly.
        """
        convo.active = False

        handoff_task = convo.handoff_task
        convo.handoff_task = None
        convo.pending_handoff = None
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
                await self.server.ingress_streamer.stop_and_clear(convo.session_id, convo.endpoint_id)
            except Exception as exc:
                logger.debug("[%s] stop_and_clear raised: %s", convo.client_id, exc)

        task = convo.gm_recv_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        ws = convo.gm_ws
        convo.gm_ws = None
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[%s] Gemini ws.close raised: %s", convo.client_id, exc)


def register(server: "BridgeServer") -> GeminiService:
    """Plugin entrypoint — instantiate `GeminiService` and register it with the bridge.

    Discovered and called by `bot_service.py` at startup if
    `GeminiService.is_configured()` returns `True` (i.e.
    `GEMINI_API_KEY` is set in the environment). The constructed
    plugin is added to the bridge's `ServiceRegistry` and
    thereafter receives every `bot.start` / `bot.end` whose
    `payload.botId` carries the `gemini:` prefix.

    Args:
        server: The owning `BridgeServer` instance.

    Returns:
        The registered `GeminiService`. Returned for tests /
        startup diagnostics; production callers do not retain
        the reference.
    """
    plugin = GeminiService(server)
    server.register_service(plugin)
    return plugin
