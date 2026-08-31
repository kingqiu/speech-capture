# Worker HTTPS and private-network setup

## 1. Enforced listener policy

The Worker starts on `127.0.0.1:8765` by default. Plain HTTP is accepted only on `localhost`, `127.0.0.0/8`,
or `::1`. A non-loopback listener must use an explicit non-public IP address and provide both a certificate and a
private key. Wildcard, public-IP, and hostname binds are rejected.

The private key must be a regular file with no group or other permissions. The server disables proxy-header trust,
version/date response headers, and routine access logs. Bearer authentication and Vault authorization remain
mandatory even when the private network already authenticates devices.

Local-only start:

```bash
uv run speech-capture-worker serve \
  --data-dir runtime/dev-worker
```

Direct TLS on a private interface:

```bash
chmod 600 /secure/path/worker.key

uv run speech-capture-worker serve \
  --data-dir runtime/dev-worker \
  --host 192.168.1.20 \
  --port 8765 \
  --ssl-certfile /secure/path/worker.crt \
  --ssl-keyfile /secure/path/worker.key
```

Use a certificate trusted by the client and connect with a hostname included in that certificate. Do not disable
certificate verification in the Obsidian client. Restart the Worker after renewing file-based certificates.

## 2. Recommended V1: Tailscale Serve

Keep the Worker on its default loopback listener and let Tailscale Serve terminate HTTPS. This prevents LAN or
tailnet clients from bypassing the proxy while retaining the Worker's own per-device authentication.

1. Install Tailscale on the Worker host and client devices, enable MagicDNS and HTTPS for the tailnet, and give the
   Worker host a non-sensitive machine name. HTTPS certificate names are recorded in Certificate Transparency.
2. Restrict access to the Worker host with tailnet grants or ACLs. Do not enable Funnel; Funnel is public internet
   exposure and is outside this project's supported posture.
3. Start the Worker on loopback using the local-only command above.
4. Configure the private HTTPS reverse proxy:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8765
tailscale serve status --json
```

5. From an authorized client, open the exact HTTPS URL printed by Tailscale and verify `/v1/health`. Then complete
   the normal Worker pairing flow. Network membership never replaces the Worker bearer credential.

Tailscale documents that Serve provisions and terminates HTTPS for the tailnet DNS name, applies tailnet access
rules, and persists across restart when run in background mode. Refer to the current official
[Tailscale Serve command](https://tailscale.com/docs/reference/tailscale-cli/serve),
[Serve overview](https://tailscale.com/docs/features/tailscale-serve), and
[HTTPS certificate disclosure and renewal notes](https://tailscale.com/docs/how-to/set-up-https-certificates).

## 3. Other secure networks

WireGuard, ZeroTier, a private LAN/VPN, or an authenticated reverse proxy may be used without changing the Worker
protocol. The deployment must preserve all of these properties:

- the endpoint is not reachable from the public internet;
- the Worker remains loopback-only behind a local TLS reverse proxy, or binds directly to one explicit private IP
  with a client-trusted certificate;
- the proxy does not weaken or replace Worker bearer authentication;
- firewall rules allow only intended private-network peers;
- certificate hostname validation and renewal are tested;
- no proxy access log records authorization headers, request bodies, source filenames, transcript text, or prompts.

Public tunnels, port forwarding from the internet, Tailscale Funnel, and `--host 0.0.0.0` are intentionally not
supported.

## 4. Verification and recovery

Before relying on remote processing:

1. verify the HTTPS certificate without an insecure client option;
2. confirm health and capability negotiation;
3. confirm an unpaired request to a private endpoint returns `401`;
4. pair one test device with a test Vault scope and verify cross-Vault access is rejected;
5. restart Worker and network services and confirm the credential still works;
6. revoke the test device and confirm access fails immediately;
7. disconnect the private network and confirm the endpoint is unreachable.

If TLS or the private network fails, keep the Worker bound to loopback. Do not fall back to public HTTP.
