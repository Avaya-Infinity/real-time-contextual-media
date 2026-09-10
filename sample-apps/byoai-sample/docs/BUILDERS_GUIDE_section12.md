## §12 Quick Reference

This section collects the most-referenced values from the guide in one place. Use it while configuring, deploying, or debugging — not as a substitute for the full sections.

---

### Environment variables

All variables live in `bridge/.env` (copy from `bridge/.env.example`). The bridge does not auto-load this file — on a VM deployment it is loaded by systemd via `EnvironmentFile=`; for local runs, source it before launching the bridge.

**Bridge security (see §6):**

| Variable | Required | Description |
|---|---|---|
| `INFINITY_JWT_PRIMARY_KEY` | When `--enable-auth` is set | Primary HS256 signing key from the Avaya Infinity Admin Dashboard |
| `INFINITY_JWT_SECONDARY_KEY` | Optional | Secondary key — used during rotation for zero-downtime key changes |

**Operational (see §4):**

| Variable | Default | Description |
|---|---|---|
| `LOG_DIGITS` | `false` | Set `true` only when troubleshooting DTMF behavior; logs raw digit values |

**Provider (set only the ones you use):**

| Variable | Description |
|---|---|
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GEMINI_MODEL` | Gemini Live model identifier (default: `gemini-3.1-flash-live-preview`) |
| `GEMINI_SYSTEM_PROMPT` / `GEMINI_SYSTEM_PROMPT_FILE` | Inline base prompt or path to a markdown prompt file |
| `GEMINI_VOICE` | Voice id (default: `Aoede`) |
| `OPENAI_API_KEY` | OpenAI Realtime API key |
| `OPENAI_MODEL` | Model id (default: `gpt-realtime-2.1`) |
| `OPENAI_VOICE` | Voice id (default: `cedar`) |
| `XAI_API_KEY` | xAI Grok Voice API key |
| `XAI_MODEL` | Model id (default: `grok-voice-think-fast-2.0`) |
| `XAI_VOICE` | Voice id (default: `ara`) |

The agent identifier for ElevenLabs comes from the `botId` at runtime (`elevenlabs:<agent_id>`) — there is no `ELEVENLABS_AGENT_ID` env var.

---

### Startup flags

```
python3 bridge/main.py [flags]
```

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8443` | Port to listen on |
| `--codec` | `G722` | Preferred audio codec (`L16`, `PCMU`, `PCMA`, `G722`) |
| `--enable-auth` | off | Require JWT bearer token validation |
| `--jwt-primary-key` | `INFINITY_JWT_PRIMARY_KEY` env | Primary HS256 key (CLI overrides env) |
| `--jwt-secondary-key` | `INFINITY_JWT_SECONDARY_KEY` env | Secondary HS256 key (CLI overrides env) |
| `--ssl-cert` | none | Path to TLS certificate (required for WSS unless fronted by a proxy) |
| `--ssl-key` | none | Path to TLS private key |
| `--log-file` | `logs/bridge_log.txt` | Path to the on-disk Python log file. Rotated to `.bak` on every restart |
| `--message-log-file` | `logs/bridge_msg.txt` | Path to the structured protocol-frame log (INBOUND/OUTBOUND JSON only). Rotated to `.bak` on every restart |
| `--verbose` / `-v` | off | Raise the root Python logger from INFO to DEBUG. Surfaces `MEDIA SUMMARY` and `FIRST MEDIA EGRESS/INGRESS` lines that are otherwise suppressed |

JWT flags are the only ones that fall back to env vars; everything else is explicit at the command line.

---

### botId prefix convention

The bridge routes each call to a provider plugin based on the `botId` value in `bot.start.payload.customParameters` (set on the IVA module — see §9).

| `botId` value | Routes to |
|---|---|
| `echo` | Echo provider (no suffix, no credentials needed) |
| `elevenlabs:<agent_id>` | ElevenLabs (agent id supplied per call) |
| `gemini:<model>` | Gemini Live (model name supplied per call) |
| `openai:<model>` | OpenAI Realtime (model name supplied per call) |
| `xai:<model>` | xAI Grok Voice (model name supplied per call) |

**Example:** `botId = openai:gpt-realtime-2.1` routes to OpenAI Realtime with that model. Echo is the only provider whose `botId` has no suffix.

When `GEMINI_MODEL` / `OPENAI_MODEL` / `XAI_MODEL` is set in the environment, it forces that model for every call and ignores the botId suffix. Leave the env var unset to let each call pick its own model via the suffix; the bridge falls back to a built-in default only if both are empty.

---

### IVA module exits and workflow variables

The IVA module has three exit branches (see §9 for full detail):

| Branch | Fires on | Workflow variable to read |
|---|---|---|
| `SUCCESSFUL` | `bot.ended` with `context.status.code = 200` | `byobotEndContext` |
| `HANDOFF` | `bot.feature LIVE_AGENT_HANDOFF` followed by `bot.ended` | `byobotLiveAgentHandoff` |
| `FAILED` | `bot.ended` with non-200 status | `byobotEndContext` |

`HANDOFF` is the workflow branch label; `LIVE_AGENT_HANDOFF` is the `bot.feature` event type that triggers it. Enable Handoff Events must be `true` on the IVA module for the branch to fire.

---

### Diagnostic log lines

A handful of journal lines partners commonly grep when something is wrong. The bridge prefixes most lines with a per-connection `[client_id]` correlator.

