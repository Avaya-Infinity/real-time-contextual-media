## §4 Bridge Installation and Configuration

### Prerequisites

Before installing the bridge, ensure you have:

- Python 3.10 or later (Python 3.13+ requires `audioop-lts` — included in `requirements.txt`)
- Git
- A server with a publicly accessible HTTPS endpoint
- A valid TLS certificate from a public CA (self-signed certificates are not supported by Avaya Infinity)

For full network and security requirements, see the [Avaya Infinity Real-time Contextual Media Streaming](https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming) developer documentation.

---

### Clone and install

```bash
git clone https://github.com/bode-avaya/infinity-rcms-byoai.git
cd infinity-rcms-byoai
python3 -m venv venv
source venv/bin/activate
pip install -r bridge/requirements.txt
```

---

### Configure environment variables

Copy the example file and edit it:

```bash
cp bridge/.env.example bridge/.env
```

Open `bridge/.env` and configure the following values for Part 1. Provider-specific variables are covered in each provider's guide — the full reference is in `bridge/.env.example`.

**Bridge security:**

| Variable | Required | Description |
|---|---|---|
| `INFINITY_JWT_PRIMARY_KEY` | Required when auth is enabled | Primary JWT signing key from the Avaya Infinity Admin Dashboard |
| `INFINITY_JWT_SECONDARY_KEY` | Optional | Secondary key — used during key rotation to maintain service continuity |

**Operational:**

| Variable | Default | Description |
|---|---|---|
| `LOG_DIGITS` | `false` | Set `true` only when troubleshooting DTMF behavior. Logs raw digit values which may include sensitive data — disable when troubleshooting is complete |

---

### Start the bridge

```bash
python3 bridge/main.py --host 127.0.0.1 --port 8444 --verbose
```

On a clean startup you should see:

```
Registered plugin: bot_echo.py
Registered backend: ElevenLabsService (botId prefix: elevenlabs:)
Registered backend: GeminiService (botId prefix: gemini:)
Registered backend: OpenAIService (botId prefix: openai:)
Registered backend: XaiService (botId prefix: xai:)
Registered plugin: bot_service.py
RCMS Bridge listening on 127.0.0.1:8444
```

Only providers with their API key configured will register. If you have not set any AI provider credentials yet, only `bot_echo.py` and `bot_service.py` will appear — that is the correct state for Part 1 of this guide.

---

### Common flags

| Flag | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Interface to bind |
| `--port` | `8443` | Port to listen on |
| `--codec` | `G722` | Preferred audio codec (`PCMU` eliminates transcoding overhead for xAI Grok) |
| `--enable-auth` | off | Enable JWT bearer token validation |
| `--jwt-primary-key` | env | JWT primary key (overrides `INFINITY_JWT_PRIMARY_KEY` env var) |
| `--jwt-secondary-key` | env | JWT secondary key (overrides `INFINITY_JWT_SECONDARY_KEY` env var) |
| `--log-file` | `logs/bridge_log.txt` | Path to the on-disk Python log file. Rotated to `.bak` on every restart |
| `--message-log-file` | `logs/bridge_msg.txt` | Path to the structured protocol-frame log (INBOUND/OUTBOUND JSON only). Rotated to `.bak` on every restart |
| `--verbose` | off | Raise the root Python logger from INFO to DEBUG. Surfaces audio-pipeline diagnostic lines (`MEDIA SUMMARY`, `FIRST MEDIA EGRESS/INGRESS`) that are otherwise suppressed |

---

### Logs and observability

The bridge writes logs to two destinations on every run:

1. **stderr** — captured by whichever process manager runs the bridge. Under `systemd`, this becomes the service journal (`journalctl -u <unit-name>`); under a bare terminal, it goes to the terminal directly.
2. **On-disk files under `logs/`** — `bridge_log.txt` (full Python log) and `bridge_msg.txt` (structured INBOUND/OUTBOUND JSON protocol frames only, with `LOG_DIGITS` redaction applied). Both are rotated to `.bak` on every restart, so pull anything you need before the next restart.

The two destinations carry different data: the journal/stderr captures everything the root Python logger emits, including provider-specific diagnostic chatter; `bridge_msg.txt` captures only the RCMS wire frames between Infinity and the bridge, which makes it the cleaner artifact for spec-conformance review. Media frames are not written to `bridge_msg.txt`.

**Log levels:**

- Default root-logger level is `INFO`.
- `--verbose` (or `-v`) raises it to `DEBUG`. The audio-pipeline diagnostic lines (`MEDIA SUMMARY (1s)` per-second counters, `FIRST MEDIA EGRESS/INGRESS` stream-open banners) are at DEBUG — they appear under `--verbose` and are absent at the INFO default. Run with `--verbose` when debugging audio path problems; leave it off for normal operation.
- `bridge_msg.txt` is independent of the root level — it captures protocol frames at a fixed cadence regardless of `--verbose`.

**Filtering by Python log level: do not use `journalctl -p info`.** systemd tags every Python `stderr` write at syslog priority `info` regardless of the Python `logging` module level, so `-p info` does **not** filter Python's levels — a Python `DEBUG` line appears in the `-p info` view alongside `INFO` lines. The reliable filter is to grep on the Python-level tag embedded in the log format:

```bash
# Python-INFO lines only
journalctl -u <unit-name> --since "..." --no-pager | grep " - INFO - "

# Python-DEBUG lines only (verbose-mode noise)
journalctl -u <unit-name> --since "..." --no-pager | grep " - DEBUG - "
```

This same pattern works on the on-disk `bridge_log.txt`:

```bash
grep " - INFO - " logs/bridge_log.txt
```

**`LOG_DIGITS` redaction scope** — DTMF digits only. When `LOG_DIGITS=false` (default), the bridge replaces `session.dtmf.payload.digits` with `"<redacted>"` in both the journal and `bridge_msg.txt`. No other fields are currently redacted. Set `LOG_DIGITS=true` only when actively troubleshooting DTMF dispatch — raw digit values land in the logs and, on payment IVR or account-verification flows, may include cardholder data.

For the per-call diagnostic-marker reference (lifecycle markers by phase, transcript marker cadence per provider, common termination shapes), see **[§12 Quick Reference](BUILDERS_GUIDE_section12.md)**.
