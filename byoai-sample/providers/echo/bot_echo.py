"""
bot_echo — Echo loopback provider for the RCMS Virtual Agent (bot) service

Role:
    Implements the Echo provider — a loopback handler that receives bot.start
    with botId='echo', echoes every inbound caller media frame back to the
    caller in real time, and emits bot.started/bot.ended on the appropriate
    lifecycle boundaries. Echo is the integration-validation provider: it
    exercises the full bridge ↔ Infinity audio pipeline without requiring an
    external AI service or API credentials.

Does not own:
    Provider routing by botId (owned by the bot dispatcher in bot_service.py).
    MIM session lifecycle and JWT authentication (owned by bridge_server.py).
    Audio frame pacing and ingress queue management (owned by IngressStreamer
    in bridge_server.py).

Dependencies:
    bridge_server.ServicePlugin: base class establishing the plugin contract
        (name, message_types, handle_message, on_session_ended).
    bridge_server.format_compact_json: outbound-message logging helper that
        produces the compact JSON form used in wire-frame logs.
    bridge_server.BridgeServer: provides get_next_sequence (per-client
        outbound sequence counter), ingress_streamer (real-time paced
        delivery to Infinity), and send_bot_ended_with_disconnect_context
        (the helper that emits bot.ended with CALLER_DISCONNECTED status
        on platform-initiated session end).

RCMS lifecycle:
    Phase 1 (Start): handles bot.start dispatched from the bot service with
        botId='echo'; emits bot.started.
    Phase 2 (During): forwards every inbound media frame back to the caller
        via IngressStreamer.send_immediate. Both base64 and binary
        transports are supported.
    Phase 3 (Closure): handles bot.end (emits bot.ended ack); on
        platform-initiated session.end with an active Echo session, emits
        bot.ended with CALLER_DISCONNECTED status before tearing down —
        Infinity expects bot.ended before session.ended on every active
        bot session.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start / bot.started /
        bot.end / bot.ended
    RCMS spec §Media Encoding Options — base64 and binary frame formats,
        the 16-byte Compact Binary Frame Header
    RCMS spec §Error Handling — bot.start failures are emitted as
        bot.ended with non-200 status in payload.context.status
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire shape and
    bridge/schema/rcms.schema.md for behavioral notes — including
    bot.ended's payload.context.status nesting and the CALLER_DISCONNECTED
    reason value used on platform-initiated termination.

See also:
    BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload field reference
    BUILDERS_GUIDE.md §3 Phase 2 — audio frames and IngressStreamer
    BUILDERS_GUIDE.md §3 Phase 3 — bot.ended status semantics
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, UTC
from typing import TYPE_CHECKING, Any, Dict

from websockets.server import WebSocketServerProtocol

from bridge_server import (
    ServicePlugin,
    format_compact_json,
)

if TYPE_CHECKING:
    from bridge_server import BridgeServer


logger = logging.getLogger(__name__)


# RCMS spec §Media Encoding Options — Compact Binary Frame Header carries
# a 16-bit flags field. Bit 0 (lastf) marks the final frame of an utterance.
# See bridge/schema/rcms.schema.md for the wire-frame layout.
_FRAME_FLAG_LAST = 0x0001


class EchoService(ServicePlugin):
    """Service plugin that handles bot.start/bot.end with botId='echo'."""

    name = "echo"

    def __init__(self, server):
        """
        Initialize per-plugin state used across the call lifecycle.

        State carried on the instance:
            _active: per-(session_id, endpoint_id) active-call dict whose
                value carries the Infinity-side websocket and client_id
                captured at bot.start time. Read on Phase 3 closure to
                emit bot.ended with CALLER_DISCONNECTED on platform-
                initiated session end.
            _stats: per-(session_id, endpoint_id) byte-counter dict
                tracking ingress and egress audio byte counts. Updated
                on each echoed frame for diagnostic logging.

        Args:
            server: the BridgeServer instance the plugin is registered
                against. Stored on self.server by the ServicePlugin base.
        """
        super().__init__(server)
        # Per-(session, endpoint) active state. Value carries the
        # Infinity-side websocket + client_id captured at bot.start time so
        # on_session_ended can emit bot.ended with CALLER_DISCONNECTED on
        # caller hangup — Infinity expects bot.ended before session.ended
        # on every active bot session, otherwise the workflow records a
        # platform-side error and cannot route the disconnect.
        self._active: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._stats: Dict[str, Dict[str, Dict[str, int]]] = {}

    @property
    def message_types(self) -> set[str]:
        """
        Return the RCMS message types this plugin claims with the
        lower-level ServiceRegistry. Empty for Echo: the bot dispatcher
        in bot_service.py owns bot.start/bot.end and routes by botId
        prefix; this plugin is invoked directly by name via
        service_registry.get_plugin('echo') and receives raw
        bot.start/bot.end through handle_message.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.start and bot.end
            are owned by the bot service dispatcher.

        Returns:
            Empty set — Echo does not register any RCMS message types
            directly with the message-type-to-plugin map.
        """
        return set()

    def _is_active(self, session_id: str, endpoint_id: str) -> bool:
        """
        Return True if Echo is actively tracking this (session, endpoint).

        Used as the gate on Phase 2 audio echo paths to avoid acting on
        a session that has already terminated.

        Args:
            session_id: RCMS session identifier from the originating
                bot.start.
            endpoint_id: media endpoint identifier from the originating
                bot.start.

        Returns:
            True iff an entry exists in self._active for this
            (session, endpoint) — i.e. between bot.start receipt and
            _stop_echo_session teardown.
        """
        return self._active.get(session_id, {}).get(endpoint_id) is not None

    def _ensure_stats(self, session_id: str, endpoint_id: str) -> Dict[str, int]:
        """
        Return the ingress/egress byte-counter dict for this (session,
        endpoint), creating it if it does not already exist. Counters
        are updated by maybe_echo_base64 / maybe_echo_binary for
        diagnostic logging.

        Args:
            session_id: RCMS session identifier.
            endpoint_id: media endpoint identifier.

        Returns:
            A mutable Dict[str, int] with keys "ingress_bytes" and
            "egress_bytes", both initialized to 0 on first call.
        """
        session_stats = self._stats.setdefault(session_id, {})
        return session_stats.setdefault(endpoint_id, {"ingress_bytes": 0, "egress_bytes": 0})

    async def handle_message(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Dispatch an inbound RCMS message to the appropriate Echo handler.

        Echo handles two RCMS message types: bot.start (handed to
        _handle_start) and bot.end (handed to _handle_end). Any other
        type is logged as a warning and dropped — Echo's contract with
        the bot dispatcher is that only bot.start/bot.end will ever be
        routed here via service_registry.get_plugin('echo').handle_message().

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.start initiates a
            bot session; bot.end terminates one.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed RCMS message envelope (version, type,
                sessionId, sequenceNum, timestamp, payload).

        Returns:
            None. Side effects: emits bot.started or bot.ended on the
            websocket as appropriate.

        See also:
            BUILDERS_GUIDE.md §3 Phase 1 — bot.start handling
            BUILDERS_GUIDE.md §3 Phase 3 — bot.end handling
        """
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_end(websocket, client_id, data)
        else:
            logger.warning("Unhandled message type '%s' in Echo service", msg_type)

    async def on_session_ended(self, session_id: str) -> None:
        """
        Handle platform-initiated session end (caller hangup, workflow
        timeout, or operator action). For every active Echo
        (session, endpoint) pair, emit bot.ended with CALLER_DISCONNECTED
        status before tearing down.

        Echo has no self-service-complete or live-agent-handoff path —
        every active session reaching on_session_ended is by definition
        a caller- or platform-initiated termination. The bridge's
        contract with Infinity requires bot.ended before session.ended
        on every active bot session; omitting it leaves the workflow's
        IVA module in an indeterminate state.

        Spec:
            RCMS spec §Session Message Definitions — session.end triggers
            this callback through the bridge's plugin lifecycle.
            RCMS spec §AI Bot Message Definitions — bot.ended must be
            emitted for every active bot session before session.ended.

        The bot.ended emission carries:
            payload.context.status.code = 200
            payload.context.status.reason = "CALLER_DISCONNECTED"
            payload.context.status.description =
                "ECHO: Caller disconnected before self-service complete."

        See bridge/schema/rcms.schema.md for the BotEndedPayload shape,
        the payload.context.status nesting, and the CALLER_DISCONNECTED
        reason semantics.

        Args:
            session_id: RCMS session identifier. May not match any
                active Echo session, in which case this is a no-op.

        Returns:
            None. Side effects: emits bot.ended for each active endpoint
            via BridgeServer.send_bot_ended_with_disconnect_context, then
            clears per-session state.

        See also:
            BUILDERS_GUIDE.md §3 Phase 3 — caller-disconnect closure path
        """
        # Snapshot active entries before teardown so iteration is stable
        # while we emit + clean up per-endpoint state. Echo has no
        # self-service-complete path; every active session reaching here
        # terminates with CALLER_DISCONNECTED.
        active_entries = list(self._active.get(session_id, {}).items())
        for endpoint_id, entry in active_entries:
            websocket = entry.get("websocket")
            client_id = entry.get("client_id")
            if websocket is not None and client_id is not None:
                try:
                    await self.server.send_bot_ended_with_disconnect_context(
                        websocket,
                        client_id,
                        session_id,
                        endpoint_id,
                        provider=self.name,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Echo disconnect-context bot.ended emit failed: %s",
                        client_id, exc,
                    )
        for endpoint_id, _entry in active_entries:
            await self._stop_echo_session(session_id, endpoint_id)
        self._active.pop(session_id, None)
        self._stats.pop(session_id, None)

    async def _handle_start(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Handle a Phase 1 bot.start from the bot dispatcher: capture the
        websocket and client_id for later Phase 3 use, allocate per-call
        state, and emit bot.started.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.start initiates a
            bot session; the service responds with bot.started on success.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed bot.start message. Read fields:
                sessionId, service, payload.endpointId.

        Returns:
            None. Side effects: stores (websocket, client_id) on
            self._active[session_id][endpoint_id]; allocates byte
            counters in self._stats; emits bot.started on the websocket.

        See also:
            BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload and
                bot.started response shape
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        service = data.get("service", "streaming")
        endpoint_id = payload.get("endpointId", "")

        logger.info("[%s] Received bot.start for Echo session: %s, endpoint: %s", client_id, session_id, endpoint_id)

        # Capture websocket + client_id for the disconnect-emit path in
        # on_session_ended — Infinity expects bot.ended before session.ended
        # on every active bot session, and the only handle Echo has on the
        # Infinity-side connection comes through this bot.start invocation.
        self._active.setdefault(session_id, {})[endpoint_id] = {
            "websocket": websocket,
            "client_id": client_id,
        }
        self._ensure_stats(session_id, endpoint_id)

        response = {
            "version": "1.0.0",
            "type": "bot.started",
            "sessionId": session_id,
            "sequenceNum": self.server.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            # The "service" top-level field on bot.* envelopes is a
            # bridge convention echoed in bot.started/bot.ended. See
            # bridge/schema/rcms.schema.md for the envelope shape.
            "service": service,
            "payload": {
                "endpointId": endpoint_id,
            },
        }

        logger.info("[%s] OUTBOUND JSON (bot.started): %s", client_id, format_compact_json(response))
        await websocket.send(json.dumps(response))

    async def _handle_end(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Handle a Phase 3 bot.end from the bot dispatcher: tear down
        per-call state and emit bot.ended.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.end terminates a
            bot session; the service responds with bot.ended.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed bot.end message. Read fields:
                sessionId, service, payload.endpointId.

        Returns:
            None. Side effects: clears per-(session, endpoint) state via
            _stop_echo_session; emits bot.ended on the websocket.

        See also:
            BUILDERS_GUIDE.md §3 Phase 3 — bot.end ack and closure paths
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        service = data.get("service", "streaming")
        endpoint_id = payload.get("endpointId", "")

        logger.info("[%s] Received bot.end for Echo session: %s, endpoint: %s", client_id, session_id, endpoint_id)

        await self._stop_echo_session(session_id, endpoint_id)

        response = {
            "version": "1.0.0",
            "type": "bot.ended",
            "sessionId": session_id,
            "sequenceNum": self.server.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            # The "service" top-level field on bot.* envelopes is a
            # bridge convention echoed in bot.started/bot.ended. See
            # bridge/schema/rcms.schema.md for the envelope shape.
            "service": service,
            "payload": {
                "endpointId": endpoint_id,
            },
        }

        logger.info("[%s] OUTBOUND JSON (bot.ended): %s", client_id, format_compact_json(response))
        await websocket.send(json.dumps(response))

    async def _stop_echo_session(self, session_id: str, endpoint_id: str) -> None:
        """
        Clear per-(session, endpoint) state and stop any in-flight
        ingress streaming. Called from both _handle_end (the bot.end ack
        path) and on_session_ended (the caller-disconnect path).

        Args:
            session_id: RCMS session identifier.
            endpoint_id: media endpoint identifier.

        Returns:
            None. Side effects: removes entries from self._active and
            self._stats; calls IngressStreamer.stop_and_clear to drain
            any queued ingress audio.
        """
        self._active.get(session_id, {}).pop(endpoint_id, None)
        session_active = self._active.get(session_id)
        if session_active is not None and not session_active:
            self._active.pop(session_id, None)

        self._stats.get(session_id, {}).pop(endpoint_id, None)
        if session_id in self._stats and not self._stats[session_id]:
            self._stats.pop(session_id, None)

        # Stop and clear ingress streamer queue for this endpoint
        if endpoint_id:
            await self.server.ingress_streamer.stop_and_clear(session_id, endpoint_id)
            logger.debug("Cleared ingress streamer for %s:%s", session_id, endpoint_id)

    async def maybe_echo_base64(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        bid: int,
        source: str,
        seq: int,
        timestamp: int,
        audio_base64: str,
        audio_size: int,
        direction: str,
        lastf: bool = False,
        supports_ingress: bool = False,
    ) -> bool:
        """
        Echo a base64-transport media frame back to the caller via the
        IngressStreamer's low-latency send_immediate path.

        The "maybe_" prefix encodes the early-return conditions: the
        negotiated transport must support bridge → Infinity media frames,
        the frame must be on the egress direction (caller's voice arriving
        from Infinity), and the (session, endpoint) must still be active.

        Spec:
            RCMS spec §Media Encoding Options — base64 transport encoding;
            media frames carry bid, src, asn, ts, lastf, audio at the
            top level of the message envelope.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for log correlation.
            session_id: RCMS session identifier.
            endpoint_id: media endpoint identifier from session.start.
            bid: bearer ID — identifies which endpoint this audio
                belongs to.
            source: audio source — "rx" (received from caller),
                "tx" (transmitted to caller), or "none".
            seq: audio sequence number.
            timestamp: frame timestamp (Unix epoch ms).
            audio_base64: base64-encoded audio payload.
            audio_size: byte length of the decoded audio.
            direction: "egress" if this is a frame the bridge received
                from Infinity (caller's voice); else "ingress" / other.
                Echo only loops back egress frames.
            lastf: True if this is the final frame of an utterance.
                Propagated to the outbound frame.
            supports_ingress: True if the negotiated transport supports
                bridge → Infinity media frames. Required for Echo to
                have anywhere to send the loopback.

        Returns:
            True if the frame was successfully queued for ingress
            delivery. False if any early-return condition held or the
            IngressStreamer rejected the send.

        See also:
            BUILDERS_GUIDE.md §3 Phase 2 — media frames and IngressStreamer
        """
        # No ingress means the transport cannot send media back to the
        # caller — nothing to echo to.
        if not supports_ingress:
            return False

        # Echo only loops back caller audio (egress, from Infinity's
        # perspective). Skipping non-egress directions prevents echoing
        # the bridge's own ingress frames back to itself.
        if direction != "egress":
            return False

        if not self._is_active(session_id, endpoint_id):
            return False

        audio_bytes = base64.b64decode(audio_base64)

        stats = self._ensure_stats(session_id, endpoint_id)
        stats["ingress_bytes"] += audio_size

        success = await self.server.ingress_streamer.send_immediate(
            websocket=websocket,
            client_id=client_id,
            session_id=session_id,
            endpoint_id=endpoint_id,
            audio_bytes=audio_bytes,
            is_last=lastf,
            transport="base64",
        )

        if success:
            stats["egress_bytes"] += audio_size

        return success

    async def maybe_echo_binary(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        bid: int,
        source: str,
        seq: int,
        timestamp_micros: int,
        flags: int,
        audio_bytes: bytes,
        direction: str,
        extension: bytes,
        supports_ingress: bool = False,
    ) -> bool:
        """
        Echo a binary-transport media frame back to the caller via the
        IngressStreamer's low-latency send_immediate path.

        The "maybe_" prefix encodes the early-return conditions: the
        negotiated transport must support bridge → Infinity media frames,
        the frame must be on the egress direction, and the (session,
        endpoint) must still be active.

        Spec:
            RCMS spec §Media Encoding Options — binary transport encoding;
            the 16-byte Compact Binary Frame Header carries bid, src,
            sequenceNum, timestamp, flags, payload length, followed by
            the opaque codec-encoded audio payload.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for log correlation.
            session_id: RCMS session identifier.
            endpoint_id: media endpoint identifier from session.start.
            bid: bearer ID.
            source: audio source — "rx" / "tx" / "none".
            seq: audio sequence number.
            timestamp_micros: frame timestamp in microseconds.
            flags: 16-bit flags field; bit 0 (_FRAME_FLAG_LAST) marks
                the final frame of an utterance.
            audio_bytes: raw codec-encoded audio payload.
            direction: "egress" if this frame came from Infinity
                (caller's voice); else "ingress" / other. Echo only
                loops back egress frames.
            extension: header-extension bytes per spec; not used by Echo.
            supports_ingress: True if the negotiated transport supports
                bridge → Infinity media frames.

        Returns:
            True if the frame was successfully queued for ingress
            delivery. False if any early-return condition held or the
            IngressStreamer rejected the send.

        See also:
            BUILDERS_GUIDE.md §3 Phase 2 — media frames and IngressStreamer
        """
        if not supports_ingress:
            return False

        # Echo only loops back caller audio (egress, from Infinity's
        # perspective). Skipping non-egress directions prevents echoing
        # the bridge's own ingress frames back to itself.
        if direction != "egress":
            return False

        if not self._is_active(session_id, endpoint_id):
            return False

        audio_size = len(audio_bytes)
        stats = self._ensure_stats(session_id, endpoint_id)
        stats["ingress_bytes"] += audio_size

        # The lastf bit (position 0 of the 16-bit flags field) marks the
        # final frame of an utterance. RCMS spec §Media Encoding Options.
        is_last = (flags & _FRAME_FLAG_LAST) != 0

        success = await self.server.ingress_streamer.send_immediate(
            websocket=websocket,
            client_id=client_id,
            session_id=session_id,
            endpoint_id=endpoint_id,
            audio_bytes=audio_bytes,
            is_last=is_last,
            transport="binary",
        )

        if success:
            stats["egress_bytes"] += audio_size

        return success


def register(server: "BridgeServer") -> EchoService:
    """
    Plugin entry point invoked by the bridge during plugin discovery at
    server startup. Constructs the EchoService instance and registers it
    with the server's service_registry under the name "echo".

    The returned plugin can also be retrieved later via
    server.service_registry.get_plugin("echo") — that's how the bot
    dispatcher routes bot.start with botId='echo' to this plugin's
    handle_message.

    Args:
        server: the BridgeServer instance the plugin is being registered
            against. Imported under TYPE_CHECKING to avoid a runtime
            circular import.

    Returns:
        The constructed EchoService plugin instance, already registered
        with server.service_registry.

    See also:
        BUILDERS_GUIDE.md §4 — Bridge Configuration and plugin discovery
    """
    plugin = EchoService(server)
    server.register_service(plugin)
    return plugin
