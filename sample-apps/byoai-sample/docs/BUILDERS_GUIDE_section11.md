## §11 Verification with Echo

With the bridge running and the workflow published, place a test call to your inbound number. Echo requires no AI credentials — it loops your audio back to confirm the full path is working.

### What to expect

Answer the call and speak. You should hear your own audio played back with a short delay — that is Echo confirming the bridge is receiving and returning audio over the RCMS connection.

### Confirm in the bridge journal

On your bridge server, check the journal:

```bash
sudo journalctl -u bridge.service -f
```

A successful Echo call produces this sequence (lines prefixed with `[<client>]`, the inbound RCMS host:port):

```
[<client>] INBOUND JSON (session.start)
[<client>] Codec negotiation - offered: [...] selected: G722
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] Received bot.start for Echo session: <session-id>
[<client>] OUTBOUND JSON (bot.started)
[binary audio frames echoing back]
[<client>] Sending bot.ended with disconnect context: provider=echo
[<client>] OUTBOUND JSON (bot.ended)
[<client>] OUTBOUND JSON (session.ended)
```

### What each line confirms

| Log line | What it confirms |
|---|---|
| `INBOUND JSON (session.start)` | Infinity reached the bridge over WSS |
| `Codec negotiation - offered: [...] selected: <codec>` | Both sides agreed on an audio codec |
| `OUTBOUND JSON (session.started)` | Bridge accepted the session |
| `INBOUND JSON (bot.start)` | Workflow reached the IVA module and invoked the bridge |
| `Received bot.start for Echo session` | Bridge routed to the Echo provider |
| `OUTBOUND JSON (bot.started)` | Echo session established |
| `Sending bot.ended with disconnect context: provider=echo` | Clean teardown on hangup |
| `OUTBOUND JSON (session.ended)` | Session closed |

If you see this sequence with no errors, your foundation is working. Move to Part 2 to add an AI provider.

### What you may also see under `--verbose`

When the bridge runs with `--verbose`, the root Python logger drops to DEBUG and the journal also surfaces audio-pipeline diagnostic lines:

```
[<client>] ========== FIRST MEDIA EGRESS (stream: ..., format: binary) ==========
[<client>] MEDIA SUMMARY (1s): id=0:1 source=tx egress: ev=4 bytes=8000 seq=4 last=False
[<client>] MEDIA SUMMARY (1s): id=0:1 source=tx egress: ev=4 bytes=8000 seq=8 last=False
...
```

`MEDIA SUMMARY (1s)` fires once per second per audio stream and is diagnostic-only — under `--verbose` it dominates the journal during any active call. At the default INFO level these lines are absent. If you see them in your journal, your bridge is running with `--verbose`; if you don't, the audio path is still working — just quietly. See **§4 — Logs and observability** for level filtering.

### Other journal patterns to recognize

The Echo path above is the success-on-hangup case. Two other lifecycle shapes will appear when you connect a real AI provider.

**Failure path — unrecognized `botId`:** if the `botId` custom parameter on the IVA module names a provider the bridge doesn't recognize (or whose API key is unset), the bridge rejects the call cleanly with a 5xx status:

```
[<client>] INBOUND JSON (session.start)
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] Sending bot.ended with failure context: code=501, reason=UNSUPPORTED_SERVICE
[<client>] OUTBOUND JSON (bot.ended)
   ... payload.context.status.description: "BACKEND_NOT_CONFIGURED: ..." or "UNRECOGNIZED_BOTID_PREFIX: ..."
[<client>] OUTBOUND JSON (session.ended)
```

The Infinity workflow receives this `bot.ended` on its IVA module SUCCESSFUL branch with `byobotEndContext.status.code = 501` — route on that to your failure recovery path (see §3 Phase 3).

**Handoff path — live agent transfer:** when the AI provider triggers a handoff (typically via a tool call), the bridge drains queued audio, then emits a `bot.feature LIVE_AGENT_HANDOFF` followed by `bot.ended` with no `status` field:

```
[<client>] INBOUND JSON (session.start)
[<client>] OUTBOUND JSON (session.started)
[<client>] INBOUND JSON (bot.start)
[<client>] <Provider> bot.start session=... endpoint=...
[<client>] OUTBOUND JSON (bot.started)
[<client>] BOT transcript: ...
[<client>] CUSTOMER transcript: ...
[<client>] OUTBOUND JSON (bot.feature)
[<client>] Emitted LIVE_AGENT_HANDOFF (reason='...')
[<client>] OUTBOUND JSON (bot.ended)
[<client>] INBOUND JSON (session.end)
[<client>] OUTBOUND JSON (session.ended)
```

The workflow takes the IVA module's HANDOFF branch in this case (provided **Enable Handoff Events** is `true` — see §9). The `byobotLiveAgentHandoff` workflow variable carries the `queueId`, `tags`, and `context` from the provider's handoff payload.

### Troubleshooting

**No audio loopback** — confirm the workflow is published (not draft) and the phone number is routed to the correct workflow version.

**Bridge not reached** — confirm your public HTTPS endpoint is reachable and the AI Media Gateway profile Destination WebSocket URL matches your bridge address.

**`bot.start` not received** — confirm the IVA module Connection is set to your AI Media Gateway profile and the profile is enabled.
