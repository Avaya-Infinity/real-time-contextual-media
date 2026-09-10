#!/usr/bin/env python3
"""
bridge_server — RCMS protocol server: session lifecycle, media transport, plugin host

Role:
    The core RCMS protocol server. Owns the WebSocket lifecycle (TLS,
    JWT validation, per-connection state), the session lifecycle
    (session.start / session.started / session.end / session.ended /
    session.ping / session.pong / session.error), media frame routing
    in both transports (base64 and binary), the IngressStreamer audio
    paced delivery system, the service plugin registry, and the
    `bot.ended` helper family used by all provider plugins to emit
    spec-correct success / failure / disconnect terminations.

    Plugin discovery is owned by main.py (calls register() entry
    points on each plugin module). This module provides the
    ServicePlugin base class and ServiceRegistry catalog that
    plugins register against.

Does not own:
    Provider routing by botId (owned by bot_service.py — registered
    here as the "bot" plugin claiming bot.start / bot.end).
    Provider-specific AI logic, audio transcoding, upstream WebSocket
    management (owned by individual provider plugins under providers/).
    Echo loopback behavior (owned by providers/echo/bot_echo.py).
    Argparse / startup orchestration (owned by main.py).

Dependencies:
    websockets, websockets.server: WebSocket server framework.
    PyJWT: JWT primary/secondary key validation per RCMS §Security.
    G722 (optional): wideband codec encode/decode; gated by
        G722_AVAILABLE flag — bridge starts with a warning if absent
        and rejects only G.722-negotiated AI provider sessions.

RCMS lifecycle:
    Phase 1 (Start): handle_session_start performs codec / transport /
        mediaEndpoints negotiation and emits session.started. Plugin
        on_session_started hooks fire. bot.start is then routed to
        the bot dispatcher.
    Phase 2 (During): handle_media (base64) and handle_binary_frame
        (compact 16-byte format) decode inbound caller audio, update
        per-session counters, and forward to the bot plugin's
        ingest_audio_chunk. IngressStreamer paces outbound bot audio
        back to Infinity at the negotiated chunk_duration_ms.
    Phase 3 (Closure): handle_session_end fans out plugin
        on_session_ended hooks (Echo's caller-disconnect emission,
        each AI provider's), then emits session.ended. The
        send_bot_ended_with_*_context helpers are the spec-correct
        emission surface for success / failure / disconnect.

Spec:
    RCMS spec §Protocol Design — message envelope, sequence numbers
        starting at 1, ISO-8601 timestamps, session recovery shape.
    RCMS spec §Message Processing Rules — overlap rules, guard
        timers, duplicate handling.
    RCMS spec §Session Message Definitions — session.start /
        session.started / session.ping / session.pong / session.end /
        session.error.
    RCMS spec §AI Bot Message Definitions — bot.* envelopes;
        BotEndedPayload shape for the failure-context helpers.
    RCMS spec §Media Encoding Options — base64 and binary transports;
        Compact Binary Frame Header layout.
    RCMS spec §Error Handling — failure-shape contract for
        [service].start failures answered via [service].end.
    RCMS spec §Status Codes — full status code table consumed by
        send_session_error and the three send_bot_ended_with_*_context
        helpers.
    RCMS spec §Security — JWT bearer token validation, primary/
        secondary key rotation, HS256 signature verification.
    https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See bridge/schema/rcms.schema.json for the authoritative wire
    shape and bridge/schema/rcms.schema.md for behavioral notes —
    including bot.ended's payload.context.status nesting, the
    workflow's byobotEndContext consumption pattern, and the
    CALLER_DISCONNECTED reason value used by the platform-initiated
    termination helper.

See also:
    BUILDERS_GUIDE.md §3 — Call Lifecycle (Phases 1, 2, 3)
    BUILDERS_GUIDE.md §4 — Bridge Configuration
    BUILDERS_GUIDE.md §6 — Avaya Infinity: Security Keys & JWT
"""

import asyncio
import base64
import json
import logging
import importlib.util
import os
import struct
import threading
import time
import uuid
import sys as _sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Callable, Dict, Any, Optional, Set, Iterable

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.http import Headers
from websockets import Response
from http import HTTPStatus

import jwt
from jwt.exceptions import InvalidTokenError, ExpiredSignatureError

try:
    import G722 as g722  # type: ignore[import]
    G722_AVAILABLE = True
except ImportError:
    G722_AVAILABLE = False
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning("G722 module not available - install with 'pip install g722' for G722 codec support")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    _sys.modules.setdefault("bridge_server", _sys.modules[__name__])

message_logger = None

# Wire-frame logging redaction. Some inbound payloads contain field
# values that are problematic to log verbatim — DTMF digits being the
# canonical case, since DTMF can carry cardholder data on payment IVR
# and account-verification flows. Redaction is applied at both the
# journal log line and bridge_msg.txt via format_compact_json (the
# convergence point for both). Override with LOG_DIGITS=true to skip
# redaction (for troubleshooting; logs raw payloads — operators should
# understand the implications of the call flow before enabling).
#
# Rules dict maps message_type → list of dotted-path field names to
# redact. Add entries here when new fields need redaction; the
# wire-frame logger consults this dict, so no other code needs to
# change.
LOG_DIGITS = os.getenv("LOG_DIGITS", "false").lower() in ("true", "1")

_LOG_REDACTION_RULES: Dict[str, list] = {
    "session.dtmf": ["payload.digits"],
}


def _redact_for_logging(msg_type: str, data: Any) -> Any:
    """Return a deep copy of `data` with field values listed in
    `_LOG_REDACTION_RULES[msg_type]` replaced by `"<redacted>"`.

    Returns `data` unchanged when:
      - LOG_DIGITS=true (operator opt-out for debugging)
      - msg_type is not in _LOG_REDACTION_RULES (no rules apply)
      - any error occurs (logging never breaks the call path)

    The redacted output keeps the field present with a `"<redacted>"`
    sentinel rather than omitting the field, so the log unambiguously
    shows that the data was present and intentionally hidden.
    """
    if LOG_DIGITS:
        return data
    paths = _LOG_REDACTION_RULES.get(msg_type)
    if not paths:
        return data
    try:
        import copy as _copy
        redacted = _copy.deepcopy(data)
        for path in paths:
            parts = path.split(".")
            cursor = redacted
            for p in parts[:-1]:
                if not isinstance(cursor, dict) or p not in cursor:
                    cursor = None
                    break
                cursor = cursor[p]
            if isinstance(cursor, dict) and parts[-1] in cursor:
                cursor[parts[-1]] = "<redacted>"
        return redacted
    except Exception:
        return data


class ServicePlugin:
    """
    Base class that every service plugin (Echo, the bot dispatcher,
    each AI provider) subclasses to attach itself to the bridge.

    The plugin contract — every concrete plugin must define:
        name: str
            Plugin name registered with ServiceRegistry. Used for
            lookup-by-name (e.g. service_registry.get_plugin("echo"))
            by other parts of the bridge that need a specific plugin.
        message_types: Set[str]
            RCMS message types this plugin claims with the registry.
            The bridge's top-level message router consults
            ServiceRegistry.get_plugin_for_message(msg_type) to route
            inbound messages — so every type listed here is delivered
            to this plugin's handle_message. Plugins invoked indirectly
            (e.g. via a dispatcher) return an empty set.
        handle_message: async coroutine
            Mandatory entry point. Receives the active websocket, an
            opaque client_id, and the parsed RCMS message envelope.

    Optional hooks (default to no-op):
        on_session_started(session_id) — fired after session.start.
        on_session_ended(session_id) — fired after session.end.
        shutdown() — fired during bridge shutdown.

    Spec:
        RCMS spec §AI Bot Message Definitions / §Session Message
        Definitions — message types are defined by the spec; each
        plugin chooses which subset to handle.
    """

    name: str = "service"

    def __init__(self, server: "BridgeServer"):
        """Store the BridgeServer reference for plugin → server access
        (sequence numbers, ingress streamer, the bot.ended helpers,
        the service registry for sibling plugin lookup)."""
        self.server = server

    @property
    def message_types(self) -> Set[str]:
        """
        Return the RCMS message types this plugin claims with the
        registry.

        The registry warns and overrides on duplicate type registration
        — the most-recently-registered plugin wins. Plugins that are
        invoked indirectly (e.g. AI providers dispatched by
        bot_service.py) return an empty set so they're not picked up
        by message-type lookup.
        """
        return set()

    async def handle_message(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        data: Dict[str, Any]
    ) -> None:
        """
        Handle an inbound RCMS message routed to this plugin.

        Spec:
            RCMS spec §Message Processing Rules — plugins must respond
            within the spec's guard timer (2s for TTS/ASR, 10s for
            other services) or the bridge's WebSocket may be torn
            down by Avaya.

        Args:
            websocket: the active Infinity-side WebSocket connection.
            client_id: opaque session identifier for sequence numbering
                and log correlation.
            data: the full parsed RCMS message envelope.

        Raises:
            NotImplementedError: subclasses must override this method.
        """
        raise NotImplementedError("Service plugins must implement handle_message.")

    async def on_session_started(self, session_id: str) -> None:
        """
        Optional hook fired by BridgeServer.handle_session_start after
        a new session is created. Subclasses override to allocate per-
        session state. Default no-op.
        """
        return None

    async def on_session_ended(self, session_id: str) -> None:
        """
        Optional hook fired by BridgeServer.handle_session_end when a
        session terminates (caller hangup, workflow end, or operator
        action). Subclasses override to release per-session resources;
        bot-service plugins also use this hook to emit bot.ended with
        CALLER_DISCONNECTED status if no termination signal has been
        sent yet on the session. Default no-op.
        """
        return None

    async def shutdown(self) -> None:
        """
        Optional hook fired during bridge server shutdown. Subclasses
        override to release any global resources (upstream connections,
        background tasks). Default no-op.
        """
        return None


class ServiceRegistry:
    """
    Catalog of registered service plugins, indexed by both plugin name
    (for direct lookup) and by message type (for routing). Owned by
    BridgeServer; populated at startup by main.py's plugin discovery.

    Two indices, one per access pattern:
        _plugins: name → plugin. Used by callers that need a specific
            plugin (e.g. the bot dispatcher fetching the echo plugin
            by name to forward bot.start, or media handlers fetching
            "bot" to forward audio frames).
        _message_map: message_type → plugin. Used by the bridge's
            top-level message router to dispatch inbound messages
            without knowing which plugin claims them.

    Collision behavior:
        Duplicate name registration: ValueError raised — names must
            be unique.
        Duplicate message-type registration: warning logged; the
            most-recently-registered plugin wins. Common when a
            development reload registers the same plugin twice.
    """

    def __init__(self):
        """Initialize empty plugin and message-type indices."""
        self._plugins: Dict[str, ServicePlugin] = {}
        self._message_map: Dict[str, ServicePlugin] = {}

    @property
    def plugins(self) -> Iterable[ServicePlugin]:
        """Iterate over every registered plugin (no defined ordering)."""
        return self._plugins.values()

    def register(self, plugin: ServicePlugin) -> None:
        """
        Register a plugin under its name and claim each of its
        message_types in the dispatch map.

        Args:
            plugin: the ServicePlugin instance to register. Must have
                a unique `name`; duplicate names raise ValueError.

        Raises:
            ValueError: a plugin with the same name is already
                registered.
        """
        if plugin.name in self._plugins:
            raise ValueError(f"Service plugin '{plugin.name}' already registered.")

        self._plugins[plugin.name] = plugin

        for msg_type in plugin.message_types:
            if msg_type in self._message_map:
                existing = self._message_map[msg_type]
                logger.warning(
                    "Message type '%s' already handled by '%s'; overriding with '%s'",
                    msg_type,
                    existing.name,
                    plugin.name,
                )
            self._message_map[msg_type] = plugin

    def get_plugin(self, name: str) -> Optional[ServicePlugin]:
        """Return the registered plugin with this name, or None."""
        return self._plugins.get(name)

    def get_plugin_for_message(self, msg_type: str) -> Optional[ServicePlugin]:
        """Return the plugin claiming this RCMS message type, or None."""
        return self._message_map.get(msg_type)

    async def shutdown_all(self) -> None:
        """
        Invoke shutdown() on every registered plugin during bridge
        teardown. Plugin exceptions are logged but don't block other
        plugins from shutting down.
        """
        for plugin in self._plugins.values():
            try:
                await plugin.shutdown()
            except Exception:
                logger.exception("Error while shutting down service plugin '%s'", plugin.name)


# Compact Binary Frame Header constants — RCMS spec §Media Encoding Options.
# The 16-byte header is the only binary frame format on the wire; every
# field width and bit position below matches the spec's documented
# Compact Binary Frame layout.
#
# Flags: uint16 big-endian at byte 0-1 of the header. Bit 0 (LSB) marks
# the final frame of an utterance — partners reading this bit can detect
# end-of-turn without parsing payload contents. Bit 1 signals an optional
# 4-byte extension-length prefix follows the header. Bit 2 signals a
# codec change (requires extension data).
FLAG_LAST_FRAME_COMPACT = 0x0001  # Last frame in sequence
FLAG_EXTENSION = 0x0002  # Extension data present
FLAG_CODEC_CHANGE_COMPACT = 0x0004  # Codec change (requires extension)

# Source enum values for binary stream-id encoding (header bytes 2-3).
# Byte 2 carries the bid (0-255); byte 3 encodes the source as 0=none,
# 1=tx, 2=rx. The string forms ("none"/"tx"/"rx") match the RCMS
# spec's documented enum for media.src on JSON-transport messages, so
# the translation here is just a numeric encoding of the same enum.
SOURCE_NONE = 0
SOURCE_TX = 1
SOURCE_RX = 2
SOURCE_MAP = {"none": SOURCE_NONE, "tx": SOURCE_TX, "rx": SOURCE_RX}
SOURCE_REVERSE_MAP = {SOURCE_NONE: "none", SOURCE_TX: "tx", SOURCE_RX: "rx"}


def to_json_safe(obj: Any) -> Any:
    """
    Return a deep copy of obj using only JSON-serializable types (dict, list, str, int, float, bool, None).
    Converts protobuf types like MapComposite to plain dict so json.dumps() succeeds.
    """
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json_safe(item) for item in obj]
    # Dict-like protobuf (e.g. MapComposite from a google.protobuf source)
    if hasattr(obj, "items") and callable(getattr(obj, "items", None)):
        return to_json_safe(dict(obj))
    # List-like protobuf (e.g. RepeatedComposite)
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
        try:
            return to_json_safe(list(obj))
        except (TypeError, ValueError):
            pass
    return str(obj)


def format_compact_json(obj: Any, indent: int = 2) -> str:
    """
    Format JSON with compact arrays - keeps arrays on single lines with full content.
    Special case: mediaEndpoints array elements are formatted on separate lines.
    
    This makes logs more readable by avoiding multi-line formatting for arrays
    while keeping all the actual content visible.
    """
    def compact_list(lst):
        """Format a list compactly on one line with full content."""
        return json.dumps(lst, separators=(',', ': '))
    
    def format_value(value, level=0, key_name=None):
        """Format a value with proper indentation."""
        base_indent = " " * (indent * level)
        next_indent = " " * (indent * (level + 1))
        
        if isinstance(value, dict):
            if not value:
                return "{}"
            lines = ["{"]
            items = list(value.items())
            for i, (k, v) in enumerate(items):
                comma = "," if i < len(items) - 1 else ""
                formatted_v = format_value(v, level + 1, key_name=k)
                lines.append(f'{next_indent}"{k}": {formatted_v}{comma}')
            lines.append(base_indent + "}")
            return "\n".join(lines)
        elif isinstance(value, list):
            # Special formatting for mediaEndpoints array - each element on its own line
            if key_name == "mediaEndpoints":
                if not value:
                    return "[]"
                lines = ["["]
                for i, item in enumerate(value):
                    comma = "," if i < len(value) - 1 else ""
                    # Format each endpoint object compactly on one line
                    item_json = json.dumps(item, separators=(',', ': '))
                    lines.append(f'{next_indent}{item_json}{comma}')
                lines.append(base_indent + "]")
                return "\n".join(lines)
            else:
                # Keep other arrays compact on one line with full content
                return compact_list(value)
        elif isinstance(value, str):
            return json.dumps(value)
        else:
            # Convert dict-like protobuf types (e.g. MapComposite from a google.protobuf source)
            if hasattr(value, "items") and callable(getattr(value, "items", None)) and not isinstance(value, dict):
                return format_value(dict(value), level, key_name)
            try:
                return json.dumps(value)
            except (TypeError, ValueError):
                return json.dumps(str(value))
    
    return format_value(obj)


def log_message_exchange(direction: str, client_id: str, message_type: str, data: Dict[str, Any], 
                         is_media: bool = False) -> None:
    """
    Centralized function to log message exchanges to the message log file.
    
    Args:
        direction: "INBOUND" or "OUTBOUND"
        client_id: Client identifier
        message_type: Type of message (e.g., "session.start", "tts.complete")
        data: Message data dictionary
        is_media: True if this is a media message (audio data)
    """
    global message_logger

    if not message_logger:
        return

    # Skip media messages — bridge_msg.txt is for protocol frames only.
    if is_media:
        return
    
    try:
        # Apply redaction before formatting so bridge_msg.txt never
        # receives raw values flagged in _LOG_REDACTION_RULES (DTMF
        # digits, etc. — see _redact_for_logging for the full ruleset
        # and the rationale).
        formatted_message = format_compact_json(_redact_for_logging(message_type, data))

        # Log to message logger
        message_logger.info(f"[{client_id}] {direction} JSON ({message_type}): {formatted_message}")
    except Exception as e:
        # Don't let logging errors break the application, but log them for debugging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to log message exchange ({direction} {message_type}): {e}", exc_info=True)


def strip_wav_header(audio_data: bytes) -> bytes:
    """
    Strip WAV header from audio data if present.
    
    WAV files start with 'RIFF' followed by file size, 'WAVE', format chunks, and 'data'.
    This function detects and removes the WAV header, returning only raw PCM data.
    
    Args:
        audio_data: Audio data that may contain a WAV header
        
    Returns:
        Raw PCM audio data with WAV header removed (if it was present)
    """
    # Check if data starts with RIFF header (WAV format)
    if len(audio_data) < 44:
        return audio_data  # Too short to have a WAV header
    
    # WAV files start with "RIFF" (bytes 0-3)
    if audio_data[0:4] != b'RIFF':
        return audio_data  # No WAV header present
    
    # Check for "WAVE" at bytes 8-11
    if audio_data[8:12] != b'WAVE':
        return audio_data  # Not a valid WAV file
    
    # Find the "data" chunk - it should come after the format chunk
    # Start searching from byte 12 (after "WAVE")
    data_offset = audio_data.find(b'data', 12)
    if data_offset == -1:
        logger.warning("WAV header detected but 'data' chunk not found - returning original data")
        return audio_data
    
    # The data chunk structure is:
    # - 4 bytes: "data" (chunk ID)
    # - 4 bytes: chunk size (little-endian uint32)
    # - N bytes: actual audio data
    
    # Get the data chunk size from bytes [data_offset+4:data_offset+8]
    if data_offset + 8 > len(audio_data):
        logger.warning(f"WAV data chunk header incomplete - returning original data")
        return audio_data
    
    data_chunk_size = struct.unpack('<I', audio_data[data_offset+4:data_offset+8])[0]
    
    # The actual PCM data starts after the 8-byte header (4 bytes "data" + 4 bytes size)
    pcm_start = data_offset + 8
    
    if pcm_start >= len(audio_data):
        logger.warning(f"WAV header size ({pcm_start}) >= audio data size ({len(audio_data)}) - returning original data")
        return audio_data
    
    # Extract only the audio data
    pcm_data = audio_data[pcm_start:pcm_start + data_chunk_size]
    
    # Verify we got reasonable data
    if len(pcm_data) == 0:
        logger.warning("WAV header stripped but no PCM data found - returning original data")
        return audio_data
    
    # Check if PCM data length is even (required for 16-bit samples)
    if len(pcm_data) % 2 != 0:
        logger.warning(f"PCM data length {len(pcm_data)} is odd - truncating last byte for alignment")
        pcm_data = pcm_data[:-1]
    
    # Check if we got less data than expected
    actual_size = len(pcm_data)
    if actual_size < data_chunk_size:
        logger.warning(f"Got {actual_size} bytes but data chunk declared {data_chunk_size} bytes")
    elif pcm_start + data_chunk_size < len(audio_data):
        extra_bytes = len(audio_data) - (pcm_start + data_chunk_size)
        logger.info(f"Stripped WAV header: {pcm_start} bytes header + {extra_bytes} bytes trailing data removed, {len(pcm_data)} bytes raw PCM remaining")
    else:
        logger.info(f"Stripped WAV header: {pcm_start} bytes removed, {len(pcm_data)} bytes raw PCM remaining")
    
    # Log first few samples for debugging
    if len(pcm_data) >= 10:
        first_samples = struct.unpack('<5h', pcm_data[0:10])  # First 5 int16 samples
        logger.debug(f"First 5 PCM samples: {first_samples}")
    
    return pcm_data