**Bridge is up:**
```
RCMS Bridge listening on 127.0.0.1:8444
Registered plugin: bot_echo.py
Registered backend: ElevenLabsService (botId prefix: elevenlabs:)
```
The startup log emits one `Registered backend:` line per provider whose API key env var is set (`ElevenLabsService` / `GeminiService` / `OpenAIService` / `XaiService`). If a provider you expect is missing, that provider's API key env var is unset.

**Auth failure (when `--enable-auth` is on, see §6):**
```
Missing or invalid Authorization header from <addr>
Invalid JWT bearer token from <addr> (could not verify with any key format)
Expired JWT bearer token from <addr>
```

**Routing failure:**
```
[<client_id>] BACKEND_NOT_CONFIGURED
```
The `botId` prefix matched a known provider but its API key env var was not set at bridge startup.

**Termination shape (one of four):**
```
[<client_id>] Sending bot.ended with success context: code=200, reason=ENDPOINT_RELEASED
[<client_id>] Sending bot.ended with failure context: code=<5xx>, reason=<...>
[<client_id>] Sending bot.ended with disconnect context: provider=<name>
[<client_id>] Emitted LIVE_AGENT_HANDOFF (reason='...')
```
The first three are `bot.ended` log prefixes for self-service complete / provider error / caller hangup. The fourth marks the live agent handoff path — the bridge originates `bot.ended` with no `status` field after emitting `bot.feature LIVE_AGENT_HANDOFF`, so the `OUTBOUND JSON (bot.ended)` line follows without a `Sending bot.ended with X context` prefix. See §3 Phase 3 for the full closure logic.

---

### Lifecycle markers by phase

When debugging a single call, grep by phase to localise where things went wrong. All markers carry the `[<client_id>]` prefix — use it to isolate one call's frames from concurrent traffic.

| Phase | Grep target | Confirms |
|---|---|---|
| 1 — Start | `INBOUND JSON (session.start)` | Infinity reached the bridge over WSS |
| 1 — Start | `Codec negotiation - offered: [...] selected: <codec>` | Codec settled |
| 1 — Start | `OUTBOUND JSON (session.started)` | Bridge accepted the session |
| 1 — Start | `INBOUND JSON (bot.start)` | Workflow IVA module invoked the bridge |
| 1 — Start | `<Provider> bot.start session=... endpoint=...` | Dispatcher routed to the provider plugin (`ElevenLabs` / `Gemini` / `OpenAI` / `Xai`) |
| 1 — Start | `OUTBOUND JSON (bot.started)` | Provider session established |
| 2 — During | `BOT transcript:` / `CUSTOMER transcript:` | Per-turn / per-chunk transcript markers (see cadence note below) |
| 2 — During | `MEDIA SUMMARY (1s)` / `FIRST MEDIA EGRESS/INGRESS` | Audio pipeline health (DEBUG-only — `--verbose` required) |
| 3 — Closure | `Emitted LIVE_AGENT_HANDOFF (reason=...)` | Handoff path fired |
| 3 — Closure | `Emitted self-service-complete bot.ended` | Provider-side self-service signal flushed cleanly |
| 3 — Closure | `Sending bot.ended with success/failure/disconnect context` | Closure shape (see termination triplet above) |
| 3 — Closure | `OUTBOUND JSON (session.ended)` | Bridge closed the session — call done |

### Transcript markers — cadence varies by provider

All four providers emit `[<client_id>] BOT transcript: <text>` and `[<client_id>] CUSTOMER transcript: <text>` lines on `logger.info`. The cadence varies because of how each provider's protocol delivers transcript text:

| Provider | Cadence | Source event |
|---|---|---|
| ElevenLabs | One line per completed turn | `user_transcript` / `agent_response` (turn-completion only in EL's protocol) |
| OpenAI | One line per completed turn | `response.output_audio_transcript.done` / `conversation.item.input_audio_transcription.completed` |
| xAI | One line per completed turn | `response.output_audio_transcript.done` / `conversation.item.input_audio_transcription.completed` |
| Gemini | Per chunk (high volume per turn) | Streaming `outputTranscription` / `inputTranscription` fields on `serverContent` |

The volume difference is expected: a 30-second Gemini call may emit dozens of `BOT transcript:` lines (each a few words); an equivalent OpenAI call emits 3–5 (each a full turn). Format is identical; only the cadence varies.

### Filtering by Python log level — use `grep`, not `journalctl -p`

When the bridge runs under `systemd`, `journalctl -p info` does **not** filter Python log levels. systemd tags every Python `stderr` write at syslog priority `info` regardless of the `logging` module level, so `-p info` drops nothing for our purposes — a Python `DEBUG` line appears in the `-p info` view alongside `INFO` lines.

The reliable filter is to grep on the Python-level tag embedded in the log format string:

```bash
# Python-INFO lines only
journalctl -u <unit-name> --since "..." --no-pager | grep " - INFO - "

# Python-DEBUG lines only (verbose-mode noise)
journalctl -u <unit-name> --since "..." --no-pager | grep " - DEBUG - "
```

The same pattern works against the on-disk `logs/bridge_log.txt` file. Use this when verifying that an INFO-level marker fired (e.g. `BOT transcript:`) without being drowned out by DEBUG-level chatter (e.g. `MEDIA SUMMARY`).
