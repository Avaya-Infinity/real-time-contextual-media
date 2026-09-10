"""
bot_service — RCMS bot lifecycle dispatcher: routes bot.start/bot.end by botId prefix

Role:
    Owns the bot.start → bot.end lifecycle. Receives bot.start from
    BridgeServer's message router, classifies the inbound botId by
    prefix (echo / elevenlabs:* / gemini:* / openai:* / xai:*), and
    dispatches to the matching provider plugin. Every exit path emits
    a spec-compliant termination on failure or hands off to the
    chosen provider on success per RCMS §Error Handling.

    Also owns Phase 2 audio ingest: BridgeServer's media-frame handlers
    invoke ingest_audio_chunk, which fans out to whichever AI provider
    plugin is bound to the (session, endpoint). Echo handles its own
    audio via maybe_echo_* hooks invoked directly by BridgeServer; only
    AI provider sessions route through this dispatcher's audio path.

    Adding a new AI provider requires updates at 8 sites in this file
    (kept hardcoded for explicitness — partners can audit each site
    by reading the file top-to-bottom):

        1. Module-top import of the new provider's service class.
        2. _is_<provider>_bot_id static method.
        3. __init__ instance attribute (conditional on
           ProviderService.is_configured()) plus the matching
           "Registered backend" log line.
        4. _handle_bot_start dispatch branch, including the
           BACKEND_NOT_CONFIGURED failure path when the provider is
           not configured.
        5. _handle_bot_end dispatch branch.
        6. on_session_ended fan-out.
        7. shutdown fan-out.
        8. ingest_audio_chunk fan-out.

    Each provider is expected to expose a classmethod is_configured()
    returning True iff its required env vars are set, plus a register()
    function and the standard ServicePlugin contract.

Does not own:
    Wire-protocol envelope and JSON encoding (owned by bridge_server.py).
    Provider-specific AI logic, audio transcoding, or upstream
    WebSocket management (owned by individual provider plugins under
    providers/).
    Echo loopback behavior (owned by providers/echo/bot_echo.py;
    Echo is registered as a separate top-level plugin and is invoked
    by name, not via this dispatcher's instance attributes).
    JWT validation (owned by BridgeServer.check_auth).

Dependencies:
    bridge_server.ServicePlugin: base class establishing the plugin
        contract (name, message_types, handle_message, on_session_ended).
    bridge_server.BridgeServer.send_session_error: emitted on
        MISSING_REQUIRED_FIELDS — the protocol-level error case where
        endpointId is itself missing and bot.ended cannot satisfy its
        schema-required endpointId field.
    bridge_server.BridgeServer.send_bot_ended_with_failure_context:
        emitted on UNRECOGNIZED_BOTID_PREFIX and BACKEND_NOT_CONFIGURED
        — the spec-correct shape for [service].start failures with a
        valid endpointId.
    providers.elevenlabs / providers.gemini / providers.openai /
    providers.xai: each AI provider plugin module. Imported at module
    top so the dispatcher can construct configured providers eagerly
    at startup.

RCMS lifecycle:
    Phase 1 (Start): receives bot.start, validates endpointId/botId,
        classifies by botId prefix, dispatches to the provider plugin
        on success or emits a failure-shape termination on any
        rejection.
    Phase 2 (During): owns ingest_audio_chunk fan-out for AI provider
        sessions. Echo's audio path bypasses this dispatcher and is
        invoked directly by BridgeServer.
    Phase 3 (Closure): receives bot.end from Infinity, dispatches to
        the provider plugin associated with the active session via
        the _active stash. on_session_ended propagates caller-disconnect
        notifications to every configured AI provider; Echo receives
        that notification directly from BridgeServer.handle_session_end
        as a separate plugin.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start / bot.started /
        bot.end / bot.ended.
    RCMS spec §Error Handling — "Any [service].start messages that
        cannot be processed are failed by returning a [service].end
        with the error in the payload."
    RCMS spec §Status Codes — failure code/reason taxonomy used by
        the failure-context emissions (501 UNSUPPORTED_SERVICE,
        503 SERVICE_UNAVAILABLE).
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire
    shape and bridge/schema/rcms.schema.md for behavioral notes —
    including bot.ended's payload.context.status nesting and the
    workflow's byobotEndContext consumption pattern.

See also:
    BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload field reference
    BUILDERS_GUIDE.md §3 Phase 3 — failure paths and IVA module exits
    BUILDERS_GUIDE.md §4 — Bridge Configuration: env vars per provider
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from websockets.server import WebSocketServerProtocol

from bridge_server import ServicePlugin

from providers.elevenlabs.bot_elevenlabs import ElevenLabsService
from providers.gemini.bot_gemini import GeminiService
from providers.openai.bot_openai import OpenAIService
from providers.xai.bot_xai import XaiService

if TYPE_CHECKING:
    from bridge_server import BridgeServer

logger = logging.getLogger(__name__)


class CombinedBotService(ServicePlugin):
    """
    Bot dispatcher plugin. Registered under the name "bot" with the
    bridge's service registry; claims bot.start, bot.end, and
    session.dtmf message types.

    Per-instance state:
        _elevenlabs / _gemini / _openai / _xai: provider plugin instances,
            constructed at __init__ time iff the matching
            ProviderService.is_configured() returns True. None if the
            provider's env vars are not set — bot.start with that
            prefix will then fail with BACKEND_NOT_CONFIGURED status.
        _active: dict mapping (session_id, endpoint_id) → provider
            kind ("echo" | "elevenlabs" | "gemini" | "openai" | "xai").
            Populated on bot.start dispatch; consumed and cleared on
            bot.end via atomic _active.pop().

    Lifecycle phase coverage:
        Phase 1: handle_message routes bot.start to _handle_bot_start.
        Phase 2: ingest_audio_chunk fans audio frames to the bound
            AI provider. Echo's audio path is separate.
        Phase 3: handle_message routes bot.end to _handle_bot_end;
            on_session_ended propagates caller-disconnect to all
            configured AI providers.

    Adding a new provider: see the module docstring's 8-site list.
    """

    name = "bot"

    def __init__(self, server: "BridgeServer"):
        """
        Construct the dispatcher and eagerly instantiate every
        configured provider plugin.

        Eager (not lazy) instantiation is intentional: doing it at
        __init__ time gives immediate startup feedback ("Registered
        backend: X" log lines, or the "no providers configured"
        warning) so misconfigurations surface at boot rather than at
        first call.

        Each provider's is_configured() classmethod is the gate. When
        it returns True the dispatcher constructs the plugin and stores
        it as a per-provider instance attribute. When it returns False
        the attribute is set to None and any matching bot.start fails
        with BACKEND_NOT_CONFIGURED at session time. is_configured()
        is by convention a pure env-var check that does not raise.

        Args:
            server: the BridgeServer instance the dispatcher is
                registered against. Stored on self.server by the
                ServicePlugin base class.
        """
        super().__init__(server)
        # Eagerly construct each AI provider iff its is_configured()
        # returns True. is_configured() is a classmethod on each
        # ProviderService; by convention it returns True iff the
        # provider's required env vars are set (e.g. ELEVENLABS_API_KEY,
        # GEMINI_API_KEY). When False the dispatcher stores None and
        # any matching bot.start will fail with BACKEND_NOT_CONFIGURED
        # at session time — the bridge does not raise at startup so a
        # partial deployment (e.g. one provider configured, three not)
        # can still serve calls for the configured provider.
        self._elevenlabs = ElevenLabsService(server) if ElevenLabsService.is_configured() else None
        if self._elevenlabs is not None:
            logger.info("Registered backend: %s (botId prefix: %s:)",
                        type(self._elevenlabs).__name__, self._elevenlabs.name)
        self._gemini = GeminiService(server) if GeminiService.is_configured() else None
        if self._gemini is not None:
            logger.info("Registered backend: %s (botId prefix: %s:)",
                        type(self._gemini).__name__, self._gemini.name)
        self._openai = OpenAIService(server) if OpenAIService.is_configured() else None
        if self._openai is not None:
            logger.info("Registered backend: %s (botId prefix: %s:)",
                        type(self._openai).__name__, self._openai.name)
        self._xai = XaiService(server) if XaiService.is_configured() else None
        if self._xai is not None:
            logger.info("Registered backend: %s (botId prefix: %s:)",
                        type(self._xai).__name__, self._xai.name)
        if not any((self._elevenlabs, self._gemini, self._openai, self._xai)):
            logger.warning(
                "No AI providers configured at startup. bot.start with "
                "elevenlabs:/gemini:/openai:/xai: prefixes will be rejected "
                "with code 501. Set at least one of ELEVENLABS_API_KEY, "
                "GEMINI_API_KEY, OPENAI_API_KEY, or XAI_API_KEY to enable "
                "a provider. Echo (botId 'echo') remains available for "
                "testing."
            )
        self._active: Dict[str, str] = {}

    def _key(self, session_id: str, endpoint_id: str) -> str:
        """Return a per-(session, endpoint) key for the _active stash."""
        return f"{session_id}:{endpoint_id}"

    @staticmethod
    def _is_elevenlabs_bot_id(bot_id: str) -> bool:
        """True if bot_id has the elevenlabs:<agent_id> prefix."""
        return (bot_id or "").strip().lower().startswith("elevenlabs:")

    @staticmethod
    def _is_gemini_bot_id(bot_id: str) -> bool:
        """True if bot_id has the gemini:<model> prefix."""
        return (bot_id or "").strip().lower().startswith("gemini:")

    @staticmethod
    def _is_openai_bot_id(bot_id: str) -> bool:
        """True if bot_id has the openai:<model> prefix."""
        return (bot_id or "").strip().lower().startswith("openai:")

    @staticmethod
    def _is_xai_bot_id(bot_id: str) -> bool:
        """True if bot_id has the xai:<model> prefix (Grok Realtime)."""
        return (bot_id or "").strip().lower().startswith("xai:")

    @property
    def message_types(self) -> set[str]:
        """
        Return the message types this plugin claims with the lower-level
        ServiceRegistry. The dispatcher owns bot.start, bot.end, and
        session.dtmf — bot.start/bot.end for protocol routing by botId,
        session.dtmf so the bridge ingests DTMF events without surfacing
        SESSION_NOT_FOUND back to Infinity (the bridge does not currently
        dispatch DTMF to any provider; the event is logged and dropped).
        """
        return {"bot.start", "bot.end", "session.dtmf"}

    async def handle_message(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Dispatch an inbound RCMS message to the appropriate handler.

        Routes bot.start to _handle_bot_start, bot.end to
        _handle_bot_end, and acknowledges session.dtmf events without
        dispatching them. session.dtmf is unimplemented on the bridge:
        the message is logged at info level and dropped so Infinity
        does not see SESSION_NOT_FOUND. No provider currently handles
        DTMF.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.start / bot.end.
            RCMS spec §Session Message Definitions — session.dtmf.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed RCMS message envelope.

        Returns:
            None. Side effects vary by message type — see the per-handler
            docstrings.
        """
        msg_type = data.get("type", "")
        if msg_type == "bot.start":
            await self._handle_bot_start(websocket, client_id, data)
        elif msg_type == "bot.end":
            await self._handle_bot_end(websocket, client_id, data)
        elif msg_type == "session.dtmf":
            # DTMF is an unimplemented capability. The bridge ingests
            # the event so Infinity does not see SESSION_NOT_FOUND,
            # then drops it. No provider currently handles DTMF.
            #
            # Wire-frame logging of session.dtmf payloads is redacted by
            # default in bridge_server._redact_for_logging; set
            # LOG_DIGITS=true to disable redaction for troubleshooting
            # (PCI-relevant — DTMF events may carry cardholder data,
            # PINs, or other sensitive values during payment IVR flows).
            session_id = data.get("sessionId", "")
            logger.info(
                "[%s] DTMF event received on session %s; not dispatched "
                "(no provider implements DTMF). Content not logged by "
                "default; set LOG_DIGITS=true to enable digit logging "
                "for troubleshooting.",
                client_id, session_id,
            )
        else:
            logger.warning("Unhandled message type '%s' in CombinedBot service", msg_type)

    async def _handle_bot_start(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Validate, classify, and dispatch a bot.start to the matching
        provider plugin. Emit a failure-shape termination on every
        non-success exit path.

        Exit paths (each emits a distinct failure shape):
            MISSING_REQUIRED_FIELDS — endpointId or botId is empty in
                the inbound payload. Emits session.error rather than
                bot.ended because bot.ended's BotEndedPayload schema
                requires endpointId — which is exactly the missing
                field. This is a protocol-level message error, not a
                service failure.
            UNRECOGNIZED_BOTID_PREFIX — botId does not match echo,
                elevenlabs:*, gemini:*, openai:*, or xai:*. Emits
                bot.ended with failure-shape context.status (501
                UNSUPPORTED_SERVICE).
            BACKEND_NOT_CONFIGURED — botId prefix matched but the
                corresponding provider plugin was not instantiated at
                startup (provider's is_configured() returned False —
                usually a missing env-var API key). Emits bot.ended
                with failure-shape context.status (501 for AI providers,
                503 SERVICE_UNAVAILABLE for the Echo plugin missing
                case).
            Success — provider plugin's handle_message is invoked with
                the raw bot.start data. The plugin emits bot.started
                (or its own provider-specific failure-shape bot.ended).
                self._active is updated with the bound provider kind.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.start initiates
                a bot session.
            RCMS spec §Error Handling — "Any [service].start messages
                that cannot be processed are failed by returning a
                [service].end with the error in the payload." For the
                MISSING_REQUIRED_FIELDS case the spec is satisfied by
                session.error since bot.ended itself cannot be emitted
                without endpointId.
            RCMS spec §Status Codes — 501 UNSUPPORTED_SERVICE for
                UNRECOGNIZED_BOTID_PREFIX and BACKEND_NOT_CONFIGURED
                on AI providers; 503 SERVICE_UNAVAILABLE for the Echo
                plugin missing case.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed bot.start message. Read fields:
                sessionId, sequenceNum, payload.endpointId, payload.botId,
                payload.direction, payload.from, payload.to,
                payload.language, payload.domain, payload.ucid,
                payload.context.

        Returns:
            None. Side effects: either updates self._active and
            dispatches to a provider plugin (which emits bot.started on
            the websocket), or emits a failure-shape termination message
            on the websocket and returns early.

        See also:
            BUILDERS_GUIDE.md §3 Phase 1 — bot.start payload reference
            BUILDERS_GUIDE.md §3 Phase 3 — failure path → workflow branch
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        endpoint_id = payload.get("endpointId") or ""
        bot_id = (payload.get("botId") or "").strip()

        if not endpoint_id or not bot_id:
            # Stays on session.error (not bot.end) per RCMS §Error
            # Handling: the spec's "[service].start failures answered
            # with [service].end" rule applies when endpointId is in
            # hand. Here endpointId is exactly the missing field, and
            # bot.end's BotEndPayload schema requires it — so this
            # protocol-level message bug uses session.error instead.
            await self.server.send_session_error(
                websocket,
                client_id,
                session_id,
                message_type="bot.start",
                message_seq_num=data.get("sequenceNum"),
                code=501,
                reason="UNSUPPORTED_SERVICE",
                description="MISSING_REQUIRED_FIELDS: botId and endpointId are required",
                endpoint=endpoint_id or None,
            )
            return

        # Echo uses an exact match (botId == "echo"); AI providers use
        # a prefix match because their botIds carry an embedded suffix
        # selecting the specific agent or model — for example
        # "elevenlabs:agent_8001..." or
        # "gemini:gemini-2.5-flash-native-audio-preview". Echo has no
        # such variant; the botId is the literal string "echo".
        is_echo = bot_id.lower() == "echo"
        is_elevenlabs = self._is_elevenlabs_bot_id(bot_id)
        is_gemini = self._is_gemini_bot_id(bot_id)
        is_openai = self._is_openai_bot_id(bot_id)
        is_xai = self._is_xai_bot_id(bot_id)

        if not (is_echo or is_elevenlabs or is_gemini or is_openai or is_xai):
            # Per RCMS §Error Handling: an unprocessable bot.start is
            # failed by [service].end with the error in the payload.
            # The bridge uses bot.ended with failure-shaped
            # payload.context.status (see bridge/schema/rcms.schema.md):
            # Avaya drives session.end at sub-second after the
            # bot.ended emission, byobotEndContext populates verbatim
            # from payload.context for workflow Decision-module
            # routing, and the IVA module exits via SUCCESSFUL allowing
            # downstream Decision-module routing on
            # byobotEndContext.status.code. The endpoint_id parsed
            # from the inbound payload satisfies BotEndedPayload's
            # schema-required field.
            await self.server.send_bot_ended_with_failure_context(
                websocket,
                client_id,
                session_id,
                endpoint_id,
                code=501,
                reason="UNSUPPORTED_SERVICE",
                description=(
                    "UNRECOGNIZED_BOTID_PREFIX: botId must be 'echo', 'elevenlabs:<agent_id>', "
                    "'gemini:<model>', 'openai:<model>', or 'xai:<model>'"
                ),
            )
            return

        key = self._key(session_id, endpoint_id)
        if is_echo:
            echo_plugin = self.server.service_registry.get_plugin("echo")
            if not echo_plugin:
                # RCMS §Error Handling: an unprocessable bot.start is
                # failed via bot.ended-with-status, not session.end.
                # send_bot_ended_with_failure_context emits the
                # spec-compliant shape (status nested in payload.context
                # — see bridge/schema/rcms.schema.md); the IVA module's
                # FAILED branch wires to bot.ended-with-non-200-status.
                await self.server.send_bot_ended_with_failure_context(
                    websocket, client_id, session_id, endpoint_id,
                    code=503,
                    reason="SERVICE_UNAVAILABLE",
                    description="BACKEND_NOT_CONFIGURED: Echo plugin is not registered on this bridge.",
                )
                return
            await echo_plugin.handle_message(websocket, client_id, data)
            self._active[key] = "echo"
        elif is_elevenlabs:
            if self._elevenlabs is None:
                # Per RCMS §Error Handling, an unprocessable bot.start
                # is failed by [service].end with the error in the
                # payload. The bridge emits bot.ended with
                # failure-shaped context.status — see
                # bridge/schema/rcms.schema.md for the BotEndedPayload
                # shape. The botId prefix matched here; the backend
                # just wasn't instantiated at startup. Same spec rule
                # applies to the three sibling sites below
                # (gemini/openai/xai).
                await self.server.send_bot_ended_with_failure_context(
                    websocket, client_id, session_id,
                    endpoint_id,
                    code=501,
                    reason="UNSUPPORTED_SERVICE",
                    description=(
                        "BACKEND_NOT_CONFIGURED: backend 'elevenlabs' is not "
                        "configured on this bridge (ELEVENLABS_API_KEY env var "
                        "must be set to enable). botId prefix matched but the "
                        "backend was not instantiated at startup."
                    ),
                )
                return
            await self._elevenlabs.handle_message(websocket, client_id, data)
            self._active[key] = "elevenlabs"
        elif is_gemini:
            if self._gemini is None:
                # bot.ended-with-failure-context per the spec rule cited
                # at the elevenlabs site above (RCMS §Error Handling).
                await self.server.send_bot_ended_with_failure_context(
                    websocket, client_id, session_id,
                    endpoint_id,
                    code=501,
                    reason="UNSUPPORTED_SERVICE",
                    description=(
                        "BACKEND_NOT_CONFIGURED: backend 'gemini' is not "
                        "configured on this bridge (GEMINI_API_KEY env var "
                        "must be set to enable). botId prefix matched but the "
                        "backend was not instantiated at startup."
                    ),
                )
                return
            await self._gemini.handle_message(websocket, client_id, data)
            self._active[key] = "gemini"
        elif is_openai:
            if self._openai is None:
                # bot.ended-with-failure-context per the spec rule cited
                # at the elevenlabs site above (RCMS §Error Handling).
                await self.server.send_bot_ended_with_failure_context(
                    websocket, client_id, session_id,
                    endpoint_id,
                    code=501,
                    reason="UNSUPPORTED_SERVICE",
                    description=(
                        "BACKEND_NOT_CONFIGURED: backend 'openai' is not "
                        "configured on this bridge (OPENAI_API_KEY env var "
                        "must be set to enable). botId prefix matched but the "
                        "backend was not instantiated at startup."
                    ),
                )
                return
            await self._openai.handle_message(websocket, client_id, data)
            self._active[key] = "openai"
        elif is_xai:
            if self._xai is None:
                # bot.ended-with-failure-context per the spec rule cited
                # at the elevenlabs site above (RCMS §Error Handling).
                await self.server.send_bot_ended_with_failure_context(
                    websocket, client_id, session_id,
                    endpoint_id,
                    code=501,
                    reason="UNSUPPORTED_SERVICE",
                    description=(
                        "BACKEND_NOT_CONFIGURED: backend 'xai' is not "
                        "configured on this bridge (XAI_API_KEY env var "
                        "must be set to enable). botId prefix matched but the "
                        "backend was not instantiated at startup."
                    ),
                )
                return
            await self._xai.handle_message(websocket, client_id, data)
            self._active[key] = "xai"

    async def _handle_bot_end(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any],
    ) -> None:
        """
        Dispatch a bot.end to the provider plugin previously bound for
        this (session, endpoint) by the matching bot.start.

        Uses self._active.pop(key) to atomically read-and-clear the
        bound provider kind, so a bot.end without a preceding bot.start
        (or after the bot.start was already torn down by a prior
        bot.end / on_session_ended) leaves no orphan stash entry.
        Unknown keys produce a no-op, consistent with at-most-once
        delivery of bot.end.

        Spec:
            RCMS spec §AI Bot Message Definitions — bot.end terminates
            a bot session; the service responds with bot.ended.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier.
            data: the full parsed bot.end message. Read fields:
                sessionId, payload.endpointId.

        Returns:
            None. Side effects: pops the (session, endpoint) entry
            from self._active and forwards the raw bot.end to the
            bound provider plugin's handle_message. The provider emits
            bot.ended.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        endpoint_id = payload.get("endpointId") or ""

        key = self._key(session_id, endpoint_id)
        kind = self._active.pop(key, None)

        if kind == "echo":
            echo_plugin = self.server.service_registry.get_plugin("echo")
            if echo_plugin:
                await echo_plugin.handle_message(websocket, client_id, data)
        elif kind == "elevenlabs":
            await self._elevenlabs.handle_message(websocket, client_id, data)
        elif kind == "gemini":
            await self._gemini.handle_message(websocket, client_id, data)
        elif kind == "openai":
            await self._openai.handle_message(websocket, client_id, data)
        elif kind == "xai":
            await self._xai.handle_message(websocket, client_id, data)

    async def on_session_ended(self, session_id: str) -> None:
        """
        Propagate session-end notification to all configured AI provider
        plugins so each can emit bot.ended with CALLER_DISCONNECTED if
        it has not already emitted a termination. Clears the local
        _active stash for any keys belonging to this session.

        Echo is NOT fanned out from here. Echo is registered as a
        separate top-level plugin in server.service_registry, and
        BridgeServer.handle_session_end iterates the entire registry
        and calls plugin.on_session_ended directly on every registered
        plugin — including Echo. This dispatcher's on_session_ended
        only needs to fan out to AI providers because they are
        inner-managed instances of CombinedBotService and not
        separately registered with the service registry.

        Spec:
            RCMS spec §Session Message Definitions — session.end
                triggers plugin on_session_ended callbacks via the
                bridge's plugin lifecycle.
            RCMS spec §AI Bot Message Definitions — every active bot
                session must be paired with a bot.ended emission
                before session.ended is sent.

        Args:
            session_id: the RCMS session identifier from the inbound
                session.end. May not match any active dispatched session.

        Returns:
            None. Side effects: clears matching entries from
            self._active; invokes on_session_ended on every configured
            AI provider plugin.
        """
        # Echo is intentionally absent from the fan-out below: it is
        # a separate top-level plugin in server.service_registry and
        # receives on_session_ended directly from
        # BridgeServer.handle_session_end. Only AI providers (which
        # are inner-managed instance attributes here) need explicit
        # fan-out from this dispatcher.
        keys = [k for k in self._active if k.startswith(f"{session_id}:")]
        for k in keys:
            self._active.pop(k, None)
        if self._elevenlabs is not None:
            await self._elevenlabs.on_session_ended(session_id)
        if self._gemini is not None:
            await self._gemini.on_session_ended(session_id)
        if self._openai is not None:
            await self._openai.on_session_ended(session_id)
        if self._xai is not None:
            await self._xai.on_session_ended(session_id)

    async def shutdown(self) -> None:
        """
        Tear down every configured provider plugin during bridge
        shutdown. Called by the bridge framework when the server is
        stopping — providers should release any open upstream
        connections (provider WebSockets, HTTP clients) and cancel
        in-flight tasks.

        Args:
            None.

        Returns:
            None. Side effects: each configured provider's shutdown()
            coroutine is awaited.
        """
        if self._elevenlabs is not None:
            await self._elevenlabs.shutdown()
        if self._gemini is not None:
            await self._gemini.shutdown()
        if self._openai is not None:
            await self._openai.shutdown()
        if self._xai is not None:
            await self._xai.shutdown()

    async def ingest_audio_chunk(
        self,
        session_id: str,
        endpoint_id: str,
        source: str,
        audio_bytes: bytes,
    ) -> bool:
        """
        Forward an inbound media frame to the AI provider plugin bound
        to this (session, endpoint).

        Echo's audio loopback path is independent — BridgeServer's
        media handlers invoke Echo's maybe_echo_base64 /
        maybe_echo_binary directly. Only AI provider sessions are
        routed through this method.

        Spec:
            RCMS spec §Media Encoding Options — base64 and binary
            frame formats; this method receives the decoded
            audio_bytes regardless of transport.

        Args:
            session_id: RCMS session identifier.
            endpoint_id: media endpoint identifier from session.start.
            source: audio source — "rx" / "tx" / "none".
            audio_bytes: raw codec-encoded audio payload.

        Returns:
            True if the frame was forwarded to a bound AI provider;
            False if the (session, endpoint) is not bound to an AI
            provider (Echo session, session not yet bound, or already
            torn down).
        """
        kind = self._active.get(self._key(session_id, endpoint_id))
        if kind == "elevenlabs":
            return await self._elevenlabs.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
        if kind == "gemini":
            return await self._gemini.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
        if kind == "openai":
            return await self._openai.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
        if kind == "xai":
            return await self._xai.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
        # Echo handles its own audio through maybe_echo_* hooks; unknown kinds return False.
        return False


def register(server: "BridgeServer") -> CombinedBotService:
    """
    Plugin entry point invoked by the bridge during plugin discovery
    at server startup. Constructs the CombinedBotService instance and
    registers it with the server's service_registry under the name
    "bot".

    Args:
        server: the BridgeServer instance the dispatcher is being
            registered against. Imported under TYPE_CHECKING to avoid
            a runtime circular import.

    Returns:
        The constructed CombinedBotService plugin instance, already
        registered with server.service_registry.

    See also:
        BUILDERS_GUIDE.md §4 — Bridge Configuration and plugin discovery
    """
    plugin = CombinedBotService(server)
    server.register_service(plugin)
    return plugin