def string_to_uuid(s: str) -> uuid.UUID:
    """
    Convert any string to a valid UUID.
    If the string is already a valid UUID, return it as-is.
    Otherwise, generate a deterministic UUID v5 from the string.
    """
    try:
        return uuid.UUID(s)
    except ValueError:
        namespace = uuid.UUID('6ba7b810-9dad-11d1-80b4-00c04fd430c8')
        return uuid.uuid5(namespace, s)


def build_stream_id(bid: int, source: str) -> bytes:
    """
    Encode a (bid, source) pair as the 2-byte stream identifier carried
    at bytes 2-3 of the Compact Binary Frame Header.

    Spec:
        RCMS spec §Media Encoding Options — Compact Binary Frame Header.

    Format:
        Byte 0: bid (uint8, 0-255)
        Byte 1: source enum (0=none, 1=tx, 2=rx)

    Args:
        bid: Bid number (0-255). Out-of-range values are clamped with
            a warning log rather than raising.
        source: Source string ("none", "tx", "rx"). Unknown values map
            to SOURCE_NONE.

    Returns:
        2-byte bytes object suitable for embedding directly in a
        Compact Binary Frame Header.

    Examples:
        build_stream_id(0, "tx") → b'\\x00\\x01'
        build_stream_id(0, "rx") → b'\\x00\\x02'
        build_stream_id(10, "none") → b'\\x0a\\x00'
        build_stream_id(255, "tx") → b'\\xff\\x01'
    """
    if bid < 0 or bid > 255:
        logger.warning(f"Invalid bid value: {bid}, clamping to 0-255")
        bid = max(0, min(255, bid))
    
    source_enum = SOURCE_MAP.get(source, SOURCE_NONE)
    return bytes([bid, source_enum])


def build_stream_id_key(bid: int, source: str) -> str:
    """
    Build an internal stream ID key string for lookup tables.
    
    Format: "<bid>:<source_enum>"
    
    Args:
        bid: Bid number (0-255)
        source: Source string ("none", "tx", "rx")
    
    Returns:
        String key for internal lookup (e.g., "0:1" for bid=0, source="tx")
    """
    source_enum = SOURCE_MAP.get(source, SOURCE_NONE)
    return f"{bid}:{source_enum}"


def parse_stream_id(stream_id: bytes) -> tuple[int, str]:
    """
    Decode the 2-byte stream identifier from bytes 2-3 of a Compact
    Binary Frame Header into a (bid, source) pair.

    Spec:
        RCMS spec §Media Encoding Options — Compact Binary Frame Header.

    Format:
        Byte 0: bid (uint8, 0-255)
        Byte 1: source enum (0=none, 1=tx, 2=rx)

    Args:
        stream_id: 2-byte binary stream identifier. Shorter inputs
            log a warning and return (0, "none") rather than raising.

    Returns:
        Tuple (bid, source_string). source_string is one of
        "none" / "tx" / "rx"; unknown enum values map to "none".

    Examples:
        parse_stream_id(b'\\x00\\x01') → (0, "tx")
        parse_stream_id(b'\\x01\\x02') → (1, "rx")
        parse_stream_id(b'\\x0a\\x00') → (10, "none")
    """
    if len(stream_id) < 2:
        logger.warning(f"Invalid stream ID length: {len(stream_id)}")
        return (0, "none")
    
    bid = stream_id[0]
    source_enum = stream_id[1]
    source = SOURCE_REVERSE_MAP.get(source_enum, "none")
    
    return (bid, source)


