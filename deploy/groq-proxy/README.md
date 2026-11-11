# Private Groq Egress

This optional sidecar gives Groq its own network route. It must not change
the host default route, other providers, or video downloads.

Build with an official Mihomo Linux release binary named `mihomo` in the
build context. Verify the release asset's published SHA-256 before extracting
it. Neither the binary nor private configuration belongs in Git.

The production sidecar uses the existing `videoinsight-network`, an
authenticated HTTP listener, and no published ports. Its private JSON
configuration contains exactly one upstream node and these routing rules:

```json
[
  "AND,((DOMAIN,api.groq.com),(DST-PORT,443)),groq-egress",
  "MATCH,REJECT"
]
```

The upstream must be named `groq-egress`. Set `mode` to `rule`, disable TUN
and the DNS listener, omit external controllers and remote providers, and
require authentication for every client. Store the private configuration
outside the source tree, readable only by UID 65532 and root.

The application gets a private `GROQ_PROXY_URL` pointing to
`videoinsight-groq-proxy:7890`, with separate generated proxy credentials.
Do not use a global `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`.

Run as UID 65532 with all capabilities dropped, `no-new-privileges`, a
read-only root filesystem, a small writable `/tmp` tmpfs, and bounded
memory/CPU/PIDs/logs. The sidecar does not need host networking, privileged
mode, device access, or Docker socket access.

The health check tests authenticated CONNECT and verified Groq TLS. It expects
401 from model listing without a Groq key, so no model inference is performed.
Allow authenticated loopback access as well as the application's private
subnet. The health check reports unhealthy egress; it does not rotate nodes
or modify the host network.

Before enabling the route in production, verify:

- Unauthenticated requests fail with proxy authentication required.
- Authenticated Groq model listing and a real speech transcription succeed.
- Requests to unrelated hosts and Groq port 80 are rejected.
- No host ports or controller listeners are exposed.
- The sidecar survives a normal container restart.

Drain application work and back up SQLite and `.env` before recreating the
application container. Keep the old image and configuration for rollback.
Do not run two application queue processes against the same database.
