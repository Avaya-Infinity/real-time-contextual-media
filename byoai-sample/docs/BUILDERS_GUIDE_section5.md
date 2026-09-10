## §5 Avaya Infinity: Network Requirements

The bridge must be reachable from Avaya Infinity over HTTPS. Infinity initiates all WebSocket connections outbound to your bridge URL — no inbound firewall rules are required on your side.

Key requirements:

- **Public HTTPS endpoint** — your bridge URL must be publicly accessible
- **Valid TLS certificate** — Avaya Infinity requires a certificate from a public CA; self-signed certificates are not accepted
- **TLS 1.2 or later** — required on all connections

For the complete network and security requirements, refer to the [Avaya Infinity Real-time Contextual Media Streaming](https://developers.avayacloud.com/avaya-infinity/docs/real-time-contextual-media-streaming) developer documentation.