def parse_compact_binary_frame(frame_data: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse a compact 16-byte binary media frame off the wire into its
    component fields.

    Spec:
        RCMS spec §Media Encoding Options — Compact Binary Frame Header.
        The 16-byte header is the only binary frame format documented
        on the wire; field widths and bit positions match the spec.

    Format (16 bytes):
        Bytes 0-1:   Flags (uint16, big-endian)
        Byte 2:      Bid (0-255)
        Byte 3:      Source enum (0=none, 1=tx, 2=rx)
        Bytes 4-7:   Sequence number (uint32, big-endian)
        Bytes 8-15:  NTP timestamp in microseconds (uint64, big-endian)
        Bytes 16+:   [Optional extension] + Media payload

    Args:
        frame_data: complete binary frame as received from the
            WebSocket (16-byte header + optional extension + payload).

    Returns:
        Dict with parsed fields, or None if the frame is malformed
        (too short, bad extension length, or struct unpack failure):
            bid: Bid number (0-255)
            source: Source string ("none", "tx", "rx")
            streamID: Internal stream ID key for lookup ("<bid>:<src_enum>")
            sequenceNum: Per-stream sequence number
            timestamp: NTP timestamp in microseconds
            flags: Flag bits
            extension: Extension bytes (empty if FLAG_EXTENSION not set)
            payload: Audio payload bytes (codec-encoded)
    """
    if len(frame_data) < 16:
        logger.warning(f"Compact binary frame too short: {len(frame_data)} bytes (minimum 16)")
        return None
    
    try:
        # Parse 16-byte header
        flags = struct.unpack('>H', frame_data[0:2])[0]
        bid = frame_data[2]
        source_enum = frame_data[3]
        source = SOURCE_REVERSE_MAP.get(source_enum, "none")
        sequence_num = struct.unpack('>I', frame_data[4:8])[0]
        timestamp_micros = struct.unpack('>Q', frame_data[8:16])[0]
        
        offset = 16
        
        # Check for optional extension data
        extension_data = b''
        if (flags & FLAG_EXTENSION) != 0:
            if len(frame_data) < offset + 4:
                logger.warning("Frame too short for extension length")
                return None
            
            ext_len = struct.unpack('>I', frame_data[offset:offset+4])[0]
            offset += 4
            
            if len(frame_data) < offset + ext_len:
                logger.warning(f"Frame too short for extension data: {len(frame_data)} < {offset + ext_len}")
                return None
            
            extension_data = frame_data[offset:offset+ext_len]
            offset += ext_len
        
        # Extract media payload (remaining bytes)
        payload = frame_data[offset:]
        
        return {
            'bid': bid,
            'source': source,
            'streamID': build_stream_id_key(bid, source),
            'sequenceNum': sequence_num,
            'timestamp': timestamp_micros,
            'flags': flags,
            'extension': extension_data,
            'payload': payload
        }
    
    except Exception as e:
        logger.error(f"Error parsing compact binary frame: {e}")
        return None


def build_compact_binary_frame(bid: int, source: str, sequence_num: int, timestamp_micros: int,
                                flags: int, media_data: bytes, extension_data: bytes = b'') -> bytes:
    """
    Build a compact 16-byte binary media frame for transmission to
    Infinity over the WebSocket.

    Spec:
        RCMS spec §Media Encoding Options — Compact Binary Frame Header.
        Bid is clamped to 0-255 (uint8 width); a warning is logged on
        out-of-range input rather than raising, so a single bad call
        cannot break the call's audio stream.

    Format (16 bytes):
        Bytes 0-1:   Flags (uint16, big-endian)
        Byte 2:      Bid (0-255)
        Byte 3:      Source enum (0=none, 1=tx, 2=rx)
        Bytes 4-7:   Sequence number (uint32, big-endian)
        Bytes 8-15:  NTP timestamp in microseconds (uint64, big-endian)
        Bytes 16+:   [Optional extension] + Media payload

    Args:
        bid: Bid number (0-255). Out-of-range values are clamped.
        source: Source string ("none", "tx", "rx").
        sequence_num: Per-stream sequence number (uint32).
        timestamp_micros: NTP timestamp in microseconds (uint64).
        flags: Flag bits (uint16). FLAG_EXTENSION is auto-set when
            extension_data is non-empty.
        media_data: Audio payload bytes (codec-encoded).
        extension_data: Optional extension bytes; if present, prefixed
            with a uint32 length and FLAG_EXTENSION is set.

    Returns:
        Complete binary frame ready for websocket.send.
    """
    # Validate bid range
    if bid < 0 or bid > 255:
        logger.warning(f"Invalid bid value: {bid}, clamping to 0-255")
        bid = max(0, min(255, bid))
    
    source_enum = SOURCE_MAP.get(source, SOURCE_NONE)
    
    # Build header
    header = struct.pack('>H', flags)  # Flags (uint16)
    header += bytes([bid, source_enum])  # Bid + Source enum (2 bytes)
    header += struct.pack('>I', sequence_num)  # Sequence number (uint32)
    header += struct.pack('>Q', timestamp_micros)  # Timestamp (uint64)
    
    # Add optional extension data
    if extension_data:
        flags |= FLAG_EXTENSION
        header = struct.pack('>H', flags) + header[2:]  # Update flags
        ext_header = struct.pack('>I', len(extension_data))  # Extension length (uint32)
        return header + ext_header + extension_data + media_data
    else:
        return header + media_data


class SimpleSession:
    """
    Per-connection session state for bot and media routing.

    Lifecycle:
        Created in BridgeServer.handle_session_start when a session.start
        message arrives. Stored in BridgeServer.sessions keyed by
        session_id. Removed when the session ends (handle_session_end)
        or the connection drops (handle_connection's finally block).

    State carried:
        session_id: RCMS session identifier (preserved across
            session.recover per RCMS spec §Protocol Design).
        client_id: opaque connection identifier ("host:port") used for
            sequence numbering and log correlation.
        endpoint_id: media endpoint identifier (set when known; may be
            None until the first media frame).
        is_running: True between session.started and session.ended.
        media_events_this_second / media_bytes_this_second /
        last_media_log_time: per-endpoint counters used by the
            once-per-second "MEDIA SUMMARY" log line. Diagnostic only;
            not part of the protocol contract.
    """
    __slots__ = ("session_id", "client_id", "endpoint_id", "is_running",
                 "media_events_this_second", "media_bytes_this_second", "last_media_log_time")

    def __init__(self, session_id: str, client_id: str):
        self.session_id = session_id
        self.client_id = client_id
        self.endpoint_id: Optional[str] = None
        self.is_running: bool = False
        self.media_events_this_second: Dict[str, int] = {}
        self.media_bytes_this_second: Dict[str, int] = {}
        self.last_media_log_time: Dict[str, float] = {}


def get_sample_rate_for_codec(codec: str) -> int:
    """
    Return the expected sample rate (Hz) for a negotiated codec.

    Spec:
        RCMS spec §Media Encoding Options — codec sample rates are
        documented in the negotiation table at session.start.

    Args:
        codec: codec name as it appears in mediaCodecs (L16, PCMU,
            PCMA, G722).

    Returns:
        16000 for G722 (wideband), 8000 for everything else (narrowband
        default).

    Note: this helper duplicates codec math also present in
    IngressStreamer._get_chunk_size. Kept duplicated rather than
    extracted because the IngressStreamer version reads
    BridgeServer.session_config directly while this helper takes a
    string argument — different call surfaces. Known smell; flag for
    a future refactor.
    """
    if codec == "G722":
        return 16000
    else:
        return 8000


def get_chunk_size_for_codec(codec: str, duration_ms: int = 100) -> int:
    """
    Return the chunk size in bytes for a codec at a given chunk duration.

    Spec:
        RCMS spec §Media Encoding Options — frame sizing is partner-
        driven; this helper computes the byte count needed to carry
        `duration_ms` of audio at the codec's sample rate.

    Args:
        codec: codec name (L16, PCMU, PCMA, G722).
        duration_ms: chunk duration in milliseconds (default 100).

    Returns:
        Byte count for one chunk:
            L16: samples_per_chunk * 2 (16-bit samples)
            PCMU/PCMA: samples_per_chunk (8-bit samples)
            G722: (64000 * duration_ms) / 8000 — G.722 compresses to
                64kbps so the byte count is independent of sample rate.
            Unknown: 800 (100ms of 8kHz 8-bit audio — safe fallback).

    Note: codec math here duplicates IngressStreamer._get_chunk_size.
    See get_sample_rate_for_codec's note above.
    """
    sample_rate = get_sample_rate_for_codec(codec)
    samples_per_chunk = (sample_rate * duration_ms) // 1000

    if codec == "L16":
        # 16-bit samples = 2 bytes per sample
        return samples_per_chunk * 2
    elif codec in ("PCMU", "PCMA"):
        # 8-bit samples = 1 byte per sample
        return samples_per_chunk
    elif codec == "G722":
        # G722 compresses 16kHz to 64kbps = 8 bytes per ms
        return (64000 * duration_ms) // (8 * 1000)
    else:
        # Default to 800 bytes (100ms of 8kHz 8-bit audio)
        return 800


class IngressStreamer:
    """Paced ingress media streamer — owns the bridge → Infinity audio path.

    Role:
        Single broker that every provider feeds when it wants audio
        delivered to the caller's leg. Providers never send `media` frames
        directly — they call `queue_audio` (or `send_immediate` for the
        Echo low-latency path), and the streamer owns the framing,
        sequencing, timestamping, pacing, and `lastf` end-of-segment
        signaling. This centralization is what lets a mixed deployment
        (e.g. Echo segment followed by an AI segment on the same endpoint)
        present a monotonic `asn` series to Infinity.

    Does not own:
        Jitter buffer priming. That happens inside libgo on the platform
        side, which combines the first two ingress frames before injecting
        them into the caller's RTP stream. The streamer paces frames at
        chunk-cadence and lets libgo absorb sub-millisecond jitter; do not
        add bridge-side priming. Egress audio (caller → bridge → provider)
        is also out of scope — that flows through `BridgeServer.handle_media`
        and `handle_binary_frame`, not here.

    Dependencies:
        Reads `server.session_config[session_id]` for codec and sample rate
        (used by `_get_chunk_size` to size each slice correctly across
        L16/8kHz, L16/16kHz, PCMU, PCMA, G722). Reads
        `server.get_ingress_bid(session_id, endpoint_id)` for the per-call
        ingress bid that prefixes every `media` frame. The websocket and
        transport selector are passed into `queue_audio`/`send_immediate`
        and stashed under the endpoint key for `_streaming_loop` to recover.

    RCMS lifecycle alignment:
        State for an endpoint is created lazily on first `queue_audio` or
        `send_immediate` and lives until either `stop_and_clear` (commanded
        teardown) or the session ends. Sequence numbers initialize to 1 per
        RCMS envelope rule; timestamps initialize to wall-clock microseconds
        and advance by `chunk_duration_ms × 1000` per send. Barge-in and
        idle drain do not reset sequence/timestamp state — only
        `stop_and_clear` does.

    Spec:
        media frames — Compact Binary Frame Header (16 bytes, the only
        spec-supported binary format) for the binary transport, and the
        RCMS `media` JSON envelope with top-level `bid`/`asn`/`ts`/`lastf`/
        `audio` fields for the base64 transport. Ingress always uses
        `src="none"`. `lastf=true` is the end-of-segment marker.
        Authoritative reference: `bridge/schema/rcms.schema.md`
        ("media messages") and `bridge/schema/rcms.schema.json`. Avaya
        portal documentation:
        https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See also:
        `BridgeServer.get_ingress_bid` — ingress bid lookup.
        `bot_service.BotService` and provider plugins
        (`providers/*/bot_*.py`) — primary callers.
        `register_playout_done_callback` — natural-drain notification used
        by providers to gate barge-in decisions.
    """

    def __init__(self, server: "BridgeServer"):
        """Initialize the per-endpoint state maps.

        Contract:
            All operational state is keyed by
            `"<session_id>:<endpoint_id>"`. Maps are created empty and
            populated lazily on first `queue_audio` or `send_immediate` for
            the endpoint. `_stop_endpoint` is the only path that purges the
            key from every map; barge-in and idle drain leave most state in
            place so a follow-on `queue_audio` resumes sequence numbering.

        State maps:
            `_queues`: `asyncio.Queue` of `(chunk, is_last, session_id,
                endpoint_id, client_id)` tuples consumed by `_streaming_loop`.
            `_tasks`: streaming task per endpoint; recreated on demand by
                `queue_audio` after natural drain or cancellation.
            `_sequence_numbers`: next RCMS `asn` value for the endpoint.
                Initialized to 1 per RCMS envelope rule.
            `_timestamps`: next RCMS `ts` value in microseconds; advances
                by `chunk_duration_ms × 1000` per send.
            `_websockets`: websocket connection used by the streaming loop;
                stored at queue time so the loop can recover it.
            `_transports`: `"binary"` or `"base64"` selector controlling
                whether sends use Compact Binary Frame Header or JSON
                envelopes.
            `_chunk_duration_ms`: pacing interval in ms. Aligns with
                providers' `INGRESS_SUB_CHUNK_BYTES = 1280` at L16/8kHz so
                per-send timestamp advancement equals the audio duration
                actually carried. Providers must flush on multiples of
                this value (see `chunk_duration_ms` property).
            `_actively_streaming`: `True` between the first chunk of a
                segment and its `is_last` chunk. Read by `barge_in` to
                decide whether emitting a `lastf=true` marker is meaningful.
            `_last_frame_at`: `time.monotonic()` of the most recent send
                on the endpoint. Used by `barge_in`'s skipped path to log
                the wall-clock gap to the last outbound frame so
                inter-frame-idle barge-ins are distinguishable from
                "stream never started".
            `_playout_done_callbacks`: provider-registered zero-arg
                callbacks fired at natural drain points (5s idle or
                `is_last` sent). One slot per endpoint; re-registering
                replaces the prior callback. See
                `register_playout_done_callback`.

        Args:
            server: The owning `BridgeServer`. Used to look up
                per-session config (codec, sample rate) and the per-endpoint
                ingress bid via `server.get_ingress_bid`.
        """
        self.server = server
        self._queues: Dict[str, asyncio.Queue] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._sequence_numbers: Dict[str, int] = {}
        self._timestamps: Dict[str, int] = {}
        self._websockets: Dict[str, WebSocketServerProtocol] = {}
        self._transports: Dict[str, str] = {}
        self._chunk_duration_ms: int = 80
        self._actively_streaming: Dict[str, bool] = {}
        self._last_frame_at: Dict[str, float] = {}
        self._playout_done_callbacks: Dict[str, Callable[[], None]] = {}

    @property
    def chunk_duration_ms(self) -> int:
        """Per-chunk pacing interval in milliseconds.

        Contract:
            Public accessor for the value `_streaming_loop` uses to pace
            sends. Provider plugins that buffer audio before calling
            `queue_audio` MUST flush on multiples of this value. Otherwise
            the chunker emits an undersized final slice that the streaming
            loop still paces at the full chunk interval, creating a buffer
            underrun at Infinity that the caller hears as a click or gap.
            Reading this property is the supported way to align without
            duplicating the constant in each provider.

        Returns:
            Pacing interval in milliseconds (currently 80, matching the
            providers' INGRESS_SUB_CHUNK_BYTES = 1280 at L16/8kHz).
        """
        return self._chunk_duration_ms

    def _endpoint_key(self, session_id: str, endpoint_id: str) -> str:
        """Compose the per-endpoint state key used by every internal map.

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier within that session.

        Returns:
            `"<session_id>:<endpoint_id>"`. The same format is used as the
            prefix-match key in `stop_and_clear` when tearing down all
            endpoints for a session.
        """
        return f"{session_id}:{endpoint_id}"

    def _get_codec(self, session_id: str) -> str:
        """Look up the negotiated codec for a session.

        Args:
            session_id: Session identifier.

        Returns:
            Codec name from `session_config[session_id]["codec_name"]`,
            falling back to `"L16"` if the session is unknown or pre-config.
        """
        config = self.server.session_config.get(session_id, {})
        return config.get("codec_name", "L16")

    def _get_chunk_size(self, session_id: str) -> int:
        """Compute the byte size of one `chunk_duration_ms` slice at the session's codec.

        Contract:
            Sized off the session's actual sample rate, not a fixed 8kHz
            assumption. This matters for 16kHz L16 where the slice must be
            3200 bytes to represent 100ms (1600 bytes would be 50ms and the
            streaming loop would underrun by half). Falls back to L16 sizing
            for unknown codecs.

            Per-codec rules:
                * L16: 2 bytes/sample × `sample_rate` × `chunk_duration_ms / 1000`.
                * PCMU/PCMA: 1 byte/sample (G.711 is sample-rate-fixed at
                  8kHz so the result is `chunk_duration_ms × 8`).
                * G722: 64 kbps fixed bitrate regardless of sample rate, so
                  size is computed from bitrate alone (8 bytes/ms).
                * Anything else: L16 sizing.

        Args:
            session_id: Session identifier; codec and sample rate are
                pulled from `session_config[session_id]`.

        Returns:
            Slice size in bytes for one `chunk_duration_ms` of audio.
        """
        config = self.server.session_config.get(session_id, {})
        codec = config.get("codec_name", "L16")
        sample_rate = config.get("sample_rate", 8000)

        samples_per_chunk = (sample_rate * self._chunk_duration_ms) // 1000

        if codec == "L16":
            # 16-bit samples = 2 bytes per sample
            return samples_per_chunk * 2
        elif codec in ("PCMU", "PCMA"):
            # 8-bit samples = 1 byte per sample
            return samples_per_chunk
        elif codec == "G722":
            # G722 compresses 16kHz to 64kbps = 8 bytes per ms
            return (64000 * self._chunk_duration_ms) // (8 * 1000)
        else:
            # Default to L16 calculation
            return samples_per_chunk * 2

    def is_streaming(self, session_id: str, endpoint_id: str) -> bool:
        """Whether ingress audio is actively flowing for an endpoint.

        Contract:
            Used as the gate in `barge_in`. Returns `True` only between the
            first chunk of a segment and the `is_last=True` chunk — the
            window during which dropping a `lastf=true` marker into the
            stream is meaningful. Returns `False` when the streaming task
            exists but is parked on the queue (idle) or when no task has
            been started. The flag is set in `_streaming_loop` when a
            non-empty chunk is dequeued and cleared on `is_last`, idle
            timeout, or barge-in.

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier.

        Returns:
            `True` if the streaming loop is mid-segment, else `False`.
        """
        key = self._endpoint_key(session_id, endpoint_id)
        return self._actively_streaming.get(key, False)

    def register_playout_done_callback(
        self, session_id: str, endpoint_id: str, callback: Callable[[], None]
    ) -> None:
        """Register a per-endpoint callback fired when ingress playout drains.

        Contract:
            Providers call this once per endpoint, typically when their
            session attaches to the streamer, to learn when their queued
            audio has fully landed on the wire. The streamer fires the
            callback at two natural drain points: (a) the 5-second idle
            timeout in `_streaming_loop` (no audio queued for 5s), and (b) a
            chunk with `is_last=True` is sent. One callback slot per
            endpoint key — re-registering replaces the prior callback,
            and `_stop_endpoint` removes it during teardown.

            The callback runs synchronously on the streaming loop's thread.
            It must be short and exception-safe; exceptions are swallowed
            and logged at WARNING but the loop continues.

            Provider use case: clear an internal "audio is playing out"
            flag so the next VAD event can decide whether there is anything
            left to barge in on.

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier.
            callback: Zero-arg callable invoked at the next drain point.

        Returns:
            None.
        """
        key = self._endpoint_key(session_id, endpoint_id)
        self._playout_done_callbacks[key] = callback

    def _fire_playout_done(self, key: str) -> None:
        """Invoke the registered playout-done callback if one is set.

        Contract:
            Internal helper called from `_streaming_loop` at the two natural
            drain points (idle timeout, is_last chunk). Returns silently if
            no callback is registered. Exceptions raised by the callback are
            logged at WARNING and swallowed so a buggy provider callback
            cannot tear down the streaming loop.

        Args:
            key: Endpoint key in `"<session_id>:<endpoint_id>"` form.

        Returns:
            None.
        """
        cb = self._playout_done_callbacks.get(key)
        if not cb:
            return
        try:
            cb()
        except Exception as exc:
            logger.warning("playout_done callback for %s raised: %s", key, exc)

    async def mark_audio_segment_complete(
        self, session_id: str, endpoint_id: str, client_id: str
    ) -> None:
        """Enqueue a zero-byte sentinel that fires the end-of-segment path.

        Contract:
            Used by providers when their upstream model has declared audio
            generation finished (e.g. OpenAI Realtime's `response.output_audio.done`)
            and the bridge should send the RCMS end-of-segment marker
            promptly instead of waiting for the 5-second idle timeout. The
            method enqueues a `(b"", is_last=True, ...)` sentinel; the
            streaming loop's existing `is_last` handling emits a `lastf=true`
            frame and fires the playout-done callback.

            Returns silently if no queue exists for the endpoint key (no
            prior `queue_audio` for this endpoint, or already torn down).
            Does not initialize the queue itself — the segment cannot be
            terminated if it never began.

        Spec: `lastf=true` is the RCMS end-of-segment marker. See
            `bridge/schema/rcms.schema.md` "media messages".

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier.
            client_id: Client identifier carried through to the streaming
                loop's log lines for the terminating frame.

        Returns:
            None.
        """
        key = self._endpoint_key(session_id, endpoint_id)
        if key not in self._queues:
            return  # Nothing was streaming; no segment to terminate.
        await self._queues[key].put((b"", True, session_id, endpoint_id, client_id))

    async def queue_audio(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        audio_bytes: bytes,
        is_last: bool = False,
        transport: str = "binary",
    ) -> None:
        """Split audio into chunk-pace slices, enqueue, and ensure the streaming task is running.

        Contract:
            Primary entrypoint used by AI providers to deliver TTS or model
            audio to the bridge. The byte stream is split into chunks sized
            for the session's codec and sample rate (`_get_chunk_size`)
            so each queued slice represents `chunk_duration_ms` of audio
            once decoded by Infinity. Slices are appended to the per-endpoint
            FIFO queue and the streaming task is started lazily — if no
            task is running for this endpoint key, one is created via
            `asyncio.create_task(_streaming_loop(...))`.

            The `is_last` flag is propagated only to the final slice of this
            call. Earlier slices carry `is_last=False` regardless of the
            argument. This means a single `queue_audio(is_last=True)` call
            terminates the segment; mid-segment calls (`is_last=False`) just
            extend it.

            On first use of an endpoint key the per-endpoint sequence number
            is initialized to 1 (RCMS spec) and the timestamp to current
            wall-clock microseconds. After that, both advance inside
            `_streaming_loop` as each chunk is sent — `queue_audio` does not
            stamp them at queue time.

        Pacing alignment: provider plugins that buffer audio before calling
            `queue_audio` MUST flush on multiples of `chunk_duration_ms`.
            Otherwise the chunker produces a final undersized slice that
            `_streaming_loop` paces at the same `chunk_interval` as a full
            slice, creating a buffer underrun at Infinity. The
            `chunk_duration_ms` property exposes the value so providers can
            align without duplicating it.

        Spec: media frames — `bridge/schema/rcms.schema.md` "media
            messages". Sequence numbers start at 1 per RCMS envelope rule.

        Args:
            websocket: WebSocket connection used by `_streaming_loop`. Stored
                under the endpoint key so the loop can recover it without
                re-plumbing.
            client_id: Client identifier, used for log lines.
            session_id: Session identifier.
            endpoint_id: Endpoint identifier.
            audio_bytes: Raw encoded audio in the session's negotiated codec.
                Empty payload returns silently without touching state.
            is_last: When `True`, the final slice produced from this byte
                buffer carries the `lastf=true` end-of-segment marker.
            transport: `"binary"` or `"base64"`. Determines whether
                `_streaming_loop` emits Compact Binary Frame Header frames
                or JSON `media` envelopes.

        Returns:
            None.
        """
        if not audio_bytes:
            return

        key = self._endpoint_key(session_id, endpoint_id)
        
        # Store websocket and transport for this endpoint
        self._websockets[key] = websocket
        self._transports[key] = transport

        # Create queue if needed
        if key not in self._queues:
            self._queues[key] = asyncio.Queue()
            # RCMS spec: ingress sequence numbers start at 1 (not 0).
            self._sequence_numbers[key] = 1
            self._timestamps[key] = int(time.time() * 1_000_000)

        # Split audio into chunks based on session's codec and sample rate
        chunk_size = self._get_chunk_size(session_id)
        config = self.server.session_config.get(session_id, {})
        sample_rate = config.get("sample_rate", 8000)
        codec = config.get("codec_name", "L16")
        chunk_duration_ms = (chunk_size * 1000) // (sample_rate * 2) if codec == "L16" else self._chunk_duration_ms
        
        logger.info(
            "[%s] INGRESS CHUNK SIZE: %d bytes = %dms at %dHz (%s) for endpoint %s",
            client_id, chunk_size, chunk_duration_ms, sample_rate, codec, endpoint_id
        )
        
        offset = 0
        total_bytes = len(audio_bytes)
        
        while offset < total_bytes:
            chunk_end = min(offset + chunk_size, total_bytes)
            chunk = audio_bytes[offset:chunk_end]
            
            # Determine if this chunk is the last one
            chunk_is_last = is_last and (chunk_end >= total_bytes)
            
            # Queue the chunk
            await self._queues[key].put((chunk, chunk_is_last, session_id, endpoint_id, client_id))
            offset = chunk_end

        logger.debug(
            "[%s] Queued %d bytes (%d chunks) for endpoint %s, is_last=%s",
            client_id, total_bytes, (total_bytes + chunk_size - 1) // chunk_size, 
            endpoint_id, is_last
        )

        # Start streaming task if not already running
        if key not in self._tasks or self._tasks[key].done():
            self._tasks[key] = asyncio.create_task(self._streaming_loop(key))

    async def send_immediate(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        audio_bytes: bytes,
        is_last: bool = False,
        transport: str = "binary",
    ) -> bool:
        """Send a single `media` frame straight to the wire, bypassing the queue.

        Contract:
            Used by services that already produce audio at real-time pace and
            therefore must not be re-paced by `_streaming_loop` — primarily
            the Echo provider, which mirrors each inbound egress packet back
            on the ingress path frame-for-frame. The send still goes through
            centralized sequence-number and timestamp accounting so that a
            mixed deployment (e.g. an Echo segment followed by an AI segment)
            preserves a monotonic `asn` series on the same endpoint key.

            If no ingress bid has been allocated for the endpoint (the
            `MediaTransportSelected` exchange has not completed), the send is
            skipped and `False` is returned. The caller is expected to drop
            the chunk; no buffering happens here.

        Spec: media binary frame — Compact Binary Frame Header (16 bytes),
            `src="none"` for ingress, flag bit 0 = is_last. JSON envelope
            uses top-level `bid`/`asn`/`ts`/`audio` and optional `lastf`.
            See `bridge/schema/rcms.schema.md` "media messages".

        Args:
            websocket: WebSocket connection used for the send.
            client_id: Client identifier, used in log lines.
            session_id: Session identifier.
            endpoint_id: Endpoint identifier; combined with `session_id` to
                form the per-endpoint state key.
            audio_bytes: One frame of encoded audio. Empty payload returns
                `False` without sending.
            is_last: When `True`, sets the `lastf` end-of-segment flag.
            transport: `"binary"` (Compact Binary Frame Header) or any other
                value (JSON `media` envelope with base64 audio).

        Returns:
            `True` if the frame was sent. `False` for an empty payload, a
            missing ingress bid, or a websocket send exception.
        """
        if not audio_bytes:
            return False

        # Get ingress bid for this endpoint
        bid = self.server.get_ingress_bid(session_id, endpoint_id)
        if bid is None:
            logger.warning(
                "[%s] No ingress bid for endpoint %s, cannot send immediate",
                client_id, endpoint_id
            )
            return False

        key = self._endpoint_key(session_id, endpoint_id)
        
        # Initialize sequence number if needed — RCMS spec: ingress seq starts at 1.
        if key not in self._sequence_numbers:
            self._sequence_numbers[key] = 1
            self._timestamps[key] = int(time.time() * 1_000_000)

        # Get sequence number and timestamp
        seq = self._sequence_numbers.get(key, 0)
        ts = self._timestamps.get(key, int(time.time() * 1_000_000))
        
        # Update for next send
        self._sequence_numbers[key] = seq + 1
        self._timestamps[key] = ts + (self._chunk_duration_ms * 1000)

        try:
            if transport == "binary":
                flags = 0x0001 if is_last else 0
                frame = build_compact_binary_frame(
                    bid=bid,
                    source="none",  # Ingress uses source "none"
                    sequence_num=seq,
                    timestamp_micros=ts,
                    flags=flags,
                    media_data=audio_bytes,
                )
                await websocket.send(frame)
            else:
                # Base64 JSON format
                media_msg = {
                    "type": "media",
                    "bid": bid,
                    "asn": seq,
                    "ts": ts,
                    "audio": base64.b64encode(audio_bytes).decode("utf-8"),
                }
                if is_last:
                    media_msg["lastf"] = True
                await websocket.send(json.dumps(media_msg))
                log_message_exchange("OUTBOUND", client_id, "media", media_msg, is_media=True)

            # Track wall-clock send time and emit the unified INGRESS FRAME
            # log shared with _streaming_loop and barge_in. The wall-clock
            # value is used by barge_in's skipped-path log to correlate a
            # barge-in event against the most recent outbound frame.
            self._last_frame_at[key] = time.monotonic()
            flags_for_log = 0x0001 if is_last else 0
            logger.info(
                "[%s] INGRESS FRAME: bid=%d seq=%d bytes=%d flags=0x%04x",
                client_id, bid, seq, len(audio_bytes), flags_for_log,
            )
            return True

        except Exception as e:
            logger.warning("[%s] Error sending immediate ingress: %s", client_id, e)
            return False

    async def barge_in(self, session_id: str, endpoint_id: str) -> None:
        """Interrupt active playout and emit a `lastf=true` end-of-segment marker.

        Contract:
            Used when the provider's VAD or upstream model decides the caller
            has started speaking and the in-flight TTS audio should stop. The
            sequence is: cancel the streaming task (so no in-flight chunk
            sneaks past the interrupt), drain any queued chunks, then send a
            zero-byte `media` frame with `lastf=true` to tell Infinity the
            segment has ended. The next `queue_audio` call rebuilds the
            streaming task and continues sequence numbering from where this
            method left it — barge_in does not reset sequence/timestamp state.

            Guarded by `is_streaming`: if the actively-streaming flag is
            already cleared (last chunk drained, idle timeout fired, or stream
            never started), the method returns without sending any frame.
            This prevents stray `lastf=true` markers during inter-segment
            silence which Infinity treats as a protocol anomaly.

            The skipped path logs the wall-clock delta from the most recent
            outbound frame so a small gap (e.g. <100ms) — barge-in firing
            during the inter-frame idle window of what is effectively still
            active playback — is distinguishable from a true "nothing is
            playing" state.

        Spec: zero-byte `media` with `lastf=true` is the RCMS end-of-segment
            signal. See `bridge/schema/rcms.schema.md` "media messages" for
            the binary frame layout (flag bit 0 = is_last) and the JSON
            envelope (`lastf` boolean).

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier.

        Returns:
            None. Returns silently when `is_streaming` is false.
        """
        key = self._endpoint_key(session_id, endpoint_id)

        # Check if we're actually streaming before doing anything
        was_streaming = self.is_streaming(session_id, endpoint_id)
        if not was_streaming:
            # Log the wall-clock gap from the last outbound frame so the
            # skipped path is distinguishable from "stream never started".
            # A small delta (sub-100ms) means the is_streaming guard is
            # firing during what is effectively still active playback —
            # i.e. barge-in arrived during the inter-frame idle gap.
            last = self._last_frame_at.get(key, 0.0)
            if last:
                delta_ms = (time.monotonic() - last) * 1000.0
                logger.info(
                    "BARGE-IN skipped (no active streaming) for %s:%s: last frame %.1fms ago",
                    session_id, endpoint_id, delta_ms,
                )
            else:
                logger.info(
                    "BARGE-IN skipped (no active streaming) for %s:%s: no prior frame on this stream",
                    session_id, endpoint_id,
                )
            return
        
        # Clear actively streaming flag since we're interrupting
        self._actively_streaming[key] = False
        
        # Cancel the streaming task to stop any in-flight chunk from being sent
        task = self._tasks.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(
                "BARGE-IN: Cancelled streaming task for %s:%s",
                session_id, endpoint_id
            )
        
        # Clear pending chunks from queue (but keep the queue itself for reuse)
        queue = self._queues.get(key)
        if queue:
            chunks_cleared = 0
            while not queue.empty():
                try:
                    queue.get_nowait()
                    chunks_cleared += 1
                except asyncio.QueueEmpty:
                    break
            if chunks_cleared > 0:
                logger.info(
                    "BARGE-IN: Cleared %d pending chunks for %s:%s",
                    chunks_cleared, session_id, endpoint_id
                )
        
        # Send empty chunk with last flag to signal barge-in
        websocket = self._websockets.get(key)
        transport = self._transports.get(key, "binary")
        bid = self.server.get_ingress_bid(session_id, endpoint_id)
        
        if websocket and bid is not None:
            # Get sequence number and timestamp
            seq = self._sequence_numbers.get(key, 0)
            ts = self._timestamps.get(key, int(time.time() * 1_000_000))
            
            # Update for next chunk
            self._sequence_numbers[key] = seq + 1
            self._timestamps[key] = ts + (self._chunk_duration_ms * 1000)
            
            try:
                if transport == "binary":
                    # Send empty frame with last flag
                    flags = 0x0001  # is_last flag
                    frame = build_compact_binary_frame(
                        bid=bid,
                        source="none",
                        sequence_num=seq,
                        timestamp_micros=ts,
                        flags=flags,
                        media_data=b"",  # Empty audio data
                    )
                    await websocket.send(frame)
                else:
                    # Base64 JSON format with last flag
                    media_msg = {
                        "type": "media",
                        "bid": bid,
                        "asn": seq,
                        "ts": ts,
                        "audio": "",  # Empty audio
                        "lastf": True,
                    }
                    await websocket.send(json.dumps(media_msg))
                    log_message_exchange("OUTBOUND", f"{session_id}:{endpoint_id}", "media (barge-in)", media_msg, is_media=True)
                
                # Track wall-clock send time and emit the unified INGRESS
                # FRAME log so all three send sites produce the same log
                # shape (used by the skipped-barge-in delta calculation).
                self._last_frame_at[key] = time.monotonic()
                logger.info(
                    "[%s:%s] INGRESS FRAME: bid=%d seq=%d bytes=0 flags=0x0001 (barge-in lastf)",
                    session_id, endpoint_id, bid, seq,
                )
                logger.info(
                    "BARGE-IN: Sent last flag for %s:%s (bid=%d, seq=%d)",
                    session_id, endpoint_id, bid, seq
                )
            except Exception as e:
                logger.warning("BARGE-IN: Error sending last flag for %s:%s: %s", session_id, endpoint_id, e)

    async def stop_and_clear(self, session_id: str, endpoint_id: str = None) -> None:
        """Tear down ingress streaming for an endpoint or for an entire session.

        Contract:
            Called when the platform commands the bridge to stop sending audio
            for an endpoint, when an endpoint is removed, and when a session
            ends. Differs from `barge_in` in that no `lastf=true` marker is
            sent — this is a permanent teardown, not an interrupt that the
            caller should resume after. The endpoint key is removed from every
            internal map; sequence numbers do not survive a stop_and_clear, so
            the next `queue_audio` for the same endpoint key restarts at the
            initial RCMS value.

            With no `endpoint_id` argument, every endpoint key prefixed with
            `"<session_id>:"` is torn down. Used during session shutdown.

        Args:
            session_id: Session identifier.
            endpoint_id: Specific endpoint to stop, or `None` to stop every
                endpoint streamer registered under `session_id`.

        Returns:
            None.
        """
        if endpoint_id:
            # Stop specific endpoint
            key = self._endpoint_key(session_id, endpoint_id)
            await self._stop_endpoint(key)
        else:
            # Stop all endpoints for session
            keys_to_stop = [k for k in self._queues.keys() if k.startswith(f"{session_id}:")]
            for key in keys_to_stop:
                await self._stop_endpoint(key)

    async def _stop_endpoint(self, key: str) -> None:
        """Cancel the streaming task and purge every per-endpoint map entry.

        Contract:
            Internal helper used by `stop_and_clear`. Cancels the streaming
            task (awaiting cancellation so no further sends fire on the
            websocket after return), drains any queued chunks, and removes the
            entry from each per-endpoint map: queue, sequence number,
            timestamp, websocket, transport, actively-streaming flag,
            playout-done callback, and last-frame wall-clock. After this
            returns the streamer holds no state for the endpoint and a fresh
            `queue_audio` call will rebuild it from the initial RCMS values.

        Args:
            key: Endpoint key in `"<session_id>:<endpoint_id>"` form.

        Returns:
            None.
        """
        # Cancel task
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Clear queue
        queue = self._queues.pop(key, None)
        if queue:
            # Drain queue
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # Clean up tracking — every per-endpoint map must drop this key
        # so the next queue_audio rebuilds state from the initial RCMS
        # values (sequenceNum starts at 1, fresh playout-done callback).
        self._sequence_numbers.pop(key, None)
        self._timestamps.pop(key, None)
        self._websockets.pop(key, None)
        self._transports.pop(key, None)
        self._actively_streaming.pop(key, None)
        self._playout_done_callbacks.pop(key, None)
        self._last_frame_at.pop(key, None)

        logger.debug("Stopped ingress streamer for %s", key)

    async def _streaming_loop(self, key: str) -> None:
        """Drain the per-endpoint queue and emit `media` frames at chunk-pace cadence.

        Contract:
            One streaming task per endpoint key. Started lazily by `queue_audio`
            and exits when (a) the queue stays empty for 5 seconds, (b) the task
            is cancelled (barge-in or stop), or (c) the websocket send raises.
            Frames are paced by absolute timing: target send time for chunk N is
            `pacing_start_time + N * chunk_interval`. This avoids drift that
            would accumulate from sleeping a fixed delta after each send. If the
            send falls more than one interval behind the schedule, a warning is
            logged but pacing is not reset — Infinity tolerates short bursts but
            sustained pacing slip causes audible underruns at the caller's leg.

            Each chunk is wrapped as RCMS `media` (binary frame for `transport
            == "binary"`, JSON envelope otherwise). Per-endpoint sequence number
            and timestamp are advanced in lockstep with each send. The is_last
            flag from the queued tuple becomes wire-level `lastf=true` (binary
            flag bit 0 / JSON `lastf` field), which Infinity treats as the
            end-of-segment marker.

            On exit the streaming task entry is popped but the queue and
            sequence/timestamp/websocket maps are left intact: a subsequent
            `queue_audio` call will start a fresh streaming task and resume
            sequence numbering where this one left off.

        Drain semantics:
            Two natural drain points fire the registered playout-done callback:
            5-second idle timeout (no audio queued) and `is_last=True` chunk
            sent. Providers use that callback to clear their own
            "audio is playing out" flag so subsequent VAD events can decide
            whether there is anything left to barge in on.

        Spec: media binary frame layout — Compact Binary Frame Header (16
            bytes); JSON envelope at `bridge/schema/rcms.schema.md` "media
            messages". `bid`/`asn`/`ts`/`lastf`/`audio` are top-level fields,
            not nested in payload.

        Args:
            key: Endpoint key in `"<session_id>:<endpoint_id>"` form. The
                websocket, transport, sequence number, and timestamp are all
                looked up under this key.

        Returns:
            None. Exits silently on idle timeout, cancellation, or send error.
        """
        queue = self._queues.get(key)
        if not queue:
            return

        chunk_interval = self._chunk_duration_ms / 1000.0  # seconds
        chunks_sent = 0
        pacing_start_time = None  # When current audio segment started

        try:
            while True:
                # Wait for next chunk with timeout
                try:
                    chunk_data = await asyncio.wait_for(queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 5s of no queued audio is treated as a natural drain.
                    # Clear the actively-streaming flag and notify the provider
                    # so its barge-in gating sees the playout as finished.
                    self._actively_streaming[key] = False
                    self._fire_playout_done(key)
                    logger.debug("Ingress streamer idle for %s, pausing", key)
                    break

                chunk, is_last, session_id, endpoint_id, client_id = chunk_data
                
                # Mark as actively streaming when we have audio to send
                if len(chunk) > 0:
                    self._actively_streaming[key] = True
                
                # Get websocket and transport
                websocket = self._websockets.get(key)
                transport = self._transports.get(key, "binary")
                
                if not websocket:
                    logger.warning("No websocket for %s, dropping chunk", key)
                    continue

                # Get ingress bid for this endpoint
                bid = self.server.get_ingress_bid(session_id, endpoint_id)
                if bid is None:
                    logger.warning(
                        "[%s] No ingress bid for endpoint %s, dropping chunk",
                        client_id, endpoint_id
                    )
                    continue

                # Get sequence number and timestamp
                seq = self._sequence_numbers.get(key, 0)
                ts = self._timestamps.get(key, int(time.time() * 1_000_000))
                
                # Update for next chunk
                self._sequence_numbers[key] = seq + 1
                self._timestamps[key] = ts + (self._chunk_duration_ms * 1000)

                # Send the chunk
                try:
                    if transport == "binary":
                        flags = 0x0001 if is_last else 0
                        frame = build_compact_binary_frame(
                            bid=bid,
                            source="none",  # Ingress uses source "none"
                            sequence_num=seq,
                            timestamp_micros=ts,
                            flags=flags,
                            media_data=chunk,
                        )
                        await websocket.send(frame)
                    else:
                        # Base64 JSON format
                        media_msg = {
                            "type": "media",
                            "bid": bid,
                            "asn": seq,
                            "ts": ts,
                            "audio": base64.b64encode(chunk).decode("utf-8"),
                        }
                        if is_last:
                            media_msg["lastf"] = True
                        await websocket.send(json.dumps(media_msg))
                        log_message_exchange("OUTBOUND", client_id, "media", media_msg, is_media=True)

                    # Track wall-clock send time for barge-in instrumentation
                    # (see barge_in()) and emit the unified INGRESS FRAME log
                    # used across all three send sites — _streaming_loop,
                    # send_immediate, and barge_in's lastf marker.
                    self._last_frame_at[key] = time.monotonic()
                    flags_for_log = 0x0001 if is_last else 0
                    logger.info(
                        "[%s] INGRESS FRAME: bid=%d seq=%d bytes=%d flags=0x%04x",
                        client_id, bid, seq, len(chunk), flags_for_log,
                    )

                    # Initialize pacing on first chunk of segment
                    if pacing_start_time is None:
                        pacing_start_time = time.monotonic()
                        logger.info(
                            "[%s] INGRESS STREAMER: first chunk bid=%d seq=%d size=%d transport=%s",
                            client_id, bid, seq, len(chunk), transport
                        )

                    chunks_sent += 1

                except Exception as e:
                    logger.warning("[%s] Error sending ingress chunk: %s", client_id, e)
                    break

                # Reset timing for next audio segment after is_last
                if is_last:
                    # End-of-segment marker reached. Clear the streaming flag
                    # and notify the provider that playout has drained so
                    # subsequent VAD events know there is nothing left to
                    # barge in on.
                    self._actively_streaming[key] = False
                    self._fire_playout_done(key)
                    logger.debug(
                        "[%s] Ingress last chunk sent, resetting timing for next segment",
                        client_id
                    )
                    chunks_sent = 0
                    pacing_start_time = None
                    continue  # Wait for next audio segment

                # Precise real-time pacing using absolute timing
                target_time = pacing_start_time + (chunks_sent * chunk_interval)
                now = time.monotonic()
                sleep_time = target_time - now
                
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                elif sleep_time < -chunk_interval:
                    # We're more than one interval behind - log warning
                    logger.warning(
                        "[%s] Ingress pacing falling behind by %.1fms",
                        client_id, -sleep_time * 1000
                    )

        except asyncio.CancelledError:
            logger.debug("Ingress streamer cancelled for %s", key)
            raise
        except Exception as e:
            logger.error("Error in ingress streamer for %s: %s", key, e, exc_info=True)
        finally:
            # Clean up task reference but keep queue for potential reuse
            self._tasks.pop(key, None)


class BridgeServer:
    """RCMS WebSocket server — owns session, media routing, and the connection lifecycle.

    Role:
        Listens for inbound WebSocket connections from Infinity, runs
        the RCMS handshake (auth → `session.start` negotiation), routes
        every subsequent message to either a registered `ServicePlugin`
        or one of the built-in handlers (`handle_session_event`,
        `handle_session_end`, `handle_session_ping`, `handle_media`,
        `handle_binary_frame`), and tears down per-session and per-
        connection state on disconnect. Owns the centralized outbound
        sequence counter (`get_next_sequence`) so every reply on a
        connection shares a monotonic series.

    Does not own:
        Provider-specific AI logic (lives in `providers/*/bot_*.py`,
        attached as `ServicePlugin` instances). Bot lifecycle
        (`bot.start` → `bot.ended`) is owned by the bot service plugin
        in `bridge/bot_service.py`. Audio pacing and ingress framing
        are owned by `IngressStreamer` (composed into this class but
        with its own state). Outbound `bot.ended` failure / success /
        disconnect helpers are this class's responsibility; their
        bodies are defined later in this module.

    Dependencies:
        IngressStreamer — composed at construction; receives a backref
            to this server so it can read `session_config` and call
            `get_ingress_bid` for per-endpoint sends.
        ServiceRegistry — owns the per-message-type plugin routing
            table consulted in `handle_message`.
        websockets (library) — accepted connections are passed in by
            `start_server` and run through `handle_connection`.
        PyJWT — `check_auth` decodes HS256 bearer tokens against the
            primary/secondary key pair.

    RCMS lifecycle:
        Phase 1 (Setup): `check_auth` runs at upgrade time;
            `handle_connection` accepts the connection;
            `handle_session_start` performs codec / transport / pacing
            negotiation and emits `session.started`. Plugin
            `on_session_started` notifications fire here.
        Phase 2 (During): `handle_message` dispatches every inbound
            envelope (per the message-type routing table). Audio flows
            through `handle_media` / `handle_binary_frame`
            and out via `IngressStreamer`. Liveness is maintained by
            `session.ping` / `session.pong` exchanges through
            `handle_session_ping`.
        Phase 3 (Closure): `handle_session_end` tears down per-session
            state and triggers plugin `on_session_ended` (which is
            where the bot service emits `bot.ended` with the
            appropriate `payload.context.status` reason — including
            `CALLER_DISCONNECTED` per RCMS §Status Codes).
            `handle_connection.finally` runs a parallel cleanup pass
            on all per-client and per-session state when the
            connection drops.

    Spec:
        RCMS spec §Connection Setup / §Session Lifecycle / §Security /
        §Media Encoding Options / §Media Transport / §Status Codes.
        Authoritative: `bridge/schema/rcms.schema.md`,
        `bridge/schema/rcms.schema.json`, and
        https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

    See also:
        bot_service.BotService — primary `ServicePlugin`, attached at
            startup; owns the `bot.*` lifecycle and the
            caller-disconnect emit path.
        IngressStreamer (this module) — paced ingress audio path.
    """
    
    def __init__(self, host: str = "localhost", port: int = 8080, ssl_cert: str = None, ssl_key: str = None,
                 enable_auth: bool = False, preferred_transport: str = "binary", preferred_codec: str = "L16",
                 tts_media_type: str = "BATCH",
                 jwt_primary_key: str = "", jwt_secondary_key: str = ""):
        """Initialize per-server config and the per-session state maps.

        Contract:
            Constructor parameters configure listener bind, transport
            security, auth, and the negotiation defaults used in
            `handle_session_start`. State maps are created empty and
            populated as connections arrive. The same set is enumerated
            in two cleanup sites: `handle_connection.finally` (per-
            connection cleanup) and `handle_session_end` (per-session
            cleanup). Adding a new state dict here requires updating
            both cleanup sites.

        Constructor parameters:
            host / port: Listener bind. The production deployment binds
                `127.0.0.1:8444` behind a Caddy TLS reverse proxy on
                port 443 — the Python process never terminates TLS.
            ssl_cert / ssl_key: When set, the WebSocket server runs
                native TLS instead of clear-text. Production leaves
                these unset (Caddy handles TLS); they are kept for
                local-dev and direct-bind deployments.
            enable_auth: Opt-in JWT bearer-token auth on the WebSocket
                upgrade. Disabled by default for Echo / sandbox flows.
                When `True`, `jwt_primary_key` is required or the
                constructor raises `ValueError`.
            preferred_transport: `"binary"` (Compact Binary Frame
                Header), `"base64"` (legacy JSON envelope), or
                `"auto"` (prefer binary, fall back to base64). Used in
                `handle_session_start` transport negotiation.
            preferred_codec: One of `"L16"`, `"PCMU"`, `"PCMA"`,
                `"G722"`. Selected from the offered codec list when
                offered; otherwise the first offered codec wins (with a
                warning).
            tts_media_type: Retained for backward compatibility with
                older deployments. Not consumed by any current code
                path; kept so existing argparse / env-var wiring does
                not break. Out of scope per the docs cleanup pass.
            jwt_primary_key / jwt_secondary_key: HS256 keys for bearer-
                token verification. Primary is required when
                `enable_auth=True`; secondary is optional and supports
                key rotation without downtime per RCMS spec §Security.

        Per-session state maps (purged in `handle_connection.finally`
        and `handle_session_end`):
            connections: client_id ("<host>:<port>") → WebSocket. Active
                upgrade-completed connections.
            sequence_numbers: client_id → next outbound RCMS
                `sequenceNum`. Initialized to 0 on connect; first
                `get_next_sequence` call returns 1 per spec.
            sessions: session_id → SimpleSession. Per-session bookkeeping
                created in `handle_session_start`.
            session_config: session_id → {codec_name, sample_rate,
                client_id, …}. Read by `IngressStreamer._get_chunk_size`
                to size egress slices. Never holds credentials.
            transport_encodings: session_id → "binary" | "base64". The
                negotiated transport from `session.start`.
            stream_id_to_endpoint: "{session_id}:{stream_id_key}" →
                {sessionId, endpointId, source, bid, supports_ingress}.
                Built from `mediaEndpoints` for inbound media routing.
            endpoint_ingress_bid: "{session_id}:{endpoint_id}" → ingress
                bid. Quick lookup for ingress media sends; consulted by
                `IngressStreamer.send_immediate` and `barge_in`.
            endpoint_tag_to_id: "{session_id}:{tag}" → endpoint_id (UUID).
                Lets providers reference endpoints by their stable tag
                rather than per-session UUID.
            active_services: session_id → {service_name: bool}. Service-
                state tracking shared across plugins.
            media_sequence_numbers: "{session_id}:{endpoint_id}:{direction}"
                → seq. Per-direction RCMS media sequence counter.
            first_media_logged: "{session_id}:{endpoint_id}:{direction}:{format}"
                → bool. Once-per-stream gate for the verbose
                `FIRST MEDIA` log line emitted by `log_first_media`.

        Singleton sub-services:
            service_registry: ServiceRegistry — owns plugin registration
                and per-message-type routing.
            ingress_streamer: IngressStreamer — owns the centralized
                bridge → Infinity audio path. Gets a backref to `self`
                so it can read session_config and call get_ingress_bid.

        Spec:
            RCMS spec §Security — bearer-token auth, primary/secondary
            key rotation.
            RCMS spec §Media Encoding Options / §Media Transport — codec
            and transport defaults applied during session.start.

        Raises:
            ValueError: when `enable_auth=True` but `jwt_primary_key`
                is empty (caller must configure or disable auth).
        """
        self.host = host
        self.port = port
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.auth_enabled = enable_auth  # Bearer token auth disabled by default
        # JWT keys for token verification. Primary is required when auth is
        # enabled; secondary is optional and used as a fallback to support
        # rotation without downtime per RCMS spec §Security.
        self.jwt_primary_key = jwt_primary_key
        self.jwt_secondary_key = jwt_secondary_key
        if self.auth_enabled and not self.jwt_primary_key:
            raise ValueError("enable_auth=True requires jwt_primary_key (or set INFINITY_JWT_PRIMARY_KEY)")
        self.preferred_transport = preferred_transport  # "binary", "base64", or "auto" (prefer binary)
        self.preferred_codec = preferred_codec  # "L16", "PCMU", "PCMA", or "G722"
        self.tts_media_type = tts_media_type.upper() if tts_media_type else "BATCH"
        self.connections: Dict[str, WebSocketServerProtocol] = {}
        self.sequence_numbers: Dict[str, int] = {}
        self.sessions: Dict[str, SimpleSession] = {}
        self.session_config: Dict[str, Dict[str, Any]] = {}
        self.transport_encodings: Dict[str, str] = {}
        self.stream_id_to_endpoint: Dict[str, Dict[str, Any]] = {}
        self.endpoint_ingress_bid: Dict[str, int] = {}
        self.endpoint_tag_to_id: Dict[str, str] = {}
        self.active_services: Dict[str, Dict[str, bool]] = {}
        self.media_sequence_numbers: Dict[str, int] = {}
        self.first_media_logged: Dict[str, bool] = {}
        self.service_registry = ServiceRegistry()
        self.ingress_streamer = IngressStreamer(self)

    def register_service(self, plugin: ServicePlugin) -> ServicePlugin:
        """Register a service plugin so it can receive routed messages."""
        self.service_registry.register(plugin)
        return plugin
    
    async def check_auth(self, websocket: WebSocketServerProtocol) -> bool:
        """Validate the WebSocket upgrade's `Authorization: Bearer <jwt>` header.

        Contract:
            Runs once per connection at WebSocket-handshake time, before
            `handle_connection`. When `self.auth_enabled` is `False`,
            returns `True` immediately — auth is opt-in and disabled
            by default for sandbox / Echo deployments. When auth is
            enabled, requires a `Bearer` JWT in the `Authorization`
            header signed with HS256 using either the configured
            primary or secondary key.

            **Key rotation.** Two keys are supported per RCMS spec
            §Security: a primary (always required when auth is
            enabled) and an optional secondary. During a rotation
            window both keys verify successfully — operators rotate
            by setting the new key as secondary, distributing it to
            issuers, swapping primary↔secondary once issuance has
            cut over, and finally clearing the now-stale secondary.
            This avoids a downtime window where in-flight tokens
            signed with the old key would fail validation. To make
            verification robust to whichever encoding the issuer
            used, each key is tried first as UTF-8 bytes, then as a
            string — any of the four combinations succeeding is
            sufficient.

            Failure modes (all return `False`):
              * Missing or non-Bearer `Authorization` header.
              * `ExpiredSignatureError` — token is well-formed and
                signed correctly but past `exp`. Returns immediately
                without trying the rotation pair (an expired token
                is expired regardless of which key signed it).
              * Every key/encoding pair raised `InvalidTokenError`.
              * Any other exception during decode (also tried against
                the next key).

            On success, logs which key form verified for post-incident
            forensics — useful when diagnosing whether a rotation
            cutover has fully landed.

        Spec:
            RCMS spec §Security — bearer-token auth on the upgrade
            request, primary/secondary key support for rotation.
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: The accepted WebSocket. Reads
                `websocket.request.headers["Authorization"]`.

        Returns:
            `True` when auth is disabled or the JWT verifies against any
            configured key/encoding. `False` for missing header, expired
            token, or no key matching.
        """
        if not self.auth_enabled:
            return True  # Auth disabled - allow all connections
        
        # Auth is enabled - validate JWT token
        auth_header = websocket.request.headers.get('Authorization', '')
        
        if not auth_header.startswith('Bearer '):
            logger.warning(f"Missing or invalid Authorization header from {websocket.remote_address}")
            return False

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Verify JWT token. Try primary key first (both encodings), then
        # secondary if configured — supports key rotation without downtime
        # per RCMS spec §Security.
        keys_to_try = [
            (self.jwt_primary_key.encode('utf-8'), "primary key as UTF-8 bytes"),
            (self.jwt_primary_key, "primary key as string"),
        ]
        if self.jwt_secondary_key:
            keys_to_try += [
                (self.jwt_secondary_key.encode('utf-8'), "secondary key as UTF-8 bytes"),
                (self.jwt_secondary_key, "secondary key as string"),
            ]

        for key, description in keys_to_try:
            try:
                claims = jwt.decode(token, key, algorithms=['HS256'])
                logger.info(f"JWT bearer token auth successful for {websocket.remote_address} (verified with {description})")
                return True
            except ExpiredSignatureError:
                logger.warning(f"Expired JWT bearer token from {websocket.remote_address}")
                return False
            except InvalidTokenError:
                # Try next key format
                continue
            except Exception as e:
                logger.warning(f"JWT verification error from {websocket.remote_address}: {e}")
                continue

        # If all verification attempts fail
        logger.warning(f"Invalid JWT bearer token from {websocket.remote_address} (could not verify with any key format)")
        return False
    
    def log_first_media(self, client_id: str, session_id: str, endpoint_id: str, 
                        direction: str, format_type: str, data: Any, 
                        stream_id: str = None, seq: int = None, 
                        audio_size: int = None) -> bool:
        """
        Log the first media event for a stream (per endpoint + direction + bid).
        
        Args:
            client_id: Client identifier
            session_id: Session UUID
            endpoint_id: Endpoint UUID
            direction: "ingress" or "egress"
            format_type: "base64" (JSON) or "binary"
            data: For base64: dict with media JSON message; For binary: parsed binary frame dict
            stream_id: Stream ID (e.g., "bid=0,src=tx", "bid=1,src=tx")
            seq: Sequence number
            audio_size: Size of audio in bytes
            
        Returns:
            True if this was the first media for this stream, False otherwise
        """
        # Key: per stream (session + stream_id to distinguish different bids/sources)
        # Use stream_id (which includes bid+src) to differentiate between multiple flows
        # from the same endpoint (e.g., customer vs agent in multi-stream sessions)
        log_key = f"{session_id}:{stream_id}:{direction}"
        
        if log_key in self.first_media_logged:
            return False  # Already logged
            
        self.first_media_logged[log_key] = True
        
        # Log based on format type
        if format_type == "base64":
            # Base64 format: log as JSON message (media)
            # Replace audio field with size info only
            log_data = data.copy()
            audio_field = log_data.get("audio", "")
            log_data["audio"] = f"<{len(audio_field)} base64 chars = {audio_size or 0} bytes>"
            
            logger.debug(f"[{client_id}] ========== FIRST MEDIA {direction.upper()} (stream: {stream_id or 'unknown'}, format: JSON/base64) ==========")
            logger.debug(f"[{client_id}] Full JSON message:")
            logger.debug(f"[{client_id}] {json.dumps(log_data, indent=2)}")
            logger.debug(f"[{client_id}] Decoded size: {audio_size or 0} bytes")
            logger.debug(f"[{client_id}] {'=' * 70}")

        elif format_type == "binary":
            # Binary format: log frame header details
            logger.debug(f"[{client_id}] ========== FIRST MEDIA {direction.upper()} (stream: {stream_id or 'unknown'}, format: binary) ==========")
            logger.debug(f"[{client_id}] Compact Binary Frame Header (16 bytes):")
            logger.debug(f"[{client_id}]   Stream ID: {stream_id}")
            logger.debug(f"[{client_id}]   Sequence Num: {seq}")
            logger.debug(f"[{client_id}]   Timestamp (μs): {data.get('timestamp', 0)}")
            logger.debug(f"[{client_id}]   Flags: 0x{data.get('flags', 0):04X}")
            logger.debug(f"[{client_id}]   Payload Length: {audio_size} bytes")
            logger.debug(f"[{client_id}] {'=' * 70}")
        
        return True
    
    async def handle_connection(self, websocket: WebSocketServerProtocol):
        """Own the lifecycle of a single WebSocket connection: open → read loop → cleanup.

        Contract:
            Called once per accepted WebSocket connection by the server
            entrypoint (`start_server`). JWT/auth has already run during
            the WebSocket handshake via `process_request` — no
            re-authentication happens here. The method:

                1. Registers the connection in `self.connections` and
                   initializes `self.sequence_numbers[client_id] = 0`
                   (the first call to `get_next_sequence` increments
                   to 1 per RCMS envelope rule).
                2. Iterates `async for message in websocket`,
                   dispatching each frame: bytes → `handle_binary_frame`,
                   str → `handle_batched_message`. Per-frame
                   exceptions are logged with traceback but do not
                   abort the loop — one bad message does not close the
                   connection.
                3. On `ConnectionClosed` or any uncaught exception,
                   exits the read loop and runs the `finally` cleanup
                   block.

            **Cleanup contract.** The `finally` block must purge every
            per-session and per-client state dict for sessions owned by
            this `client_id`. The bridge has no central state registry;
            cleanup is enumerated by hand here and (separately) in
            `handle_session_end`. Adding a new state dict to `__init__`
            requires also adding it to *both* cleanup sites — a
            forgotten cleanup leaks state across connection cycles
            until restart. The current cleanup set is:

                * `self.sessions` — by session_id
                * `self.stream_id_to_endpoint` — by `"{session_id}:"` prefix
                * `self.endpoint_tag_to_id` — by `"{session_id}:"` prefix
                * `self.endpoint_ingress_bid` — by `"{session_id}:"` prefix
                * `self.session_config` — by session_id
                * `self.connections` — by client_id
                * `self.sequence_numbers` — by client_id

            Cleanup is best-effort; cleanup-time exceptions are not
            currently caught (an exception inside the `finally` would
            propagate out of `handle_connection`).

        Spec:
            RCMS spec §Connection Setup / Connection Teardown.
            See `bridge/schema/rcms.schema.md` for envelope semantics
            consumed inside the read loop.

        Args:
            websocket: The accepted WebSocket connection.

        Returns:
            None. Runs until the peer closes the connection or an
            unrecoverable error tears down the read loop.
        """
        client_id = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"

        # Authentication is already validated during handshake via process_request
        # No need to check again here - if we reach this point, auth passed

        self.connections[client_id] = websocket
        self.sequence_numbers[client_id] = 0

        logger.info(f"New connection from {client_id}")

        try:
            async for message in websocket:
                try:
                    # Check if message is binary or text
                    if isinstance(message, bytes):
                        # Binary frame - check for pending media header
                        await self.handle_binary_frame(websocket, client_id, message)
                    elif isinstance(message, str):
                        # Text frame — may contain multiple JSON objects in a single frame (RCMS batching)
                        await self.handle_batched_message(websocket, client_id, message)
                    else:
                        logger.warning(f"[{client_id}] Unknown message type: {type(message)}")
                except Exception as e:
                    logger.error(f"Error processing message from {client_id}: {e}", exc_info=True)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed: {client_id}")
        except Exception as e:
            logger.error(f"Error handling connection {client_id}: {e}")
        finally:
            # Per-session and per-client state cleanup. Every dict listed in
            # __init__ that is keyed by session_id (or by composite keys
            # prefixed with "{session_id}:") must be enumerated here so a
            # closed connection does not leak state. handle_session_end
            # mirrors this set for the in-session teardown path. Adding a
            # new state dict to __init__ requires updating both sites.
            sessions_to_stop = [
                session for session in self.sessions.values() 
                if session.client_id == client_id
            ]
            for session in sessions_to_stop:
                del self.sessions[session.session_id]
            
            # Clean up all stream_id and endpoint tag mappings for this connection
            stream_keys_to_remove = []
            for session_id_to_remove in [s.session_id for s in sessions_to_stop]:
                stream_keys = [
                    key for key in self.stream_id_to_endpoint.keys()
                    if key.startswith(session_id_to_remove + ":")
                ]
                stream_keys_to_remove.extend(stream_keys)
            
            for key in stream_keys_to_remove:
                del self.stream_id_to_endpoint[key]
            
            tag_keys_to_remove = []
            for session_id_to_remove in [s.session_id for s in sessions_to_stop]:
                tag_keys = [
                    key for key in self.endpoint_tag_to_id.keys()
                    if key.startswith(session_id_to_remove + ":")
                ]
                tag_keys_to_remove.extend(tag_keys)
            
            for key in tag_keys_to_remove:
                if key in self.endpoint_tag_to_id:
                    del self.endpoint_tag_to_id[key]
            
            # Clean up endpoint ingress bid mappings
            ingress_bid_keys_to_remove = []
            for session_id_to_remove in [s.session_id for s in sessions_to_stop]:
                ingress_bid_keys = [
                    key for key in self.endpoint_ingress_bid.keys()
                    if key.startswith(session_id_to_remove + ":")
                ]
                ingress_bid_keys_to_remove.extend(ingress_bid_keys)
            
            for key in ingress_bid_keys_to_remove:
                if key in self.endpoint_ingress_bid:
                    del self.endpoint_ingress_bid[key]
            
            # Clean up session configs
            for session_id_to_remove in [s.session_id for s in sessions_to_stop]:
                if session_id_to_remove in self.session_config:
                    del self.session_config[session_id_to_remove]
            
            if client_id in self.connections:
                del self.connections[client_id]
            if client_id in self.sequence_numbers:
                del self.sequence_numbers[client_id]
    
    async def handle_batched_message(self, websocket: WebSocketServerProtocol, client_id: str, message: str):
        """Split a WebSocket text frame into one or more RCMS messages and dispatch each.

        Contract:
            RCMS does not require one-message-per-WebSocket-frame. Avaya
            commonly concatenates multiple JSON envelopes — most often
            `session.start` immediately followed by `bot.start` — into
            a single text frame, separated only by whitespace. Each
            envelope must be parsed independently and dispatched to
            `handle_message` in arrival order so plugin state
            transitions (e.g. session must exist before `bot.start`
            registers a bot on it) line up.

            Implemented with `json.JSONDecoder.raw_decode` to extract
            one envelope at a time, advancing `pos` past whitespace
            between envelopes. A parse failure on any envelope logs
            and stops processing the remainder of the frame — partial
            success is preferred over dropping the whole batch.

            Empty / whitespace-only frames return without action.
            Single-envelope frames take the same code path; the
            "processed N batched messages" log line only fires for
            `count > 1` to avoid log spam.

        Spec:
            RCMS spec §Protocol Design — multiple messages may share a
            single WebSocket frame.

        Args:
            websocket: WebSocket connection the frame arrived on,
                forwarded to `handle_message` for each envelope.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            message: Raw text-frame payload, possibly containing
                multiple concatenated JSON envelopes.

        Returns:
            None. Returns early on empty input; stops mid-batch on a
            parse error and logs the failure.
        """
        message = message.strip()
        if not message:
            return

        decoder = json.JSONDecoder()
        pos = 0
        count = 0
        while pos < len(message):
            # Skip whitespace between objects
            while pos < len(message) and message[pos] in ' \t\n\r':
                pos += 1
            if pos >= len(message):
                break
            try:
                obj, end_pos = decoder.raw_decode(message, pos)
                pos = end_pos
                count += 1
                msg_type = obj.get("type", "unknown")
                logger.info(f"[{client_id}] Batched frame: processing message {count} type={msg_type} (frame offset {pos})")
                await self.handle_message(websocket, client_id, json.dumps(obj))
            except json.JSONDecodeError as e:
                logger.error(f"[{client_id}] Failed to parse JSON object at position {pos} in batched frame: {e}")
                break

        if count > 1:
            logger.info(f"[{client_id}] Processed {count} batched messages from single frame")

    async def handle_message(self, websocket: WebSocketServerProtocol, client_id: str, message: str):
        """Route a single decoded RCMS message to its handler.

        Contract:
            Receives a single JSON-encoded RCMS message (already split
            from any batch by `handle_batched_message`), parses it, and
            dispatches to the appropriate handler. Routing precedence:

                1. **Plugin claim.** `service_registry.
                   get_plugin_for_message(msg_type)` — if any registered
                   `ServicePlugin` declares it handles this message
                   type, the plugin's `handle_message` is invoked and
                   routing returns. This is how `bot.start` /
                   `bot.end` / `bot.feature` and provider-specific
                   message types reach the bot service or echo plugin.

                2. **Built-in routing table** (when no plugin claims
                   the type):

                        ===================== ========================
                        type                  handler
                        ===================== ========================
                        session.start         handle_session_start
                        media                 handle_media
                        session.event         handle_session_event
                        session.end           handle_session_end
                        session.stop          handle_session_end
                        session.ping          handle_session_ping
                        ===================== ========================

                3. **Unhandled fallthrough.** Anything else (including
                   message types that *would* route to a plugin if it
                   were registered, e.g. `session.dtmf` arriving before
                   `bot.start`) draws a `session.error` 404 /
                   `SESSION_NOT_FOUND` so MAG can produce an MSML
                   `dialog.exit` and tear down the workflow gracefully.

            Logging policy: media-bearing messages (top-level `media`
            and `session.event` with `eventType: "media"`) skip the
            INFO-level full-JSON dump and only land in the
            message-exchange logger if `--log-audio` is set. All other
            messages are dumped at INFO with redaction applied via
            `_redact_for_logging`.

            Exceptions inside the dispatcher are caught at the bottom:
            `JSONDecodeError` is logged and dropped (no error reply,
            since we have no `sessionId` to address it to); any other
            exception is logged with traceback and dropped.

        Spec:
            RCMS spec §Protocol Design — message envelope (`type`,
            `sessionId`, `sequenceNum`, `payload`).
            RCMS spec §Status Codes — 404 `SESSION_NOT_FOUND` for
            messages routed to a non-existent session or service.

        Args:
            websocket: WebSocket connection the message arrived on; used
                by handlers to send replies and by `send_session_error`
                for the fallthrough case.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            message: One JSON-encoded RCMS envelope as a string.

        Returns:
            None. Errors are logged and dropped; the connection stays
            open.
        """
        try:
            data = json.loads(message)
            msg_type = data.get("type", "unknown").strip()  # Strip whitespace
            session_id = data.get("sessionId", "unknown")
            sequence = data.get("sequenceNum", 0)
            service = data.get("service", "streaming")
            endpoint = data.get("endpoint", "")

            # Check if this is a media event for special logging
            is_media_event = False
            if msg_type == "media":
                is_media_event = True
            elif msg_type == "session.event":
                payload = data.get("payload", {})
                event_type = payload.get("eventType", "")
                is_media_event = (event_type == "media")

            if not is_media_event:
                # Log all non-media messages in full JSON
                endpoint_info = f", endpoint: {endpoint}" if endpoint else ""
                logger.info(f"[{client_id}] INBOUND JSON ({msg_type}): {format_compact_json(_redact_for_logging(msg_type, data))}")
                # Log to message logger using centralized function
                log_message_exchange("INBOUND", client_id, msg_type, data, is_media=False)
            else:
                # Log media messages to message logger only if --log-audio is enabled
                log_message_exchange("INBOUND", client_id, msg_type, data, is_media=True)

            # Allow registered services to handle the message.
            plugin = self.service_registry.get_plugin_for_message(msg_type)
            if plugin:
                await plugin.handle_message(websocket, client_id, data)
                return

            if msg_type == "session.start":
                await self.handle_session_start(websocket, client_id, data)
            elif msg_type == "media":
                await self.handle_media(websocket, client_id, data)
            elif msg_type == "session.event":
                await self.handle_session_event(websocket, client_id, data)
            elif msg_type == "session.end" or msg_type == "session.stop":
                await self.handle_session_end(websocket, client_id, data)
            elif msg_type == "session.ping":
                await self.handle_session_ping(websocket, client_id, data)
            else:
                # Unhandled message type: no plugin registered (e.g. session.dtmf when bot not started)
                # or unknown protocol message. Send session.error so MAG can produce MSML dialog.exit.
                logger.warning(f"[{client_id}] Unhandled message type: {msg_type} (no service to handle it)")
                payload = data.get("payload", {})
                endpoint_id = endpoint or payload.get("endpointId", "")
                await self.send_session_error(
                    websocket,
                    client_id,
                    session_id,
                    message_type=msg_type,
                    message_seq_num=sequence,
                    code=404,
                    reason="SESSION_NOT_FOUND",
                    description=f"No handler for message type {msg_type} (service not started or session not found)",
                    endpoint=endpoint_id or None,
                )

        except json.JSONDecodeError as e:
            logger.error(f"[{client_id}] Invalid JSON: {e}")
        except Exception as e:
            logger.error(f"[{client_id}] Error processing message: {e}", exc_info=True)
    
    async def handle_session_start(self, websocket: WebSocketServerProtocol, client_id: str, data: Dict[str, Any]):
        """Negotiate transport, codec, and pacing; register session state; emit `session.started`.

        Contract:
            Entry point for the RCMS session-establishment phase. By the
            time this method runs, JWT/auth has already been validated
            during the WebSocket handshake (`check_auth` runs at upgrade
            time). All work here is post-auth.

            Phases, in order:

                1. **Codec negotiation.** Reads `payload.mediaTransports[0].
                   mediaCodecs` (a list of `[type, name, sample_rate,
                   channels]` tuples). The server's configured
                   `preferred_codec` ("L16" / "PCMU" / "PCMA" / "G722")
                   is selected if offered; otherwise the first offered
                   codec is taken and a warning is logged. Codec name
                   and sample rate are persisted to
                   `session_config[session_id]` so `IngressStreamer.
                   _get_chunk_size` can size slices correctly.
                   See RCMS spec §Media Encoding Options.

                2. **Transport-encoding negotiation.** Reads
                   `payload.mediaTransports[0].transportEncodings` (list)
                   or the legacy `transportEncoding` (scalar). The
                   server's `preferred_transport` ("binary" / "base64"
                   / "auto") is honored where the offered set permits;
                   the decision falls back to base64 if neither
                   preferred nor "binary" is offered. Stored in
                   `transport_encodings[session_id]` for
                   `IngressStreamer` to consult.

                3. **Pacing declaration: `preferredPTimeMs: 80`.** The
                   bridge declares 80 ms regardless of negotiation
                   outcome. 80 ms matches the actual egress framing
                   observed on the wire from Infinity (250 ms is the
                   delivered cadence for sub-100 ms declared values).
                   The declared value is prescriptive per RCMS spec but
                   non-compliant on the platform — the bridge declares
                   the value the platform *actually* uses on egress so
                   that downstream IngressStreamer pacing matches the
                   media duration carried per send.

                4. **`mediaEndpoints` stream-ID mapping.** For each
                   endpoint in `payload.mediaEndpoints`: extract
                   `flows.audio.egress.sources` (filtering out "none")
                   and `flows.audio.ingress.target`, assign sequential
                   bid values, and register two lookup tables:
                   `stream_id_to_endpoint` (keyed by
                   `"{session_id}:{stream_id_key}"`) and, for endpoints
                   that support ingress, `endpoint_ingress_bid` (keyed
                   by `"{session_id}:{endpoint_id}"`). Endpoint tags
                   are registered in `endpoint_tag_to_id` so providers
                   can look up the UUID by tag.

                5. **Implicit service binding.** Reads
                   `payload.services` and intersects with the server's
                   supported set (built-ins `asr` / `tts` / `echo` plus
                   every plugin name in `service_registry`). The
                   acknowledged subset is included in the
                   `session.started` response payload.

                6. **`session.started` emission.** Builds the response
                   `mediaTransport` (selected codec only, selected
                   encoding, `preferredPTimeMs: 80`), assigns the next
                   outbound `sequenceNum`, and sends.

            Plugins are notified via `on_session_started(session_id)`
            after `SimpleSession` is registered and before
            `mediaEndpoints` parsing — exceptions are logged but do not
            abort the start. On any exception inside the try/except
            block, a `session.end` with `status.code: 500 /
            INTERNAL_ERROR` is emitted in lieu of `session.started` so
            the platform can release the call.

        Spec:
            RCMS spec §Session Lifecycle — `session.start` / `session.started`.
            RCMS spec §Media Encoding Options — codec negotiation list
            shape (`[type, name, sample_rate, channels]`).
            RCMS spec §Media Transport — `transportEncodings` /
            `preferredPTimeMs` semantics.
            See `bridge/schema/rcms.schema.md` "session.start extra
            fields" for `engagementId` / `workflowSessionId` (read for
            correlation; not used here).
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: WebSocket connection used for the
                `session.started` (or error) reply.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            data: The decoded `session.start` envelope. Reads
                `sessionId`, `endpoint`, `service`, and `payload.{
                mediaTransports, mediaEndpoints, services}`.

        Returns:
            None. Always sends one outbound message — `session.started`
            on the success path, `session.end` with `INTERNAL_ERROR`
            on the failure path.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        endpoint = data.get("endpoint", "")
        service = data.get("service", "streaming")
        
        try:
            # Extract media configuration
            media_transports = payload.get("mediaTransports", [])
            if not media_transports:
                raise ValueError("Missing mediaTransports in payload")
            
            # Get codec information and transport encoding
            transport = media_transports[0]
            media_codecs = transport.get("mediaCodecs", [])
            if not media_codecs:
                raise ValueError("Missing mediaCodecs in transport")
            
            # Get offered transport encodings and select the best one
            offered_encodings = transport.get("transportEncodings", ["base64"])
            if not isinstance(offered_encodings, list):
                # Handle legacy single encoding format
                offered_encodings = [transport.get("transportEncoding", "base64")]
            
            # Select transport encoding based on server preference
            if self.preferred_transport == "binary" or self.preferred_transport == "auto":
                # Prefer binary if offered, otherwise base64
                if "binary" in offered_encodings:
                    selected_encoding = "binary"
                elif "base64" in offered_encodings:
                    selected_encoding = "base64"
                else:
                    selected_encoding = "base64"  # Default fallback
            elif self.preferred_transport == "base64":
                # Prefer base64 if offered, otherwise binary
                if "base64" in offered_encodings:
                    selected_encoding = "base64"
                elif "binary" in offered_encodings:
                    selected_encoding = "binary"
                else:
                    selected_encoding = "base64"  # Default fallback
            else:
                # Unknown preference, use default behavior (prefer binary)
                if "binary" in offered_encodings:
                    selected_encoding = "binary"
                else:
                    selected_encoding = "base64"
            
            # Store transport encoding for this session
            self.transport_encodings[session_id] = selected_encoding
            logger.info(f"[{client_id}] Transport encoding negotiation - offered: {offered_encodings}, preference: {self.preferred_transport}, selected: {selected_encoding}")
            
            # Select codec from offered list based on preference
            selected_codec = None
            for codec in media_codecs:
                if isinstance(codec, list) and len(codec) >= 2:
                    if codec[1] == self.preferred_codec:
                        selected_codec = codec
                        break
            
            # If preferred codec not found, fall back to the first offered
            # codec rather than rejecting the session. Infinity always offers
            # at least one codec it can encode/decode, so a non-match means
            # the deployment's preference is mismatched against this caller's
            # MAG configuration — log a warning so the operator can correct
            # the preference, but keep the call alive on whatever was offered.
            if selected_codec is None:
                selected_codec = media_codecs[0]
                logger.warning(f"[{client_id}] Preferred codec '{self.preferred_codec}' not offered, using '{selected_codec[1]}' instead")
            
            # Parse selected codec: [["audio", "L16", 8000, 1]]
            codec = selected_codec
            if isinstance(codec, list) and len(codec) >= 4:
                codec_type = codec[0]      # "audio"
                codec_name = codec[1]      # "L16", "PCMU", "PCMA", or "G722"
                sample_rate = codec[2]     # 8000 or 16000
                channels = codec[3]        # 1
            else:
                raise ValueError("Invalid codec format")
            
            logger.info(f"[{client_id}] Codec negotiation - offered: {[c[1] if isinstance(c, list) and len(c) >= 2 else '?' for c in media_codecs]}, preference: {self.preferred_codec}, selected: {codec_name}")
            
            # Store codec and sample rate in session config for plugins
            if session_id not in self.session_config:
                self.session_config[session_id] = {}
            self.session_config[session_id]["codec_name"] = codec_name
            self.session_config[session_id]["sample_rate"] = sample_rate
            self.session_config[session_id]["client_id"] = client_id
            
            logger.info(f"[{client_id}] Session start - codec: {codec_name}, rate: {sample_rate}Hz, channels: {channels}")
            
            session = SimpleSession(session_id=session_id, client_id=client_id)
            self.sessions[session_id] = session

            # Notify plugins that a new session is available.
            for plugin in self.service_registry.plugins:
                try:
                    await plugin.on_session_started(session_id)
                except Exception:
                    logger.exception(f"[{client_id}] Error while notifying plugin '{plugin.name}' of session start")
            
            # Parse mediaEndpoints and build stream ID lookup tables
            # New format: { "flows": { "audio": { "egress": { "sources": ["tx"], "bid": N }, "ingress": { "target": ["auto"], "bid": M } } } }
            media_endpoints = payload.get("mediaEndpoints", [])
            bid_counter = 0  # Assign bids sequentially per flow direction
            
            for ep in media_endpoints:
                endpoint_id = ep.get("endpointId", "")
                tag = ep.get("tag", "")
                
                # Extract flows object through audio media type layer
                flows = ep.get("flows", {})
                audio_flows = flows.get("audio", {})
                egress = audio_flows.get("egress", {})
                ingress = audio_flows.get("ingress", {})
                
                # Extract sources from flows.audio.egress.sources, filtering out "none"
                sources_raw = egress.get("sources", [])
                sources = [s for s in sources_raw if s.lower() != "none"]
                
                # Check if this endpoint supports ingress
                ingress_target = ingress.get("target", [])
                supports_ingress = bool(ingress_target and ingress_target != ["none"])
                
                # Assign bid for egress flow and build stream ID lookup table
                if sources:
                    egress_bid = bid_counter
                    bid_counter += 1
                    for source in sources:
                        stream_id_key = build_stream_id_key(egress_bid, source)
                        composite_key = f"{session_id}:{stream_id_key}"
                        self.stream_id_to_endpoint[composite_key] = {
                            "sessionId": session_id,
                            "endpointId": endpoint_id,
                            "source": source,
                            "bid": egress_bid,
                            "supports_ingress": supports_ingress
                        }
                        logger.info(f"[{client_id}] Mapped egress stream bid={egress_bid}, src={source} (key: '{composite_key}') -> endpoint '{endpoint_id}', ingress={supports_ingress}")
                
                # Assign bid for ingress flow and register mapping
                if supports_ingress:
                    ingress_bid = bid_counter
                    bid_counter += 1
                    # Ingress media uses src=none since it's identified by bid only
                    ingress_source = "none"
                    stream_id_key = build_stream_id_key(ingress_bid, ingress_source)
                    composite_key = f"{session_id}:{stream_id_key}"
                    self.stream_id_to_endpoint[composite_key] = {
                        "sessionId": session_id,
                        "endpointId": endpoint_id,
                        "source": ingress_source,
                        "bid": ingress_bid,
                        "supports_ingress": supports_ingress
                    }
                    # Store ingress bid for quick lookup when sending ingress media
                    ingress_bid_key = f"{session_id}:{endpoint_id}"
                    self.endpoint_ingress_bid[ingress_bid_key] = ingress_bid
                    logger.info(f"[{client_id}] Mapped ingress stream bid={ingress_bid}, src={ingress_source} (key: '{composite_key}') -> endpoint '{endpoint_id}', ingress={supports_ingress}")
                
                # Store endpoint tag to ID mapping
                if tag:
                    tag_key = f"{session_id}:{tag}"
                    self.endpoint_tag_to_id[tag_key] = endpoint_id
                    logger.info(f"[{client_id}] Mapped endpoint tag '{tag}' -> endpoint ID '{endpoint_id}'")
            
            # Build media transport response — return only the selected
            # codec so the platform does not assume codec-list parity.
            #
            # preferredPTimeMs: 80 is declared regardless of what we
            # accept. 80 ms matches the actual egress framing observed
            # from Infinity on the wire. RCMS spec treats this field as
            # prescriptive (the platform should honor the declared
            # value), but in practice Infinity delivers ~250 ms frames
            # for any declared value in the spec-defined range. The
            # bridge declares the value that aligns with the cadence
            # the platform *actually* sends, so IngressStreamer pacing
            # matches per-send audio duration.
            media_transport = {
                "type": transport.get("type", "avaya-wss"),
                "transportEncoding": selected_encoding,  # Return selected encoding
                "mediaCodecs": [selected_codec],  # Return only the selected codec
                "preferredPTimeMs": 80
            }
            
            # Handle implicit service binding from session.start
            # Extract requested services and filter to only those we support
            requested_services = payload.get("services", [])
            supported_services = ["asr", "tts", "echo"]  # Built-in supported services
            # Add plugin services
            for plugin in self.service_registry.plugins:
                if hasattr(plugin, "name") and plugin.name not in supported_services:
                    supported_services.append(plugin.name)
            
            # Filter to only services we support (intersection of requested and supported)
            acknowledged_services = [svc for svc in requested_services if svc in supported_services]
            if acknowledged_services:
                logger.info(f"[{client_id}] Implicit service binding - requested: {requested_services}, supported: {supported_services}, acknowledged: {acknowledged_services}")
            
            # Send session.started response
            response_payload = {
                "mediaTransport": media_transport
            }
            
            # Include acknowledged services in response (if any were requested)
            if acknowledged_services:
                response_payload["services"] = acknowledged_services
            
            response = {
                "version": "1.0.0",
                "type": "session.started",
                "sessionId": session_id,
                "sequenceNum": self.get_next_sequence(client_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": response_payload
            }
            
            if endpoint:
                response["endpoint"] = endpoint
            
            logger.info(f"[{client_id}] OUTBOUND JSON (session.started): {format_compact_json(response)}")
            # Log to message logger using centralized function
            log_message_exchange("OUTBOUND", client_id, "session.started", response, is_media=False)
            await websocket.send(json.dumps(response))

        except Exception as e:
            logger.error(f"[{client_id}] Session start failed: {e}", exc_info=True)
            
            # Send error response
            error_response = {
                "version": "1.0.0",
                "type": "session.end",
                "sessionId": session_id,
                "sequenceNum": self.get_next_sequence(client_id),
                "timestamp": datetime.now(UTC).isoformat(),
                "service": service,
                "payload": {
                    "status": {
                        "code": 500,
                        "reason": "INTERNAL_ERROR",
                        "description": str(e)
                    }
                }
            }
            
            if endpoint:
                error_response["endpoint"] = endpoint
            
            logger.error(f"[{client_id}] OUTBOUND JSON (session.error): {format_compact_json(error_response)}")
            # Log to message logger using centralized function
            log_message_exchange("OUTBOUND", client_id, "session.error", error_response, is_media=False)
            await websocket.send(json.dumps(error_response))
    
    async def handle_session_event(self, websocket: WebSocketServerProtocol, client_id: str, data: Dict[str, Any]):
        """Process RCMS `session.event` messages — primarily legacy base64 media stats.

        Contract:
            RCMS `session.event` is a generic event channel. The only
            event subtype this method handles is `eventType: "media"`,
            which carries base64-encoded audio in the legacy JSON
            transport encoding (paired with binary frames in the modern
            transport, which routes through `handle_media` /
            `handle_binary_frame` instead). The payload's `audio`,
            `sampleRate`, and the message-level `endpoint` are read; the
            audio is base64-decoded only to count its bytes for the
            once-per-second `MEDIA SUMMARY` log line. No audio is
            forwarded to providers from this path.

            Non-media event subtypes are logged and dropped.

            Sessions unknown at the point of dispatch (i.e. message
            arrived after the session was torn down or before
            `handle_session_start` registered it) draw a
            `session.error` 404 / `SESSION_NOT_FOUND`.

        Spec:
            RCMS spec §Session Lifecycle — `session.event`.
            See `bridge/schema/rcms.schema.md` for the envelope shape.
            Note: `session.event` with `eventType: "media"` is the legacy
            base64 path; current production flows use top-level `media`
            messages and binary frames documented in
            `rcms.schema.md` "media messages".

        Args:
            websocket: WebSocket connection used for any error reply.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            data: The decoded `session.event` envelope. Reads
                `sessionId`, `payload.eventType`, `payload.audio`,
                `payload.sampleRate`, and message-level `endpoint`.

        Returns:
            None. Returns early on unknown session, on non-media event
            type, or on a missing session lookup mid-method.
        """
        session_id = data.get("sessionId", "unknown")
        payload = data.get("payload", {})
        event_type = payload.get("eventType", "unknown")
        endpoint = data.get("endpoint", "")
        
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"[{client_id}] session.event for unknown session {session_id}")
            await self.send_session_error(
                websocket, 
                client_id, 
                session_id,
                message_type="session.event",
                code=404,
                reason="SESSION_NOT_FOUND",
                description=f"Session {session_id} does not exist or was terminated"
            )
            return
        
        if event_type != "media":
            logger.info(f"[{client_id}] Non-media session event - eventType: {event_type}, session: {session_id}")
            return
        
        # Get session
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"[{client_id}] No session found for ID: {session_id}")
            return
        
        # Extract audio data
        audio_data = payload.get("audio", "")
        sample_rate = payload.get("sampleRate", 8000)
        
        if audio_data:
            try:
                # Decode base64 audio
                audio_bytes = base64.b64decode(audio_data)
                audio_size = len(audio_bytes)
                
                # Per-endpoint tracking key
                endpoint_key = f"{endpoint if endpoint else 'default'}:ingress"
                
                # Initialize tracking for this endpoint if needed
                if endpoint_key not in session.media_events_this_second:
                    session.media_events_this_second[endpoint_key] = 0
                    session.media_bytes_this_second[endpoint_key] = 0
                    session.last_media_log_time[endpoint_key] = time.time()
                
                # Update media statistics for this second
                session.media_events_this_second[endpoint_key] += 1
                session.media_bytes_this_second[endpoint_key] += audio_size
                
                # Log summary once per second for this endpoint (both directions)
                current_time = time.time()
                if current_time - session.last_media_log_time[endpoint_key] >= 1.0:
                    ep_name = endpoint if endpoint else 'default'
                    in_key = f"{ep_name}:ingress"
                    out_key = f"{ep_name}:egress"
                    in_ev = session.media_events_this_second.get(in_key, 0)
                    in_bytes = session.media_bytes_this_second.get(in_key, 0)
                    out_ev = session.media_events_this_second.get(out_key, 0)
                    out_bytes = session.media_bytes_this_second.get(out_key, 0)
                    logger.debug(f"[{client_id}] MEDIA SUMMARY (1s): ep={ep_name} in: ev={in_ev} bytes={in_bytes} out: ev={out_ev} bytes={out_bytes} rate={sample_rate}Hz")
                    session.media_events_this_second[in_key] = 0
                    session.media_bytes_this_second[in_key] = 0
                    if out_key in session.media_events_this_second:
                        session.media_events_this_second[out_key] = 0
                        session.media_bytes_this_second[out_key] = 0
                    session.last_media_log_time[endpoint_key] = current_time
                    if out_key in session.last_media_log_time:
                        session.last_media_log_time[out_key] = current_time
                
            except Exception as e:
                logger.error(f"[{client_id}] Error processing media: {e}")
        else:
            logger.warning(f"[{client_id}] Media event received but audio data is empty!")
    
    async def handle_media(self, websocket: WebSocketServerProtocol, client_id: str, data: Dict[str, Any]):
        """Decode a JSON `media` envelope and dispatch the base64 audio to bot/echo.

        Contract:
            Phase 2 entry point for the JSON / base64 transport. Mirror
            of `handle_binary_frame` for the legacy text-frame path —
            same dispatch sequence, only the framing differs. The
            payload fields (`bid`, `src`, `asn`, `ts`, `lastf`,
            `audio`) sit at the **top level of the message envelope**
            rather than nested inside a `payload` object.

            Sequence:

                1. Read top-level `bid`, `src`, `asn`, `ts`, `audio`,
                   and `lastf`. `src` defaults to `"none"` (used for
                   bridge-originated ingress) and `ts` may arrive as a
                   string and is coerced to int.
                2. The composite key
                   `"<session_id>:<stream_id_key>"` is looked up
                   in `stream_id_to_endpoint`, with the available
                   keys for this session logged on miss to make
                   negotiation-race or out-of-order issues
                   diagnosable.
                3. Frames for unknown sessions draw a
                   `session.error` 404 / `SESSION_NOT_FOUND` so
                   Infinity tears the connection down cleanly. This
                   is the same stale-restart signature handled by
                   `handle_session_ping`.
                4. **Direction is set to `"egress"` unconditionally**
                   for this code path. `src` (`tx`/`rx`) is a
                   *source* identifier per RCMS spec, not a direction
                   marker — frames arriving over this WebSocket are
                   incoming-to-bridge, hence egress, regardless of
                   the source label.
                5. Bot plugin is invoked via
                   `bot_plugin.ingest_audio_chunk(...)`. Plugin
                   exceptions are logged but do not abort the frame.
                6. `log_first_media` emits the once-per-stream
                   FIRST MEDIA banner. The `data` argument carries
                   the original JSON envelope so it lands in the
                   message-exchange logger if `--log-audio` is on.
                7. Per-second MEDIA SUMMARY counters are advanced;
                   the summary line emits at most once per second
                   per `(endpoint, direction, src)` triple.
                8. Echo plugin is invoked via
                   `echo_plugin.maybe_echo_base64(...)`. The plugin
                   decides whether the call's botId selects the
                   echo path.

            Empty `audio` payloads are logged as a warning and
            dropped — they should not occur in practice.

        Spec:
            RCMS spec §Media Encoding Options — `media` message JSON
            envelope. `bridge/schema/rcms.schema.md` "media messages"
            documents that runtime production traffic carries the
            payload fields at the top level (the spec's
            `MediaPayload` definition nests them; the wire reality
            does not), and that `lastf` is a boolean at runtime even
            though the spec defines it as integer. `src` includes
            `"none"` at runtime in addition to `"rx"` / `"tx"`.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: WebSocket connection the message arrived on,
                forwarded to the echo plugin and to
                `send_session_error` for the unknown-session case.
            client_id: WebSocket peer identifier ("<host>:<port>");
                reverse-mapped to a session via `session_config`.
            data: The decoded JSON `media` envelope. Reads top-level
                `bid`, `src`, `asn`, `ts`, `audio`, `lastf`.

        Returns:
            None. Returns early on missing session, unknown stream,
            or empty audio. Plugin exceptions are logged and
            swallowed.
        """
        # Per RCMS spec, media-message fields sit at the top level of the
        # envelope at runtime (the spec's MediaPayload definition nests
        # them inside payload — the wire reality does not). See
        # bridge/schema/rcms.schema.md "media messages".
        bid = data.get("bid", 0)
        src = data.get("src", "none")  # Default to "none" for ingress media (src is optional)
        seq = data.get("asn", 0)
        ts = data.get("ts", 0)
        if isinstance(ts, str):
            ts = int(ts)
        audio_data = data.get("audio", "")
        lastf = data.get("lastf", False)
        
        # Build stream ID key for internal routing
        stream_id_key = build_stream_id_key(bid, src)
        
        # Find session_id for this client_id
        session_id = None
        for sid, config in self.session_config.items():
            if config.get("client_id") == client_id:
                session_id = sid
                break
        
        if not session_id:
            logger.warning(f"[{client_id}] No session found for client_id when processing media")
            return
        
        endpoint_id = ""
        source = ""

        # Look up endpoint info from stream ID using composite key (session_id:stream_id_key)
        composite_key = f"{session_id}:{stream_id_key}"
        endpoint_info = self.stream_id_to_endpoint.get(composite_key)
        if not endpoint_info:
            logger.warning(f"[{client_id}] Unknown stream: bid={bid}, src={src} (composite key: '{composite_key}') - Available keys for this session: {[k for k in self.stream_id_to_endpoint.keys() if k.startswith(session_id + ':')]}")
            return
        
        session_id = endpoint_info["sessionId"]
        endpoint_id = endpoint_info.get("endpointId") or ""
        source = endpoint_info.get("source") or ""
        bid = endpoint_info.get("bid")
        supports_ingress = endpoint_info.get("supports_ingress", False)

        if not endpoint_id:
            logger.warning(f"[{client_id}] Incomplete endpoint info for bid={bid}, src={src} (session={session_id})")
            return
        
        # Get session
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"[{client_id}] media for unknown session {session_id} - likely stale connection after restart")
            await self.send_session_error(
                websocket, 
                client_id, 
                session_id,
                message_type="media",
                code=404,
                reason="SESSION_NOT_FOUND",
                description=f"Session {session_id} does not exist or was terminated"
            )
            return
        
        if audio_data:
            try:
                # Decode base64 audio
                audio_bytes = base64.b64decode(audio_data)
                audio_size = len(audio_bytes)
                
                # Direction is fixed to "egress" for this code path.
                # The RCMS `src` field (tx/rx) identifies the media source
                # relative to Infinity, not the direction relative to the
                # bridge. Direction is determined by which side is sending:
                # frames arriving here are always incoming-to-bridge
                # (i.e., egress from libgo/MPC's perspective), regardless
                # of the source label.
                direction = "egress"
                
                bot_plugin = self.service_registry.get_plugin("bot")
                if bot_plugin and hasattr(bot_plugin, "ingest_audio_chunk"):
                    try:
                        await bot_plugin.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
                    except Exception:
                        logger.exception(f"[{client_id}] BOT plugin ingest failed for session {session_id}")
                
                # Per-endpoint+source tracking key for separate summaries
                ep_name = endpoint_id[:8] if endpoint_id else 'default'  # Use short form for logging
                endpoint_key = f"{ep_name}:{direction}:{src}"
                
                # Log first media for this stream (as JSON) with new format
                self.log_first_media(
                    client_id=client_id,
                    session_id=session_id,
                    endpoint_id=endpoint_id,
                    direction=direction,
                    format_type="base64",
                    data={"type": "media", "bid": bid, "src": src, "asn": seq, "ts": ts, "audio": audio_data},
                    stream_id=f"bid={bid},src={src}",
                    seq=seq,
                    audio_size=audio_size
                )
                
                # Log incoming media with direction and routing info (DEBUG level to reduce log flooding)
                logger.debug(f"[{client_id}] MEDIA {direction.upper()}: session={session_id}, bid={bid}, src={src}, stream_id={stream_id_key}, asn={seq}, bytes={audio_size}, transport=base64")
                
                # Initialize tracking for this endpoint if needed
                if endpoint_key not in session.media_events_this_second:
                    session.media_events_this_second[endpoint_key] = 0
                    session.media_bytes_this_second[endpoint_key] = 0
                    session.last_media_log_time[endpoint_key] = time.time()
                
                # Update media statistics for this second
                session.media_events_this_second[endpoint_key] += 1
                session.media_bytes_this_second[endpoint_key] += audio_size
                
                # Log summary once per second for this stream
                current_time = time.time()
                if current_time - session.last_media_log_time[endpoint_key] >= 1.0:
                    # Get stats for this specific source
                    in_ev = session.media_events_this_second.get(endpoint_key, 0)
                    in_bytes = session.media_bytes_this_second.get(endpoint_key, 0)
                    
                    logger.debug(f"[{client_id}] MEDIA SUMMARY (1s): bid={bid} src={src} {direction}: ev={in_ev} bytes={in_bytes} seq={seq} ts={ts}")
                    
                    # Reset this source's stats
                    session.media_events_this_second[endpoint_key] = 0
                    session.media_bytes_this_second[endpoint_key] = 0
                    session.last_media_log_time[endpoint_key] = current_time
                
                echo_plugin = self.service_registry.get_plugin("echo")
                if echo_plugin and hasattr(echo_plugin, "maybe_echo_base64"):
                    await echo_plugin.maybe_echo_base64(
                        websocket=websocket,
                        client_id=client_id,
                        session_id=session_id,
                        endpoint_id=endpoint_id,
                        bid=bid,
                        source=src,
                        seq=seq,
                        timestamp=ts,
                        audio_base64=audio_data,
                        audio_size=audio_size,
                        direction=direction,
                        lastf=lastf,
                        supports_ingress=supports_ingress,
                    )
                
            except Exception as e:
                logger.error(f"[{client_id}] Error processing media: {e}")
        else:
            logger.warning(f"[{client_id}] media received with empty audio data (bid={bid}, src={src})")
    
    async def handle_binary_frame(self, websocket: WebSocketServerProtocol, client_id: str, binary_data: bytes):
        """Decode a Compact Binary Frame Header and dispatch the audio payload to bot/echo.

        Contract:
            Phase 2 entry point for the binary transport — the wire
            shape used in all observed production sessions. Inverse of
            `IngressStreamer`'s frame-build path. Each frame carries a
            16-byte Compact Binary Frame Header followed by the
            opaque audio payload.

            Sequence:

                1. `parse_compact_binary_frame(binary_data)` decodes
                   the 16-byte header. A failed parse is logged and
                   the frame is dropped — bridge does not attempt
                   recovery, the next inbound frame stands alone.
                2. The `client_id` is reverse-mapped to a `session_id`
                   by walking `session_config`. Frames arriving on a
                   client_id with no registered session are dropped.
                3. The composite key `"<session_id>:<base_stream_id>"`
                   is looked up in `stream_id_to_endpoint` (built
                   during `handle_session_start`). An unknown stream
                   is logged and dropped. A registered stream missing
                   `endpointId` is also dropped — both indicate a
                   negotiation race or a state bug.
                4. The session is fetched from `self.sessions`. A
                   missing session here means a stale frame that
                   survived `handle_session_end` cleanup; logged and
                   dropped.
                5. **Direction is set to `"egress"` unconditionally**
                   for this code path. The Compact Binary Frame
                   Header's `source` field carries `"tx"` or `"rx"`
                   identifying the *media source* relative to
                   Infinity, not the direction relative to the bridge.
                   Frames arriving over this WebSocket are always
                   incoming-to-bridge, hence egress.
                6. Bot plugin is invoked via
                   `bot_plugin.ingest_audio_chunk(...)`. Plugin
                   exceptions are logged but do not abort the frame —
                   the rest of the pipeline still runs.
                7. `log_first_media` runs the once-per-stream verbose
                   FIRST MEDIA banner gated by
                   `self.first_media_logged`.
                8. Per-second MEDIA SUMMARY counters are advanced;
                   the summary line emits at most once per second
                   per `(endpoint, direction, source)` triple to
                   bound log volume. The summary surfaces the
                   compact frame's is_last bit derived from
                   `flags & FLAG_LAST_FRAME_COMPACT`.
                9. Echo plugin is invoked via
                   `echo_plugin.maybe_echo_binary(...)`. The plugin
                   itself decides whether the call's botId selects
                   the echo path; if not, it returns without sending.

        Spec:
            RCMS spec §Media Encoding Options — Compact Binary Frame
            Header layout (16 bytes total: bid, source, streamID,
            sequenceNum, timestamp, flags). The flag bit
            `FLAG_LAST_FRAME_COMPACT` (bit 0) is the binary-transport
            equivalent of the JSON `lastf` field. Authoritative
            reference: `bridge/schema/rcms.schema.md` "media messages"
            and the Avaya developer portal spec page:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: WebSocket connection the frame arrived on,
                forwarded to the echo plugin for the in-flight
                ingress mirror path.
            client_id: WebSocket peer identifier ("<host>:<port>");
                reverse-mapped to a session via `session_config`.
            binary_data: Full WebSocket binary message bytes,
                including the 16-byte header.

        Returns:
            None. Returns early on parse failure, missing session,
            unknown stream, or unrecoverable lookup gap. Plugin
            exceptions are logged and swallowed.
        """
        # Parse the compact binary media frame
        parsed = parse_compact_binary_frame(binary_data)
        if not parsed:
            logger.warning(f"[{client_id}] Failed to parse compact binary media frame, ignoring")
            return
        
        bid = parsed['bid']
        source = parsed['source']
        base_stream_id = parsed['streamID']  # Internal lookup key (e.g., "0:1")
        seq = parsed['sequenceNum']
        ts_micros = parsed['timestamp']
        flags = parsed['flags']
        audio_bytes = parsed['payload']
        
        # Find session_id for this client_id
        session_id = None
        for sid, config in self.session_config.items():
            if config.get("client_id") == client_id:
                session_id = sid
                break
        
        if not session_id:
            logger.warning(f"[{client_id}] No session found for client_id when processing binary frame")
            return
        
        endpoint_id = ""

        # Look up endpoint info from stream ID using composite key (session_id:stream_id_key)
        stream_id_key = f"{session_id}:{base_stream_id}"
        endpoint_info = self.stream_id_to_endpoint.get(stream_id_key)
        if not endpoint_info:
            logger.warning(f"[{client_id}] Unknown stream in binary frame: bid={bid}, src={source} (key: {stream_id_key})")
            return
        
        session_id = endpoint_info["sessionId"]
        endpoint_id = endpoint_info.get("endpointId") or ""
        supports_ingress = endpoint_info.get("supports_ingress", False)

        if not endpoint_id:
            logger.warning(f"[{client_id}] Incomplete endpoint info in binary frame: {endpoint_info}")
            return
        
        # Get session
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"[{client_id}] No session found for ID: {session_id}")
            return
        
        try:
            audio_size = len(audio_bytes)
            
            # Determine direction from message flow (NOT from source).
            #
            # This handler processes compact binary media frames RECEIVED by the WSS from the WebSocket
            # client (libgo/MPC). That is always EGRESS (incoming to WSS). The "source" field (tx/rx)
            # indicates the media source, not direction.
            direction = "egress"
            
            bot_plugin = self.service_registry.get_plugin("bot")
            if bot_plugin and hasattr(bot_plugin, "ingest_audio_chunk"):
                try:
                    await bot_plugin.ingest_audio_chunk(session_id, endpoint_id, source, audio_bytes)
                except Exception:
                    logger.exception(f"[{client_id}] BOT plugin ingest failed for session {session_id} (binary)")
            # Per-endpoint+source tracking key for separate summaries
            ep_name = endpoint_id[:8] if endpoint_id else 'default'
            endpoint_key = f"{ep_name}:{direction}:{source}"
            
            # Log first media for this stream (as binary)
            self.log_first_media(
                client_id=client_id,
                session_id=session_id,
                endpoint_id=endpoint_id,
                direction=direction,
                format_type="binary",
                data=parsed,
                stream_id=f"bid={bid},src={source}",
                seq=seq,
                audio_size=audio_size
            )
            
            # Log incoming media with direction and routing info (DEBUG level to reduce log flooding)
            logger.debug(f"[{client_id}] MEDIA {direction.upper()}: session={session_id}, bid={bid}, src={source}, stream_id={base_stream_id}, seq={seq}, bytes={audio_size}, flags=0x{flags:04X}, transport=binary")
            
            # Initialize tracking for this endpoint if needed
            if endpoint_key not in session.media_events_this_second:
                session.media_events_this_second[endpoint_key] = 0
                session.media_bytes_this_second[endpoint_key] = 0
                session.last_media_log_time[endpoint_key] = time.time()
            
            # Update media statistics for this endpoint+source
            session.media_events_this_second[endpoint_key] += 1
            session.media_bytes_this_second[endpoint_key] += audio_size
            
            # Log summary once per second for this stream
            current_time = time.time()
            if current_time - session.last_media_log_time[endpoint_key] >= 1.0:
                is_last = (flags & FLAG_LAST_FRAME_COMPACT) != 0
                # Get stats for this specific source
                in_ev = session.media_events_this_second.get(endpoint_key, 0)
                in_bytes = session.media_bytes_this_second.get(endpoint_key, 0)
                
                logger.debug(f"[{client_id}] MEDIA SUMMARY (1s): id={base_stream_id} source={source} {direction}: ev={in_ev} bytes={in_bytes} seq={seq} last={is_last}")
                
                # Reset this source's stats
                session.media_events_this_second[endpoint_key] = 0
                session.media_bytes_this_second[endpoint_key] = 0
                session.last_media_log_time[endpoint_key] = current_time
            
            echo_plugin = self.service_registry.get_plugin("echo")
            if echo_plugin and hasattr(echo_plugin, "maybe_echo_binary"):
                await echo_plugin.maybe_echo_binary(
                    websocket=websocket,
                    client_id=client_id,
                    session_id=session_id,
                    endpoint_id=endpoint_id,
                    bid=bid,
                    source=source,
                    seq=seq,
                    timestamp_micros=ts_micros,
                    flags=flags,
                    audio_bytes=audio_bytes,
                    direction=direction,
                    extension=parsed.get("extension", b""),
                    supports_ingress=supports_ingress,
                )
            
        except Exception as e:
            logger.error(f"[{client_id}] Error processing compact binary media: {e}")
    
    async def handle_session_end(self, websocket: WebSocketServerProtocol, client_id: str, data: Dict[str, Any]):
        """Tear down a session in response to RCMS `session.end` / `session.stop`.

        Contract:
            Triggered by Infinity sending `session.end` (caller hangup,
            workflow-driven shutdown) or `session.stop`. Both message
            types route here. The teardown sequence is:

                1. `IngressStreamer.stop_and_clear(session_id)` — cancel
                   every per-endpoint streaming task in this session,
                   purging queues and per-endpoint state.
                2. Plugin fan-out: every registered `ServicePlugin`
                   receives `on_session_ended(session_id)`. This is the
                   hook that drives the caller-disconnect contract — the
                   bot service's `on_session_ended` is what emits
                   `bot.ended` with `payload.context.status.reason =
                   "CALLER_DISCONNECTED"` (RCMS spec §Status Codes 200
                   CALLER_DISCONNECTED). Plugin exceptions are logged but
                   do not abort the teardown.
                3. Local state cleanup: `self.sessions`,
                   `stream_id_to_endpoint`, `endpoint_tag_to_id`,
                   `endpoint_ingress_bid`, and `session_config` are
                   purged of every entry keyed under `session_id`.
                4. `session.ended` reply is sent with the next outbound
                   `sequenceNum` and a current UTC timestamp.

            Cleanup is the symmetric counterpart to the per-session state
            built up in `handle_session_start`. Any state dict added
            there must be added here as well — there is no central
            registry, so this function and `handle_connection.finally`
            both enumerate the dict set explicitly.

        Spec:
            RCMS spec §Session Lifecycle — `session.end` / `session.ended`.
            RCMS spec §Status Codes — `200 CALLER_DISCONNECTED` is emitted
            by the bot plugin's `on_session_ended` when caller hangup
            drives the end. See `bridge/schema/rcms.schema.md`
            "session.ended reason value".

        Args:
            websocket: WebSocket connection used for the `session.ended`
                reply.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            data: The decoded `session.end` / `session.stop` JSON
                envelope; only `sessionId` is read.

        Returns:
            None. Always sends `session.ended`; plugin exceptions are
            swallowed.
        """
        session_id = data.get("sessionId", "unknown")
        
        logger.info(f"[{client_id}] Session end - session: {session_id}")
        
        # Stop all ingress streaming for this session
        await self.ingress_streamer.stop_and_clear(session_id)
        
        # Notify registered plugins so they can release per-session resources.
        for plugin in self.service_registry.plugins:
            try:
                await plugin.on_session_ended(session_id)
            except Exception:
                logger.exception(f"[{client_id}] Error while notifying plugin '{plugin.name}' of session end")
        
        session = self.sessions.get(session_id)
        if session:
            del self.sessions[session_id]
        
        # Clean up stream_id mappings for this session
        if session_id in self.session_config:
            session_client_id = self.session_config[session_id].get("client_id")
            if session_client_id:
                # Remove all stream_id mappings for this session
                keys_to_remove = [
                    key for key, info in self.stream_id_to_endpoint.items()
                    if info["sessionId"] == session_id
                ]
                for key in keys_to_remove:
                    del self.stream_id_to_endpoint[key]
                    logger.debug(f"[{client_id}] Removed stream_id mapping: {key}")
            
            # Clean up endpoint tag mappings for this session
            tag_keys_to_remove = [
                key for key in self.endpoint_tag_to_id.keys()
                if key.startswith(session_id + ":")
            ]
            for key in tag_keys_to_remove:
                del self.endpoint_tag_to_id[key]
                logger.debug(f"[{client_id}] Removed endpoint tag mapping: {key}")
            
            # Clean up endpoint ingress bid mappings for this session
            ingress_bid_keys_to_remove = [
                key for key in self.endpoint_ingress_bid.keys()
                if key.startswith(session_id + ":")
            ]
            for key in ingress_bid_keys_to_remove:
                del self.endpoint_ingress_bid[key]
                logger.debug(f"[{client_id}] Removed endpoint ingress bid mapping: {key}")
            
            # Clean up session config
            del self.session_config[session_id]
        
        # Send session.ended response
        response = {
            "version": "1.0.0",
            "type": "session.ended",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat()
        }
        
        logger.info(f"[{client_id}] OUTBOUND JSON (session.ended): {format_compact_json(response)}")
        # Log to message logger using centralized function
        log_message_exchange("OUTBOUND", client_id, "session.ended", response, is_media=False)
        await websocket.send(json.dumps(response))

    async def handle_session_ping(self, websocket: WebSocketServerProtocol, client_id: str, data: Dict[str, Any]):
        """Reply to RCMS `session.ping` with `session.pong`, validating the session is live.

        Contract:
            The bridge accepts `session.ping` only on a live session
            (i.e. an entry exists in `self.sessions` for `sessionId`).
            A ping for an unknown or already-ended session draws a
            `session.error` with code 404 / `SESSION_NOT_FOUND` and
            description identifying the missing session — this is the
            standard signature of a stale connection that survived a
            bridge restart, where Infinity still thinks the session is
            up but bridge state is empty.

            On a live session, replies with `session.pong` carrying the
            same `sessionId`, the next outbound `sequenceNum`, and a
            current ISO-8601 UTC timestamp. No platform-side state
            change happens — ping/pong is purely a liveness probe.

        Spec:
            RCMS spec §Session Lifecycle — `session.ping` / `session.pong`.
            Authoritative reference: `bridge/schema/rcms.schema.md` and
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: WebSocket connection used for the response.
            client_id: WebSocket peer identifier in `"<host>:<port>"` form.
            data: The decoded `session.ping` JSON envelope; only
                `sessionId` is read.

        Returns:
            None. Returns early after sending `session.error` if the
            session is unknown.
        """
        session_id = data.get("sessionId", "unknown")
        
        # Validate session exists - ping should only work on active sessions
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"[{client_id}] session.ping for unknown session {session_id} - likely stale connection after restart")
            await self.send_session_error(
                websocket, 
                client_id, 
                session_id,
                message_type="session.ping",
                code=404,
                reason="SESSION_NOT_FOUND",
                description=f"Session {session_id} does not exist or was terminated"
            )
            return
        
        logger.info(f"[{client_id}] Session ping - session: {session_id}")
        
        # Send session.pong response
        response = {
            "version": "1.0.0",
            "type": "session.pong",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat()
        }
        
        logger.info(f"[{client_id}] OUTBOUND JSON (session.pong): {format_compact_json(response)}")
        # Log to message logger using centralized function
        log_message_exchange("OUTBOUND", client_id, "session.pong", response, is_media=False)
        await websocket.send(json.dumps(response))
    
    def get_next_sequence(self, client_id: str) -> int:
        """Allocate the next outbound RCMS `sequenceNum` for a client connection.

        Per RCMS spec each side maintains its own sequence counter starting
        at 1, not 0. The bridge's counter is keyed by `client_id` (the
        WebSocket peer), shared across every outbound message type
        (`session.started`, `session.pong`, `bot.started`, `bot.ended`,
        `session.ended`, `session.error`, etc.). A reset to 1 indicates a
        session restart on this side.

        Initializes the counter lazily on first call so message paths
        that race ahead of `handle_connection` (or that arrive after a
        client_id was cleaned up) do not raise.

        Spec:
            RCMS spec §Protocol Design — `sequenceNum` starts at 1, not 0;
            each side maintains its own counter.
            See `bridge/schema/rcms.schema.md` "Message envelope".

        Args:
            client_id: WebSocket peer identifier in `"<host>:<port>"` form,
                used as the counter key.

        Returns:
            The next sequence number (1 on first call for an unknown
            client_id, monotonically increasing thereafter).
        """
        if client_id not in self.sequence_numbers:
            self.sequence_numbers[client_id] = 0
        self.sequence_numbers[client_id] += 1
        return self.sequence_numbers[client_id]
    
    async def send_session_error(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        message_type: str = None,
        message_seq_num: int = None,
        code: int = 400,
        reason: str = "INVALID_REQUEST",
        description: str = "Request could not be processed",
        endpoint: str = None
    ):
        """Send a `session.error` for protocol-level failures that cannot use `bot.ended`.

        Contract:
            Used for failures that occur before a bot session is bound
            to the connection, where `bot.ended` is not a valid response
            shape. Two main use cases:

                1. **Session-level routing failures.** Messages
                   addressed to a session that does not exist or has
                   been torn down (`session.event`, `session.ping`,
                   `media`, etc. arriving for an unknown
                   `sessionId`) — typically the signature of a stale
                   connection that survived a bridge restart.
                2. **Pre-bot-start protocol errors.** A `bot.start`
                   that is missing required fields cannot be answered
                   with `bot.ended` because `BotEndedPayload` requires
                   `endpointId`. The dispatcher uses this helper for
                   the `MISSING_REQUIRED_FIELDS` category instead.

            Doc-vs-code gap (carried, not fixed here): the helper's
            default `code` is 501 in legacy callers but the
            `MISSING_REQUIRED_FIELDS` case structurally aligns with the
            spec's recommended `400 MISSING_FIELD`. Callers currently
            override the defaults explicitly, so the default value
            never lands on the wire — the gap is documentation, not
            behavior. Flagged for the audit trail; not changed here
            because the slice is a no-behavior-change pass.

            Emits `session.error` with an envelope that carries the
            originating message's `type` and `sequenceNum` (when
            provided) so Infinity can correlate the error with the
            message it rejected. `endpointId` is included in the
            error payload for routing-sensitive cases (e.g.
            `session.dtmf`).

        Spec:
            RCMS spec §Error Handling — `session.error` envelope.
            See `bridge/schema/rcms.schema.md` "Message envelope" and
            §Status Codes (404 SESSION_NOT_FOUND, 400 INVALID_REQUEST).
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: WebSocket connection
            client_id: Client identifier for logging
            session_id: Session ID for the error
            message_type: Type of message that caused the error (optional)
            message_seq_num: Sequence number of message that caused error (optional)
            code: HTTP-style error code (400, 404, 500, etc.)
            reason: Short error reason (e.g., SESSION_NOT_FOUND)
            description: Human-readable error description
            endpoint: Endpoint ID for routing (optional, e.g. for session.dtmf errors)

        Returns:
            None. Always sends one outbound `session.error` frame.
        """
        error_payload = {
            "status": {
                "code": code,
                "reason": reason,
                "description": description
            }
        }
        
        # Include message identification if provided
        if message_type:
            error_payload["messageType"] = message_type
        if message_seq_num:
            error_payload["messageSequenceNum"] = message_seq_num
        if endpoint:
            error_payload["endpointId"] = endpoint
        
        response = {
            "version": "1.0.0",
            "type": "session.error",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": error_payload
        }
        
        logger.warning(f"[{client_id}] Sending session.error: code={code}, reason={reason}")
        logger.info(f"[{client_id}] OUTBOUND JSON (session.error): {format_compact_json(response)}")
        log_message_exchange("OUTBOUND", client_id, "session.error", response, is_media=False)
        await websocket.send(json.dumps(response))

    async def send_bot_ended_with_failure_context(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        code: int = 501,
        reason: str = "UNSUPPORTED_SERVICE",
        description: str = "Request could not be processed",
        service: str = "streaming",
        convo: Any = None,
    ):
        """Emit `bot.ended` with failure-shaped context — the spec-compliant response to an unprocessable `bot.start`.

        Per RCMS §Error Handling: a `[service].start` message that
        cannot be processed is failed by returning a `[service].end`
        with the error in the payload. `bot.ended` with failure-shaped
        `context.status` produces the partner-observable failure
        semantics this rule requires while remaining structurally
        clean for workflow consumption: Infinity drives `session.end`
        sub-second after the `bot.ended` emission, `byobotEndContext`
        populates verbatim from `payload.context` for workflow-author
        consumption, and the IVA module exits via SUCCESSFUL — letting
        downstream Decision modules branch on
        `byobotEndContext.status` rather than on a separate
        bot.end-with-top-level-status path.

        The session itself is not torn down by this call — session
        lifecycle stays under Avaya's control. The bridge sends the
        `bot.ended` frame and waits for Infinity's `session.end` to
        drive the rest of the cleanup.

        Caller contract: endpoint_id must be a valid identifier parsed
        from the inbound bot.start payload. For failures where
        endpoint_id is itself absent (the MISSING_REQUIRED_FIELDS
        category in the dispatcher), use send_session_error instead —
        BotEndedPayload schema requires endpointId, so the spec reserves
        session.error for that protocol-level case. The dispatcher's
        description-prefix convention (UNRECOGNIZED_BOTID_PREFIX:,
        BACKEND_NOT_CONFIGURED:) is preserved by this helper — the
        description string is passed through verbatim into
        payload.context.status.description.

        Bridge contract: emits a schema-valid bot.ended frame with
        payload.context.status carrying the failure code/reason/description,
        logs the outbound message via the standard wire-frame logger,
        returns after send. Does not initiate session teardown; does not
        retry. BotEndedPayload schema (endpointId required, context
        optional, no top-level status field) is preserved — failure
        semantics live inside payload.context.status, not at top-level
        payload.status.

        Spec:
            RCMS spec §Error Handling — service.start failure path
            requires emitting service.end with the error in payload.
            RCMS spec §Status Codes — `501 UNSUPPORTED_SERVICE` is the
            default for botId routing failures.
            `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context" pins the
            `payload.context.status.{code,reason,description}` shape.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: the client websocket
            client_id: client identifier for logging and sequence numbering
            session_id: session identifier from the originating bot.start
            endpoint_id: endpoint identifier from the originating bot.start
                (schema-required on bot.ended's payload)
            code: HTTP-style status code (default 501 UNSUPPORTED_SERVICE)
            reason: short reason string (default UNSUPPORTED_SERVICE)
            description: human-readable diagnostic; the dispatcher's
                description-prefix convention applies
                (BACKEND_NOT_CONFIGURED:, UNRECOGNIZED_BOTID_PREFIX:, etc.)
            service: service identifier echoed in the outbound frame
                (default 'streaming')

        Returns:
            None. Sends one `bot.ended` frame on the success path;
            returns early without sending if `convo.bot_ended_sent`
            is already `True`.
        """
        response = {
            "version": "1.0.0",
            "type": "bot.ended",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": service,
            "payload": {
                "endpointId": endpoint_id,
                "context": {
                    "status": {
                        "code": code,
                        "reason": reason,
                        "description": description,
                    },
                },
            },
        }

        if convo is not None and getattr(convo, "bot_ended_sent", False):
            logger.warning(
                f"[{client_id}] bot.ended already sent for this conversation — skipping failure-context emission"
            )
            return

        logger.warning(f"[{client_id}] Sending bot.ended with failure context: code={code}, reason={reason}")
        logger.info(f"[{client_id}] OUTBOUND JSON (bot.ended): {format_compact_json(response)}")
        log_message_exchange("OUTBOUND", client_id, "bot.ended", response, is_media=False)
        await websocket.send(json.dumps(response))
        if convo is not None:
            convo.bot_ended_sent = True

    async def send_bot_ended_with_success_context(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        code: int = 200,
        reason: str = "ENDPOINT_RELEASED",
        description: str = "Self-service interaction completed.",
        service: str = "streaming",
        convo: Any = None,
    ):
        """Emit `bot.ended` with success-shaped context — the self-service-complete signal.

        Sibling to `send_bot_ended_with_failure_context`. Same wire
        shape (`endpointId` at payload, `status` object inside
        `payload.context`), same logging path, same no-teardown
        contract — only the default parameter values differ.
        `status.code: 200` with `reason: "ENDPOINT_RELEASED"`
        populates `byobotEndContext` verbatim and routes the IVA
        module via the SUCCESSFUL branch, giving workflow authors a
        single numeric discriminator across all termination outcomes
        (`byobotEndContext.status.code == 200` → success;
        `>= 400` → failure).

        The session itself is not torn down by this call — session
        lifecycle stays under Avaya's control. The bridge sends the
        `bot.ended` frame and waits for Infinity's `session.end` to
        drive the rest of the cleanup.

        Caller contract: invoked by provider plugins when the LLM signals
        that the caller's need has been fully resolved (e.g. Gemini /
        OpenAI / xAI end_session function tool fires; ElevenLabs upstream
        WebSocket closes cleanly because the native End Call system tool
        fired). Caller is responsible for any provider-side quiescence
        drain — see _wait_for_quiescence_and_emit_session_end on the LLM
        plugins. Use send_bot_ended_with_failure_context for unrecoverable
        failure paths (dispatcher BACKEND_NOT_CONFIGURED / UNRECOGNIZED_-
        BOTID_PREFIX, mid-session provider error). The two helpers form
        a discriminator pair: code 200 success vs code >= 400 failure.

        Bridge contract: emits a schema-valid bot.ended frame with
        payload.context.status carrying success code/reason/description,
        logs the outbound message via the standard wire-frame logger,
        returns after send. Does not initiate session teardown; does not
        retry. BotEndedPayload schema (endpointId required, context
        optional, no top-level status field) is preserved — outcome
        semantics live inside payload.context.status, not at top-level
        payload.status.

        Spec:
            RCMS spec §Error Handling and §Status Codes — `200
            ENDPOINT_RELEASED` as the self-service-complete outcome.
            `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context" pins the
            `payload.context.status.{code,reason,description}` shape.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: the client websocket
            client_id: client identifier for logging and sequence numbering
            session_id: session identifier from the originating bot.start
            endpoint_id: endpoint identifier from the originating bot.start
                (schema-required on bot.ended's payload)
            code: HTTP-style status code (default 200 ENDPOINT_RELEASED)
            reason: short reason string (default ENDPOINT_RELEASED)
            description: human-readable diagnostic; provider plugins prefix
                with their identifier (GEMINI:, OPENAI:, XAI:, ELEVENLABS:)
            service: service identifier echoed in the outbound frame
                (default 'streaming')

        Returns:
            None. Sends one `bot.ended` frame on the success path;
            returns early without sending if `convo.bot_ended_sent`
            is already `True`.
        """
        response = {
            "version": "1.0.0",
            "type": "bot.ended",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": service,
            "payload": {
                "endpointId": endpoint_id,
                "context": {
                    "status": {
                        "code": code,
                        "reason": reason,
                        "description": description,
                    },
                },
            },
        }

        if convo is not None and getattr(convo, "bot_ended_sent", False):
            logger.warning(
                f"[{client_id}] bot.ended already sent for this conversation — skipping success-context emission"
            )
            return

        logger.info(f"[{client_id}] Sending bot.ended with success context: code={code}, reason={reason}")
        logger.info(f"[{client_id}] OUTBOUND JSON (bot.ended): {format_compact_json(response)}")
        log_message_exchange("OUTBOUND", client_id, "bot.ended", response, is_media=False)
        await websocket.send(json.dumps(response))
        if convo is not None:
            convo.bot_ended_sent = True

    async def send_bot_ended_with_disconnect_context(
        self,
        websocket: WebSocketServerProtocol,
        client_id: str,
        session_id: str,
        endpoint_id: str,
        provider: str,
        convo: Any = None,
        service: str = "streaming",
    ):
        """Emit `bot.ended` with `CALLER_DISCONNECTED` context — the caller-hangup termination signal.

        Contract:
            Used when Infinity drives `session.end` while a bot session
            is still active (the caller hung up before self-service
            completed). Without this signal the bridge would tear down
            silently and the workflow runtime would report the bot
            as having ended without a recorded outcome. Emitting
            `bot.ended` with structured disconnect context lets the
            workflow distinguish caller hangup from operator
            termination, dialog timeouts, and other platform actions.

            Status semantics: code 200 (intentional caller-driven end,
            not an error) with reason `"CALLER_DISCONNECTED"`.
            `200/CALLER_DISCONNECTED` is preferred over the spec's
            ambiguous `DIALOG_TERMINATED` because workflow analytics
            need a stable discriminator between caller-driven end,
            self-service complete, and failure paths.

            Typically called from each provider's
            `_shutdown_conversation` when `convo.bot_ended_sent` is
            `False`. If called when already `True`, the helper warns
            and skips — this defensive guard catches any future caller
            that forgets the precheck and silently prevents a
            duplicate emission.

            The session itself is not torn down by this call —
            session lifecycle stays under Avaya's control. This helper
            runs inside the provider's `session.end` response path;
            the bridge emits `session.ended` after this returns.

        Spec:
            RCMS spec §Status Codes — `200 CALLER_DISCONNECTED` is
            documented as a recognized disconnect-context outcome.
            See `bridge/schema/rcms.schema.md` "bot.ended — status is
            nested in context" for the
            `payload.context.status.{code,reason,description}`
            shape consumed by the workflow's `byobotEndContext`.
            Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming

        Args:
            websocket: the active Infinity-side WebSocket connection
            client_id: opaque session identifier for logging
            session_id: RCMS session identifier from the originating
                bot.start
            endpoint_id: media endpoint identifier from bot.start
                (schema-required on bot.ended's payload)
            provider: provider identifier for description prefix; one
                of 'echo', 'elevenlabs', 'gemini', 'openai', 'xai'
            convo: optional BotConversation instance; if provided,
                bot_ended_sent is set True after successful send to
                prevent duplicate emissions
            service: RCMS service name (default 'streaming')

        Returns:
            None. Always sends one `bot.ended` frame on the success
            path; returns early without sending if `convo.bot_ended_sent`
            is already `True`.
        """
        if convo is not None and getattr(convo, "bot_ended_sent", False):
            logger.warning(
                f"[{client_id}] bot.ended already sent for this conversation — skipping disconnect-context emission"
            )
            return

        provider_prefix = provider.upper() if provider else "BRIDGE"
        description = f"{provider_prefix}: Caller disconnected before self-service complete."

        response = {
            "version": "1.0.0",
            "type": "bot.ended",
            "sessionId": session_id,
            "sequenceNum": self.get_next_sequence(client_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "service": service,
            "payload": {
                "endpointId": endpoint_id,
                "context": {
                    "status": {
                        "code": 200,
                        "reason": "CALLER_DISCONNECTED",
                        "description": description,
                    },
                },
            },
        }

        logger.info(
            f"[{client_id}] Sending bot.ended with disconnect context: provider={provider}"
        )
        logger.info(f"[{client_id}] OUTBOUND JSON (bot.ended): {format_compact_json(response)}")
        log_message_exchange("OUTBOUND", client_id, "bot.ended", response, is_media=False)
        await websocket.send(json.dumps(response))
        if convo is not None:
            convo.bot_ended_sent = True

    def get_ingress_bid(self, session_id: str, endpoint_id: str) -> Optional[int]:
        """Look up the per-endpoint ingress bid registered during `session.start`.

        Contract:
            Each `(session_id, endpoint_id)` pair has at most one
            ingress bid, assigned in `handle_session_start` while
            walking `payload.mediaEndpoints` and registered in
            `self.endpoint_ingress_bid`. The bid prefixes every `media`
            frame the bridge sends back toward the caller's leg so
            Infinity can route the audio to the right RTP destination.

            Looks up the composite key `"<session_id>:<endpoint_id>"`
            and returns the stored value, or `None` if the endpoint
            was never registered for ingress (the
            `flows.audio.ingress.target` field was empty or `["none"]`)
            or the session has been torn down. `IngressStreamer.
            send_immediate` and `IngressStreamer.barge_in` both call
            this and treat `None` as "skip the send / skip the
            lastf marker."

        Spec:
            RCMS spec §Media Encoding Options — `mediaEndpoints` and
            per-flow bid assignment. Authoritative reference:
            `bridge/schema/rcms.schema.md`, "media messages".

        Args:
            session_id: Session identifier.
            endpoint_id: Endpoint identifier within that session.

        Returns:
            The ingress bid (positive int) if registered, otherwise
            `None`.
        """
        key = f"{session_id}:{endpoint_id}"
        return self.endpoint_ingress_bid.get(key)

    async def start_server(self):
        """Bind the WebSocket listener, attach the auth gate, and run forever.

        Contract:
            Single entrypoint called from `main.py` once command-line
            arguments and environment have been resolved. Performs four
            things in order:

                1. Resolves `host` (`localhost` is rewritten to
                   `127.0.0.1` so binding succeeds on hosts that lack
                   IPv6 loopback resolution; `0.0.0.0` and explicit
                   addresses pass through). Forces `socket.AF_INET` on
                   the listener so the bridge is reachable on IPv4
                   regardless of the host's IPv6 configuration —
                   production deploys behind Caddy on a single IPv4
                   loopback (`127.0.0.1:8444`).
                2. Builds an `ssl.SSLContext` from `self.ssl_cert` /
                   `self.ssl_key` if both are set. In the production
                   topology TLS terminates at Caddy upstream of the
                   bridge, so these are typically unset and the bridge
                   runs clear-text `ws://` on the loopback. The
                   "TLS/SSL: DISABLED" warning is informational, not
                   an error — Caddy is still terminating TLS on 443.
                3. Defines the nested `process_request` coroutine and
                   wires it into `websockets.serve` as the
                   handshake-time auth hook. See its own docstring for
                   the JWT validation contract.
                4. Awaits an unresolvable Future to run until SIGINT /
                   SIGTERM, at which point a graceful shutdown is
                   logged and the context manager closes the listener.

            `ping_interval=30` / `ping_timeout=10` keep the connection
            healthy through any intermediate TCP layer that drops idle
            flows; `close_timeout=10` bounds the graceful-close phase
            so a misbehaving peer cannot stall shutdown indefinitely.

        Spec:
            RCMS spec §Connection Setup — TLS expectations and
            bearer-token auth on the upgrade. Authoritative reference:
            https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming
            Auth header behavior is specified in `check_auth` (post-
            upgrade) and `process_request` (pre-upgrade) docstrings.

        Returns:
            Never returns under normal operation. Exits cleanly on
            `KeyboardInterrupt`; the WebSocket library propagates other
            exceptions out of the `async with` block.
        """
        import socket
        import ssl
        
        # Resolve host to IPv4 address if possible
        bind_host = self.host
        if self.host in ['localhost', '0.0.0.0', '']:
            # These are safe defaults that should work with IPv4
            bind_host = self.host if self.host != 'localhost' else '127.0.0.1'
        
        # Setup SSL context if certificates provided
        ssl_context = None
        protocol = "ws"
        if self.ssl_cert and self.ssl_key:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.ssl_cert, self.ssl_key)
            protocol = "wss"
            logger.info("TLS/SSL: ENABLED (WSS)")
        else:
            logger.warning("TLS/SSL: DISABLED (WS) - using unencrypted connection")
        
        logger.info(f"RCMS Bridge listening on {self.host}:{self.port}")
        
        # Create process_request function for authentication during handshake
        async def process_request(connection, request):
            """Validate JWT bearer auth on the HTTP upgrade request before WebSocket negotiation completes.

            Contract:
                Invoked by `websockets.serve` once per inbound TCP
                connection, after the HTTP request line + headers have
                been read but before the WebSocket upgrade is
                acknowledged. Two return-value contracts:

                  * `None` — auth passed (or auth disabled); the
                    library proceeds with the WebSocket upgrade
                    handshake and `handle_connection` will run once
                    upgrade completes.
                  * `Response(...)` — auth failed; the library returns
                    that HTTP response to the peer instead of
                    upgrading. The connection never reaches
                    `handle_connection`. All failure paths return
                    `401 Unauthorized` with a plain-text body
                    describing the specific failure mode.

                When `self.auth_enabled` is `False`, returns `None`
                immediately. With auth enabled, the gate enforces:

                    1. `Authorization: Bearer <jwt>` header must be
                       present.
                    2. The token must be parseable as a JWT (header +
                       payload decodable without verification).
                       Token contents (`sub`, `iat`, `exp`, `jti`,
                       `iss`, `aud`) are logged in a structured
                       `header=...` / `payload={...}` format for
                       post-incident forensics — particularly useful
                       to confirm which issuer's token was rejected.
                    3. The signature must verify under one of the
                       configured keys, with the same primary /
                       secondary rotation contract as `check_auth`:
                       try primary as UTF-8 bytes, then primary as
                       string, then (if configured) secondary in the
                       same two encodings. Any matching key/encoding
                       pair allows the request through; only `None`
                       returns get logged at INFO with the matching
                       key form for forensics.
                    4. Expired tokens short-circuit to a 401 with body
                       "Expired token\\n" — the log line includes the
                       parsed `exp` timestamp so operators can see
                       how long the token was past expiry.

            Why this exists separately from `check_auth`:
                Defense in depth at two distinct phases. This hook
                fires before WebSocket upgrade (HTTP layer); a failed
                check here means the peer never gets a WebSocket at
                all, so an attacker cannot consume server resources
                allocating a WebSocket connection. `check_auth` is the
                post-upgrade equivalent that runs at the start of
                `handle_connection`, providing a second gate in case
                the library's pre-upgrade hook is bypassed by a future
                websockets-library change. The signature-verification
                logic is intentionally the same in both sites; the
                duplication is a known smell carried for safety until
                a JWT test rig is built that can validate them
                separately as factored helpers.

            Args:
                connection: The websockets-library connection object;
                    `remote_address` (or transport peername fallback)
                    is read for log lines.
                request: The parsed HTTP request; `headers` is read
                    for the `Authorization` value.

            Returns:
                `None` to allow the upgrade. A `Response` with
                `status_code=401` to reject — body identifies the
                specific failure (`Missing or invalid Authorization
                header`, `Invalid token format`, `Expired token`,
                or `Invalid token`).
            """
            # If auth is disabled, proceed with handshake
            if not self.auth_enabled:
                return None

            # Get client address for logging
            try:
                client_addr = getattr(connection, 'remote_address', None)
                if client_addr:
                    client_id = f"{client_addr[0]}:{client_addr[1]}"
                else:
                    # Try to get from transport if available
                    transport = getattr(connection, 'transport', None)
                    if transport and hasattr(transport, 'get_extra_info'):
                        peername = transport.get_extra_info('peername')
                        if peername:
                            client_id = f"{peername[0]}:{peername[1]}"
                        else:
                            client_id = "unknown"
                    else:
                        client_id = "unknown"
            except Exception:
                client_id = "unknown"

            # Extract Authorization header
            auth_header = request.headers.get('Authorization', '')

            if not auth_header.startswith('Bearer '):
                logger.warning(f"Missing or invalid Authorization header from {client_id}")
                headers = Headers([('Content-Type', 'text/plain')])
                return Response(
                    status_code=HTTPStatus.UNAUTHORIZED,
                    reason_phrase="Unauthorized",
                    headers=headers,
                    body=b"Missing or invalid Authorization header\n"
                )

            token = auth_header[7:]  # Remove "Bearer " prefix

            # First, try to decode without verification to inspect token contents
            token_header = None
            token_payload = None
            decode_error = None
            try:
                token_header = jwt.get_unverified_header(token)
                token_payload = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
            except Exception as e:
                decode_error = str(e)
                logger.warning(f"Unable to decode JWT token from {client_id}: {decode_error}")
                headers = Headers([('Content-Type', 'text/plain')])
                return Response(
                    status_code=HTTPStatus.UNAUTHORIZED,
                    reason_phrase="Unauthorized",
                    headers=headers,
                    body=b"Invalid token format\n"
                )

            # Log token information
            token_info = []
            if token_header:
                token_info.append(f"header={token_header}")
            if token_payload:
                # Log key payload fields
                payload_fields = []
                for key in ['sub', 'iat', 'exp', 'jti', 'iss', 'aud']:
                    if key in token_payload:
                        value = token_payload[key]
                        if key == 'exp' or key == 'iat':
                            # Convert timestamp to readable format
                            from datetime import datetime
                            try:
                                dt = datetime.fromtimestamp(value)
                                payload_fields.append(f"{key}={dt.isoformat()}")
                            except:
                                payload_fields.append(f"{key}={value}")
                        else:
                            payload_fields.append(f"{key}={value}")
                if payload_fields:
                    token_info.append(f"payload={{{', '.join(payload_fields)}}}")

            token_info_str = " | ".join(token_info) if token_info else "no token info"

            # Verify JWT token. Try primary key first (both encodings),
            # then secondary if configured — supports key rotation without
            # downtime per RCMS spec §Security.
            keys_to_try = [
                (self.jwt_primary_key.encode('utf-8'), "primary key as UTF-8 bytes"),
                (self.jwt_primary_key, "primary key as string"),
            ]
            if self.jwt_secondary_key:
                keys_to_try += [
                    (self.jwt_secondary_key.encode('utf-8'), "secondary key as UTF-8 bytes"),
                    (self.jwt_secondary_key, "secondary key as string"),
                ]

            verification_failed = False
            last_error = None

            for key, description in keys_to_try:
                try:
                    claims = jwt.decode(token, key, algorithms=['HS256'])
                    logger.info(f"JWT bearer token auth successful for {client_id} (verified with {description}) | {token_info_str}")
                    return None  # Proceed with handshake
                except ExpiredSignatureError:
                    # Check expiration time
                    exp_time = token_payload.get('exp') if token_payload else None
                    exp_info = ""
                    if exp_time:
                        try:
                            from datetime import datetime
                            exp_dt = datetime.fromtimestamp(exp_time)
                            exp_info = f" (expired at {exp_dt.isoformat()})"
                        except:
                            exp_info = f" (exp={exp_time})"
                    logger.warning(f"Expired JWT bearer token from {client_id}{exp_info} | {token_info_str}")
                    headers = Headers([('Content-Type', 'text/plain')])
                    return Response(
                        status_code=HTTPStatus.UNAUTHORIZED,
                        reason_phrase="Unauthorized",
                        headers=headers,
                        body=b"Expired token\n"
                    )
                except InvalidTokenError as e:
                    # Try next key format
                    last_error = str(e)
                    verification_failed = True
                    continue
                except Exception as e:
                    last_error = str(e)
                    verification_failed = True
                    continue

            # If all verification attempts fail
            reason = f"signature verification failed: {last_error}" if last_error else "signature verification failed"
            logger.warning(f"Invalid JWT bearer token from {client_id} - {reason} | {token_info_str}")
            headers = Headers([('Content-Type', 'text/plain')])
            return Response(
                status_code=HTTPStatus.UNAUTHORIZED,
                reason_phrase="Unauthorized",
                headers=headers,
                body=b"Invalid token\n"
            )

        async with websockets.serve(
            self.handle_connection,
            bind_host,
            self.port,
            ssl=ssl_context,
            family=socket.AF_INET,  # Force IPv4
            process_request=process_request,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=10
        ):
            logger.info(f"Server running on {protocol}://{bind_host}:{self.port}")
            if self.auth_enabled:
                logger.info(f"JWT bearer token authentication: ENABLED")
            else:
                logger.info("JWT bearer token authentication: DISABLED")
            logger.info("Press Ctrl+C to stop")
            
            try:
                await asyncio.Future()  # Run forever
            except KeyboardInterrupt:
                logger.info("Shutting down server...")


