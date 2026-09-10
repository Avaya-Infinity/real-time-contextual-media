"""
bot_openai — OpenAI Realtime provider for the RCMS Virtual Agent (bot) service

Role:
    Implements the OpenAI provider — a translator between an Infinity
    RCMS bot session and an OpenAI Realtime API session. OpenAI
    Realtime exposes the model directly through a stateful WebSocket;
    there is no hosted agent surface (no dashboard-configured prompt,
    no platform-side tools, no first-message field). The bridge owns
    every piece of orchestration:

        * **System prompt composition** — `_load_base_prompt` resolves
          a four-tier chain (`OPENAI_SYSTEM_PROMPT_FILE` env →
          `OPENAI_SYSTEM_PROMPT` env → sibling `system_prompt.md` →
          `_DEFAULT_INSTRUCTIONS` constant), and `_compose_instructions`
          interpolates CRM context fields from
          `bot.start.payload.context` into the resolved base via Python
          `.format()`; the composed instructions are sent in the
          `session.update` frame on connect.
        * **Tool definitions** — `_TRANSFER_TO_AGENT_TOOL` and
          `_END_SESSION_TOOL` declared inline in this module and
          registered via `session.update.session.tools[]`. Both are
          bridge-implemented; OpenAI Realtime does not provide
          platform tools.
        * **Conversation initiation** — `_on_session_updated` fires on
          the `session.updated` ack and sends a proactive
          `response.create` so the agent speaks first; without this,
          the agent stays silent until the caller speaks.
        * **Output audio chunking and pacing** — `_enqueue_output_audio`
          buffers OpenAI's `response.output_audio.delta` chunks into pacer-
          aligned blocks matching `IngressStreamer.chunk_duration_ms`,
          so each paced tick carries exactly one chunk's worth of
          audio.
        * **Two-path termination orchestration** — bridge owns both
          drain pipelines: `_wait_for_quiescence_and_emit` for the
          handoff path (`transfer_to_agent` toolCall) and
          `_wait_for_quiescence_and_emit_session_end` for the
          self-service-complete path (`end_session` toolCall).
        * **Per-turn transcript handling** — `response.audio_transcript.
          delta` events accumulate per-`response_id`; flush on
          `response.output_audio_transcript.done`. Caller-side transcripts
          arrive whole on `conversation.item.input_audio_transcription.
          completed`.

OpenAI Realtime architecture (relevant to this implementation):
    Audio path: codec-aware OpenAI format (`_openai_audio_format`),
        symmetric input/output. PCMU → `audio/pcmu` and PCMA →
        `audio/pcma` are zero-transcode (frames base64-wrapped/unwrapped
        only, no audioop in the hot loop). G722 / L16 → `audio/pcm` at
        24 kHz, transcoded via audioop (G722 also via its decoder/encoder)
        — this preserves the decoded wideband content and avoids the
        µ-law companding + narrowband downsample the 8 kHz path imposed.
        PCMU/PCMA remain the lowest-CPU operating configuration.
    Server VAD: OpenAI detects speech-start and speech-stop server-
        side and emits `input_audio_buffer.speech_started` /
        `speech_stopped` / `committed` events. The bridge reacts to
        `speech_started` for both turn-start timestamp capture and
        barge-in dispatch.
    Two-step session bringup: `session.created` (informational, logged
        and discarded) precedes `session.updated`. The
        `session.updated` ack is the "configuration accepted"
        signal and triggers `_on_session_updated`, which sends the
        proactive greeting and emits `bot.started` to Infinity.
    Response lifecycle: a single conversation turn produces a stream
        of discrete events — `response.created`, `response.output_audio.delta`
        (many), `response.output_audio.done`, `response.output_audio_transcript.delta`
        (many), `response.output_audio_transcript.done`, `response.done`. The
        bridge tracks two response-ID sets:
            - `active_response_ids` (list — handles overlapping
              responses) for targeted `response.cancel` on barge-in.
            - `completed_response_ids` (set) — server-side audio
              generation has finished, even though paced playout to
              Infinity may still be running. A barge-in for a
              completed response skips `response.cancel` because
              the cancel would always race-lose against OpenAI's
              already-finished state.
    Tool invocation: a function call surfaces as `response.function_
        call_arguments.done` carrying `call_id`, `name`, and a JSON
        `arguments` string. The bridge dispatches to
        `_handle_function_call`, which sends the result back via
        `conversation.item.create` with `type: "function_call_output"`.
        For non-terminal tools, the bridge follows up with
        `response.create` so the model can speak the result. For
        terminal tools (`transfer_to_agent`, `end_session`) the bridge
        suppresses the post-tool `response.create` — see "Pre-tool
        acknowledgment" below.
    Pre-tool acknowledgment (two-layer protocol): the model is
        instructed via the system prompt to speak a complete
        acknowledgment utterance as a single complete unit and only
        then invoke the terminal tool. This is the prompt-side layer.
        The bridge-side layer is the post-tool `response.create`
        suppression in `_handle_function_call`: an explicit
        `response.create` after a terminal tool would prompt the
        model to generate a continuation that lands in the audio
        drain window and reaches the caller as a duplicate
        acknowledgment. Both layers are required — the prompt
        instruction relies on model adherence; the bridge suppression
        removes the trigger that the model's adherence cannot
        always overcome.
    Barge-in: triggered by `input_audio_buffer.speech_started` while
        `audio_playing_out` is True. `_handle_barge_in` clears the
        IngressStreamer queue, sends a `lastf=true` flag to Infinity
        on the current segment, and issues `response.cancel` for
        every active response that has not already completed
        server-side.

Does not own:
    Provider routing by botId (owned by the bot dispatcher in
        bot_service.py — this plugin is registered against the
        `openai:` prefix and invoked through handle_message after
        the dispatcher has matched).
    RCMS session lifecycle, JWT authentication, and Infinity-side
        WebSocket transport (owned by bridge_server.py).
    Audio frame pacing and ingress queue management (owned by
        IngressStreamer in bridge_server.py — this plugin queues
        chunks via _send_ingress_chunked and otherwise stays out of
        the cadence path).
    Speech recognition, turn detection (server VAD), language model
        inference, and voice synthesis (owned by OpenAI Realtime;
        surfaced to the bridge via the WebSocket protocol's events).

Dependencies:
    websockets: the upstream OpenAI Realtime WebSocket client.
    audioop (stdlib; on Python 3.13+ install audioop-lts as a
        drop-in): µ-law decode/encode and resampling for the
        non-PCMU codec paths. Optional — gated by the
        `_AUDIOOP_AVAILABLE` flag. When unavailable, the bridge
        accepts only PCMU sessions and rejects L16 / PCMA / G722
        at bot.start time.
    G722 (optional): wideband codec encode/decode; gated by
        bridge_server.G722_AVAILABLE — when absent, G722-negotiated
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
        (`openai:`), resolves the model id (env-var override,
        botId suffix, or built-in default), reads `OPENAI_API_KEY`
        (with `botCredentials` fallback for per-call overrides),
        negotiates codec / sample rate, composes the system prompt,
        builds a BotConversation, connects upstream via
        _connect_openai (Bearer auth), launches _openai_recv_loop
        as a background task,
        and waits for `session.updated` before emitting bot.started
        and the proactive greeting in _on_session_updated. Every
        validation failure emits bot.ended with a non-200 status
        via the failure-context helper.
    Phase 2 (During): ingest_audio_chunk transcodes Infinity-side
        audio to the codec-aware OpenAI input format and forwards as
        `input_audio_buffer.append`. _openai_recv_loop drains the
        upstream WS and dispatches each event to
        _handle_openai_message. Audio deltas are transcoded and
        fed through _enqueue_output_audio's accumulator into
        pacer-aligned chunks. Transcript deltas accumulate per
        response and flush on transcript.done; caller transcripts
        arrive whole. Server VAD speech-start triggers turn-start
        timestamp capture and (when bot audio is playing out)
        barge-in dispatch.
    Phase 3 (Closure): four termination shapes converge on this
        plugin —
            (a) caller-disconnect → on_session_ended emits
                CALLER_DISCONNECTED via the disconnect-context
                helper;
            (b) Infinity-driven bot.end → _handle_bot_end acks
                with a manual bot.ended build;
            (c) `transfer_to_agent` toolCall → _handle_function_call
                stashes the LIVE_AGENT_HANDOFF and launches the
                handoff drain; _emit_pending_handoff fires the
                bot.feature plus a manual bot.ended (absent-status
                shape) after the audio queue drains;
            (d) `end_session` toolCall → _handle_function_call
                stashes a session-end latch and launches the
                session-end drain; _emit_session_end_complete
                fires success-context bot.ended after the audio
                queue drains.
        _shutdown_conversation is the single resource-release path
        invoked from every removal site.

Spec:
    RCMS spec §AI Bot Message Definitions — bot.start / bot.started
        / bot.end / bot.ended / bot.feature (TRANSCRIPT,
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

    OpenAI Realtime API protocol — `session.update` / `session.created`
        / `session.updated` / `input_audio_buffer.append` /
        `input_audio_buffer.speech_started` /
        `input_audio_buffer.speech_stopped` /
        `response.create` / `response.created` /
        `response.output_audio.delta` / `response.output_audio.done` /
        `response.output_audio_transcript.delta` /
        `response.output_audio_transcript.done` / `response.done` /
        `response.cancel` / `response.function_call_arguments.done` /
        `conversation.item.create` /
        `conversation.item.input_audio_transcription.completed` /
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
import urllib.request
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


# ── Constants ─────────────────────────────────────────────────────────────────

OPENAI_REALTIME_BASE_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_OPENAI_MODEL = "gpt-realtime-2.1"

OPENAI_SAMPLE_RATE = 8000           # G.711 (µ-law / A-law) is always 8 kHz
OPENAI_PCM_RATE = 24000             # OpenAI Realtime audio/pcm minimum rate (16 kHz is rejected)

DEFAULT_VOICE = "cedar"


def _openai_audio_format(codec_name: str) -> Dict[str, Any]:
    """Return the OpenAI Realtime audio-format dict for the negotiated Infinity codec.

    Codec-aware so each deployment declares the highest-fidelity OpenAI format
    that matches its inbound codec, without forcing a transcode on codecs that
    already line up with an OpenAI-native G.711 format. All three lanes were
    verified accepted via a `session.update` handshake (pcmu, pcma, pcm@24000;
    pcm@16000 is rejected — `rate` minimum is 24000):

        * PCMU  → ``audio/pcmu``          (8 kHz µ-law; zero transcode)
        * PCMA  → ``audio/pcma``          (8 kHz A-law; zero transcode)
        * G722 / L16 / other → ``audio/pcm`` @ 24000  (preserves the decoded
          wideband content; drops the µ-law companding and the narrowband
          16k→8k downsample the µ-law path imposed)

    Applied symmetrically to ``session.audio.input.format`` and
    ``output.format``.
    """
    c = (codec_name or "").upper()
    if c == "PCMU":
        return {"type": "audio/pcmu"}
    if c == "PCMA":
        return {"type": "audio/pcma"}
    return {"type": "audio/pcm", "rate": OPENAI_PCM_RATE}

# Bound on the pre-ingress-ready buffer (see BotConversation.ingress_buffer
# and the gate in _handle_openai_message's response.output_audio.delta branch).
# Infinity's ingress path opens after it begins emitting egress; the buffer
# absorbs any OpenAI audio that lands before that point. Cap is conservative
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
# (e.g. OpenAI WS hung, or an egress queue that never drains under a pacing
# collapse). A healthy handoff drain completes in ~4-6s (the acknowledgment
# phrase playout) and never exceeded ~11s in observed calls; 12s bounds
# worst-case caller dead air while leaving margin above the longest
# legitimate drain. Trip logs at WARNING and emits the terminal envelope
# (LIVE_AGENT_HANDOFF or success-context bot.ended) anyway so the workflow
# does not stall.
_QUIESCENCE_DEADLOCK_SAFETY_S = 12.0

# Maximum WebSocket frame size for the upstream OpenAI connection. 8 MiB
# accommodates the largest audio frames OpenAI emits without triggering
# websockets.exceptions.PayloadTooBig on long-form responses.
_WS_MAX_FRAME_BYTES = 2**23


def _resolve_language_name(code: str) -> str:
    """Map a BCP-47 / ISO 639-1 language code to a human-readable language name.

    Contract:
        Used by `_compose_instructions` so the persona prompt's
        `{language}` placeholder reads naturally ("Always respond
        in English") rather than as a code ("Always respond in
        en-US"). The mapping is lower-cased on the base subtag
        (`"en-US"` → `"en"` → `"English"`). Unknown codes pass
        through verbatim — the prompt then receives the raw code,
        which is still comprehensible to the model. Empty /
        missing input defaults to `"English"`.

    Args:
        code: BCP-47 or ISO 639-1 language tag (e.g.
            `"en-US"`, `"es"`, `"zh-Hant"`).

    Returns:
        Human-readable language name when the base subtag is
        recognized; the input string otherwise; `"English"` on
        empty / whitespace-only input.
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
# OpenAIService). Also returned by `_compose_instructions` when
# `.format()` raises on a placeholder mismatch — `_DEFAULT_INSTRUCTIONS`
# carries no placeholders so it is safe to return unformatted on either
# path. The deployment-tuned base prompt lives in the sibling
# `system_prompt.md`; the design rationale (no-confirmation transfer,
# tool-call preamble pattern, OpenAI Realtime Prompting Guide
# alignment) is captured in those `system_prompt.md` section headers.
_DEFAULT_INSTRUCTIONS = (
    "You are Cedar, a helpful and professional AI customer service agent. "
    "Be concise, empathetic, and professional."
)

# transfer_to_agent tool definition — single optional `reason` string
# parameter. Registered in session.update.session.tools[] in _connect_openai;
# invocation surfaces as response.function_call_arguments.done and is
# dispatched in _handle_function_call.
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

# end_session tool definition — self-service-complete signal. The model
# invokes this when the caller's need has been fully resolved and the
# call should terminate cleanly. The bridge maps the invocation to a
# success-context bot.ended via send_bot_ended_with_success_context after
# the closing-line audio drains; Infinity drives session.end teardown
# in response.
_END_SESSION_TOOL = {
    "type": "function",
    "name": "end_session",
    "description": (
        "End the call when you have fully resolved the caller's need "
        "and no further assistance is required. The caller will hear "
        "your final response, then the call will disconnect."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ── OpenAI Conversation mirroring (Agent Assist cross-channel parity) ───────────

def _format_crm_seed(context: Dict[str, Any]) -> str:
    """Build the digital-parity "Customer context" system seed from the flat CRM
    scalar fields the IVA module forwards in `payload.context`.

    The IVA flattens the CRM record into top-level scalar keys (camelCase) on
    the wire — there is no nested `crm.*` object — so each field is read
    directly by key. Label wording and field ordering match the digital channel
    so Agent Assist cannot distinguish a voice-originated `conv_` from a
    digital-originated one by the seed item.

    Field set (digital ordering): Name, Email, Phone, Open case (subject + id),
    Case priority, Case status, Case description, Assigned to, Contact ID,
    Account ID, Last contacted.

    Lines for missing or empty fields are omitted entirely — empty strings are
    treated the same as absent (no "Not provided" placeholders, which would
    pollute the conv_ for Agent Assist). Returns "" when no fields are present,
    in which case the caller skips the seed and only mirrors turns. Fields the
    IVA does not yet forward (e.g. `phone`) simply omit and will appear
    automatically once forwarding is added — no code change required.
    """
    if not isinstance(context, dict):
        return ""

    def f(key: str) -> str:
        # Collapse missing (None), empty, and whitespace-only to "" so the
        # truthiness guards below omit them identically.
        return (context.get(key) or "").strip()

    first = f("firstName")
    last = f("lastName")
    email = f("email")
    phone = f("phone")
    subject = f("caseSubject")
    case_id = f("caseId")
    priority = f("casePriority")
    status = f("caseStatus")
    description = f("caseDescription")
    assigned_to = f("caseAssignedTo")
    contact_id = f("contactId")
    account_id = f("accountId")
    last_contacted = f("lastContactedAt")

    lines = []
    name = " ".join(p for p in (first, last) if p)
    if name:
        lines.append(f"- Name: {name}")
    if email:
        lines.append(f"- Email: {email}")
    if phone:
        lines.append(f"- Phone: {phone}")
    if subject and case_id:
        lines.append(f'- Open case: "{subject}" (ID: {case_id})')
    elif subject:
        lines.append(f'- Open case: "{subject}"')
    elif case_id:
        lines.append(f"- Open case ID: {case_id}")
    if priority:
        lines.append(f"- Case priority: {priority}")
    if status:
        lines.append(f"- Case status: {status}")
    if description:
        lines.append(f"- Case description: {description}")
    if assigned_to:
        lines.append(f"- Assigned to: {assigned_to}")
    if contact_id:
        lines.append(f"- Contact ID: {contact_id}")
    if account_id:
        lines.append(f"- Account ID: {account_id}")
    if last_contacted:
        lines.append(f"- Last contacted: {last_contacted}")

    if not lines:
        return ""
    return "Customer context:\n" + "\n".join(lines)


class ConvMirror:
    """Best-effort mirror of finalized Realtime turns into a pre-created OpenAI
    Conversation object (`conv_…`) for cross-channel Agent Assist parity.

    The Realtime API will not write into a conversation created via
    `POST /v1/conversations` (empirically confirmed — items stay empty), so the
    bridge mirrors finalized transcript turns itself via
    `POST /v1/conversations/{conv_id}/items`. Items are enqueued from the
    realtime hot path (non-blocking `put_nowait`) and posted by a single
    background worker. The POST uses stdlib `urllib.request` run in a thread
    (`asyncio.to_thread`) so it never blocks the event loop and adds no
    dependency, reusing the same truststore SSL context as the upstream WS
    connect so the corporate-TLS-interception path keeps working.

    Mirroring is strictly best-effort: every failure is swallowed with a
    WARNING and the live call is never affected. Item content types follow the
    Conversations API shape — `input_text` for system/user, `output_text` for
    assistant.

    NOTE: items must be POSTed with a key in the same OpenAI project that
    created the `conv_` — a cross-project key returns HTTP 404.
    """

    _ITEMS_URL = "https://api.openai.com/v1/conversations/{conv_id}/items"
    _MAX_RETRIES = 3
    _BACKOFF_BASE_S = 0.5
    _POST_TIMEOUT_S = 10
    _DRAIN_TIMEOUT_S = 2.5

    def __init__(self, conv_id: str, api_key: str, client_id: str) -> None:
        self.conv_id = conv_id
        self.client_id = client_id
        self._api_key = api_key
        self._url = self._ITEMS_URL.format(conv_id=conv_id)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        # Mirror the SSL-context selection used by _connect_openai so corporate
        # TLS-interception roots resolve identically on the REST path.
        if _truststore is not None:
            self._ssl_ctx = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        else:
            self._ssl_ctx = _ssl.create_default_context()

    def start(self) -> None:
        """Spawn the background drain worker (idempotent)."""
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def enqueue(self, role: str, text: str) -> None:
        """Queue one message item. `role` is 'system' | 'user' | 'assistant'.
        No-op on blank text; never raises into the realtime caller."""
        text = (text or "").strip()
        if not text:
            return
        content_type = "output_text" if role == "assistant" else "input_text"
        item = {
            "type": "message",
            "role": role,
            "content": [{"type": content_type, "text": text}],
        }
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:  # unbounded in practice — defensive only
            logger.warning(
                "[%s] conv_ mirror queue full; dropped role=%s item",
                self.client_id, role,
            )

    async def _run(self) -> None:
        """Drain the queue, posting one item per request, until cancelled."""
        while True:
            item = await self._queue.get()
            try:
                await self._post_with_retry(item)
            finally:
                self._queue.task_done()

    async def _post_with_retry(self, item: Dict[str, Any]) -> None:
        body = json.dumps({"items": [item]}).encode("utf-8")
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                msg_id = await asyncio.to_thread(self._post, body)
                logger.info(
                    "[%s] conv_ item posted: role=%s %s",
                    self.client_id, item["role"], msg_id,
                )
                return
            except Exception as exc:
                if attempt == self._MAX_RETRIES:
                    logger.warning(
                        "[%s] conv_ item dropped after %d attempts: role=%s err=%s",
                        self.client_id, self._MAX_RETRIES, item["role"], exc,
                    )
                    return
                await asyncio.sleep(self._BACKOFF_BASE_S * (2 ** (attempt - 1)))

    def _post(self, body: bytes) -> str:
        """Synchronous POST (runs in a worker thread). Returns the created item
        id, or "" if the response can't be parsed. Raises on HTTP/transport
        error so `_post_with_retry` can retry."""
        req = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(
            req, timeout=self._POST_TIMEOUT_S, context=self._ssl_ctx,
        ) as resp:
            raw = resp.read()
        try:
            data = (json.loads(raw) or {}).get("data") or []
            return data[0].get("id", "") if data else ""
        except Exception:
            return ""

    async def aclose(self) -> None:
        """Drain pending items (bounded by `_DRAIN_TIMEOUT_S`), then cancel the
        worker. Items still queued at timeout are dropped with a WARNING."""
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=self._DRAIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "[%s] conv_ mirror drain timed out; %d item(s) dropped",
                self.client_id, self._queue.qsize(),
            )
        self._worker.cancel()
        try:
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass
        self._worker = None


# ── Per-call state ─────────────────────────────────────────────────────────────

@dataclass
class BotConversation:
    """Per-call state for one Infinity-side endpoint bridged to one OpenAI Realtime session.

    Allocated in `_handle_bot_start` and stored in
    `OpenAIService._conversations` under `(session_id, endpoint_id)`
    for the lifetime of the call. Removed by `_handle_bot_end`
    (Infinity-driven teardown) or `on_session_ended` (caller
    disconnect / platform-driven session end).
    `_shutdown_conversation` is the single resource-release path;
    every removal site calls it.

    Field groups:

        * **Identity (set at construction, never mutated):**
          `session_id`, `endpoint_id`, `source` (rx / tx),
          `websocket` (the Infinity-side WS), `client_id`, `service`,
          `model` (resolved from botId suffix or `OPENAI_MODEL` env),
          `instructions` (composed by `_compose_instructions`),
          `language_code`, `codec_name`, `sample_rate`,
          `transport_encoding`.

        * **Upstream WebSocket:** `oa_ws` (the
          `websockets.WebSocketClientProtocol` connected to OpenAI
          Realtime), `oa_recv_task` (the background coroutine
          draining `oa_ws`), `active` (cooperative shutdown flag
          consulted at the head of each recv-loop iteration).

        * **Termination tracking:** `bot_ended_sent` is flipped True
          by every code path that emits a terminal `bot.ended`
          (`send_bot_ended_with_*_context` helpers and the manual
          builds in `_handle_bot_end` / `_emit_pending_handoff`).
          Read by `on_session_ended` and the helpers themselves to
          suppress duplicate emissions when an outcome has already
          been signalled.

        * **Audio codec state:** `ratecv_in_state` and
          `ratecv_out_state` thread `audioop.ratecv` calls per
          direction (the stdlib resampler returns a fresh state per
          call and the two directions cannot share). `g722_decoder`
          and `g722_encoder` are lazily initialised when G722 is
          negotiated. The PCMU codec path bypasses these entirely —
          µ-law on both ends means base64-wrap and base64-unwrap
          are the only operations needed.

        * **Ingress readiness latch:** `ingress_ready` and
          `ingress_buffer` solve the timing skew where OpenAI starts
          streaming the greeting before Infinity's ingress path
          opens (Infinity opens its ingress only after emitting its
          first egress frame). Audio that lands before the latch
          flips is buffered (capped at `_INGRESS_BUFFER_MAX_CHUNKS`);
          the latch flips on the first call to `ingest_audio_chunk`
          and flushes the buffer.

        * **Barge-in state:** `audio_playing_out` is True whenever
          there is bot audio queued at the IngressStreamer that has
          not yet been fully paced out to Infinity. Set True when a
          chunk is queued; cleared by the IngressStreamer's
          playout-done callback (registered in `_handle_bot_start`)
          when the queue drains, or explicitly by `_handle_barge_in`.
          This flag — not `active_response_ids` — is the gate for
          VAD-triggered barge-in: OpenAI generates faster than
          playout, so `response.done` can land while audio is still
          paced out for several seconds afterwards. The list of
          active responses tracks generation, the flag tracks
          playout, and barge-in needs to react to playout.

        * **Response lifecycle tracking:** `active_response_ids`
          (list — handles overlapping-response edge case) carries
          response IDs added on `response.created` and removed on
          `response.done`. Used to issue targeted `response.cancel`
          on barge-in. `completed_response_ids` (set) tracks
          response IDs whose server-side audio generation has
          finished (signalled by `response.output_audio.done`). Barge-in
          uses this set to skip `response.cancel` sends that would
          always race-lose against OpenAI's already-completed
          state.

        * **Output audio accumulator:** `ingress_accumulator` and
          `ingress_chunk_size`. OpenAI emits variable-size
          `response.output_audio.delta` chunks; the IngressStreamer paces
          at `chunk_duration_ms` per chunk regardless of frame
          content duration. The accumulator buffers until a full
          pacer-aligned flush is available; misalignment between
          this boundary and the streamer's pacing interval would
          split each flush into mismatched chunks paced uniformly,
          producing sub-real-time delivery and buffer underruns at
          Infinity. Coupled to
          `IngressStreamer.chunk_duration_ms` via `_chunk_size_for`.

        * **Transcript accumulators:** `transcript_deltas` keys per
          `response_id` for accumulating
          `response.output_audio_transcript.delta` events; moved to
          `pending_bot_text` on `response.output_audio_transcript.done`.
          `pending_customer_text` accumulates per-turn caller
          transcript fragments. BL-003: Infinity timestamps TRANSCRIPT
          frames by arrival (`createdAt`) and ignores `startTsMs`, so
          CUSTOMER must hit the wire before BOT. BOT is stashed until
          CUSTOMER for the turn is flushed, or until `response.done`
          on greeting / BOT-only turns.

        * **Turn-start timestamps:** `customer_turn_started_at` is
          captured on `input_audio_buffer.speech_started` (server
          VAD detected speech onset). `bot_turn_started_at` is
          captured on `response.created` (the moment OpenAI
          accepted the response request). Both are used as
          `payload.transcript.startTsMs` on the eventual
          TRANSCRIPT emit and reset to None after the corresponding
          flush. Capturing turn-start at the actual onset rather
          than at flush time preserves correct chronological
          ordering on the wire even when the bot's transcript
          completion event lands ahead of the caller's
          transcription pipeline.

        * **Deferred live-agent handoff:** `pending_handoff` stashes
          the `bot.feature LIVE_AGENT_HANDOFF` payload while the
          IngressStreamer queue drains; `pending_handoff_args`
          holds the raw tool argument values (read by
          `_emit_pending_handoff` to log the post-emit reason
          string); `handoff_task` is the drain coroutine running
          `_wait_for_quiescence_and_emit`. The deferred path
          exists because the `transfer_to_agent` `function_call`
          arrives before the acknowledgment audio finishes
          streaming; emitting LIVE_AGENT_HANDOFF immediately would
          have Infinity tear down playback mid-utterance.

        * **Deferred self-service-complete:** `pending_session_end`
          is a boolean latch (no stashed payload — the
          success-context `bot.ended` carries no per-call payload,
          only the static success status). Set True on the
          `end_session` tool invocation; `session_end_task` runs
          `_wait_for_quiescence_and_emit_session_end` until the
          audio queue drains, then `_emit_session_end_complete`
          fires the success-context emit.
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

    # Upstream WebSocket to OpenAI Realtime.
    oa_ws: Optional[Any] = None
    oa_recv_task: Optional[asyncio.Task] = None
    active: bool = False
    # Set True by send_bot_ended_with_*_context helpers after a successful
    # bot.ended emission (success / failure / disconnect). Read by
    # on_session_ended and the helpers themselves to suppress duplicate
    # disconnect emissions when self-service-complete, handoff, or failure
    # already signalled the outcome.
    bot_ended_sent: bool = False

    # Resampling state — audioop.ratecv returns a new tuple each call;
    # never share between in and out (rates differ on non-PCMU codecs).
    ratecv_in_state: Any = None   # Infinity → OpenAI
    ratecv_out_state: Any = None  # OpenAI → Infinity

    # G722 codec objects (lazy-initialised; 16 kHz internal rate).
    g722_decoder: Any = None
    g722_encoder: Any = None

    # Ingress readiness latch — see class docstring "Ingress readiness latch"
    # group. Flipped True on the first ingest_audio_chunk; ingress_buffer
    # holds OpenAI audio chunks that landed before Infinity's ingress
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

    # OpenAI Conversation mirror — set in _handle_bot_start when the workflow
    # passes openAIConversationId in session metadata; None disables mirroring.
    # Closed (drain-then-cancel) by _shutdown_conversation. See ConvMirror.
    conv_mirror: Optional["ConvMirror"] = None


# ── Service plugin ─────────────────────────────────────────────────────────────

class OpenAIService(ServicePlugin):
    """Service plugin that proxies an Infinity RCMS bot session to an OpenAI Realtime API session.

    Plugin contract:
        Subclass of `ServicePlugin`. Discovered by the plugin loader
        at bridge startup if `is_configured()` returns True
        (`OPENAI_API_KEY` set). Registered against the bridge's
        `ServiceRegistry` under the name `openai`. The bot
        dispatcher (`bot_service.py`) claims the RCMS `bot.start` /
        `bot.end` message types and routes per-call to this plugin
        based on the `openai:<model>` botId prefix; the plugin
        itself reports an empty `message_types` set.

    Raw-model framing:
        OpenAI Realtime exposes the model directly through a
        stateful WebSocket. There is no hosted agent surface (no
        dashboard prompt, no platform-side tools, no first-message
        field). The bridge owns every piece of orchestration:

            * **System prompt** — `_load_base_prompt` resolves a
              four-tier chain (`OPENAI_SYSTEM_PROMPT_FILE` env →
              `OPENAI_SYSTEM_PROMPT` env → sibling
              `system_prompt.md` → `_DEFAULT_INSTRUCTIONS`
              constant) for the base persona and tool-protocol
              template; `_compose_instructions` interpolates CRM
              context fields from `bot.start.payload.context`
              and the language code into that base via Python
              `.format()`.
            * **Tool schemas** — `_TRANSFER_TO_AGENT_TOOL` and
              `_END_SESSION_TOOL` declared inline. Both are
              bridge-implemented; OpenAI Realtime has no
              platform-side tools, and there is no equivalent of
              a dashboard tool registry.
            * **Conversation initiation** — `_on_session_updated`
              fires on the `session.updated` ack and sends a
              proactive `response.create` so the agent speaks
              first. Without this trigger, the agent stays silent
              until the caller speaks.
            * **Output pacing** — OpenAI emits variable-size
              `response.output_audio.delta` chunks; the bridge buffers
              them into pacer-aligned chunks via
              `_enqueue_output_audio` so the IngressStreamer's
              `chunk_duration_ms` cadence is honored.
            * **Termination drains** — both `transfer_to_agent`
              and `end_session` have their own bridge-side drain
              pipelines (`_wait_for_quiescence_and_emit` and
              `_wait_for_quiescence_and_emit_session_end`) that
              wait for the IngressStreamer queue to empty before
              emitting the terminal RCMS envelope, so the
              acknowledgment audio plays out before Infinity
              tears down playback.
            * **Pre-tool acknowledgment** — the system prompt
              instructs the model to speak a complete
              acknowledgment utterance as a single complete unit
              and only then invoke the terminal tool. The
              bridge-side post-tool `response.create` suppression
              in `_handle_function_call` is the second layer of
              the protocol — it removes the trigger that prompts
              the model to generate a duplicate acknowledgment
              after the tool fires.

        See the module docstring "Role" section for the full
        ownership boundary.

    Per-call state:
        `self._conversations` maps `(session_id, endpoint_id)` to
        a `BotConversation` instance for the lifetime of each
        call. Populated by `_handle_bot_start`, removed by
        `_handle_bot_end` and `on_session_ended`. The instance
        carries the upstream OpenAI WebSocket, the recv-loop task,
        codec state, the response-lifecycle tracking (active and
        completed response IDs), the deferred-handoff and
        deferred-session-end latches, the ingress-readiness latch,
        the output accumulator, the transcript accumulators, the
        turn-start timestamps, and the `audio_playing_out` barge-
        in gate.

    Lifecycle hooks:
        * `handle_message` — `bot.start` / `bot.end` dispatch.
        * `ingest_audio_chunk` — per-frame caller audio handoff
          to OpenAI Realtime.
        * `on_session_ended` — caller-disconnect /
          platform-initiated session end.
        * `shutdown` — bridge process shutdown.

    Spec:
        RCMS spec §AI Bot Message Definitions — `bot.start` /
        `bot.started` / `bot.end` / `bot.ended` / `bot.feature`
        (TRANSCRIPT, LIVE_AGENT_HANDOFF ftypes).
        OpenAI Realtime API protocol — `session.update` /
        `session.created` / `session.updated` /
        `input_audio_buffer.*` / `response.*` /
        `conversation.item.*` / `error`. Bearer token auth via
        the `Authorization` header on the WebSocket handshake.
    """

    name = "openai"

    @classmethod
    def is_configured(cls) -> bool:
        """Return True iff `OPENAI_API_KEY` is set in the bridge process environment.

        Consulted by the plugin loader at bridge startup. When
        False, the plugin is skipped — `bot.start` envelopes
        carrying an `openai:` `botId` will then surface as
        `BACKEND_START_FAILED` from the dispatcher because no
        plugin claims the prefix.

        Per-call API keys can also arrive on
        `payload.botCredentials` (see `_extract_api_key`); the
        env-var check here is the startup-time gate, not the
        only source of credentials.
        """
        return bool(os.environ.get("OPENAI_API_KEY", "").strip())

    def __init__(self, server: "BridgeServer"):
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
        transcoders to drive resampling between OpenAI's 8 kHz
        µ-law and the Infinity-side codec.
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
            `botId` prefix (`openai:`) against the registered
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
            logger.warning("[%s] Unhandled message '%s' in OpenAI service", client_id, msg_type)

    async def on_session_ended(self, session_id: str) -> None:
        """Phase 3 hook — emit `bot.ended` (CALLER_DISCONNECTED) for any active OpenAI conversation under this session.

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
                   recv loop, OpenAI WS).

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
        """Tear down every active OpenAI conversation — called on bridge process shutdown.

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
            queues drained, OpenAI recv loops cancelled, OpenAI
            WebSockets closed.
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
        """Handle Infinity-driven `bot.start` for an OpenAI Realtime session: validate, compose instructions, connect, ack-deferred.

        Contract:
            Phase 1 entry point dispatched by `handle_message` when
            the inbound message type is `bot.start` and the
            dispatcher has routed by `botId` prefix. Performs the
            full start-of-call sequence:

                1. **Validate `endpointId`**: missing endpointId
                   fails the `bot.ended` schema, so respond with
                   `session.error` (`MISSING_REQUIRED_FIELDS`,
                   501) instead. The only `session.error` exit on
                   this path; all subsequent failures use
                   `bot.ended`-with-status.
                2. **Validate `botId` prefix**: must start with
                   `openai:` (case-insensitive). Failures emit
                   `bot.ended` with `BAD_REQUEST` (400) /
                   `UNRECOGNIZED_BOTID_PREFIX` via the
                   failure-context helper.
                3. **Resolve model id**: prefer the `OPENAI_MODEL`
                   environment variable (deployment override);
                   otherwise read the suffix after `openai:` from
                   the botId; fall back to `DEFAULT_OPENAI_MODEL`.
                4. **Resolve API key**: prefer
                   `OPENAI_API_KEY` from the environment; fall
                   back to per-call credentials decoded from
                   `payload.botCredentials` (base64-wrapped JSON
                   `{"apiKey": "..."}`) via `_extract_api_key`.
                   Missing key → 503 `BACKEND_START_FAILED`.
                5. **Resolve codec / sample rate**: from
                   `session_config[session_id]` and the inbound
                   payload. Reject unsupported codecs (anything
                   outside PCMU / L16 / PCMA / G722) with 503
                   `BACKEND_START_FAILED`. Reject G722 negotiation
                   when the optional G722 package is not
                   installed. Reject non-PCMU codecs when audioop
                   is unavailable — the bridge can run an OpenAI
                   session zero-transcode on PCMU but every other
                   codec needs audioop for the µ-law conversion.
                6. **Compose instructions**: `_compose_instructions`
                   interpolates `{{variable}}` placeholders against
                   `payload.context` plus the language code and
                   call-context fields. The composed prompt is
                   per-call and stored on the `BotConversation`.
                7. **Build `BotConversation`** from negotiated
                   codec, transport encoding from
                   `server.transport_encodings`, and the composed
                   instructions.
                8. **Register playout-done callback**: bind a
                   closure that clears `convo.audio_playing_out`
                   to the IngressStreamer for this
                   `(session_id, endpoint_id)`. The streamer
                   invokes the callback when the egress queue
                   drains naturally (idle timeout or `is_last`
                   chunk sent) so VAD-triggered barge-in can gate
                   on actual playout state.
                9. **G722 decoder lazy-init** when negotiated.
                   Decoder init failure → 503
                   `BACKEND_START_FAILED`.
               10. **Connect to OpenAI** via `_connect_openai`.
                   The helper sends the `session.update` frame
                   including instructions, voice, audio formats,
                   server VAD, transcription model, and tool
                   registration. Failure here → 503
                   `BACKEND_START_FAILED`.
               11. **Replace any pre-existing conversation** under
                   the same `(session_id, endpoint_id)` by
                   calling `_shutdown_conversation` on the prior
                   entry — protects against duplicate `bot.start`
                   racing the prior session's teardown.
               12. **Activate**: set `convo.active = True`, store
                   in `self._conversations`, and start
                   `_openai_recv_loop` as a background task.

            **Note on `bot.started` deferral.** Unlike a
            single-handshake provider, OpenAI Realtime requires a
            `session.update` round-trip before the session is
            actually configured (server VAD, transcription model,
            tools, voice). `bot.started` is therefore not sent
            here — it is sent from `_on_session_updated` after the
            `session.updated` ack arrives. Sending `bot.started`
            here would tell Infinity the bot is ready before
            OpenAI has actually accepted the configuration; the
            proactive greeting that follows would land on a
            half-configured session.

            Every error path uses
            `send_bot_ended_with_failure_context` with the
            appropriate RCMS status code, so the workflow's
            `byobotEndContext` consumption pattern routes the bot
            session to a non-200 terminal state. Per the RCMS
            spec, an unprocessable `bot.start` is failed via
            `bot.ended`-with-status, not `session.error`; the
            only `session.error` exit is the schema-impossible
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
                `payload.source`, `payload.botCredentials`,
                `payload.language`, `payload.context`,
                `payload.to`, `payload.from`, `payload.ucid`,
                `payload.direction`, and `payload.sampleRate` are
                read.

        Returns:
            None. Side effects: optional `session.error` /
            `bot.ended`-with-failure send on validation failure;
            `BotConversation` registered in `self._conversations`;
            playout-done callback registered with the
            IngressStreamer; background OpenAI recv loop
            launched. `bot.started` is sent later by
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

        if not bot_id.lower().startswith("openai:"):
            # RCMS §Error Handling: an unprocessable bot.start is failed via
            # bot.ended-with-status, not session.end. send_bot_ended_with_failure_context
            # emits the spec-compliant shape; the IVA module's FAILED branch wires
            # to bot.ended-with-non-200-status.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=400,
                reason="BAD_REQUEST",
                description=f"UNRECOGNIZED_BOTID_PREFIX: OpenAI plugin requires botId prefix 'openai:', got '{bot_id}'",
            )
            return

        # Model resolution: env var overrides botId suffix; suffix overrides
        # the built-in default.
        env_model = os.environ.get("OPENAI_MODEL", "").strip()
        model = env_model or bot_id[len("openai:"):].strip() or DEFAULT_OPENAI_MODEL

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            # Also accept per-call credentials (base64 JSON {"apiKey": "..."})
            api_key = self._extract_api_key(payload) or ""
        if not api_key:
            # RCMS §Error Handling — see canonical comment at line 476 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: No OpenAI API key (OPENAI_API_KEY not set and botCredentials missing)",
            )
            return

        codec_name = self._resolve_codec(session_id).upper()
        sample_rate = self._resolve_sample_rate(session_id, payload)

        if codec_name not in ("PCMU", "L16", "PCMA", "G722"):
            # RCMS §Error Handling — see canonical comment at line 476 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: Unsupported codec '{codec_name}' for OpenAI provider",
            )
            return

        if codec_name == "G722" and not G722_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 476 above.
            await self.server.send_bot_ended_with_failure_context(
                websocket, client_id, session_id, endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description="BACKEND_START_FAILED: Codec G722 negotiated but g722 package is not installed",
            )
            return

        if codec_name != "PCMU" and not _AUDIOOP_AVAILABLE:
            # RCMS §Error Handling — see canonical comment at line 476 above.
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
            "[%s] OpenAI bot.start session=%s endpoint=%s model=%s codec=%s/%d source=%s",
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
                # RCMS §Error Handling — see canonical comment at line 476 above.
                await self.server.send_bot_ended_with_failure_context(
                    convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                    code=503,
                    reason="SERVICE_UNAVAILABLE",
                    description=f"BACKEND_START_FAILED: G722 decoder init failed: {exc}",
                    convo=convo,
                )
                return

        try:
            await self._connect_openai(convo, api_key)
        except Exception as exc:
            logger.error("[%s] Failed to connect to OpenAI: %s", client_id, exc, exc_info=True)
            # RCMS §Error Handling — see canonical comment at line 476 above.
            await self.server.send_bot_ended_with_failure_context(
                convo.websocket, convo.client_id, convo.session_id, convo.endpoint_id,
                code=503,
                reason="SERVICE_UNAVAILABLE",
                description=f"BACKEND_START_FAILED: OpenAI connect failed: {exc}",
                convo=convo,
            )
            return

        key = self._key(session_id, endpoint_id)
        existing = self._conversations.pop(key, None)
        if existing:
            await self._shutdown_conversation(existing)
        convo.active = True
        self._conversations[key] = convo
        convo.oa_recv_task = asyncio.create_task(self._openai_recv_loop(convo))

        # OpenAI Conversation mirroring (Agent Assist cross-channel parity).
        # The workflow creates a conv_ via REST before the IVA module and
        # passes its id in session metadata; the Realtime API will not write
        # into it, so we mirror finalized turns ourselves (see ConvMirror).
        # Started only after a successful connect+register so a failed connect
        # never leaks a worker; torn down by _shutdown_conversation. Soft-fail:
        # a missing/empty conv id disables mirroring and the call runs normally.
        conv_id = (context.get("openAIConversationId") or "").strip()
        if conv_id:
            convo.conv_mirror = ConvMirror(conv_id, api_key, client_id)
            convo.conv_mirror.start()
            # Seed the conv_ with CRM context as the FIRST item (single FIFO
            # worker guarantees ordering ahead of any transcript turn).
            seed = _format_crm_seed(context)
            if seed:
                convo.conv_mirror.enqueue("system", seed)
            logger.info(
                "[%s] OpenAI conversation mirroring enabled: %s", client_id, conv_id,
            )
        else:
            logger.warning(
                "[%s] OpenAI conversation mirroring disabled: "
                "no openAIConversationId in session metadata", client_id,
            )

        # bot.started is sent after session.updated arrives from OpenAI, in
        # _on_session_updated. Do NOT send it here — the session.update
        # round-trip must complete first so OpenAI is configured before we
        # signal Infinity that the bot is ready.

    def _extract_api_key(self, payload: Dict[str, Any]) -> Optional[str]:
        """Decode an OpenAI API key from the `bot.start` payload's `botCredentials` field.

        Contract:
            `payload.botCredentials` is a base64-encoded JSON
            object (per RCMS spec §AI Bot Message Definitions).
            Decode the base64, parse the JSON, and return
            `obj["apiKey"]` if it is a non-empty string.
            Whitespace is stripped from the returned value.

            Returns `None` (not raises) on every error path so
            the caller can fall back to the `OPENAI_API_KEY`
            environment variable. The bridge supports either
            source.

            The caller (`_handle_bot_start`) treats the disjoint
            "neither source produced a key" outcome as a 503
            `BACKEND_START_FAILED` and emits the failure-context
            `bot.ended`.

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.start`
            payload, `botCredentials` field as base64-wrapped
            JSON.

        Args:
            payload: The decoded `bot.start` payload object.

        Returns:
            The stripped API key string when present and
            well-formed, otherwise `None`.
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

                1. **`OPENAI_SYSTEM_PROMPT_FILE` env var** —
                   explicit file path (deploy override; e.g. the
                   VM's `/opt/bridge-server/openai_system_prompt.md`).
                   Read failure or empty contents falls through.
                2. **`OPENAI_SYSTEM_PROMPT` env var** — inline
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
            `_load_base_prompt` and `bot_xai.py`'s
            `_load_base_prompt`; the three stay in sync by
            convention.

        Returns:
            The resolved base instructions string. Always
            non-empty — the built-in fallback is never empty.
        """
        prompt_file = os.environ.get("OPENAI_SYSTEM_PROMPT_FILE", "").strip()
        if prompt_file:
            try:
                with open(prompt_file, "r", encoding="utf-8") as fh:
                    text = fh.read().strip()
                if text:
                    return text
                logger.warning("OPENAI_SYSTEM_PROMPT_FILE %s is empty; falling back", prompt_file)
            except OSError as exc:
                logger.warning("Could not read OPENAI_SYSTEM_PROMPT_FILE %s: %s", prompt_file, exc)

        inline = os.environ.get("OPENAI_SYSTEM_PROMPT", "").strip()
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
        """Compose the per-call session instructions by interpolating CRM context fields into the resolved base persona.

        Contract:
            Calls `_load_base_prompt` to resolve the base
            instructions template (4-tier env / sibling-file /
            constant chain), then renders it with Python
            `str.format()`, supplying the per-call values for
            `firstName`, `lastName`, `email`, `caseId`,
            `caseSubject`, `caseDescription`, `direction`,
            `from_num`, `to_num`, `ucid`, and `language`. The
            CRM fields come from `payload.context`; the call-
            metadata fields come from `payload` directly; the
            `language` value is mapped to a human-readable name
            via `_resolve_language_name`. Missing fields render
            as empty strings.

            Falls back to `_DEFAULT_INSTRUCTIONS` (the minimal
            persona) on any `format()` failure — for example, if
            the resolved base contains a placeholder that is not
            in the supplied values. The fallback path keeps the
            bot operational even when prompt-side customization
            breaks.

            Static method: takes no `self`. The composition is
            stateless beyond what `_load_base_prompt` resolves.

        Args:
            payload: The decoded `bot.start.payload`.
                `direction`, `from`, `to`, `ucid` are read.
            context: The decoded `payload.context` object.
                `firstName`, `lastName`, `email`, `caseId`,
                `caseSubject`, `caseDescription` are read.
                Non-dict values are coerced to an empty dict.
            language: BCP-47 / ISO 639-1 code from
                `payload.language`. Resolved to a human-readable
                name via `_resolve_language_name`.

        Returns:
            The rendered instructions string ready for
            `session.update.session.instructions`.
        """
        if not isinstance(context, dict):
            context = {}

        base = OpenAIService._load_base_prompt()
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
        """Handle Infinity-driven `bot.end` for an OpenAI conversation: tear down upstream and ack.

        Contract:
            Inverse of `_handle_bot_start`. Two outcomes:

                * **Missing `endpointId`**: the `bot.ended`
                  schema requires `endpointId`, so the failure
                  cannot be expressed via `bot.ended`; respond
                  with `session.error`
                  (`MISSING_REQUIRED_FIELDS`, status 501)
                  instead.
                * **Normal teardown**: pop the conversation
                  from `self._conversations` (warn if absent,
                  e.g. the caller already disconnected), call
                  `_shutdown_conversation` to cancel any
                  pending handoff or session-end drain, drain
                  and close the IngressStreamer, cancel the
                  OpenAI recv loop, and close the upstream WS.
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
            logger.warning("[%s] No active OpenAI convo for %s:%s", client_id, session_id, endpoint_id)

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
            `self._conversations`. Returns `False` immediately if
            the conversation is missing, inactive, sourced from
            the wrong direction (`convo.source != source`), or
            has no upstream OpenAI WebSocket — the bridge falls
            through to other registered services on `False`.

            **Ingress-ready latch.** The first call to this
            method on a conversation marks
            `convo.ingress_ready = True` and flushes anything
            that `_handle_openai_message` had buffered into
            `convo.ingress_buffer` while waiting for Infinity's
            ingress path to open. The buffer holds OpenAI's
            initial greeting audio, which lands before Infinity's
            first egress frame arrives; without the latch +
            buffer pair, those greeting chunks would be
            discarded.

            After the latch handling, the inbound bytes are
            converted to the OpenAI input format by `_prepare_input_audio`
            (a no-op pass-through when the negotiated codec is
            PCMU or PCMA). If transcoding fails (returns `None`), this
            method returns `False`. Otherwise the µ-law is
            base64-wrapped in an `input_audio_buffer.append`
            envelope and sent on `convo.oa_ws`. WS send failures
            log at ERROR and return `False`.

        Spec:
            OpenAI Realtime API protocol —
            `input_audio_buffer.append` envelope shape.

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
            sent to OpenAI. False on every reject / failure
            path.
        """
        convo = self._conversations.get(self._key(session_id, endpoint_id))
        if not convo or not convo.active or convo.source != source or not convo.oa_ws:
            return False

        # First egress frame from Infinity: ingress path is now open.
        # Flush any OpenAI audio buffered before bot.started was acknowledged.
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
            await convo.oa_ws.send(json.dumps(msg))
        except Exception as exc:
            logger.error("[%s] Send to OpenAI failed: %s", convo.client_id, exc)
            return False
        return True

    def _prepare_input_audio(
        self, convo: BotConversation, audio_bytes: bytes
    ) -> Optional[bytes]:
        """Convert an inbound Infinity-side audio frame to the OpenAI Realtime input format.

        Contract:
            Inbound (Infinity → OpenAI) counterpart of
            `_transcode_output_audio`. The OpenAI input format is
            codec-aware (`_openai_audio_format`): G.711 codecs pass
            through to their native OpenAI format; wideband / linear
            codecs become `audio/pcm` at 24 kHz. Branches on
            `convo.codec_name.upper()`:

                * **PCMU**: pass through unchanged → `audio/pcmu`
                  (µ-law, 8 kHz). Zero-transcode fast path.
                * **PCMA**: pass through unchanged → `audio/pcma`
                  (A-law, 8 kHz). Zero-transcode fast path.
                * **L16**: → `audio/pcm` @ 24 kHz. Pass through if
                  already 24 kHz; otherwise resample via
                  `audioop.ratecv` (threading
                  `convo.ratecv_in_state`).
                * **G722**: decode via `convo.g722_decoder`
                  (lazily initialised in `_handle_bot_start` when
                  G722 is negotiated) into 16 kHz S16LE, then
                  upsample to 24 kHz `audio/pcm` — no µ-law
                  companding, no narrowband downsample.

            The L16 and G722 paths require `_AUDIOOP_AVAILABLE` to
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
            Bytes in the negotiated OpenAI input format, ready for
            the OpenAI `input_audio_buffer.append` envelope, or `None`
            on transcoding failure or unsupported codec.
        """
        if not audio_bytes:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate

        try:
            if codec == "PCMU":
                # µ-law → audio/pcmu: no conversion. Zero-transcode fast path.
                return audio_bytes

            if codec == "PCMA":
                # A-law → audio/pcma: no conversion. Zero-transcode fast path.
                return audio_bytes

            if not _AUDIOOP_AVAILABLE:
                logger.error(
                    "[%s] audioop unavailable — cannot transcode %s input", convo.client_id, codec
                )
                return None

            if codec == "L16":
                # Linear PCM16 → audio/pcm @ 24 kHz. Pass through if already 24 kHz.
                if rate == OPENAI_PCM_RATE:
                    return audio_bytes
                resampled, convo.ratecv_in_state = audioop.ratecv(
                    audio_bytes, 2, 1, rate, OPENAI_PCM_RATE, convo.ratecv_in_state
                )
                return resampled

            if codec == "G722":
                if not G722_AVAILABLE or not convo.g722_decoder:
                    logger.error("[%s] G722 decoder not available", convo.client_id)
                    return None
                # G.722 → 16 kHz PCM16 → upsample to 24 kHz (audio/pcm). No µ-law
                # companding, no narrowband downsample — preserves decoded content.
                pcm_16k = convo.g722_decoder.decode(audio_bytes).tobytes()
                pcm_24k, convo.ratecv_in_state = audioop.ratecv(
                    pcm_16k, 2, 1, 16000, OPENAI_PCM_RATE, convo.ratecv_in_state
                )
                return pcm_24k

        except Exception as exc:
            logger.error(
                "[%s] Input audio prep failed (codec=%s rate=%d): %s",
                convo.client_id, codec, rate, exc,
            )
            return None

        logger.warning("[%s] Unsupported input codec '%s'", convo.client_id, codec)
        return None

    def _transcode_output_audio(
        self, convo: BotConversation, oa_audio: bytes
    ) -> Optional[bytes]:
        """Convert OpenAI Realtime output audio into the Infinity-side codec for this call.

        Contract:
            Outbound (OpenAI → Infinity) counterpart of
            `_prepare_input_audio`. OpenAI emits audio in the
            codec-aware format declared at session.start
            (`_openai_audio_format`): `audio/pcmu` / `audio/pcma`
            for those codecs, else `audio/pcm` at 24 kHz. The
            Infinity-side codec / sample rate are whatever was
            negotiated at session.start.

            Branches on `convo.codec_name.upper()`:

                * **PCMU**: pass through unchanged (`audio/pcmu`
                  → µ-law 8 kHz). Zero-transcode fast path.
                * **PCMA**: pass through unchanged (`audio/pcma`
                  → A-law 8 kHz). Zero-transcode fast path.
                * **L16**: `audio/pcm` @ 24 kHz → PCM16 at the
                  negotiated rate; pass through if rate is
                  24 kHz, else resample via `audioop.ratecv`
                  (threading `convo.ratecv_out_state`).
                * **G722**: `audio/pcm` @ 24 kHz → downsample to
                  16 kHz (G722's internal rate), lazy-init
                  `convo.g722_encoder` on first call, then encode
                  via the G722 module's `encode` on a numpy int16
                  view.

            The L16 and G722 paths require `_AUDIOOP_AVAILABLE` to
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
            oa_audio: Bytes decoded from OpenAI's base64-encoded
                `response.output_audio.delta` payload, in the
                negotiated OpenAI output format (µ-law / A-law at
                8 kHz, or PCM16 at 24 kHz). Empty buffers return `None`.

        Returns:
            Bytes ready for the Infinity wire (µ-law, PCM at
            negotiated rate, A-law, or G.722 encoded), or
            `None` on transcoding failure or unsupported codec.
        """
        if not oa_audio:
            return None
        codec = convo.codec_name.upper()
        rate = convo.sample_rate

        try:
            if codec == "PCMU":
                # audio/pcmu → µ-law: no conversion.
                return oa_audio

            if codec == "PCMA":
                # audio/pcma → A-law: no conversion.
                return oa_audio

            if not _AUDIOOP_AVAILABLE:
                logger.error(
                    "[%s] audioop unavailable — cannot transcode %s output", convo.client_id, codec
                )
                return None

            if codec == "L16":
                # audio/pcm @ 24 kHz → PCM16 at the negotiated rate.
                if rate == OPENAI_PCM_RATE:
                    return oa_audio
                resampled, convo.ratecv_out_state = audioop.ratecv(
                    oa_audio, 2, 1, OPENAI_PCM_RATE, rate, convo.ratecv_out_state
                )
                return resampled

            if codec == "G722":
                if not G722_AVAILABLE:
                    return None
                # audio/pcm @ 24 kHz → downsample to 16 kHz → G.722 encode.
                pcm_16k, convo.ratecv_out_state = audioop.ratecv(
                    oa_audio, 2, 1, OPENAI_PCM_RATE, 16000, convo.ratecv_out_state
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
            flush into mismatched chunks paced uniformly,
            producing sub-real-time delivery and buffer
            underruns at Infinity.

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
        """Accumulate OpenAI's variable-size audio chunks into pacer-aligned chunks and flush each full chunk.

        Contract:
            OpenAI Realtime emits `response.output_audio.delta` chunks
            of variable size; the bridge's IngressStreamer paces
            at `chunk_duration_ms` per chunk regardless of the
            duration of content fed to it. This method buffers
            the variable-size deltas in
            `convo.ingress_accumulator` until a full pacer-
            aligned chunk is available, then flushes one chunk
            at a time via `_send_ingress_chunked`. The leftover
            stays in the accumulator for the next call, so
            pacing tracks actual audio content rather than
            fragmenting per inbound delta.

            The accumulator's chunk size
            (`convo.ingress_chunk_size`) is computed lazily on
            first use via `_chunk_size_for`, which reads
            `IngressStreamer.chunk_duration_ms`. Using any other
            value desynchronizes the flush boundary from the
            streamer's pacing interval — the resulting
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
            Called from `_handle_openai_message`'s `response.done`
            and `response.output_audio.done` branches so the tail of a
            response (whatever doesn't make a full pacer-aligned
            chunk) plays out instead of being stranded in the
            accumulator until the next response. The tail is sent
            as a single sub-pacer-sized chunk; the
            IngressStreamer accepts it and paces it like any
            other chunk.

            Empty accumulator returns silently.

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
            accumulator that does this.

            Sets `convo.audio_playing_out = True` after a
            successful queue_audio call. The flag stays True
            until either the streamer signals playout-done via
            the registered callback (`_on_playout_done`) on
            natural drain, or `_handle_barge_in` clears it
            explicitly. Together with `audio_playing_out`, the
            VAD-triggered barge-in dispatch in
            `_handle_openai_message` correctly gates on actual
            playout state rather than on OpenAI's generation
            state.

            Errors from `queue_audio` are logged at WARNING and
            swallowed — a per-chunk send failure should not
            tear the conversation down. The flag is *not* set
            on the error path, since no audio is in flight.

        Args:
            convo: The active conversation. Provides the
                bridge-side websocket, identifiers, and
                transport encoding. Mutated:
                `audio_playing_out` set True on success.
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
            # Bot audio is now in flight to Infinity. Stays True until
            # natural drain (playout-done callback) or barge-in.
            convo.audio_playing_out = True
        except Exception as exc:
            logger.warning("[%s] queue_audio failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- OpenAI WS

    async def _connect_openai(self, convo: BotConversation, api_key: str) -> None:
        """Open the upstream OpenAI Realtime WebSocket and send the `session.update` configuration frame.

        Contract:
            Two-step connect:

                1. Open
                   `wss://api.openai.com/v1/realtime?model=<model>`
                   with the `Authorization: Bearer <api_key>`
                   header. `max_size=_WS_MAX_FRAME_BYTES` (8 MiB)
                   accommodates the largest audio frames OpenAI
                   emits.
                2. Immediately send a single `session.update`
                   frame carrying:
                   - **`instructions`** — the per-call composed
                     prompt produced by `_compose_instructions`
                     (CRM-context-interpolated persona).
                   - **`voice`** — read from the `OPENAI_VOICE`
                     environment variable; defaults to
                     `DEFAULT_VOICE` (`"cedar"`).
                   - **`audio.input.format`** / **`audio.output.format`**
                     — set symmetrically by `_openai_audio_format`
                     from the negotiated Infinity codec: `audio/pcmu`
                     (PCMU) / `audio/pcma` (PCMA) for the zero-transcode
                     path, else `audio/pcm` @ 24000 (G722 / L16).
                   - **`turn_detection`** — `{"type": "server_vad"}`
                     so OpenAI handles speech-start / speech-stop
                     detection server-side and emits
                     `input_audio_buffer.speech_started` /
                     `speech_stopped` / `committed` events.
                   - **`input_audio_transcription`** —
                     `{"model": "whisper-1"}`. Required to
                     receive
                     `conversation.item.input_audio_transcription.completed`
                     events for caller-side transcripts.
                   - **`tools`** — registers
                     `_TRANSFER_TO_AGENT_TOOL` and
                     `_END_SESSION_TOOL`. Both are inline
                     definitions in this module; OpenAI Realtime
                     has no platform-side tool registry.
                   - **`tool_choice`** — `"auto"`. Lets the
                     model decide whether and when to invoke a
                     tool based on conversational context. The
                     system prompt's Transfer Protocol and End
                     Call Protocol sections direct *when* each
                     tool should fire.

            Stores the connected websocket on `convo.oa_ws`. On
            any failure during connect or session.update send,
            the exception propagates to `_handle_bot_start`,
            which translates it into `bot.ended` with
            `BACKEND_START_FAILED` via the failure-context
            helper.

            **TLS context selection.** When the optional
            `truststore` package is available, an outbound TLS
            context backed by the OS trust store is built per
            call so corporate TLS-interception roots (e.g.
            Zscaler) are honored. Scoping to this call site
            avoids globally replacing `ssl.SSLContext`, which
            would break the server-side WSS context the bridge
            accepts inbound connections on. When `truststore` is
            not installed, `_ssl.create_default_context()` falls
            back to the Python CA bundle.

            **Note on `bot.started` deferral.** This method does
            not emit `bot.started` to Infinity. The
            `session.update` round-trip must complete first;
            `bot.started` is sent later from
            `_on_session_updated` after the `session.updated`
            ack arrives.

        Spec:
            OpenAI Realtime API protocol — `session.update`
            envelope shape, Bearer-token auth via
            `Authorization` header, and the URL-query model
            selection.

        Args:
            convo: The conversation receiving the connection.
                Mutated: `oa_ws` is set on success.
            api_key: OpenAI API key, sourced from
                `OPENAI_API_KEY` in the bridge environment or
                from per-call `payload.botCredentials`.

        Returns:
            None. Raises any websockets / TLS / send error to
            the caller; no bridge-side bot.ended is emitted from
            here.
        """
        url = f"{OPENAI_REALTIME_BASE_URL}?model={convo.model}"
        if _truststore is not None:
            ssl_context = _truststore.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        else:
            ssl_context = _ssl.create_default_context()

        voice = os.environ.get("OPENAI_VOICE", DEFAULT_VOICE).strip()

        convo.oa_ws = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {api_key}",
            },
            max_size=_WS_MAX_FRAME_BYTES,
            ssl=ssl_context,
        )
        logger.info(
            "[%s] Connected to OpenAI Realtime model=%s voice=%s",
            convo.client_id, convo.model, voice,
        )

        # Configure the session. session.updated ack triggers bot.started.
        # Session payload follows the OpenAI Realtime GA spec (the Beta
        # interface was removed 2026-05-12):
        #   - `session.type: "realtime"` is the GA discriminator.
        #   - Audio config is consolidated under `session.audio.input` /
        #     `session.audio.output`. The Beta-era flat fields
        #     `input_audio_format`, `output_audio_format`, `turn_detection`,
        #     and `input_audio_transcription` are no longer recognized.
        #   - `tools` and `tool_choice` remain top-level fields of `session`
        #     per the GA spec (verified against the openai-agents-js SDK).
        oa_format = _openai_audio_format(convo.codec_name)
        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": convo.instructions,
                "audio": {
                    "input": {
                        "format": oa_format,
                        "turn_detection": {"type": "server_vad"},
                        "transcription": {"model": "whisper-1"},
                    },
                    "output": {
                        "format": oa_format,
                        "voice": voice,
                    },
                },
                "tools": [_TRANSFER_TO_AGENT_TOOL, _END_SESSION_TOOL],
                "tool_choice": "auto",
            },
        }
        await convo.oa_ws.send(json.dumps(session_update))
        logger.info(
            "[%s] Sent session.update (instructions_len=%d)",
            convo.client_id, len(convo.instructions),
        )

    async def _openai_recv_loop(self, convo: BotConversation) -> None:
        """Drain the OpenAI Realtime WebSocket and dispatch each event to `_handle_openai_message`.

        Contract:
            Long-lived coroutine started from `_handle_bot_start`
            and cancelled from `_shutdown_conversation`. Iterates
            `convo.oa_ws`, JSON-decodes each frame, and hands the
            parsed event to `_handle_openai_message`. Non-JSON
            frames are logged at WARNING and skipped — never
            raised. The loop ends on one of:

                * `convo.active = False` observed at the
                  iteration head (cooperative exit, set by
                  `_shutdown_conversation`).
                * `asyncio.CancelledError` — re-raised so the
                  cancelling coroutine sees it.
                * `ConnectionClosed` — log at INFO and exit; no
                  bridge-side `bot.ended` emission from this
                  branch (the relevant termination shapes are
                  handled elsewhere by `_emit_pending_handoff`,
                  `_emit_session_end_complete`,
                  `on_session_ended`, and the in-flight tool
                  invocation paths).
                * Any other exception — log at ERROR with full
                  traceback and exit; `convo.active` is flipped
                  False in the `finally` block so any in-flight
                  ingest sees the dead conversation and stops
                  sending.

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
            async for raw in convo.oa_ws:
                if not convo.active:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    logger.warning("[%s] Non-JSON frame from OpenAI", convo.client_id)
                    continue
                await self._handle_openai_message(convo, msg)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed as exc:
            logger.info("[%s] OpenAI WS closed: %s", convo.client_id, exc)
        except Exception as exc:
            logger.error("[%s] OpenAI recv loop error: %s", convo.client_id, exc, exc_info=True)
        finally:
            convo.active = False

    async def _handle_openai_message(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a single decoded event from the OpenAI Realtime WebSocket to its handler branch.

        Contract:
            Single switch on `msg["type"]`. OpenAI Realtime emits a
            high-volume event stream (~15+ types per conversation
            turn); only state-machine-relevant events act, most are
            logged at DEBUG and discarded. Branches handled, in
            declaration order:

                * **`session.created`** — informational session
                  bootstrap notice. Logged at DEBUG; no further
                  action. The configuration-accepted signal is
                  `session.updated` below.
                * **`session.updated`** — OpenAI has accepted the
                  `session.update` configuration sent in
                  `_connect_openai`. Triggers `_on_session_updated`,
                  which sends the proactive greeting via
                  `response.create` and emits `bot.started` to
                  Infinity.
                * **`input_audio_buffer.speech_started`** — server
                  VAD detected speech onset. Two effects:
                  (a) capture `customer_turn_started_at` for use
                  as `startTsMs` on the eventual CUSTOMER
                  TRANSCRIPT emit; (b) when `audio_playing_out` is
                  True, dispatch `_handle_barge_in` to clear the
                  IngressStreamer queue and cancel any active
                  responses. The barge-in gate is
                  `audio_playing_out`, not `active_response_ids`,
                  because OpenAI generates faster than playout —
                  `response.done` can clear the active list while
                  audio is still paced out for several seconds
                  afterwards.
                * **`input_audio_buffer.speech_stopped`** /
                  **`input_audio_buffer.committed`** —
                  informational VAD events; logged at DEBUG.
                * **`response.created`** — OpenAI accepted the
                  response request. Two effects:
                  (a) append the response_id to
                  `active_response_ids` so a subsequent barge-in
                  can issue targeted `response.cancel`; (b)
                  capture `bot_turn_started_at` for use as
                  `startTsMs` on the eventual BOT TRANSCRIPT
                  emit.
                * **`response.done`** — response generation is
                  fully complete. Remove the response_id from
                  `active_response_ids` and flush any partial
                  audio chunk left in `ingress_accumulator`.
                * **`response.output_audio.done`** — server-side audio
                  generation has finished for this response, even
                  though paced playout to Infinity may still be
                  running. Two effects:
                  (a) add the response_id to
                  `completed_response_ids` so a subsequent
                  barge-in skips `response.cancel` for this
                  response (the cancel would always race-lose
                  against OpenAI's already-finished state);
                  (b) flush any audio remainder still in the
                  accumulator and call
                  `IngressStreamer.mark_audio_segment_complete`
                  so the streamer sends `lastf=true` to Infinity
                  and fires the playout-done callback that
                  clears `audio_playing_out` via the
                  natural-drain path.
                * Other response-lifecycle events
                  (`response.output_item.added`,
                  `response.content_part.added`, `.done`
                  variants) — logged at DEBUG; no state-machine
                  action.
                * **`response.output_audio.delta`** — base64-encoded
                  µ-law audio chunk. Decoded, transcoded via
                  `_transcode_output_audio` (no-op when codec is
                  PCMU), and either buffered on
                  `convo.ingress_buffer` (while
                  `convo.ingress_ready` is False) or fed through
                  `_enqueue_output_audio` for pacer-aligned
                  flush. Buffer is bounded by
                  `_INGRESS_BUFFER_MAX_CHUNKS`; oldest chunk is
                  dropped on overflow with a WARNING.
                * **`response.output_audio_transcript.delta`** —
                  per-`response_id` accumulating text fragments
                  for the bot's transcript. Appended to
                  `transcript_deltas[response_id]`.
                * **`response.output_audio_transcript.done`** — bot's
                  transcript is final for this response. Pop and
                  emit via `_emit_transcript` with
                  `speaker="BOT"` and `start_ts_ms` from
                  `bot_turn_started_at`. Reset
                  `bot_turn_started_at` to None.
                * **`conversation.item.input_audio_transcription.
                  completed`** — caller's transcript landed whole
                  (server-side Whisper transcription). Emit via
                  `_emit_transcript` with `speaker="CUSTOMER"`
                  and `start_ts_ms` from
                  `customer_turn_started_at`. Reset
                  `customer_turn_started_at` to None.
                * **`response.function_call_arguments.done`** —
                  the function-call invocation completed. Hands
                  off to `_handle_function_call`.
                * **`error`** — OpenAI Realtime protocol error.
                  Logged at ERROR with type / code / message
                  fields. The bot.error → Infinity translation
                  is a planned addition (currently the bridge
                  logs and lets the call continue or drop based
                  on whether the error closed the WebSocket).

        Spec:
            OpenAI Realtime API protocol — full event catalog.
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            with `TRANSCRIPT` ftype emitted via the transcript
            branches.

        Args:
            convo: The active conversation whose upstream
                WebSocket produced this event. Mutated by
                branches that update accumulators, turn-start
                fields, ingress buffer / accumulator, response-ID
                tracking sets, or `audio_playing_out`.
            msg: Decoded JSON object from the OpenAI Realtime
                WebSocket.

        Returns:
            None. Outbound effects: optional audio sends to
            Infinity via the IngressStreamer, optional
            `bot.feature` TRANSCRIPT envelopes to Infinity,
            optional `response.cancel` to OpenAI on barge-in.
        """
        event_type = msg.get("type", "")

        # ── Session ──────────────────────────────────────────────────────────

        if event_type == "session.created":
            logger.debug("[%s] session.created: %s", convo.client_id,
                         msg.get("session", {}).get("id"))
            return

        if event_type == "session.updated":
            _fmt = _openai_audio_format(convo.codec_name)
            logger.info(
                "[%s] session.updated — configuration accepted; audio format negotiated: "
                "Infinity=%s/%dHz, OpenAI in=out=%s",
                convo.client_id, convo.codec_name, convo.sample_rate, _fmt,
            )
            await self._on_session_updated(convo)
            return

        # ── VAD / speech ──────────────────────────────────────────────────────

        if event_type == "input_audio_buffer.speech_started":
            logger.debug("[%s] VAD: speech_started", convo.client_id)
            # Capture caller turn-start at the moment server VAD detected
            # speech onset (see method docstring for ordering rationale).
            convo.customer_turn_started_at = int(time.time() * 1000)
            # Barge-in gate is audio_playing_out (queue still has audio
            # paced out to Infinity), not active_response_ids (OpenAI's
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
            return

        if event_type == "response.done":
            response_id = msg.get("response", {}).get("id")
            if response_id and response_id in convo.active_response_ids:
                convo.active_response_ids.remove(response_id)
            await self._flush_output_remainder(convo)
            await self._flush_stashed_transcripts(convo)
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
                    "[%s] Buffering OpenAI audio — Infinity ingress not ready (buffered: %d)",
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

        if event_type == "conversation.item.input_audio_transcription.completed":
            text = msg.get("transcript", "").strip()
            if text:
                logger.info("[%s] CUSTOMER transcript: %s", convo.client_id, text)
                await self._emit_transcript(
                    convo, "CUSTOMER", text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            convo.customer_turn_started_at = None
            await self._flush_stashed_transcripts(convo)
            return

        # ── Tool / function calls ──────────────────────────────────────────────

        if event_type == "response.function_call_arguments.done":
            await self._handle_function_call(convo, msg)
            return

        # ── Errors ────────────────────────────────────────────────────────────

        if event_type == "error":
            error = msg.get("error", {})
            logger.error(
                "[%s] OpenAI error: type=%s code=%s message=%s",
                convo.client_id,
                error.get("type"),
                error.get("code"),
                error.get("message"),
            )
            # TODO: translate error code and emit bot.error to Infinity.
            # Decide reconnect vs. terminate based on error class.
            return

        # ── Everything else ───────────────────────────────────────────────────
        logger.debug("[%s] Unhandled OpenAI event: %s", convo.client_id, event_type)

    # ---------------------------------------------------------------- session.updated

    async def _on_session_updated(self, convo: BotConversation) -> None:
        """Handle the `session.updated` ack: send the proactive greeting and emit `bot.started` to Infinity.

        Contract:
            Triggered by `_handle_openai_message` on the
            `session.updated` event. The session-config round-trip
            sent in `_connect_openai` has now completed; OpenAI
            has accepted the instructions, voice, audio formats,
            server VAD, transcription model, and tool registration.
            From this point on, the session is live and ready to
            generate audio.

            Two effects, in order:

                1. **Proactive greeting via `response.create`.**
                   A bare `response.create` frame (no per-response
                   override) is sent so the model generates the
                   greeting using the session-level persona
                   prompt. The persona prompt's "Greeting"
                   section instructs the model what the first
                   turn should include. A per-response
                   `instructions` override would *replace* the
                   session-level persona for that response,
                   stripping the name / case / branding context
                   established in the session-level prompt; bare
                   `response.create` is the correct invocation
                   for this step. Send failures are logged at
                   WARNING and swallowed (the call continues; the
                   caller may have to speak first).
                2. **`bot.started` to Infinity.** Built and sent
                   directly here (not via a helper). Carries the
                   standard envelope plus
                   `payload.endpointId`. Infinity reads this as
                   the cue to begin sending caller audio; sending
                   it here (rather than at the end of
                   `_handle_bot_start`) ensures OpenAI is fully
                   configured before Infinity routes audio at the
                   bridge.

            **Why `session.updated` is the right anchor for both
            effects.** `session.created` fires on session-bootstrap
            but predates configuration acceptance; sending the
            greeting then would race the configuration apply on
            OpenAI's side. `session.updated` is the
            "configuration accepted" signal — voice, VAD,
            transcription, and tool registration are all in
            effect. Any greeting audio that lands before
            Infinity's ingress path opens is caught by the
            `ingress_ready` latch on the BotConversation (see
            `BotConversation` "Ingress readiness latch" group)
            and flushed when Infinity's first egress frame
            arrives.

        Spec:
            OpenAI Realtime API protocol — `session.updated` is
            the configuration-accepted ack; `response.create` is
            the response-solicitation event.
            RCMS spec §AI Bot Message Definitions —
            `bot.started` envelope shape and
            `payload.endpointId` requirement.

        Args:
            convo: The active conversation whose
                `session.updated` ack triggered this handler.
                Reads `oa_ws`, `session_id`, `endpoint_id`,
                `client_id`, `service`. No mutation.

        Returns:
            None. Side effects: outbound `response.create` to
            OpenAI; outbound `bot.started` to Infinity.
        """
        greeting = {"type": "response.create"}
        try:
            await convo.oa_ws.send(json.dumps(greeting))
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
            invokes this synchronously from inside its streaming
            loop when the per-endpoint egress queue drains under
            either of two conditions:

                * The idle timeout fires (no chunks queued for the
                  configured idle window).
                * An `is_last=True` chunk is paced out (sent by
                  `IngressStreamer.mark_audio_segment_complete`
                  on `response.output_audio.done`).

            Effect: clears `convo.audio_playing_out` so the
            barge-in gate closes — subsequent
            `input_audio_buffer.speech_started` events skip
            barge-in dispatch because there is no audio left to
            interrupt.

            **Synchronous and called from the streaming loop.**
            Keep this cheap. No I/O, no awaits, no complex state
            traversal. The flag flip is the entire body.

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
            Triggered from `_handle_openai_message`'s
            `input_audio_buffer.speech_started` branch when
            `audio_playing_out` is True. Performs the full
            barge-in sequence:

                1. **Close the barge-in gate.** Set
                   `audio_playing_out = False` explicitly. The
                   streamer's playout-done callback fires only on
                   the natural-drain path (idle timeout or
                   `is_last=True`); on the barge-in path the
                   queue is force-cleared, so the bridge clears
                   the flag itself.
                2. **Discard buffered output tail.** Clear
                   `ingress_accumulator` so any chunk fragment
                   accumulated for the current paced flush
                   doesn't leak past the barge-in into the next
                   turn.
                3. **Flush IngressStreamer queue.** Call
                   `IngressStreamer.barge_in` for this
                   `(session_id, endpoint_id)` to clear the
                   per-endpoint queue and send a `lastf=true`
                   flag to Infinity on the current segment.
                   Errors are logged at DEBUG and swallowed.
                4. **Cancel the active OpenAI response.** Look up
                   the most recent entry in
                   `active_response_ids`. If its server-side
                   audio generation has already completed
                   (response_id in `completed_response_ids`),
                   skip the `response.cancel` send — OpenAI
                   would always race-lose and respond with a
                   `response_cancel_not_active` error; the
                   bridge-side queue clear above is the actual
                   barge-in mechanism. Otherwise send
                   `response.cancel` with the response_id.
                   Errors are logged at WARNING and swallowed.

            Returns silently when there are no active responses
            or no upstream WebSocket — the bridge-side queue
            clear (steps 1–3) has already happened, and there is
            nothing to cancel upstream.

        Spec:
            OpenAI Realtime API protocol — `response.cancel` for
            in-flight response cancellation. Calls against
            already-completed responses produce
            `response_cancel_not_active`.

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

        if not (convo.active_response_ids and convo.oa_ws):
            return
        response_id = convo.active_response_ids[-1]
        if response_id in convo.completed_response_ids:
            # Server-side audio generation already completed
            # (response.output_audio.done fired). response.cancel would race-lose
            # and produce response_cancel_not_active from OpenAI. Skip it;
            # the bridge-side queue clear above is the barge-in mechanism.
            logger.debug(
                "[%s] Skipping response.cancel for %s — audio.done already fired",
                convo.client_id, response_id,
            )
            return
        cancel = {"type": "response.cancel", "response_id": response_id}
        try:
            await convo.oa_ws.send(json.dumps(cancel))
            logger.info("[%s] Sent response.cancel for %s", convo.client_id, response_id)
        except Exception as exc:
            logger.warning("[%s] response.cancel failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- tool calls

    async def _handle_function_call(
        self, convo: BotConversation, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a completed `response.function_call_arguments.done` to its handler and reply with `function_call_output`.

        Contract:
            OpenAI Realtime delivers the tool invocation as a
            `response.function_call_arguments.done` event carrying
            `call_id`, `name`, and a JSON `arguments` string. The
            bridge implements two tools (declared inline in this
            module and registered via `session.update.session.tools`):

                * **`transfer_to_agent`**: stash a
                  `bot.feature LIVE_AGENT_HANDOFF` payload on
                  `convo.pending_handoff` and start the drain task
                  in `_wait_for_quiescence_and_emit`. The wire-level
                  emit is deferred until the IngressStreamer queue
                  drains so the acknowledgment audio plays out
                  before Infinity tears down playback for the
                  handoff. Returns `result_text = "Transfer
                  initiated."` to OpenAI. `is_terminal_tool` is
                  True.
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
                  initiated."` to OpenAI. `is_terminal_tool` is
                  True.
                * **anything else**: logged at WARNING; the result
                  text reflects the unknown tool name.
                  `is_terminal_tool` is False.

            **Two-layer terminal-tool protocol.** The system prompt
            instructs the model to speak a complete acknowledgment
            utterance before invoking either terminal tool, as a
            single complete unit, and only then fire the tool
            (the prompt-side layer). The bridge-side layer is the
            **post-tool `response.create` suppression** below: for
            non-terminal tools, the bridge solicits a follow-up
            response by sending `response.create` after delivering
            the function-call result, so the model can speak the
            tool's output. For terminal tools, the bridge sends
            **only** the `function_call_output` and **does not**
            solicit a follow-up. An explicit post-tool
            `response.create` for a terminal tool would prompt the
            model to generate a continuation that lands in the
            audio drain window and reaches the caller as a
            duplicate acknowledgment; the prompt instruction
            telling the model not to generate further output is
            necessary but not sufficient because the model's
            adherence is undermined when the bridge actively asks
            for output. Suppressing `response.create` removes the
            trigger.

            **Defensive task replacement.** If a prior
            `handoff_task` or `session_end_task` is still running
            (the model invokes the terminal tool twice), the prior
            task is cancelled before the new one starts. In
            practice the model picks one terminal tool per call,
            but the guard prevents a duplicate-call race from
            leaving an orphan drain coroutine.

            **Function call reply protocol.** Every invocation
            with a non-empty `call_id` requires a matching
            `conversation.item.create` with
            `type: "function_call_output"` keyed by that call_id.
            Even unknown-tool branches send the output so the
            model's continuation isn't blocked waiting for one.

        Spec:
            OpenAI Realtime API protocol —
            `response.function_call_arguments.done` event shape and
            the `conversation.item.create` reply with
            `function_call_output`.
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            with `LIVE_AGENT_HANDOFF` ftype (emitted later by
            `_emit_pending_handoff` after drain).

        Args:
            convo: The active conversation. Mutated to stash the
                pending handoff payload, set the session-end
                latch, and launch the drain task.
            msg: The decoded
                `response.function_call_arguments.done` event.
                `call_id`, `name`, and `arguments` (JSON string)
                are read.

        Returns:
            None. The `function_call_output` (and conditional
            `response.create`) is sent on the upstream OpenAI
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

        # is_terminal_tool: True if invoking this tool ends the conversation
        # (handoff or session-end). For terminal tools, suppress the post-
        # tool response.create — see method docstring "Two-layer terminal-
        # tool protocol".
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
            # terminal-tool protocol applies to both).
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
            logger.warning("[%s] Unknown OpenAI tool '%s'", convo.client_id, name)
            result_text = f"Unknown tool: {name}"
            is_terminal_tool = False

        # Return the function result to OpenAI. For non-terminal tools,
        # also solicit a response so the model can speak the result.
        # Terminal tools skip response.create — see method docstring
        # "Two-layer terminal-tool protocol".
        if call_id and convo.oa_ws:
            try:
                await convo.oa_ws.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": result_text,
                    },
                }))
                if not is_terminal_tool:
                    await convo.oa_ws.send(json.dumps({"type": "response.create"}))
            except Exception as exc:
                logger.warning("[%s] function_call_output send failed: %s", convo.client_id, exc)

    # ---------------------------------------------------------------- transcripts

    async def _flush_stashed_transcripts(self, convo: BotConversation) -> None:
        """Emit stashed BOT transcript when no CUSTOMER turn is in flight.

        BL-003: Infinity stamps each TRANSCRIPT by frame arrival and
        ignores `startTsMs`. Hold BOT while `customer_turn_started_at`
        or `pending_customer_text` indicates an in-flight caller turn
        so CUSTOMER `.completed` can land first. Greeting / BOT-only
        turns have neither, so BOT flushes immediately.
        """
        customer_in_flight = bool(
            convo.pending_customer_text.strip() or convo.customer_turn_started_at
        )
        if customer_in_flight:
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
        """Emit a `bot.feature` TRANSCRIPT envelope for a single transcript line.

        Contract:
            Wraps a single transcript line in the bridge-standard
            transcript shape: `payload.ftype = "TRANSCRIPT"` plus
            `payload.transcript = {turnId, speaker, isFinal,
            text, confidence, language, startTsMs}`. A fresh
            UUID4 turnId is generated per emit; `confidence` is
            set to 1.0 (OpenAI Realtime audio-mode transcription
            does not surface a per-utterance confidence value);
            `language` is read from `convo.language_code`.

            **`startTsMs` derivation.** When `start_ts_ms` is
            supplied (the normal path from
            `_handle_openai_message`'s
            `response.output_audio_transcript.done` and
            `conversation.item.input_audio_transcription.
            completed` branches, plus the pre-handoff flush in
            `_emit_pending_handoff`), it is the moment the
            speaker actually started their turn — captured on
            `input_audio_buffer.speech_started` for the caller,
            on `response.created` for the bot. Using turn-start
            timestamps preserves correct chronological ordering
            on the wire and in Infinity's call record even when the
            bot's transcript completion event lands ahead of the
            caller's transcription pipeline. Falls back to
            flush-time `int(time.time() * 1000)` when no value
            is supplied.

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

        # Mirror the finalized turn into the OpenAI conversation object, if the
        # workflow enabled mirroring. Best-effort and non-blocking — enqueue
        # only; the background worker handles the POST. CUSTOMER → user item
        # (input_text), BOT → assistant item (output_text).
        if convo.conv_mirror is not None:
            convo.conv_mirror.enqueue(
                "user" if speaker == "CUSTOMER" else "assistant", text,
            )

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
        logger.info("[%s] OUTBOUND JSON (%s): %s",
                    convo.client_id, event_type, format_compact_json(event))
        log_message_exchange("OUTBOUND", convo.client_id, event_type, event, is_media=False)
        await convo.websocket.send(json.dumps(event))

    # ---------------------------------------------------------------- handoff drain

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
            for any audio chunk crossing the network during the
            iteration to land in the queue before the empty
            check.

            Drain timing rationale: the
            `transfer_to_agent` `function_call_arguments.done`
            event arrives before the acknowledgment audio
            finishes streaming. The first poll after the initial
            sleep typically sees a non-empty queue (OpenAI is
            still streaming the acknowledgment); subsequent
            polls continue until the queue is observably empty.
            The grace period absorbs network jitter so a
            momentarily-empty queue doesn't trigger a
            premature emit on a still-streaming
            acknowledgment.

            **Safety net.** `deadlock_safety_s` is an
            upstream-failure backstop — e.g. OpenAI WS hung in a
            way that prevents the audio queue from ever
            draining. Trip indicates something genuinely wrong
            upstream, not a drain-timing issue. Logs at WARNING
            and emits the handoff anyway so the workflow does
            not stall indefinitely.

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
                Default `_QUIESCENCE_POLL_MS` (250 ms).
            deadlock_safety_s: Upper bound on total wait time.
                Default `_QUIESCENCE_DEADLOCK_SAFETY_S` (12 s).

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
            queue-empty, loop) and same safety net
            (`deadlock_safety_s`). The two methods differ in the
            latch they observe and the terminal envelope they
            fire:

                * Handoff drain → latches on `pending_handoff`,
                  fires `_emit_pending_handoff` (which sends
                  `bot.feature` LIVE_AGENT_HANDOFF + manual
                  absent-status `bot.ended`).
                * Session-end drain (this method) → latches on
                  `pending_session_end`, fires
                  `_emit_session_end_complete` (which sends
                  success-context `bot.ended` via the helper).

            Same drain-timing reasoning applies: the
            `end_session` invocation arrives before the closing
            line finishes streaming, so emitting the success-
            context `bot.ended` immediately would cause Infinity
            to tear down playback mid-utterance and the caller
            would miss the closing line. The drain ensures the
            audio plays out first.

            Latching on a separate field
            (`pending_session_end` vs `pending_handoff`) means
            the two paths cannot collide if both ever activate
            on the same call.

            Cancellation-safe via the same `CancelledError`
            return as the handoff drain.

        Args:
            convo: The conversation with `pending_session_end`
                latched True.
            poll_ms: Sleep interval per iteration in milliseconds.
                Default `_QUIESCENCE_POLL_MS` (250 ms).
            deadlock_safety_s: Upper bound on total wait time.
                Default `_QUIESCENCE_DEADLOCK_SAFETY_S` (12 s).

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

            Cancellation-self guard: when this method is invoked
            at the end of the drain loop,
            `asyncio.current_task()` IS `session_end_task`, and
            cancelling it would raise `CancelledError` into the
            in-flight `send_bot_ended_with_success_context`
            call. Skip the cancel for the running task.

            Clears `pending_session_end` and `session_end_task`
            before emitting so a duplicate trigger sees an
            already-flushed conversation and returns silently
            (the early-out at the top).

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
        # Cancellation-self guard — current_task() IS session_end_task on
        # the drain-completion path; skip the cancel.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        try:
            await self.server.send_bot_ended_with_success_context(
                convo.websocket,
                convo.client_id,
                convo.session_id,
                convo.endpoint_id,
                description="OPENAI: Self-service interaction completed.",
                service=convo.service,
                convo=convo,
            )
            logger.info("[%s] Emitted self-service-complete bot.ended", convo.client_id)
        except Exception as exc:
            logger.warning("[%s] success-context bot.ended emit failed: %s",
                           convo.client_id, exc)

    async def _emit_pending_handoff(self, convo: BotConversation) -> None:
        """Flush stranded transcripts, emit `bot.feature` LIVE_AGENT_HANDOFF, then originate `bot.ended`.

        Contract:
            Terminus of the deferred-handoff path: drain has
            already completed in
            `_wait_for_quiescence_and_emit`, and this method
            performs three sequenced wire-level emits:

                1. **Pre-handoff transcript flush.** The
                   `transfer_to_agent` invocation can preempt
                   the transcript completion events for the
                   acknowledgment turn, leaving partial caller
                   transcripts in `pending_customer_text` and
                   bot transcript fragments in
                   `transcript_deltas`. Flushing here produces
                   the wire-level TRANSCRIPT envelopes the
                   workflow needs for the handoff record. Empty
                   strings are skipped so a tool-only turn
                   doesn't emit blank frames. Wrapped in its own
                   try block so a flush failure cannot block the
                   LIVE_AGENT_HANDOFF emit below. Turn-start
                   timestamps preserved per the same per-speaker
                   capture mechanism used elsewhere — passing
                   them keeps stranded text correctly anchored
                   even though the flush itself happens later
                   than the original turn.
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
                   termination shape has **no status object**.
                   The workflow consumes the
                   `byobotLiveAgentHandoff` value populated from
                   the prior `bot.feature` rather than
                   `byobotEndContext.status`. Bridge-originating
                   `bot.ended` here triggers Infinity to send
                   `session.end` as a clean teardown ack;
                   staying silent leads to a teardown timeout
                   race observable as a delayed `session.ended`.
                   `convo.bot_ended_sent` is set explicitly here
                   because the helper-flag-flip path is bypassed.

            Cancellation-self guard: when this method is invoked
            at the end of the drain loop,
            `asyncio.current_task()` IS `handoff_task`. Skip the
            cancel for the running task to avoid raising
            `CancelledError` into our own awaits below.

            Clears `pending_handoff`, `pending_handoff_args`,
            and `handoff_task` before emitting so a duplicate
            trigger sees an already-flushed conversation and
            returns silently (the early-out at the top).

        Spec:
            RCMS spec §AI Bot Message Definitions — `bot.feature`
            envelope and `LIVE_AGENT_HANDOFF` ftype.
            `bridge/schema/rcms.schema.md` "bot.ended — status
            is nested in context" — the absent-status row pins
            the handoff termination shape.

        Args:
            convo: The active conversation. Reads
                `pending_handoff` / `pending_handoff_args` /
                accumulators / turn-start fields. Mutates:
                `pending_handoff` → None,
                `pending_handoff_args` → None, `handoff_task`
                → None, accumulators cleared, turn-start fields
                → None, `bot_ended_sent` → True after the
                bot.ended send completes.

        Returns:
            None. Errors on any of the three emits are logged
            at WARNING and swallowed; the method always returns
            cleanly.
        """
        payload = convo.pending_handoff
        args = convo.pending_handoff_args or {}
        if not payload:
            return
        convo.pending_handoff = None
        convo.pending_handoff_args = None
        task = convo.handoff_task
        convo.handoff_task = None
        # Cancellation-self guard — see method docstring. current_task()
        # IS handoff_task on the drain-completion path.
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

        # Pre-handoff transcript flush — see method docstring step 1.
        # The try/except isolates flush errors from the handoff emit below.
        try:
            customer_text = convo.pending_customer_text.strip()
            if customer_text:
                await self._emit_transcript(
                    convo, "CUSTOMER", customer_text,
                    start_ts_ms=convo.customer_turn_started_at,
                )
            convo.pending_customer_text = ""
            convo.customer_turn_started_at = None
            await self._flush_stashed_transcripts(convo)
            for resp_id, text in list(convo.transcript_deltas.items()):
                text = text.strip()
                if text:
                    await self._emit_transcript(
                        convo, "BOT", text,
                        start_ts_ms=convo.bot_turn_started_at,
                    )
            convo.transcript_deltas.clear()
            convo.pending_bot_text = ""
            convo.bot_turn_started_at = None
        except Exception as exc:
            logger.warning("[%s] Pre-handoff transcript flush failed: %s", convo.client_id, exc)

        try:
            await self._emit_session_event(convo, "bot.feature", payload)
            reason = args.get("reason", "")
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
        """Tear down every per-conversation resource: drain tasks, ingress streamer, OpenAI WS.

        Contract:
            Idempotent shutdown for a single `BotConversation`.
            Called on every exit path: `_handle_bot_end`,
            `on_session_ended`, and `shutdown` (process
            shutdown). Marks `convo.active = False` first so any
            in-flight `_openai_recv_loop` iteration sees the
            flag and exits at its next message boundary.

            Order of cleanup:
                1. Cancel the **handoff drain task**
                   (`handoff_task`) if still running. A caller
                   hangup mid-handoff-wait means Infinity is
                   already tearing the session down — there is
                   nothing to drain to and nothing useful to
                   emit. Cancel without emitting; do not invoke
                   `_emit_pending_handoff` from this path.
                   Clears `pending_handoff` and
                   `pending_handoff_args`.
                2. Cancel the **session-end drain task**
                   (`session_end_task`) symmetrically. Same
                   reasoning: a caller hangup mid-session-end-
                   wait means the success-context `bot.ended`
                   is moot. Clears `pending_session_end`.
                3. Close the **conv_ mirror** (`conv_mirror`) if
                   present: drain pending item POSTs under a
                   bounded timeout, then cancel its worker.
                   Best-effort; timed-out items are dropped.
                4. Drain and tear down the IngressStreamer for
                   this `(session_id, endpoint_id)` via
                   `stop_and_clear`. Purges the per-endpoint
                   queue and cancels the streaming task.
                5. Cancel the OpenAI receive loop task and
                   await its exit so no further events are
                   dispatched after this point.
                6. Close the upstream OpenAI WebSocket. Errors
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

        # Drain any pending conv_ mirror posts (bounded timeout), then cancel
        # the worker. Best-effort: timed-out items are dropped with a WARNING.
        mirror = convo.conv_mirror
        convo.conv_mirror = None
        if mirror is not None:
            await mirror.aclose()

        if convo.endpoint_id:
            try:
                await self.server.ingress_streamer.stop_and_clear(
                    convo.session_id, convo.endpoint_id
                )
            except Exception as exc:
                logger.debug("[%s] stop_and_clear raised: %s", convo.client_id, exc)

        task = convo.oa_recv_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        ws = convo.oa_ws
        convo.oa_ws = None
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[%s] OpenAI ws.close raised: %s", convo.client_id, exc)


def register(server: "BridgeServer") -> OpenAIService:
    """Plugin entrypoint — instantiate `OpenAIService` and register it with the bridge.

    Discovered and called by `bot_service.py` at startup if
    `OpenAIService.is_configured()` returns `True` (i.e.
    `OPENAI_API_KEY` is set in the environment). The constructed
    plugin is added to the bridge's `ServiceRegistry` and
    thereafter receives every `bot.start` / `bot.end` whose
    `payload.botId` carries the `openai:` prefix.

    Args:
        server: The owning `BridgeServer` instance.

    Returns:
        The registered `OpenAIService`. Returned for tests /
        startup diagnostics; production callers do not retain
        the reference.
    """
    plugin = OpenAIService(server)
    server.register_service(plugin)
    return plugin
