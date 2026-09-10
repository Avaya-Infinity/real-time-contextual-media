"""
bot_elevenlabs — ElevenLabs Conversational AI provider for the RCMS Virtual Agent (bot) service

Role:
    Implements the ElevenLabs provider — a thin protocol translator
    between an Infinity RCMS bot session and a hosted ElevenLabs
    Conversational AI agent. ElevenLabs hosts the agent itself: the
    system prompt, voice configuration, native system tools
    (`end_call`, `language_detection`, etc.), tool schemas,
    dynamic-variable bindings, and dialogue policy all live in the
    ElevenLabs dashboard, keyed by the agent_id carried in the
    `bot.start` botId (`elevenlabs:<agent_id>`). The plugin's job is
    to (a) negotiate codecs, (b) move audio between Infinity's
    transport (PCMU / L16 / PCMA / G722 at 8 / 16 kHz) and ElevenLabs's
    fixed 16 kHz S16LE PCM, (c) translate transcript / tool / barge-in
    events on the EL WebSocket to the corresponding RCMS envelopes,
    and (d) emit spec-correct `bot.ended` shapes on every termination
    path (success, failure, caller disconnect, live-agent handoff).

    The agent-as-a-service distinction is the single most important
    framing for this module. There is no agent logic in the bridge:
    the LLM, prompt, voice, and platform tools all execute on
    ElevenLabs's side. The bridge runs three things — the audio
    pipeline, the RCMS envelope shaping, and the deferred-emit
    handoff drain — and nothing else.

Does not own:
    Provider routing by botId (owned by the bot dispatcher in
        bot_service.py — this plugin is registered against the
        `elevenlabs:` prefix and invoked through handle_message after
        the dispatcher has matched).
    RCMS session lifecycle, JWT authentication, and Infinity-side
        WebSocket transport (owned by bridge_server.py).
    Audio frame pacing and ingress queue management (owned by
        IngressStreamer in bridge_server.py — this plugin queues
        chunks via _send_ingress_chunked and otherwise stays out of
        the cadence path).
    Agent prompt, voice, native tool implementations, dialogue
        policy, and conversation memory (owned by ElevenLabs;
        configured per-agent in the ElevenLabs dashboard).

Dependencies:
    websockets: the upstream ElevenLabs Conversational AI WebSocket
        client.
    audioop (stdlib; on Python 3.13+ install audioop-lts as a
        drop-in): µ-law / A-law encode/decode and 8↔16 kHz
        resampling.
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
        _handle_bot_start, which validates the botId prefix,
        resolves the API key (botCredentials → ELEVENLABS_API_KEY),
        negotiates codec / sample rate, builds a BotConversation,
        connects upstream via _connect_elevenlabs (`xi-api-key`
        header, `?agent_id=` query, `conversation_initiation_client_data`
        as the first frame), launches _elevenlabs_recv_loop as a
        background task, and emits bot.started on success. Every
        validation failure emits bot.ended with a non-200 status
        via the failure-context helper; the workflow's IVA module
        consumes byobotEndContext.status.code to route to the
        FAILED branch.
    Phase 2 (During): ingest_audio_chunk transcodes Infinity-side
        audio to 16 kHz S16LE and forwards as user_audio_chunk.
        _elevenlabs_recv_loop drains the upstream WS and dispatches
        each frame to _handle_elevenlabs_message: audio frames are
        transcoded and queued via the IngressStreamer (gated by an
        ingress-readiness latch that buffers EL's opening greeting
        until Infinity's ingress path opens); user_transcript /
        agent_response become bot.feature TRANSCRIPT envelopes;
        interruption flushes the IngressStreamer queue;
        client_tool_call dispatches to bridge-implemented tools;
        agent_tool_response surfaces native EL system tool results
        (notably end_call's success-context emit).
    Phase 3 (Closure): four termination shapes converge on this
        plugin —
            (a) caller-disconnect → on_session_ended emits
                CALLER_DISCONNECTED via the disconnect-context
                helper;
            (b) Infinity-driven bot.end → _handle_bot_end acks
                with a manual bot.ended build;
            (c) ElevenLabs native end_call →
                _handle_elevenlabs_message's agent_tool_response
                branch emits success-context bot.ended directly;
            (d) clean upstream WS close →
                _elevenlabs_recv_loop's ConnectionClosedOK branch
                emits success-context bot.ended.
        Live-agent handoff is its own deferred path:
        client_tool_call(transfer_to_agent) stashes the payload on
        BotConversation, launches _wait_for_quiescence_and_emit to
        drain the IngressStreamer queue, then
        _emit_pending_handoff sends the bot.feature
        LIVE_AGENT_HANDOFF and a bot.ended shape with no
        payload.context.status (the absent-status row in the
        bot.ended schema, consumed via byobotLiveAgentHandoff).
        _shutdown_conversation is the single resource-release path
        invoked from every removal site.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start / bot.started
        / bot.end / bot.ended / bot.feature (TRANSCRIPT and
        LIVE_AGENT_HANDOFF ftypes).
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
    including bot.ended's payload.context.status nesting and the
    workflow's byobotEndContext / byobotLiveAgentHandoff consumption
    patterns.

    ElevenLabs Conversational AI WebSocket protocol — endpoint URL,
        xi-api-key auth header, conversation_initiation_client_data
        framing, and the message type catalog (audio,
        user_transcript, agent_response, interruption, ping/pong,
        client_tool_call, agent_tool_response,
        conversation_initiation_metadata).

See also:
    BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload field reference
    BUILDERS_GUIDE.md §3 Phase 2 — audio frames and IngressStreamer
    BUILDERS_GUIDE.md §3 Phase 3 — bot.ended status semantics and
        the four-shape termination ladder
"""

from __future__ import annotations

import asyncio
import audioop  # stdlib; on Python 3.13+ install audioop-lts as a drop-in replacement
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
    import truststore as _truststore
except ImportError:
    _truststore = None  # outbound EL TLS will fall back to default CA bundle

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


ELEVENLABS_WS_URL = "wss://api.elevenlabs.io/v1/convai/conversation"

# Native sample rate of the ElevenLabs Conversational AI WebSocket — both
# directions (user_audio_chunk in, audio_event out) are 16 kHz S16LE PCM.
# All Infinity-side codec / sample-rate combinations are converted to and
# from this rate by _prepare_input_audio / _transcode_output_audio.
_ELEVENLABS_SAMPLE_RATE = 16000

# Bound on the pre-ingress-ready buffer (see BotConversation.ingress_buffer
# and the gate in _handle_elevenlabs_message). Infinity's ingress path opens
# ~150 ms after ElevenLabs starts greeting; the buffer absorbs that gap.
# Cap is conservative — exceeding it indicates an upstream stall, not normal
# greeting overlap. Oldest chunk is dropped on overflow.
_INGRESS_BUFFER_MAX_CHUNKS = 50

# Polling cadence for the quiescence drain in _wait_for_quiescence_and_emit.
# Each iteration sleeps this long, then checks whether the IngressStreamer
# queue has gone empty. The sleep itself is the grace period — long enough
# for any chunk crossing the network during the iteration to land in the
# queue before the empty check.
_QUIESCENCE_POLL_MS = 250

# Safety net on the quiescence drain loop. Trips only on genuine upstream
# failure (e.g. ElevenLabs WS hung); a healthy drain completes in well
# under a second. Trip logs at WARNING and emits the handoff anyway.
_QUIESCENCE_DEADLOCK_SAFETY_S = 30.0

