# Aurum VPS Preflight Report — ailcloud_esc

Timestamp: 2026-06-13 03:06 CST

## Verdict

`ailcloud_esc` / existing Alibaba Cloud Guangzhou ECS is **blocked** for Aurum's Polymarket recorder.

Do not deploy the Aurum market recorder there as the primary node.

## Scope

Read-only checks only:

- no system DNS changes;
- no firewall/security-group changes;
- no package installation;
- no credentials;
- no wallet/private key/API key;
- no live trading endpoint calls.

## Host snapshot

- MCP server: `ailcloud_esc`
- OS: Alibaba Cloud Linux 8 style kernel `5.10.134-19.1.al8.x86_64`
- Python: `3.6.8`
- Disk `/`: `40G`, `32G` used, `5.3G` free, `86%` used
- Memory: `1.8Gi` total, about `1.1Gi` available
- DNS config: NetworkManager-generated `/etc/resolv.conf`
  - `[REDACTED_IP]`
  - `[REDACTED_IP]`

## Preflight result

Running `/tmp/aurum-vps-preflight.py --dns-server [REDACTED_IP] --timeout 12 --json` on the server returned:

- verdict: `fail`
- OK: `0/4`
- HTTPS public endpoints: failed
- WebSocket: failed

Endpoint results:

- `https://gamma-api.polymarket.com/markets?limit=1`
  - status: `URLError`
  - detail: timed out
- `https://clob.polymarket.com/markets?limit=1`
  - status: `URLError`
  - detail: timed out
- `https://data-api.polymarket.com/trades?limit=1`
  - status: `URLError`
  - detail: timed out
- `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  - status: `ConnectionRefusedError`
  - detail: connection refused

## Network observations

### Default Alibaba Cloud internal DNS is broken from this host

The configured resolvers did not answer direct DNS UDP checks:

- `[REDACTED_IP]`: DNS query timeout
- `[REDACTED_IP]`: DNS query timeout

This caused normal system resolution to fail with `Name or service not known`.

### External DNS can resolve, but Polymarket direct connectivity still fails

Manual DNS queries through `[REDACTED_IP]` returned A records, but direct TCP to the returned Polymarket endpoint IPs failed:

- `gamma-api.polymarket.com` resolved, but `443` timed out
- `clob.polymarket.com` resolved, but `443` timed out
- `data-api.polymarket.com` resolved, but `443` timed out
- `ws-subscriptions-clob.polymarket.com` resolved, but `443` returned connection refused

### Generic outbound IP connectivity is not completely dead

The host could open TCP connections to unrelated numeric IPs on port `443`, and could reach `[REDACTED_IP]:53` quickly. So this is not simply “no internet.” It is a combination of broken default DNS plus poor/blocked route to Polymarket endpoints from this Guangzhou ECS.

## Interpretation

Changing only `/etc/resolv.conf` would not be enough. DNS override fixed name resolution for the test, but the actual Polymarket HTTPS/WebSocket paths still failed.

This server may still be useful for other Alibaba/China-facing services, but it is a poor candidate for a 24/7 Polymarket recorder.

## Decision

For Aurum Phase 0/1:

1. Do not use `ailcloud_esc` Guangzhou as the primary recorder.
2. Buy or test a non-mainland node before deploying the recorder:
   - Hong Kong;
   - Singapore;
   - Japan;
   - US/EU if latency and cost are acceptable.
3. Prefer a plain Debian 12 / Ubuntu 24.04 image, not an app image.
4. Minimum practical recorder node:
   - 2 vCPU;
   - 2–4 GB RAM for the first prototype;
   - at least 40 GB free disk, preferably 80 GB+;
   - working direct HTTPS/WebSocket to Polymarket.

## Follow-up

The Alibaba Cloud Hong Kong lightweight server shown earlier becomes reasonable if no existing overseas node is available, but it should still be tested with `scripts/vps_network_preflight.py` immediately after creation and before installing the recorder.