@dataclass
class BotConversation:
    """Per-call state for one Infinity-side endpoint bridged to one ElevenLabs Conversational AI session.

    Allocated in `_handle_bot_start` and stored in
    `ElevenLabsService._conversations` under `(session_id,
    endpoint_id)` for the lifetime of the call. Removed by
    `_handle_bot_end` (Infinity-driven teardown) or
    `on_session_ended` (caller disconnect / platform-driven session
    end). `_shutdown_conversation` is the single resource-release
    path; both removal sites call it.

    Field groups:

        * **Identity (set at construction, never mutated):**
          `session_id`, `endpoint_id`, `source` (rx / tx),
          `websocket` (the Infinity-side WS), `client_id`, `service`,
          `agent_id` (ElevenLabs agent), `language_code`,
          `codec_name`, `sample_rate`, `transport_encoding`,
          `dynamic_variables` (prompt-time variables sent in
          `conversation_initiation_client_data`).

        * **Upstream WebSocket:** `el_ws` (the
          `websockets.WebSocketClientProtocol` connected to
          ElevenLabs), `el_recv_task` (the background coroutine
          draining `el_ws`), `active` (cooperative shutdown flag
          consulted at the head of each recv-loop iteration).

        * **Termination tracking:** `bot_ended_sent` is flipped True
          by every code path that emits a terminal `bot.ended`
          (`send_bot_ended_with_*_context` helpers and the manual
          builds in `_handle_bot_end` / `_emit_pending_handoff`).
          Read by `on_session_ended` and `_emit_pending_handoff` to
          suppress duplicate emissions when an outcome has already
          been signalled.

        * **Audio codec state:** `ratecv_in_state` /
          `ratecv_out_state` thread `audioop.ratecv` calls (the
          stdlib resampler returns a fresh state per call and the
          two directions cannot share). `g722_decoder` /
          `g722_encoder` are lazily initialised when G722 is
          negotiated.

        * **Deferred live-agent handoff:** `pending_handoff` /
          `pending_handoff_args` carry the
          `bot.feature LIVE_AGENT_HANDOFF` payload while the
          IngressStreamer queue drains; `handoff_task` is the
          drain coroutine running `_wait_for_quiescence_and_emit`.
          The deferred path exists because ElevenLabs sends
          `client_tool_call` for `transfer_to_agent` before the
          transfer-announcement audio finishes streaming.

        * **Ingress readiness latch:** `ingress_ready` and
          `ingress_buffer` solve the timing skew where ElevenLabs
          greets within ~200 ms of connecting but Infinity's ingress
          path opens only after Infinity emits its first egress
          frame (~350 ms). Audio that lands before the latch flips
          is buffered (capped at `_INGRESS_BUFFER_MAX_CHUNKS`); the
          latch flips on the first call to `ingest_audio_chunk` and
          flushes the buffer.

        * **Turn-start timestamps:** `customer_turn_started_at` /
          `bot_turn_started_at` capture message-arrival time at the
          start of each transcript-handler branch, used as
          `payload.transcript.startTsMs`. ElevenLabs does not expose
          explicit turn-start events in its WS protocol, so this
          arrival-time approximation is the closest available
          anchor. Reset to None after each transcript flush.
    """

    session_id: str
    endpoint_id: str
    source: str
    websocket: WebSocketServerProtocol
    client_id: str
    service: str
    agent_id: str
    language_code: str
    codec_name: str
    sample_rate: int
    transport_encoding: str
    dynamic_variables: Dict[str, Any]
    el_ws: Optional[Any] = None
    el_recv_task: Optional[asyncio.Task] = None
    active: bool = False
    # Set True by send_bot_ended_with_*_context helpers after a successful
    # bot.ended emission (success / failure / disconnect). Read by
    # _shutdown_conversation to suppress duplicate disconnect emissions
    # when self-service-complete, handoff, or failure already signalled
    # the outcome.
    bot_ended_sent: bool = False
    # Resampling state (audioop.ratecv returns a new tuple each call).
    # Independent per direction — never share between in and out.
    ratecv_in_state: Any = None
    ratecv_out_state: Any = None
    # G722 codec objects (lazy-initialised; 16 kHz internal rate).
    g722_decoder: Any = None
    g722_encoder: Any = None
    # Pending live-agent handoff: ElevenLabs invokes `transfer_to_agent` via
    # `client_tool_call` before the pre-tool-speech audio finishes streaming,
    # so the bridge stashes the handoff payload and emits the wire-level
    # bot.feature LIVE_AGENT_HANDOFF only after the IngressStreamer queue
    # has drained. See `_handle_tool_call` and
    # `_wait_for_quiescence_and_emit` for the staged emit.
    pending_handoff: Optional[Dict[str, Any]] = None
    pending_handoff_args: Optional[Dict[str, Any]] = None
    handoff_task: Optional[asyncio.Task] = None
    # Ingress readiness: Infinity's ingress audio path only becomes ready after it begins
    # emitting egress (caller audio). ElevenLabs greets within ~200ms but Infinity's ingress
    # isn't open until ~350ms — greeting chunks sent before then are dropped. Buffer until
    # the first egress frame arrives, then flush.
    ingress_ready: bool = False
    ingress_buffer: list = field(default_factory=list)

    # Turn-start timestamps used as `startTsMs` on bot.feature TRANSCRIPT
    # emits. ElevenLabs does not expose an explicit turn-start event in
    # its WebSocket protocol — both `user_transcript` and `agent_response`
    # fire on turn completion. The bridge captures message-arrival time
    # at the start of the respective handler block as the closest available
    # turn-start approximation. Reset to None after each transcript flush.
    customer_turn_started_at: Optional[int] = None
    bot_turn_started_at: Optional[int] = None


class ElevenLabsService(ServicePlugin):
    """Service plugin that proxies an Infinity RCMS bot session to a hosted ElevenLabs Conversational AI agent.

    Plugin contract:
        Subclass of `ServicePlugin`. Discovered by the plugin loader
        at bridge startup if `is_configured()` returns True
        (`ELEVENLABS_API_KEY` set). Registered against the bridge's
        `ServiceRegistry` under the name `elevenlabs`. The bot
        dispatcher (`bot_service.py`) claims the RCMS `bot.start` /
        `bot.end` message types and routes per-call to this plugin
        based on the `elevenlabs:<agent_id>` botId prefix; the
        plugin itself reports an empty `message_types` set.

    Agent-as-a-service split:
        ElevenLabs hosts the agent — the system prompt, voice
        configuration, native system tools (end_call, etc.), tool
        schemas, dynamic-variable bindings, and dialogue policy all
        live in the ElevenLabs dashboard. The bridge does not run
        any agent logic; it is a thin protocol translator that:

            * Converts Infinity-side codec frames to/from
              16 kHz S16LE PCM (the fixed ElevenLabs WS format).
            * Forwards `client_tool_call` invocations to bridge-
              implemented tools (currently only `transfer_to_agent`)
              and replies with `client_tool_result`.
            * Translates ElevenLabs-side termination signals
              (`agent_tool_response` for `end_call`, clean WS close,
              ConnectionClosedError) into the corresponding Infinity-
              side `bot.ended`-with-context shapes.

        Native ElevenLabs system tools (`end_call`,
        `language_detection`, etc.) report through
        `agent_tool_response` and remain platform-side; the bridge
        observes the result but does not implement the tool.
        Bridge-implemented tools live in `_handle_tool_call`.

    Per-call state:
        `self._conversations` maps `(session_id, endpoint_id)` to
        a `BotConversation` instance for the lifetime of each call.
        Populated by `_handle_bot_start`, removed by
        `_handle_bot_end` and `on_session_ended`. The instance carries
        the upstream EL WebSocket, the EL recv-loop task, codec
        state, the deferred-handoff payload, and the ingress-readiness
        latch (see `BotConversation`).

    Lifecycle hooks:
        * `handle_message` — `bot.start` / `bot.end` dispatch.
        * `ingest_audio_chunk` — per-frame caller audio handoff
          to ElevenLabs.
        * `on_session_ended` — caller-disconnect /
          platform-initiated session end.
        * `shutdown` — bridge process shutdown.

    Spec:
        RCMS spec §AI Bot Message Definitions — `bot.start` /
        `bot.started` / `bot.end` / `bot.ended` / `bot.feature`.
        ElevenLabs Conversational AI WebSocket protocol — message
        type catalog and `xi-api-key` auth.
    """

    name = "elevenlabs"

    @classmethod
    def is_configured(cls) -> bool:
        """Return True iff `ELEVENLABS_API_KEY` is set in the bridge process environment.

        Consulted by the plugin loader at bridge startup. When False,
        the plugin is skipped — `bot.start` envelopes carrying an
        `elevenlabs:` `botId` will then surface as
        `BACKEND_START_FAILED` from the dispatcher because no plugin
        claims the prefix.

        Per-call API keys can also arrive on `payload.botCredentials`
        (see `_extract_api_key`); the env-var check here is the
        startup-time gate, not the only source of credentials.
        """
        return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())

    def __init__(self, server):
        """Initialise per-plugin state used across the call lifecycle.

        Sets up the per-(session_id, endpoint_id) conversation map.
        Keys follow the `_key` convention; values are
        `BotConversation` instances, populated by `_handle_bot_start`
        and removed by `_handle_bot_end` /
        `_shutdown_conversation`.

        Args:
            server: The owning `BridgeServer`. Stored on `self.server`
                by the `ServicePlugin` base.
        """
        super().__init__(server)
        self._conversations: Dict[str, BotConversation] = {}

    @property
    def message_types(self) -> set[str]:
        """Return the empty set — this plugin does not register any RCMS message types directly.

        The bot dispatcher (`bot_service.py:CombinedBotService`)
        claims `bot.start` / `bot.end` with the `ServiceRegistry` and
        routes to provider plugins by `botId` prefix. This plugin is
        invoked through `handle_message` only after that prefix-match
        has already happened, so it does not need its own message-type
        claim.
        """
        return set()

    def _key(self, session_id: str, endpoint_id: str) -> str:
        """Build the `(session_id, endpoint_id)` lookup key for `self._conversations`.

        A session can host multiple endpoints concurrently (one
        Infinity session, multiple media legs), so the
        composite key is required to keep per-endpoint state distinct.
        """
        return f"{session_id}:{endpoint_id}"

    def _resolve_codec(self, session_id: str) -> str:
        """Return the negotiated codec name for `session_id`, or `"L16"` if no negotiation has stored one.

        Reads `BridgeServer.session_config[session_id]["codec_name"]`,
        which is populated during `session.start` codec negotiation
        in `BridgeServer.handle_session_start`. Caller upper-cases
        the value before comparing.
        """
        return self.server.session_config.get(session_id, {}).get("codec_name", "L16")

    def _resolve_sample_rate(self, session_id: str, payload: Dict[str, Any]) -> int:
        """Return the negotiated sample rate, preferring `payload.sampleRate`, then session config, then 8000.

        Resolution order matches the precedence of explicit per-call
        override (`payload.sampleRate` from `bot.start`) over
        session-wide negotiation
        (`session_config[session_id]["sample_rate"]`) over the codec-
        agnostic default. The final value flows into
        `BotConversation.sample_rate` and is read by the audio
        transcoders to drive resampling.
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
            (`bot_service.py:_handle_bot_start` and `_handle_bot_end`)
            after it has matched the inbound `botId` prefix
            (`elevenlabs:`) against the registered providers and
            selected this plugin. The dispatcher hands off the raw
            envelope; this method switches on `data["type"]` and
            forwards.

            Only `bot.start` and `bot.end` are expected — the
            dispatcher's prefix-routing contract guarantees no other
            type ever lands here. Anything else logs at WARNING and is
            dropped (the catch-all is defensive: a future dispatcher
            change must not silently misroute frames).

        Args:
            websocket: The Infinity-side WebSocket the message arrived
                on; passed through to the matching handler.
            client_id: Connection identifier for log lines and the
                outbound sequence counter.
            data: The decoded RCMS envelope.

        Returns:
            None. All effects happen inside `_handle_bot_start` /
            `_handle_bot_end`.
        """
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_bot_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_bot_end(websocket, client_id, data)
        else:
            logger.warning("[%s] Unhandled message '%s' in ElevenLabs service", client_id, msg_type)

    async def on_session_ended(self, session_id: str) -> None:
        """Phase 3 hook — emit `bot.ended` (CALLER_DISCONNECTED) for any active EL conversation under this session.

        Contract:
            Plugin lifecycle hook fired by `BridgeServer` when an
            inbound `session.end` arrives on the Infinity WebSocket
            (caller disconnect or platform-initiated session close).
            Walks `self._conversations` for every key prefixed with
            `f"{session_id}:"` — there can be multiple endpoints under
            one session — and for each conversation:

                1. If `bot_ended_sent` is False (no termination signal
                   has gone out yet), emit `bot.ended` with
                   `payload.context.status` carrying
                   `CALLER_DISCONNECTED` via
                   `send_bot_ended_with_disconnect_context`. Without
                   this, the IVA module records a UC3 workflow error
                   ("session ended with no bot.ended on an active bot
                   session"). The helper sets `bot_ended_sent` itself
                   so subsequent code paths see the flag flipped.
                2. Call `_shutdown_conversation` to release the
                   per-conversation resources (handoff drain task,
                   IngressStreamer queue, EL recv loop, EL WS).

            **Why this method emits and `_shutdown_conversation` does
            not.** `_shutdown_conversation` is also called from
            `_handle_bot_end`, whose own `bot.ended` ack is a separate
            emission with a different payload shape — placing the
            disconnect emit inside the shared shutdown path would
            cause `_handle_bot_end` to send two `bot.ended` envelopes
            tagged differently. Keeping the disconnect emit here means
            it only fires on the caller-disconnect path.

        Spec:
            RCMS spec §Session Message Definitions — `session.end`.
            RCMS spec §AI Bot Message Definitions — `bot.ended` with
            `CALLER_DISCONNECTED` reason. See
            `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context".

        Args:
            session_id: The RCMS session identifier whose `session.end`
                triggered this hook.

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
                # Caller disconnect / platform-initiated session end. Emit
                # bot.ended with CALLER_DISCONNECTED if no termination signal
                # has been sent yet on this conversation. Without this, the
                # workflow sees a UC3 error (session ended with no bot.ended).
                # Placed here rather than in _shutdown_conversation because
                # _shutdown_conversation is also called from _handle_bot_end,
                # whose bot.end ack is a separate bot.ended emission and must
                # not be tagged CALLER_DISCONNECTED. Placing the disconnect-
                # context emit here (rather than in _shutdown_conversation)
                # ensures the CALLER_DISCONNECTED reason only fires on the
                # caller-disconnect path.
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
        """Tear down every active ElevenLabs conversation — called on bridge process shutdown.

        Contract:
            ServicePlugin shutdown hook. Iterates a snapshot of
            `self._conversations.keys()` (snapshot because
            `_shutdown_conversation` mutates the dict) and calls
            `_shutdown_conversation` on each entry. Does NOT emit any
            outbound `bot.ended` — process shutdown is its own teardown
            shape, distinct from the per-call termination contract:
            Infinity sees the WebSocket close at the transport layer
            and reconciles via its own session timeout path.

            Idempotent and exception-tolerant: each conversation's
            cleanup path swallows its own errors, so this method
            always completes.

        Returns:
            None. Effects are confined to closing per-conversation
            resources: handoff drain tasks cancelled, IngressStreamer
            queues drained, EL recv loops cancelled, EL WebSockets
            closed.
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
        """Handle Infinity-driven `bot.start` for an ElevenLabs agent: validate, connect, and ack.

        Contract:
            Phase 1 entry point dispatched by `handle_message` when the
            inbound message type is `bot.start` and the dispatcher has
            routed by `botId` prefix. Performs the full start-of-call
            sequence:

                1. **Validate `endpointId`**: missing endpointId fails
                   the `bot.ended` schema, so respond with
                   `session.error` (`MISSING_REQUIRED_FIELDS`, 501)
                   instead. Mirrors the dispatcher's
                   `MISSING_REQUIRED_FIELDS` exception in
                   `bot_service.py:_handle_bot_start`.
                2. **Validate `botId` prefix and agent id**: must
                   start with `elevenlabs:` and carry a non-empty
                   suffix. Failures here are emitted as `bot.ended`
                   with `BAD_REQUEST` (400) /
                   `UNRECOGNIZED_BOTID_PREFIX` via the failure-context
                   helper.
                3. **Resolve API key**: prefer
                   `payload.botCredentials.apiKey`
                   (`_extract_api_key`); fall back to the
                   `ELEVENLABS_API_KEY` environment variable. Neither
                   present → 503 `BACKEND_START_FAILED`.
                4. **Resolve codec / sample rate**: from
                   `session_config[session_id]` and the inbound
                   payload. Reject unsupported codecs (anything outside
                   PCMU / L16 / PCMA / G722) with 503
                   `BACKEND_START_FAILED`. Reject G722 negotiation
                   when the optional G722 package is not installed,
                   with the same status.
                5. **Build `BotConversation`** from negotiated codec,
                   transport encoding from
                   `server.transport_encodings`, and a
                   `dynamic_variables` dict carrying call_to,
                   call_from, ucid, call_direction, language, domain,
                   and any `payload.context` overlay. These become
                   prompt-time variables on the ElevenLabs side at
                   conversation initiation.
                6. **G722 decoder lazy-init** when negotiated:
                   instantiate the decoder up front; the encoder is
                   created on demand by `_transcode_output_audio`.
                   Decoder init failure → 503
                   `BACKEND_START_FAILED`.
                7. **Connect to ElevenLabs** via `_connect_elevenlabs`.
                   Failure here → 503 `BACKEND_START_FAILED`.
                8. **Replace any pre-existing conversation** under the
                   same `(session_id, endpoint_id)` by calling
                   `_shutdown_conversation` on the prior entry —
                   protects against duplicate `bot.start` racing the
                   prior session's teardown.
                9. **Activate**: set `convo.active = True`, store in
                   `self._conversations`, and start
                   `_elevenlabs_recv_loop` as a background task.
               10. **Emit `bot.started`** ack on the Infinity side —
                   manual envelope build (the helper family is for
                   `bot.ended`).

            Every error path uses
            `send_bot_ended_with_failure_context` with the appropriate
            RCMS status code, so the workflow's `byobotEndContext`
            consumption pattern routes the bot session to a non-200
            terminal state. Per the RCMS spec, an unprocessable
            `bot.start` is failed via `bot.ended`-with-status, *not*
            `session.error`; the only `session.error` exit is the
            schema-impossible `MISSING_REQUIRED_FIELDS: endpointId`
            case in step 1.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.start`
            payload fields and `bot.started` ack shape.
            RCMS spec §Error Handling — failure-shape contract for
            unprocessable `bot.start` answered via `bot.ended`-with-
            status.
            RCMS spec §Status Codes — 400 / 501 / 503 codes consumed
            by the failure helpers.
            See `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context".

        Args:
            websocket: The Infinity-side WebSocket; the `bot.started`
                ack and any failure `bot.ended` are sent here.
            client_id: Connection identifier used in log lines and the
                outbound sequence counter.
            data: Decoded `bot.start` envelope. `sessionId`,
                `service`, `payload.endpointId`,
                `payload.botId`, `payload.source`,
                `payload.botCredentials`, `payload.language`,
                `payload.context`, `payload.to`, `payload.from`,
                `payload.ucid`, `payload.direction`, `payload.domain`,
                and `payload.sampleRate` are read.

        Returns:
            None. Side effects: optional `session.error` /
            `bot.ended`-with-failure send on validation failure;
            `BotConversation` registered in `self._conversations`;
            background EL recv loop launched; `bot.started` ack sent
            on success.
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

        if not bot_id.startswith("elevenlabs:"):
            # RCMS §Error Handling: an unprocessable bot.start is failed via
            # bot.ended-with-status, not session.end. send_bot_ended_with_failure_context
            # emits the spec-compliant shape; the IVA module's FAILED branch wires
            # to bot.ended-with-non-200-status.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=400,
                reason="BAD_REQUEST",
                description=f"UNRECOGNIZED_BOTID_PREFIX: ElevenLabs plugin requires botId prefix 'elevenlabs:', got '{bot_id}'",
            )
            return
        agent_id = bot_id[len("elevenlabs:"):].strip()
        if not agent_id:
            # RCMS §Error Handling — see canonical comment at line 202 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=400,
                reason="BAD_REQUEST",
                description="UNRECOGNIZED_BOTID_PREFIX: ElevenLabs botId missing agent id after prefix",
            )
            return

        api_key = self._extract_api_key(payload) or os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            # RCMS §Error Handling — see canonical comment at line 202 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: No ElevenLabs API key (botCredentials missing and ELEVENLABS_API_KEY not set)",
            )
            return

        codec_name = self._resolve_codec(session_id).upper()
        sample_rate = self._resolve_sample_rate(session_id, payload)

        if codec_name not in ("PCMU", "L16", "PCMA", "G722"):
            # RCMS §Error Handling — see canonical comment at line 202 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: Unsupported codec '{codec_name}' for ElevenLabs provider",
            )
            return

        if codec_name == "G722" and not G722_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 202 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: Codec G722 negotiated but g722 package is not installed on the bridge server",
            )
            return

        language_code = payload.get("language") or "en-US"
        context = payload.get("context") or {}
        dynamic_variables: Dict[str, Any] = {
            "call_to": payload.get("to", ""),
            "call_from": payload.get("from", ""),
            "ucid": payload.get("ucid", ""),
            "call_direction": payload.get("direction", "INBOUND"),
            "language": language_code,
            "domain": payload.get("domain", ""),
        }
        if isinstance(context, dict):
            dynamic_variables.update(context)

        logger.info(
            "[%s] ElevenLabs bot.start session=%s endpoint=%s agent=%s codec=%s/%d source=%s",
            client_id, session_id, endpoint_id, agent_id, codec_name, sample_rate, source,
        )

        convo = BotConversation(
            session_id=session_id,
            endpoint_id=endpoint_id,
            source=source,
            websocket=websocket,
            client_id=client_id,
            service=service,
            agent_id=agent_id,
            language_code=language_code,
            codec_name=codec_name,
            sample_rate=sample_rate,
            transport_encoding=self.server.transport_encodings.get(session_id, "base64"),
            dynamic_variables=dynamic_variables,
        )

        if codec_name == "G722" and G722_AVAILABLE:
            try:
                import G722 as g722  # type: ignore

                convo.g722_decoder = g722.G722(sample_rate=16000, bit_rate=64000)
            except Exception as exc:
                logger.error("[%s] Failed to initialise G722 decoder: %s", client_id, exc)
                # RCMS §Error Handling — see canonical comment at line 202 above.
                await self.server.send_bot_ended_with_failure_context(
                    convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                    code=503,
                    reason="SERVICE_UNAVAILABLE",
                    description=f"BACKEND_START_FAILED: G722 decoder init failed: {exc}",
                    convo=convo,
                )
                return

        try:
            await self._connect_elevenlabs(convo, api_key)
        except Exception as exc:
            logger.error("[%s] Failed to connect to ElevenLabs: %s", client_id, exc, exc_info=True)
            # RCMS §Error Handling — see canonical comment at line 202 above.
            await self.server.send_bot_ended_with_failure_context(
                convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: ElevenLabs connect failed: {exc}",
                convo=convo,
            )
            return

        key = self._key(session_id, endpoint_id)
        existing = self._conversations.pop(key, None)
        if existing:
            await self._shutdown_conversation(existing)
        convo.active = True
        self._conversations[key] = convo
        convo.el_recv_task = asyncio.create_task(self._elevenlabs_recv_loop(convo))

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

    def _extract_api_key(self, payload: Dict[str, Any]) -> Optional[str]:
        """Decode an ElevenLabs `xi-api-key` from the `bot.start` payload's `botCredentials` field.

        Contract:
            `payload.botCredentials` is a base64-encoded JSON object
            (per RCMS spec §AI Bot Message Definitions). Decode the
            base64, parse the JSON, and return `obj["apiKey"]` if it
            is a non-empty string. Whitespace is stripped from the
            returned value.

            Returns `None` (not raises) on every error path so the
            caller can fall back to the `ELEVENLABS_API_KEY` environment
            variable — the bridge supports either source. Specific
            outcomes:

                * Field absent → `None` (silent).
                * Decoded but no `apiKey` field → log at WARNING,
                  return `None`.
                * base64 / JSON / decode error → log at ERROR, return
                  `None`.

            The caller (`_handle_bot_start`) treats the disjoint
            "neither source produced a key" outcome as a 503
            `BACKEND_START_FAILED` and emits the failure-context
            `bot.ended`.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.start`
            payload, `botCredentials` field as base64-wrapped JSON.

        Args:
            payload: The decoded `bot.start` payload object.

        Returns:
            The stripped API key string when present and well-formed,
            otherwise `None`.
        """
        creds_b64 = payload.get("botCredentials")
        if not creds_b64:
            return None
        try:
            raw = base64.b64decode(creds_b64).decode("utf-8")
            obj = json.loads(raw)
            key = obj.get("apiKey") if isinstance(obj, dict) else None
            if isinstance(key, str) and key.strip():
                return key.strip()
            logger.warning("botCredentials decoded but no apiKey field present")
            return None
        except Exception as exc:
            logger.error("Failed to decode botCredentials: %s", exc)
            return None

    # ------------------------------------------------------------------ bot.end

    async def _handle_bot_end(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """Handle Infinity-driven `bot.end` for an ElevenLabs conversation: tear down upstream and ack.

        Contract:
            Inverse of `_handle_bot_start`. Two outcomes:

                * **Missing `endpointId`**: the `bot.ended` schema
                  requires `endpointId`, so the failure cannot be
                  expressed via `bot.ended`; respond with
                  `session.error` (`MISSING_REQUIRED_FIELDS`,
                  status 501) instead. Mirrors the dispatcher's
                  rejection path in `bot_service.py:_handle_bot_start`
                  for the symmetric case on `bot.start`.
                * **Normal teardown**: pop the conversation from
                  `self._conversations` (warn if absent, e.g. the
                  caller already disconnected), call
                  `_shutdown_conversation` to cancel any pending
                  handoff drain, drain and close the IngressStreamer,
                  cancel the EL recv loop, and close the upstream
                  WS. Then send `bot.ended` directly (manual envelope
                  build — this is the bot.end ack shape, with no
                  `payload.context.status`; the
                  `send_bot_ended_with_*_context` helpers all originate
                  a status object and so do not fit this path).

            **Why `bot_ended_sent` is set explicitly here.** The
            `send_bot_ended_with_disconnect_context` helper flips this
            flag as part of its emit; the manual build here bypasses
            the helper, so `on_session_ended` (which would otherwise
            try to emit a disconnect-context `bot.ended` on the
            following `session.end`) needs the flag set explicitly to
            avoid a duplicate emission.

            `payload.context` from the inbound `bot.end` is preserved
            on the outbound `bot.ended` ack so any caller-specified
            metadata round-trips back as the spec allows.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.ended`
            envelope shape and `payload.endpointId` requirement.
            RCMS spec §Error Handling — `session.error` is the correct
            response when an inbound message fails schema validation
            so badly that the ack envelope itself cannot be built.
            See `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context".

        Args:
            websocket: The Infinity-side WebSocket the inbound
                `bot.end` arrived on; the ack is sent on the same
                connection.
            client_id: Connection identifier used in log lines and the
                outbound sequence counter.
            data: Decoded `bot.end` envelope. `sessionId`,
                `payload.endpointId`, `payload.context`, `service`, and
                `sequenceNum` are read.

        Returns:
            None. Side effects: optional `session.error` send;
            `_shutdown_conversation` for the matching conversation;
            outbound `bot.ended` send; `convo.bot_ended_sent` set
            after the send.
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
            logger.warning("[%s] No active ElevenLabs convo for %s:%s", client_id, session_id, endpoint_id)

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
            `self._conversations`. Returns `False` immediately if the
            conversation is missing, inactive, sourced from the wrong
            direction (`convo.source != source`), or has no upstream
            ElevenLabs WebSocket — the bridge falls through to other
            registered services on `False` (the dispatcher's
            multi-plugin fan-out contract).

            **Ingress-ready latch.** The first call to this method on a
            conversation marks `convo.ingress_ready = True` and flushes
            anything that `_handle_elevenlabs_message` had buffered
            into `convo.ingress_buffer` while waiting for Infinity's
            ingress path to open. The buffer holds ElevenLabs's
            opening greeting audio, which lands ~150 ms before
            Infinity emits its first egress frame; without the latch +
            buffer pair, those greeting chunks would be discarded.
            See `_handle_elevenlabs_message` for the gating side.

            After the latch handling, the inbound bytes are converted
            to 16 kHz S16LE PCM by `_prepare_input_audio`. If
            transcoding fails (returns `None`), this method returns
            `False`. Otherwise the PCM is base64-wrapped in a
            `user_audio_chunk` envelope and sent on `convo.el_ws`. WS
            send failures log at ERROR and return `False`.

        Args:
            session_id: RCMS session identifier from the originating
                `bot.start`.
            endpoint_id: Media endpoint identifier from the originating
                `bot.start`.
            source: Frame direction sentinel (`"rx"` or `"tx"`); must
                match `convo.source`. Mismatches return `False` so
                the dispatcher can route the frame elsewhere.
            audio_bytes: Raw frame payload from the Infinity media
                transport (codec-encoded per `convo.codec_name`).

        Returns:
            True iff the frame was successfully transcoded and sent
            to ElevenLabs. False on every reject / failure path.
        """
        convo = self._conversations.get(self._key(session_id, endpoint_id))
        if not convo or not convo.active or convo.source != source or not convo.el_ws:
            return False

        # First egress frame from Infinity signals the ingress path is open. Flush any
        # ElevenLabs audio that was buffered while we waited.
        if not convo.ingress_ready:
            convo.ingress_ready = True
            if convo.ingress_buffer:
                logger.info(
                    "[%s] Infinity ingress ready — flushing %d buffered audio chunks",
                    convo.client_id, len(convo.ingress_buffer),
                )
                for buffered in convo.ingress_buffer:
                    await self._send_ingress_chunked(convo, buffered)
                convo.ingress_buffer.clear()

        pcm_16k = self._prepare_input_audio(convo, audio_bytes)
        if not pcm_16k:
            return False

        try:
            msg = {"user_audio_chunk": base64.b64encode(pcm_16k).decode("ascii")}
            await convo.el_ws.send(json.dumps(msg))
        except Exception as exc:
            logger.error("[%s] Send to ElevenLabs failed: %s", convo.client_id, exc)
            return False
        return True

    def _prepare_input_audio(self, convo: BotConversation, audio_bytes: bytes) -> Optional[bytes]:
        """Convert an inbound Infinity-side audio frame to 16 kHz S16LE PCM for ElevenLabs.

        Contract:
            Inbound (Infinity → ElevenLabs) counterpart of
            `_transcode_output_audio`. Branches on
            `convo.codec_name.upper()`:

                * **L16**: pass through if `sample_rate == 16000`;
                  otherwise resample to 16 kHz via `audioop.ratecv`,
                  threading `convo.ratecv_in_state`.
                * **PCMU**: decode µ-law (`audioop.ulaw2lin`) at
                  8 kHz, then resample to 16 kHz.
                * **PCMA**: decode A-law (`audioop.alaw2lin`) at
                  8 kHz, then resample to 16 kHz.
                * **G722**: decode via `convo.g722_decoder` (lazily
                  initialised in `_handle_bot_start` when G722 is
                  negotiated) into 16 kHz S16LE; the G722 module
                  yields a numpy view, materialised via `.tobytes()`.

            `ratecv_in_state` is independent of `ratecv_out_state` —
            audioop returns a fresh state per call and the two
            directions cannot share it.

            Errors from `audioop` / G722 are logged at ERROR and
            yield `None`. Unsupported codec names log at WARNING and
            yield `None`. The caller drops the chunk on `None`.

        Args:
            convo: The active conversation. Provides `codec_name`,
                `sample_rate`, `g722_decoder`, and the resampler
                state; the latter two are mutated.
            audio_bytes: Raw inbound frame bytes from Infinity. Empty
                buffers return `None`.

        Returns:
            16 kHz S16LE little-endian PCM, mono, ready for the
            ElevenLabs `user_audio_chunk` envelope, or `None` on
            transcoding failure or unsupported codec.
        """
        if not audio_bytes:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate
        try:
            if codec == "L16":
                if rate == 16000:
                    return audio_bytes
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    audio_bytes, 2, 1, rate, 16000, convo.ratecv_in_state
                )
                return pcm
            if codec == "PCMU":
                pcm8 = audioop.ulaw2lin(audio_bytes, 2)
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    pcm8, 2, 1, 8000, 16000, convo.ratecv_in_state
                )
                return pcm
            if codec == "PCMA":
                pcm8 = audioop.alaw2lin(audio_bytes, 2)
                pcm, convo.ratecv_in_state = audioop.ratecv(
                    pcm8, 2, 1, 8000, 16000, convo.ratecv_in_state
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
        self, convo: BotConversation, pcm_16k: bytes
    ) -> Optional[bytes]:
        """Convert 16 kHz S16LE PCM from ElevenLabs into the Infinity-side codec for this call.

        Contract:
            Outbound (ElevenLabs → Infinity) counterpart of
            `_prepare_input_audio`. Branches on
            `convo.codec_name.upper()`:

                * **L16**: pass through if `sample_rate == 16000`;
                  otherwise resample 16 kHz → `convo.sample_rate` via
                  `audioop.ratecv`, threading `convo.ratecv_out_state`.
                * **PCMU**: resample 16 kHz → 8 kHz, then encode µ-law
                  (`audioop.lin2ulaw`).
                * **PCMA**: resample 16 kHz → 8 kHz, then encode A-law
                  (`audioop.lin2alaw`).
                * **G722**: lazy-init `convo.g722_encoder` on first
                  call (the encoder is gated by module-level
                  `G722_AVAILABLE`; `_handle_bot_start` rejects the
                  session at start time if the package is missing).
                  Encode via the G722 module's `encode` on a numpy
                  int16 view.

            `ratecv_out_state` is independent of `ratecv_in_state` —
            audioop returns a fresh state per call and the two
            directions cannot share it.

            Errors from `audioop` / G722 / numpy import are logged at
            ERROR and yield `None`. Unsupported codec names log at
            WARNING and yield `None`. The caller drops the chunk on
            `None`.

        Args:
            convo: The active conversation. Provides `codec_name`,
                `sample_rate`, `g722_encoder`, and the resampler
                state; the latter two are mutated.
            pcm_16k: Raw 16 kHz S16LE little-endian PCM, mono. Empty
                buffers return `None`.

        Returns:
            Bytes ready for the Infinity wire (PCM, µ-law, A-law, or
            G.722 encoded), or `None` on transcoding failure or
            unsupported codec.
        """
        if not pcm_16k:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate
        try:
            if codec == "L16":
                if rate == 16000:
                    return pcm_16k
                pcm, convo.ratecv_out_state = audioop.ratecv(
                    pcm_16k, 2, 1, 16000, rate, convo.ratecv_out_state
                )
                return pcm
            if codec == "PCMU":
                pcm8, convo.ratecv_out_state = audioop.ratecv(
                    pcm_16k, 2, 1, 16000, 8000, convo.ratecv_out_state
                )
                return audioop.lin2ulaw(pcm8, 2)
            if codec == "PCMA":
                pcm8, convo.ratecv_out_state = audioop.ratecv(
                    pcm_16k, 2, 1, 16000, 8000, convo.ratecv_out_state
                )
                return audioop.lin2alaw(pcm8, 2)
            if codec == "G722":
                if not G722_AVAILABLE:
                    return None
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

    async def _send_ingress_chunked(
        self, convo: BotConversation, audio_bytes: bytes
    ) -> None:
        """Forward a single audio chunk to Infinity via the IngressStreamer's paced sender.

        Contract:
            Hands the chunk to `IngressStreamer.queue_audio`, which
            re-emits frames at the negotiated chunk_duration_ms cadence.
            The paced path is the only correct one for TTS audio:
            calling `send_immediate` in a sub-chunk loop would dump
            audio at multiples of real-time and break barge-in coherence
            (the IngressStreamer's `barge_in` flush is responsible for
            cancelling the bot's in-flight playback when the caller
            interrupts, and that mechanism assumes the queue is the
            source of truth for what is still pending).

            Errors from `queue_audio` are logged at WARNING and
            swallowed — a per-chunk send failure should not tear the
            conversation down.

        Args:
            convo: The active conversation. Provides the bridge-side
                websocket, identifiers, and transport encoding.
            audio_bytes: Codec-converted PCM/encoded audio ready for
                the wire (output of `_transcode_output_audio`). Empty
                buffers return immediately.

        Returns:
            None. The send is fire-and-forget from the caller's
            perspective; pacing happens inside the IngressStreamer.
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
            logger.warning("[%s] queue_audio failed: %s", convo.client_id, exc)

    # ------------------------------------------------------------------ ElevenLabs WS

    async def _connect_elevenlabs(self, convo: BotConversation, api_key: str) -> None:
        """Open the upstream ElevenLabs Conversational AI WebSocket and send the initiation frame.

        Contract:
            Two-step connect:

                1. Open `wss://api.elevenlabs.io/v1/convai/conversation`
                   with `?agent_id=` from `convo.agent_id` and the API
                   key in the `xi-api-key` header. `max_size=2**23`
                   (8 MiB) accommodates large audio frames.
                2. Immediately send a
                   `conversation_initiation_client_data` frame carrying
                   `convo.dynamic_variables` (call_to, call_from, ucid,
                   call_direction, language, domain, plus any
                   `payload.context` overlay from `bot.start`). These
                   become the agent's prompt-time variables in the
                   ElevenLabs platform.

            Stores the connected websocket on `convo.el_ws`. On any
            failure during connect or initiation, the exception
            propagates to `_handle_bot_start`, which translates it into
            `bot.ended` with `BACKEND_START_FAILED` via the failure-
            context helper.

            **TLS context selection.** When the optional `truststore`
            package is available, an outbound TLS context backed by the
            OS trust store is built per call so corporate TLS-interception
            roots (e.g. Zscaler) are honored. Scoping to this call site
            avoids globally replacing `ssl.SSLContext`, which would
            break the server-side WSS context the bridge accepts inbound
            connections on. When `truststore` is not installed,
            `_ssl.create_default_context()` falls back to the Python CA
            bundle.

        Spec:
            ElevenLabs Conversational AI WebSocket — connection
            endpoint, `xi-api-key` auth header, and the
            `conversation_initiation_client_data` frame shape.

        Args:
            convo: The conversation receiving the connection. Mutated:
                `el_ws` is set on success.
            api_key: ElevenLabs `xi-api-key` value, sourced from
                `botCredentials.apiKey` or the `ELEVENLABS_API_KEY`
                environment variable.

        Returns:
            None. Raises any websockets / TLS / send error to the
            caller; no bridge-side bot.ended is emitted from here.
        """
        url = f"{ELEVENLABS_WS_URL}?agent_id={convo.agent_id}"
        if _truststore is not None:
            ssl_context = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        else:
            ssl_context = _ssl.create_default_context()
        convo.el_ws = await websockets.connect(
            url,
            additional_headers={"xi-api-key": api_key},
            max_size=2**23,  # 8 MiB; audio frames can be large
            ssl=ssl_context,
        )
        init = {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": convo.dynamic_variables,
        }
        await convo.el_ws.send(json.dumps(init))
        logger.info(
            "[%s] Connected to ElevenLabs agent=%s (dynamic_vars=%s)",
            convo.client_id, convo.agent_id, list(convo.dynamic_variables.keys()),
        )

    async def _elevenlabs_recv_loop(self, convo: BotConversation) -> None:
        """Drain the ElevenLabs WebSocket and dispatch each frame; emit `bot.ended` on clean upstream close.

        Contract:
            Long-lived coroutine started from `_handle_bot_start` and
            cancelled from `_shutdown_conversation`. Iterates `convo.el_ws`,
            JSON-decodes each frame, and hands it to
            `_handle_elevenlabs_message`. Non-JSON frames are logged at
            WARNING and skipped — never raised. The loop ends on one of:

                * `convo.active = False` observed at the iteration head
                  (cooperative exit driven by `_shutdown_conversation`).
                * `asyncio.CancelledError` — re-raised so the cancelling
                  coroutine sees it. No bridge emission on this path:
                  `_shutdown_conversation` cancels this task *after* its
                  own bot.ended emission has already gone out, so
                  emitting again would duplicate.
                * `ConnectionClosedOK` (close codes 1000/1001) — the
                  upstream half of "self-service complete." The natural
                  cause is the agent's native End Call system tool
                  firing (configured in the ElevenLabs agent dashboard,
                  not in the bridge); emit success-context `bot.ended`
                  via the helper, after which Infinity drives
                  `session.end` and the workflow routes the IVA module
                  via SUCCESSFUL.
                * `ConnectionClosed` (any non-OK close) — log only.
                  Genuine upstream failures are observable via Infinity's
                  caller-hangup or session-timeout path; emitting
                  `bot.ended` here would race against that path.

            **Why a dedicated `ConnectionClosedOK` branch.** Three
            distinct teardown shapes converge on this method, and only
            one of them should emit:

                1. **Upstream clean close** (this branch): ElevenLabs
                   ended the conversation. Bridge originates `bot.ended`
                   with success context.
                2. **Upstream error close**: bridge stays silent. Avaya
                   drives the teardown via caller hangup or timeout.
                3. **Bridge-driven teardown**: `_shutdown_conversation`
                   cancels this task *before* `ws.close()`, so it
                   surfaces here as `CancelledError`, not as
                   `ConnectionClosed` — no risk of redundant emission.

            The `finally` block flips `convo.active = False` on every
            exit path so any in-flight ingest sees the dead conversation
            and stops sending.

        Spec:
            ElevenLabs Conversational AI WebSocket protocol — message
            framing.
            RCMS spec §AI Bot Message Definitions — `bot.ended` with
            success-context status. See `bridge/schema/rcms.schema.md`
            "bot.ended — status is nested in context".

        Args:
            convo: The conversation whose upstream WebSocket this loop
                drains. Mutated only on exit (`convo.active = False`).

        Returns:
            None. Cancellation re-raises; all other exceptions are
            logged and swallowed so the bridge does not crash on
            upstream protocol errors.
        """
        try:
            async for raw in convo.el_ws:
                if not convo.active:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.warning("[%s] Non-JSON frame from ElevenLabs", convo.client_id)
                    continue
                await self._handle_elevenlabs_message(convo, msg)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosedOK as exc:
            # Clean upstream close = self-service-complete: ElevenLabs
            # ended the conversation (typically the agent's native
            # End Call system tool firing). Emit success-context
            # bot.ended; Infinity then drives session.end and the
            # workflow routes via SUCCESSFUL. The branch-split rationale
            # — why this case emits and the other ConnectionClosed
            # cases do not — is in this method's header docstring.
            logger.info("[%s] ElevenLabs WS closed cleanly: %s", convo.client_id, exc)
            try:
                await self.server.send_bot_ended_with_success_context(
                    convo.websocket,
                    convo.client_id,
                    convo.session_id,
                    convo.endpoint_id,
                    description="ELEVENLABS: Self-service interaction completed.",
                    service=convo.service,
                    convo=convo,
                )
            except Exception as emit_exc:
                logger.warning(
                    "[%s] success-context bot.ended emit failed: %s",
                    convo.client_id, emit_exc,
                )
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("[%s] ElevenLabs WS closed: %s", convo.client_id, exc)
        except Exception as exc:
            logger.error("[%s] ElevenLabs recv loop error: %s", convo.client_id, exc, exc_info=True)
        finally:
            convo.active = False

    async def _handle_elevenlabs_message(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a single decoded message from the ElevenLabs WebSocket to its handler branch.

        Contract:
            Single switch on `msg["type"]`. Each branch is one of the
            ElevenLabs Conversational AI WebSocket message types and
            performs its work inline (audio playback, transcript emit,
            barge-in, tool dispatch, ping reply, log) before returning.
            Unknown types are logged at DEBUG and dropped — the bridge
            does not echo unrecognized frames back to ElevenLabs.

            Branches handled, in declaration order:

                * **`audio`**: 16 kHz S16LE base64 PCM. Decoded,
                  transcoded to the negotiated Infinity codec via
                  `_transcode_output_audio`, and either forwarded
                  immediately via `_send_ingress_chunked` or buffered
                  on `convo.ingress_buffer` while
                  `convo.ingress_ready` is False (Infinity's ingress
                  path opens after its first egress frame; ElevenLabs
                  greets earlier than that). Buffer is bounded by
                  `_INGRESS_BUFFER_MAX_CHUNKS`; oldest chunk is
                  dropped on overflow with a WARNING.
                * **`user_transcript`** / **`agent_response`**: emitted
                  by ElevenLabs only on turn completion — the protocol
                  does not expose explicit turn-start events. The
                  bridge captures message-arrival time at the start of
                  each branch as the closest available approximation
                  of when the speaker's turn began, passes it to
                  `_emit_transcript` as `start_ts_ms`, then resets the
                  per-side turn-start field.
                * **`interruption`**: caller barge-in. Reset the
                  drain-diagnostic counters and call
                  `IngressStreamer.barge_in` to flush the per-endpoint
                  audio queue. `pending_handoff` is *not* cancelled —
                  brief caller utterances during the transfer line
                  must not abort an in-flight handoff.
                * **`ping`**: reply with a `pong` carrying the same
                  `event_id`. Required by the ElevenLabs WebSocket
                  protocol to keep the upstream connection alive.
                * **`client_tool_call`**: bridge-implemented tool
                  invocation; delegated to `_handle_tool_call`. The
                  matching system-tool surface (`agent_tool_response`)
                  is handled below — see that branch for the split.
                * **`agent_tool_response`**: ElevenLabs platform-side
                  tool completion. The agent's native `end_call`
                  system tool reports here (with `tool_type="system"`)
                  and does *not* close the upstream WebSocket on its
                  own; this branch emits the success-context
                  `bot.ended` directly. No quiescence drain is needed
                  — `pre_tool_speech="force"` on the EL `end_call`
                  configuration means the goodbye line plays out
                  before the tool fires, so audio has already
                  completed when this branch runs. The
                  `ConnectionClosedOK` branch in `_elevenlabs_recv_loop`
                  remains as a backstop for genuine upstream closes
                  (max_duration reached, network glitch, EL
                  platform-driven termination).
                * **`conversation_initiation_metadata`**: logged at
                  INFO for diagnostics — captures the negotiated audio
                  formats and the EL-side conversation_id for
                  post-call correlation. No further action.

        Spec:
            ElevenLabs Conversational AI WebSocket protocol — message
            type catalog (`audio`, `user_transcript`, `agent_response`,
            `interruption`, `ping`/`pong`, `client_tool_call`,
            `agent_tool_response`, `conversation_initiation_metadata`).

        Args:
            convo: The active conversation whose upstream WebSocket
                produced this message. Mutated in branches that update
                turn-start fields, ingress buffer, drain counters, or
                `bot_ended_sent`.
            msg: Decoded JSON object from the ElevenLabs WebSocket.

        Returns:
            None. Outbound effects: optional audio sends to Infinity,
            optional `bot.feature` / `bot.ended` envelopes to Infinity,
            optional `pong` / `client_tool_result` frames to ElevenLabs.
        """
        mtype = msg.get("type", "")

        if mtype == "audio":
            audio_event = msg.get("audio_event") or {}
            b64 = audio_event.get("audio_base_64") or ""
            if not b64:
                return
            pcm_16k = base64.b64decode(b64)
            out_bytes = self._transcode_output_audio(convo, pcm_16k)
            if not out_bytes:
                return
            # Gate on Infinity ingress readiness — buffer until first egress frame seen.
            if not convo.ingress_ready:
                if len(convo.ingress_buffer) >= _INGRESS_BUFFER_MAX_CHUNKS:
                    logger.warning(
                        "[%s] Ingress buffer full (%d chunks) — discarding oldest",
                        convo.client_id, _INGRESS_BUFFER_MAX_CHUNKS,
                    )
                    convo.ingress_buffer.pop(0)
                convo.ingress_buffer.append(out_bytes)
                logger.debug(
                    "[%s] Buffering ElevenLabs audio — Infinity ingress not ready yet (buffered: %d chunks)",
                    convo.client_id, len(convo.ingress_buffer),
                )
                return
            await self._send_ingress_chunked(convo, out_bytes)
            return

        if mtype == "user_transcript":
            # ElevenLabs emits user_transcript on turn-completion only —
            # there is no explicit turn-start event in the WS protocol.
            # Capture message-arrival time as the turn-start approximation
            # (see method docstring); reset the field after emit.
            convo.customer_turn_started_at = int(time.time() * 1000)
            evt = msg.get("user_transcription_event") or {}
            text = evt.get("user_transcript") or ""
            if text:
                logger.info("[%s] CUSTOMER transcript: %s", convo.client_id, text)
                await self._emit_transcript(
                    convo, "CUSTOMER", text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            convo.customer_turn_started_at = None
            return

        if mtype == "agent_response":
            # Same turn-start approximation as the customer branch above:
            # message-arrival time at handler start, since EL does not
            # expose an explicit agent-turn-started event.
            convo.bot_turn_started_at = int(time.time() * 1000)
            evt = msg.get("agent_response_event") or {}
            text = evt.get("agent_response") or ""
            if text:
                logger.info("[%s] BOT transcript: %s", convo.client_id, text)
                await self._emit_transcript(
                    convo, "BOT", text,
                    start_ts_ms=convo.bot_turn_started_at,
                )
            convo.bot_turn_started_at = None
            return

        if mtype == "interruption":
            # Logged at INFO so the receipt can be correlated against the
            # bridge's INGRESS FRAME and BARGE-IN-skipped log lines for
            # post-call diagnostics.
            evt = msg.get("interruption_event") or {}
            event_id = evt.get("event_id")
            logger.info(
                "[%s] ElevenLabs interruption received: event_id=%s",
                convo.client_id, event_id,
            )
            # Flush the IngressStreamer audio queue via barge_in. Do not
            # cancel any pending_handoff: small caller utterances during
            # the transfer line should not abort the handoff.
            try:
                await self.server.ingress_streamer.barge_in(convo.session_id, convo.endpoint_id)
            except Exception as exc:
                logger.debug("[%s] barge_in call raised: %s", convo.client_id, exc)
            return

        if mtype == "ping":
            evt = msg.get("ping_event") or {}
            event_id = evt.get("event_id")
            if event_id is not None:
                try:
                    await convo.el_ws.send(json.dumps({"type": "pong", "event_id": event_id}))
                except Exception as exc:
                    logger.warning("[%s] pong send failed: %s", convo.client_id, exc)
            return

        if mtype == "client_tool_call":
            await self._handle_tool_call(convo, msg.get("client_tool_call") or {})
            return

        if mtype == "agent_tool_response":
            # ElevenLabs platform-side (system) tool completion. The
            # branch split between client_tool_call and agent_tool_response,
            # the rationale for direct success-context emit here, and the
            # "no quiescence drain needed" reasoning are all documented in
            # this method's header docstring. agent_tool_response with
            # tool_name="end_call" + is_error=False is the only case that
            # produces an outbound emission; everything else is logged.
            response = msg.get("agent_tool_response") or {}
            tool_name = response.get("tool_name") or ""
            is_error = bool(response.get("is_error"))
            if tool_name == "end_call" and not is_error:
                try:
                    await self.server.send_bot_ended_with_success_context(
                        convo.websocket,
                        convo.client_id,
                        convo.session_id,
                        convo.endpoint_id,
                        description="ELEVENLABS: Self-service interaction completed.",
                        service=convo.service,
                        convo=convo,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] success-context bot.ended emit failed: %s",
                        convo.client_id, exc,
                    )
            else:
                logger.info(
                    "[%s] agent_tool_response (tool=%s, is_error=%s) — no bridge action",
                    convo.client_id, tool_name, is_error,
                )
            return

        if mtype == "conversation_initiation_metadata":
            meta = msg.get("conversation_initiation_metadata_event") or {}
            logger.info(
                "[%s] ElevenLabs initiation_metadata: in=%s out=%s conv=%s",
                convo.client_id,
                meta.get("user_input_audio_format"),
                meta.get("agent_output_audio_format"),
                meta.get("conversation_id"),
            )
            return

        logger.debug("[%s] Unhandled ElevenLabs message: %s", convo.client_id, mtype)

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
            Wraps a single transcript line (from either side of the
            conversation) in the bridge-standard transcript shape:
            `payload.ftype = "TRANSCRIPT"` plus
            `payload.transcript = {turnId, speaker, isFinal, text,
            confidence, language, startTsMs}`. A fresh UUID4 turnId is
            generated per emit; `confidence` is set to 1.0 (ElevenLabs
            does not surface a per-utterance confidence value).

            **`startTsMs` derivation.** ElevenLabs's WebSocket protocol
            does not expose explicit turn-start events; both
            `user_transcript` and `agent_response` fire on turn
            completion. The bridge captures message-arrival time at the
            start of the respective handler as the closest available
            approximation of when the speaker's turn began, and passes
            that value here as `start_ts_ms`. When no value is supplied,
            falls back to flush-time `int(time.time() * 1000)`.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.feature` with
            `TRANSCRIPT` ftype. See `bridge/schema/rcms.schema.md`
            "bot.feature — endpointId at payload level".

        Args:
            convo: Active conversation; provides session/endpoint
                identifiers and `language_code`.
            speaker: `"CUSTOMER"` or `"BOT"` per the bridge's transcript
                speaker convention.
            text: The transcript text. Empty strings are still emitted.
            is_final: Whether this is the final transcript for the turn.
                ElevenLabs only emits final transcripts; partial
                streaming transcripts are not in the protocol.
            start_ts_ms: Approximate turn-start in epoch milliseconds.
                When `None`, falls back to flush time.

        Returns:
            None. Send exceptions propagate to the caller.
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
        await self._emit_session_event(convo, "bot.feature", payload, include_endpoint=True)

    async def _handle_tool_call(
        self, convo: BotConversation, call: Dict[str, Any]
    ) -> None:
        """Dispatch a `client_tool_call` from ElevenLabs to its handler and reply with `client_tool_result`.

        Contract:
            Bridge-implemented tools (those defined in the ElevenLabs
            agent dashboard with type `client`) fire here. The matching
            platform-implemented (`type: system`) tools — `end_call`,
            `language_detection`, etc. — surface as `agent_tool_response`
            and are handled in `_handle_elevenlabs_message`, not here.

            Tools recognized:

                * **`transfer_to_agent`**: stash a
                  `bot.feature LIVE_AGENT_HANDOFF` payload and start the
                  drain task in `_wait_for_quiescence_and_emit`. The
                  wire-level emit is deferred until the IngressStreamer
                  queue drains so the transfer-announcement audio plays
                  out before Infinity tears down playback for the
                  handoff. Returns `{"status": "ok", "queue_id": ...}`
                  to ElevenLabs.

                * **anything else**: logged as a warning, returns
                  `{"status": "error", ...}` so the agent's tool result
                  reflects the unknown tool.

            **Three-layer behavioral constraint for `transfer_to_agent`.**
            The tool's schema, the populated tool result, and the agent's
            prompt instructions are all load-bearing — removing any one
            re-introduces a failure mode that the other two do not
            cover:

                1. **Schema constraint.** The agent's `transfer_to_agent`
                   tool schema marks `queue_id` as required. The
                   required-field constraint acts as a behavioral anchor
                   for the LLM: combined with a populated tool result,
                   it signals "this tool finished its work, stop
                   generating after invoking it."
                2. **Populated tool result.** The bridge always returns
                   a non-empty `queue_id` in the tool result, falling
                   back to `"default-queue"` when the agent passes empty
                   (the agent's prompt does not include queue-selection
                   logic, and caller context does not populate the
                   field). Returning a populated result keeps the
                   required-field signal clean.
                3. **Prompt instruction.** The agent's system prompt
                   includes "do not generate any further response after
                   invoking the tool." Without this, the agent
                   re-emits a verbatim acknowledgment after the tool
                   fires.

            The first two layers are bridge-side; the third lives in the
            ElevenLabs agent dashboard. All three together produce the
            single-acknowledgment behavior expected by partner workflows.

            **Why deferred emit, not immediate emit:** ElevenLabs sends
            the `client_tool_call` envelope before the
            transfer-announcement TTS finishes streaming. Emitting
            `LIVE_AGENT_HANDOFF` immediately would have Infinity tear
            down playback mid-utterance and the caller would hear
            silence. The deferred path waits for IngressStreamer queue
            quiescence and then emits, preserving transcript-before-
            handoff ordering on the call-record side as a side effect.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.feature` with
            `LIVE_AGENT_HANDOFF` ftype.
            See `bridge/schema/rcms.schema.md`
            "bot.feature — endpointId at payload level".

        Args:
            convo: The active conversation; mutated to stash the pending
                handoff and to launch the drain task.
            call: The decoded `client_tool_call` body — `tool_name`,
                `tool_call_id`, and `parameters` are read.

        Returns:
            None. The `client_tool_result` is sent on the upstream
            ElevenLabs WebSocket; send failures are logged at WARNING
            and swallowed.
        """
        tool_name = call.get("tool_name") or ""
        tool_call_id = call.get("tool_call_id") or ""
        parameters = call.get("parameters") or {}
        if isinstance(parameters, str):
            try:
                parameters = json.loads(parameters)
            except Exception:
                parameters = {}

        if tool_name == "transfer_to_agent":
            # Default-queue fallback: see the three-layer constraint in
            # this method's docstring. Empty queue_id from the agent is
            # normal — the prompt has no queue-selection logic; caller
            # context does not populate it. Returning a populated value
            # keeps the schema's required-field signal clean.
            queue_id = str(parameters.get("queue_id") or "default-queue")
            reason = str(parameters.get("reason") or "")
            tags_raw = parameters.get("tags") or []
            tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
            handoff_payload = {
                "ftype": "LIVE_AGENT_HANDOFF",
                "liveAgentHandoff": {
                    "queueId": queue_id,
                    "tags": tags,
                    "context": {"reason": reason},
                },
            }
            # Stash payload and start the drain task. Wire-level emit
            # waits for IngressStreamer queue quiescence so the
            # transfer-announcement audio plays out before Infinity
            # tears down playback for the handoff.
            convo.pending_handoff = handoff_payload
            convo.pending_handoff_args = {
                "queue_id": queue_id,
                "tags": tags,
                "reason": reason,
            }
            if convo.handoff_task and not convo.handoff_task.done():
                convo.handoff_task.cancel()
            convo.handoff_task = asyncio.create_task(
                self._wait_for_quiescence_and_emit(convo)
            )
            logger.info(
                "[%s] Stashed LIVE_AGENT_HANDOFF (reason=%r) — awaiting audio quiescence",
                convo.client_id, reason,
            )
            result = {"status": "ok", "queue_id": queue_id}
        else:
            logger.warning("[%s] Unknown ElevenLabs tool '%s'", convo.client_id, tool_name)
            result = {"status": "error", "error": f"Unknown tool: {tool_name}"}

        if tool_call_id:
            response = {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": json.dumps(result),
                "is_error": result.get("status") != "ok",
            }
            try:
                await convo.el_ws.send(json.dumps(response))
            except Exception as exc:
                logger.warning("[%s] client_tool_result send failed: %s", convo.client_id, exc)

    async def _emit_session_event(
        self,
        convo: BotConversation,
        event_type: str,
        payload: Dict[str, Any],
        include_endpoint: bool = True,
    ) -> None:
        """Build and send a generic outbound RCMS envelope (`bot.feature`, etc.) for this conversation.

        Contract:
            Internal helper used by `_emit_transcript` and
            `_emit_pending_handoff`. Wraps the caller-supplied `payload`
            in the standard RCMS envelope (`version`, `type`,
            `sessionId`, `sequenceNum`, `timestamp`, `payload`),
            allocates the next outbound sequence number from the
            bridge's per-client counter, logs the message at INFO, and
            sends it.

            When `include_endpoint=True` (the default), `endpointId` is
            injected into `payload` at the **payload level** rather than
            inside any feature sub-object. This matches the bridge-added
            `endpointId` placement documented in
            `bridge/schema/rcms.schema.md` "bot.feature — endpointId at
            payload level".

        Spec:
            RCMS message envelope — see `bridge/schema/rcms.schema.md`
            "Message envelope". Bridge-originated `bot.feature` carries
            `endpointId` at payload level (bridge-added field).

        Args:
            convo: The active conversation; provides `session_id`,
                `client_id`, `endpoint_id`, and the WebSocket to send on.
            event_type: RCMS message type (e.g. `"bot.feature"`).
            payload: The message-specific payload object. Shallow-copied
                before any `endpointId` injection so the caller's dict is
                not mutated.
            include_endpoint: When `True`, inject
                `payload["endpointId"] = convo.endpoint_id`.

        Returns:
            None. Send exceptions propagate to the caller.
        """
        body = {**payload}
        if include_endpoint:
            body["endpointId"] = convo.endpoint_id
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

    # ------------------------------------------------------------------ handoff drain

    async def _wait_for_quiescence_and_emit(
        self,
        convo: BotConversation,
        poll_ms: int = _QUIESCENCE_POLL_MS,
        deadlock_safety_s: float = _QUIESCENCE_DEADLOCK_SAFETY_S,
    ) -> None:
        """Sleep-and-check drain loop — emits the pending handoff once the IngressStreamer queue is empty.

        Contract:
            Single-rule drain: sleep `poll_ms`, then check whether the
            per-endpoint IngressStreamer queue is empty. Empty → emit and
            return. Non-empty → loop. The sleep itself is the grace
            period — long enough for any audio chunk crossing the
            network during the iteration to land in the queue before
            the empty check.

            One rule covers every observed pattern:

                * **ElevenLabs (audio queues before tool_call):** queue
                  non-empty on first check; drain continues until
                  observably empty.
                * **Tool-call-first providers (e.g. Gemini):** audio
                  arrives during the sleep cycle; queue stays non-empty;
                  drain continues.
                * **Multi-burst (>1 s gaps):** queue refills during a
                  sleep cycle; drain extends naturally.
                * **No audio (pathological):** queue empty on first
                  check; method exits silently after one sleep.

            **Safety net.** `deadlock_safety_s` (default 30 s) is an
            upstream-failure backstop — e.g. provider WS hung in a way
            that prevents the audio queue from ever draining. Trip
            indicates something genuinely wrong upstream, not a
            drain-timing issue. Logs at WARNING and emits the handoff
            anyway so the workflow does not stall indefinitely.

            Cancellation-safe: `asyncio.CancelledError` returns silently
            without emitting. `_shutdown_conversation` cancels the
            handoff_task on a caller-disconnect mid-handoff so this
            method exits without firing the emit (the conversation is
            being torn down anyway).

        Args:
            convo: The conversation whose handoff is pending. Reads
                `pending_handoff`; passes through to
                `_emit_pending_handoff` on drain completion.
            poll_ms: Sleep interval per iteration in milliseconds.
                Default `_QUIESCENCE_POLL_MS` (250 ms) is empirically the
                right grace period across the four AI providers; tune at
                call sites only if a provider has unusual chunking.
            deadlock_safety_s: Upper bound on total wait time. Default
                `_QUIESCENCE_DEADLOCK_SAFETY_S` (30 s).

        Returns:
            None. Always either emits via `_emit_pending_handoff` (drain
            done or safety-net trip) or exits silently on cancellation.
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

        # Drain complete; emit handoff (no drain_deadline — already drained).
        await self._emit_pending_handoff(convo)

    async def _emit_pending_handoff(self, convo: BotConversation) -> None:
        """Emit the wire-level `bot.feature` LIVE_AGENT_HANDOFF and the bridge-originated `bot.ended`.

        Contract:
            Terminus of the staged handoff path: drain has already
            completed in `_wait_for_quiescence_and_emit`, and this method
            performs the wire-level emissions in two parts:

                1. `bot.feature` with `payload.ftype = "LIVE_AGENT_HANDOFF"`
                   and `payload.liveAgentHandoff = {queueId, tags,
                   context}` per RCMS spec §AI Bot Message Definitions.
                2. `bot.ended` with `payload.endpointId` and **no
                   `payload.context.status`** — the absent-status shape
                   per `bridge/schema/rcms.schema.md` table:
                   `absent → Live agent handoff — read
                   byobotLiveAgentHandoff`. The workflow consumes the
                   `byobotLiveAgentHandoff` value populated from the
                   prior `bot.feature` rather than `byobotEndContext.
                   status`.

            **Why this site uses a manual `bot.ended` build instead of
            `send_bot_ended_with_*_context`:** those three helpers
            all originate a `payload.context.status` object (success,
            failure, or disconnect). The handoff termination shape has
            no status object — it is a fourth, distinct termination
            signal. A future `send_bot_ended_with_handoff_context` helper
            would standardize this; until then the manual build is the
            documented deviation. `convo.bot_ended_sent` is set
            explicitly here because the helper-flag-flip path is
            bypassed.

            **Why the bridge originates `bot.ended` for handoff** (vs.
            waiting for Infinity to drive it): bridge-originated
            `bot.ended` triggers Infinity to send `session.end` as a
            clean teardown ack; staying silent leads to a teardown
            timeout race observable as a delayed `session.ended` and
            occasional UC3 workflow errors.

            Clears `pending_handoff` / `pending_handoff_args` /
            `handoff_task` before emitting so a duplicate trigger (e.g.
            multiple `client_tool_call`s for the same tool) sees an
            already-flushed conversation and returns silently.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            envelope and `LIVE_AGENT_HANDOFF` ftype.
            `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context" — the absent-status row pins the
            handoff termination shape.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            convo: The active conversation. Reads `pending_handoff` /
                `pending_handoff_args`; mutates `pending_handoff` →
                `None`, `pending_handoff_args` → `None`, `handoff_task`
                → `None`, and `bot_ended_sent` → `True` after the
                `bot.ended` send completes.

        Returns:
            None. Exits early without sending if `pending_handoff` was
            already cleared. Errors on either emission are logged at
            WARNING and swallowed.
        """
        payload = convo.pending_handoff
        args = convo.pending_handoff_args or {}
        if not payload:
            return
        convo.pending_handoff = None
        convo.pending_handoff_args = None
        convo.handoff_task = None

        try:
            await self._emit_session_event(convo, "bot.feature", payload, include_endpoint=True)
            reason = args.get("reason", "")
            logger.info(
                "[%s] Emitted LIVE_AGENT_HANDOFF (reason=%r)", convo.client_id, reason,
            )
        except Exception as exc:
            logger.warning("[%s] LIVE_AGENT_HANDOFF emit failed: %s", convo.client_id, exc)

        # bot.ended — handoff termination shape. payload.context.status is
        # intentionally absent: the workflow reads byobotLiveAgentHandoff
        # populated from the bot.feature emitted above. Slice-5 helpers
        # do not fit (they all originate a status object); future
        # `send_bot_ended_with_handoff_context` would standardize this.
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
            # Helper-flag-flip path is bypassed by this manual build —
            # set bot_ended_sent explicitly so on_session_ended does not
            # emit a duplicate disconnect-context bot.ended.
            convo.bot_ended_sent = True
        except Exception as exc:
            logger.warning("[%s] bot.ended originator send failed: %s", convo.client_id, exc)

    # ------------------------------------------------------------------ shutdown

    async def _shutdown_conversation(self, convo: BotConversation) -> None:
        """Tear down every per-conversation resource: handoff task, ingress streamer, ElevenLabs WS.

        Contract:
            Idempotent shutdown for a single `BotConversation`. Called on
            every exit path: `_handle_bot_end`, `on_session_ended`, and
            `shutdown`. Marks `convo.active = False` first so any
            in-flight `_elevenlabs_recv_loop` iteration sees the flag and
            exits at its next message boundary.

            Order of cleanup:
                1. Cancel the handoff drain task (`handoff_task`) if it
                   is still running. A caller hangup mid-handoff-wait
                   (caller hangs up while the transfer line is still
                   streaming) means Infinity is already tearing the
                   session down — there is nothing to drain to and
                   nothing useful to emit. Cancel without emitting; do
                   not invoke `_emit_pending_handoff` from this path.
                   Clears `pending_handoff` / `pending_handoff_args` so
                   any later observer sees the conversation as quiesced.
                2. Drain and tear down the IngressStreamer for this
                   `(session_id, endpoint_id)` via `stop_and_clear`. This
                   purges the per-endpoint queue and cancels the
                   streaming task.
                3. Cancel the ElevenLabs receive loop task and await its
                   exit so no further messages are dispatched after this
                   point.
                4. Close the upstream ElevenLabs WebSocket. Errors here
                   are logged at DEBUG and swallowed — the connection is
                   being torn down anyway.

            Does NOT emit `bot.ended`. The disconnect-context emit lives
            in `on_session_ended` (caller-driven path); the bot.end ack
            lives in `_handle_bot_end`. Both call this method *after*
            their respective bot.ended emissions.

        Args:
            convo: The conversation to tear down. Mutated in place: every
                resource attribute is cleared or cancelled.

        Returns:
            None. All exceptions raised by cleanup steps are logged and
            swallowed; the method always returns cleanly.
        """
        convo.active = False

        # Caller hangup or platform-driven session end mid-handoff: cancel
        # the drain task without emitting. By the time _shutdown_conversation
        # is reached, Infinity has already initiated session teardown — the
        # handoff is moot and emitting would race against session.ended.
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

        if convo.endpoint_id:
            try:
                await self.server.ingress_streamer.stop_and_clear(convo.session_id, convo.endpoint_id)
            except Exception as exc:
                logger.debug("[%s] stop_and_clear raised: %s", convo.client_id, exc)

        task = convo.el_recv_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        ws = convo.el_ws
        convo.el_ws = None
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[%s] ElevenLabs ws.close raised: %s", convo.client_id, exc)


def register(server: "BridgeServer") -> ElevenLabsService:
    """Plugin entrypoint — instantiate `ElevenLabsService` and register it with the bridge.

    Discovered and called by `bot_service.py` at startup if
    `ElevenLabsService.is_configured()` returns `True` (i.e.
    `ELEVENLABS_API_KEY` is set in the environment). The constructed
    plugin is added to the bridge's `ServiceRegistry` and thereafter
    receives every `bot.start` / `bot.end` whose `payload.botId` carries
    the `elevenlabs:` prefix.

    Args:
        server: The owning `BridgeServer` instance.

    Returns:
        The registered `ElevenLabsService`. Returned for tests / startup
        diagnostics; production callers do not retain the reference.
    """
    plugin = ElevenLabsService(server)
    server.register_service(plugin)
    return plugin
